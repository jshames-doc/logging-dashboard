import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import server
import pricing

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT, model TEXT, created_at INTEGER,
  last_activity_at INTEGER, title TEXT, hidden INTEGER DEFAULT 0);
CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, node_id INTEGER,
  chat_message TEXT, created_at INTEGER);
CREATE INDEX idx_message_nodes_session ON message_nodes(session_id);
"""


def msg(role, rid=None, mid="m", model=None, inp=0, out=0, cw=0, cr=0, at=None):
    meta = {"request_id": rid, "generation_model": model, "created_at": at}
    if role == "assistant" and rid:
        meta["metrics"] = {"input_tokens": inp, "output_tokens": out, "cache_creation_tokens": cw, "cache_read_tokens": cr}
    return json.dumps({"message_id": mid, "role": role, "content": "", "metadata": meta}, separators=(",", ":"))


def day_of(iso):
    """Local-day bucket key for an ISO timestamp (mirrors aggregate_session)."""
    return datetime.fromtimestamp(server.parse_ts(iso, 0)).date().isoformat()


def week_of(iso):
    i = datetime.fromtimestamp(server.parse_ts(iso, 0)).date().isocalendar()
    return f"{i[0]}-W{i[1]:02d}"


class DBFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "s.db"
        c = sqlite3.connect(self.db)
        c.executescript(SCHEMA)
        c.execute("INSERT INTO sessions VALUES ('s1',?,'',100,300,'T',0)", ("C:\\ws\\a",))
        nodes = [
            msg("user", mid="u1"),
            msg("assistant", "r1", "a1", "model-a", inp=4, out=10, cw=100, cr=1000),
            msg("assistant", "r1", "a1", "model-a", inp=4, out=10, cw=100, cr=1000),  # forked duplicate
            msg("tool", mid="t1"),
            msg("assistant", "r2", "a2", "model-b", inp=1, out=5, cw=None, cr=50),
            msg("assistant", "r3", "a3", "model-a", inp=2, out=3, cw=0, cr=7),
            msg("assistant", mid="a4"),  # no metrics
        ]
        c.executemany("INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES ('s1',?,?,?)",
                      [(i, n, 100 + i) for i, n in enumerate(nodes)])
        priced = [
            msg("assistant", "r1", "a1", "claude-sonnet-4-6", inp=4, out=10, cw=100, cr=1000),
            msg("assistant", "r3", "a3", "claude-sonnet-4-6", inp=2, out=3, cw=0, cr=7),
        ]
        c.executemany("INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES ('s2',?,?,?)",
                      [(i, n, 200 + i) for i, n in enumerate(priced)])
        c.commit()
        c.close()
        self.conn = server.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class AggregationTest(DBFixture):
    def test_aggregates_per_model_and_dedupes(self):
        res = {m["model"]: m for m in server.aggregate_session(self.conn, "s1")[0]}
        self.assertEqual(set(res), {"model-a", "model-b"})
        a = res["model-a"]
        self.assertEqual((a["messages"], a["input"], a["output"], a["cached"]), (2, 106, 13, 1007))
        self.assertEqual((a["first"], a["last"]), (101, 105))
        b = res["model-b"]
        self.assertEqual((b["messages"], b["input"], b["output"], b["cached"]), (1, 1, 5, 50))

    def test_refresh_reuses_cache_when_unchanged(self):
        fake = {"s1": {"last_activity": 300, "models": [{"model": "cached"}]}}
        out = server.refresh_sessions(self.conn, fake)
        self.assertEqual(out["s1"]["models"], [{"model": "cached"}])
        fake["s1"]["last_activity"] = 299
        out = server.refresh_sessions(self.conn, fake)
        self.assertEqual({m["model"] for m in out["s1"]["models"]}, {"model-a", "model-b"})

    def test_flatten_one_row_per_model(self):
        rows = server.flatten(server.refresh_sessions(self.conn, {}))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["id"] == "s1" and r["workspace"] == "C:\\ws\\a" for r in rows))


class ModelFilterTest(unittest.TestCase):
    def run_script(self, script):
        setup = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, {
    value: "", checked: false, textContent: "", innerHTML: "", open: false,
    style: {}, listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    contains(target) { return target === this; },
    querySelectorAll() { return []; },
    focus() { this.focused = true; },
    showModal() { this.open = true; },
    close() { this.open = false; },
    getBoundingClientRect() { return { left: 0, top: 0, right: 800, bottom: 600 }; },
  });
  return elements.get(id);
};
const document = {
  getElementById: element,
  querySelectorAll: () => [],
  addEventListener(type, fn) { this[type] = fn; },
};
const context = vm.createContext({ document, Intl, fetch: () => new Promise(() => {}), setInterval: () => 0 });
vm.runInContext(fs.readFileSync("static/app.js", "utf8"), context);
"""
        result = subprocess.run(["node", "-e", setup + script], cwd=Path(__file__).parent,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_multi_model_filter_and_reset(self):
        self.run_script(r"""
vm.runInContext(`
  state.rows = [
    { id: "a", model: "model-a", workspace: "one", created: 100 },
    { id: "b", model: "model-b", workspace: "one", created: 100 },
    { id: "c", model: "model-c", workspace: "two", created: 100 },
    { id: "h", model: "model-b", workspace: "one", created: 100, hidden: true },
  ];
  state.models = new Set(["model-a", "model-b"]);
`, context);
const ids = () => Array.from(vm.runInContext("filtered().map(r => r.id)", context));
assert.deepEqual(ids(), ["a", "b"]);
vm.runInContext("render()", context);
assert.ok(element("totals").innerHTML.includes("2 model rows"));
assert.ok(!element("rows").innerHTML.includes("model-c"));
element("f-ws").value = "two";
assert.deepEqual(ids(), []);
element("f-ws").value = "";
element("f-hidden").checked = true;
assert.deepEqual(ids(), ["a", "b", "h"]);
element("q").value = "model-a";
assert.deepEqual(ids(), ["a"]);
element("q").value = "";
vm.runInContext("fillModels(['model-a', 'model-b', 'model-c'])", context);
assert.equal(element("f-model-label").textContent, "2 models selected");
vm.runInContext("fillModels(['model-b', 'model-c'])", context);
assert.deepEqual(Array.from(vm.runInContext("state.models", context)), ["model-b"]);
assert.equal(element("f-model-label").textContent, "model-b");
vm.runInContext("render = () => {}", context);
element("f-model-options").listeners.change({ target: { type: "checkbox", value: "model-c", checked: true } });
assert.deepEqual(ids(), ["b", "c", "h"]);
element("f-model-options").listeners.change({ target: { type: "checkbox", value: "model-b", checked: false } });
assert.deepEqual(ids(), ["c"]);
element("f-model-all").listeners.click();
assert.deepEqual(ids(), ["a", "b", "c", "h"]);
assert.equal(element("f-model-label").textContent, "All models");
vm.runInContext("state.models.add('model-a')", context);
element("clear").listeners.click();
assert.deepEqual(ids(), ["a", "b", "c"]);
assert.equal(element("f-model-label").textContent, "All models");
element("f-model").open = true;
document.keydown({ key: "Escape" });
assert.equal(element("f-model").open, false);
assert.equal(element("f-model-label").focused, true);
element("f-model").open = true;
document.click({ target: element("f-model") });
assert.equal(element("f-model").open, true);
document.click({ target: element("q") });
assert.equal(element("f-model").open, false);
""")

    def test_report_model_select_all(self):
        self.run_script(r"""
vm.runInContext(`
  state.rows = [
    { id: "a", model: "model-a" }, { id: "b", model: "model-b" },
    { id: "c", model: "model-b" }, { id: "d", model: "—" },
  ];
  fillReportModels();
`, context);
assert.deepEqual(Array.from(vm.runInContext("report.models", context)), ["model-a", "model-b"]);
assert.equal(element("report-model-label").textContent, "All models");
assert.equal(element("report-models-all").checked, true);
element("report-models").listeners.change({ target: { type: "checkbox", id: "", value: "model-a", checked: false } });
assert.equal(element("report-model-label").textContent, "1 of 2 models");
assert.equal(element("report-models-all").indeterminate, true);
element("report-models").listeners.change({ target: { type: "checkbox", id: "report-models-all", checked: false } });
assert.equal(element("report-model-label").textContent, "0 of 2 models");
assert.equal(element("report-models-all").indeterminate, false);
assert.equal(element("report-model-options").innerHTML.includes(" checked"), false);
element("report-models").listeners.change({ target: { type: "checkbox", id: "report-models-all", checked: true } });
assert.equal(element("report-model-label").textContent, "All models");
assert.ok(element("report-model-options").innerHTML.includes("model-a"));
element("report-models").listeners.change({ target: { type: "checkbox", id: "", value: "model-b", checked: false } });
vm.runInContext("fillReportModels()", context);
assert.equal(element("report-model-label").textContent, "1 of 2 models");
""")

    def test_session_details_include_all_models_and_handle_unpriced_usage(self):
        self.run_script(r"""
vm.runInContext(`
  state.rows = [
    { id: 's"1', title: '<script>title</script>', workspace: 'C:/work/<demo>', created: 100,
      last_activity: 400, hidden: true, model: 'model-a', messages: 2, input: 100,
      cache_write: 40, output: 20, cached: 300, price: 1.25, price_model: 'Model A', first: 110, last: 200 },
    { id: 's"1', title: '<script>title</script>', workspace: 'C:/work/<demo>', created: 100,
      last_activity: 400, hidden: true, model: '<model-b>', messages: 3, input: 50,
      cache_write: 10, output: 30, cached: 150, price: null, price_model: null, first: 210, last: 350 },
    { id: 'other', title: 'Other session', workspace: '', created: 500, last_activity: 600,
      model: 'model-c', messages: 99, input: 999, output: 0, cached: 0, price: 50, first: 500, last: 600 },
  ];
  state.models.add('model-a');
`, context);
element("f-hidden").checked = true;
vm.runInContext("render()", context);
assert.ok(element("rows").innerHTML.includes('data-session-id="s&quot;1"'));
assert.ok(element("rows").innerHTML.includes('tabindex="0"'));
const row = { dataset: { sessionId: 's"1' }, focus() { this.focused = true; } };
const cell = { closest: () => row };
element("rows").listeners.dblclick({ target: cell });
assert.equal(element("session-details").open, true);
assert.equal(element("session-title").textContent, '<script>title</script>');
assert.ok(element("session-meta").innerHTML.includes('C:/work/&lt;demo&gt;'));
assert.ok(element("session-meta").innerHTML.includes('Hidden'));
assert.ok(element("session-stats").innerHTML.includes('150'));
assert.ok(element("session-stats").innerHTML.includes('450'));
assert.ok(element("session-stats").innerHTML.includes('75.0%'));
assert.ok(element("session-stats").innerHTML.includes('$1.25'));
assert.ok(element("session-cost-note").textContent.includes('1 model without published rates'));
assert.ok(element("session-models").innerHTML.includes('&lt;model-b&gt;'));
assert.ok(!element("session-models").innerHTML.includes('model-c'));
assert.ok(!element("session-models").innerHTML.includes('<model-b>'));
element("session-details").listeners.click({ target: element("session-models") });
assert.equal(element("session-details").open, true);
element("session-details").listeners.click({ target: element("session-details"), clientX: 10, clientY: 10 });
assert.equal(element("session-details").open, true);
element("session-close").listeners.click();
assert.equal(element("session-details").open, false);
element("session-details").listeners.close();
assert.equal(row.focused, true);
let prevented = false;
element("rows").listeners.keydown({ target: cell, key: 'Enter', preventDefault() { prevented = true; } });
assert.equal(prevented, true);
assert.equal(element("session-details").open, true);
element("session-details").listeners.click({ target: element("session-details"), clientX: -1, clientY: -1 });
assert.equal(element("session-details").open, false);
vm.runInContext("showSession('other')", context);
assert.equal(element("session-title").textContent, 'Other session');
assert.ok(!element("session-models").innerHTML.includes('&lt;model-b&gt;'));
vm.runInContext(`
  state.rows = [{ id: 'empty', title: '', workspace: '', created: 100, last_activity: 100,
    model: '—', messages: 0, input: 0, output: 0, cached: 0, price: null, first: 100, last: 100 }];
  showSession('empty');
`, context);
assert.equal(element("session-title").textContent, 'Untitled');
assert.ok(element("session-stats").innerHTML.includes('0.0%'));
assert.ok(!element("session-stats").innerHTML.includes('NaN'));
assert.ok(element("session-cost-note").textContent.includes('No published rates'));
""")


class ReportTest(DBFixture):
    D12 = "2026-10-12T12:00:00Z"  # midday UTC: same local day for nearly all timezones
    D13 = "2026-10-13T12:00:00Z"

    def add_session(self, sid, nodes, hidden=0):
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO sessions VALUES (?,?,'',100,300,'T',?)", (sid, f"C:\\ws\\{sid}", hidden))
        c.executemany("INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES (?,?,?,?)",
                      [(sid, i, n, 100 + i) for i, n in enumerate(nodes)])
        c.commit()
        c.close()

    def refresh(self):
        return server.refresh_sessions(self.conn, {})

    def test_daily_buckets_dedupe_and_group_by_day(self):
        self.add_session("s4", [
            msg("assistant", "d1", "d1", "model-a", inp=10, out=5, at=self.D12),
            msg("assistant", "d2", "d2", "model-b", inp=20, out=6, at=self.D12),
            msg("assistant", "d3", "d3", "model-a", inp=30, out=7, at=self.D13),
            msg("assistant", "d1", "d1", "model-a", inp=10, out=5, at=self.D12),  # forked duplicate
        ])
        sessions = self.refresh()
        self.assertEqual(len(sessions["s4"]["daily"]), 3)
        days = {r["key"]: r for r in server.report(sessions, group="day")["rows"]}
        d12, d13 = day_of(self.D12), day_of(self.D13)
        self.assertEqual((days[d12]["messages"], days[d12]["input"], days[d12]["output"]), (2, 30, 11))
        self.assertEqual((days[d12]["sessions"], days[d12]["models"]), (1, 2))
        self.assertEqual((days[d13]["messages"], days[d13]["input"]), (1, 30))

    def test_report_filters_models_days_and_hidden(self):
        self.add_session("s4", [
            msg("assistant", "d1", "d1", "model-a", inp=10, out=5, at=self.D12),
            msg("assistant", "d3", "d3", "model-a", inp=30, out=7, at=self.D13),
        ])
        self.add_session("s5", [msg("assistant", "h1", "h1", "model-h", inp=1, out=1, at=self.D12)], hidden=1)
        sessions = self.refresh()
        keys = {r["key"] for r in server.report(sessions, exclude={"model-a"}, group="model")["rows"]}
        self.assertNotIn("model-a", keys)
        self.assertIn("model-b", keys)
        self.assertNotIn("model-h", keys)  # hidden session excluded by default
        keys = {r["key"] for r in server.report(sessions, group="model", hidden=True)["rows"]}
        self.assertIn("model-h", keys)
        days = {r["key"] for r in server.report(sessions, from_day=day_of(self.D13), group="day")["rows"]}
        self.assertEqual(days, {day_of(self.D13)})
        days = {r["key"] for r in server.report(sessions, to_day=day_of(self.D12), group="day")["rows"]}
        self.assertIn(day_of(self.D12), days)
        self.assertNotIn(day_of(self.D13), days)

    def test_report_groups_by_week_and_month(self):
        self.add_session("s4", [
            msg("assistant", "w1", "w1", "model-a", inp=5, out=1, at="2026-10-14T12:00:00Z"),
            msg("assistant", "w2", "w2", "model-a", inp=5, out=1, at="2026-10-16T12:00:00Z"),
            msg("assistant", "w3", "w3", "model-a", inp=5, out=1, at="2026-10-21T12:00:00Z"),
        ])
        sessions = self.refresh()
        weeks = {r["key"]: r for r in server.report(sessions, group="week")["rows"]}
        self.assertEqual(weeks[week_of("2026-10-14T12:00:00Z")]["input"], 10)
        self.assertEqual(weeks[week_of("2026-10-21T12:00:00Z")]["input"], 5)
        months = {r["key"]: r for r in server.report(sessions, group="month")["rows"]}
        mk = day_of("2026-10-14T12:00:00Z")[:7]
        self.assertEqual(months[mk]["input"], 15)
        self.assertEqual(months[mk]["label"], "October 2026")

    def test_report_prices_per_model_within_groups(self):
        self.add_session("s6", [
            msg("assistant", "p1", "p1", "claude-sonnet-4-6", inp=10, out=5, cw=10, cr=100, at=self.D12),
            msg("assistant", "p2", "p2", "model-a", inp=10, out=5, at=self.D12),
        ])
        sessions = self.refresh()
        row = next(r for r in server.report(sessions, group="day")["rows"] if r["key"] == day_of(self.D12))
        ir, orr, cwr, crr = pricing.RATES["Claude Sonnet 4.6"]
        self.assertAlmostEqual(row["price"], (10 * ir + 5 * orr + 10 * cwr + 100 * crr) / 1e6)
        self.assertEqual(row["unpriced"], 1)
        by_model = {r["key"]: r for r in server.report(sessions, group="model")["rows"]}
        self.assertEqual(by_model["claude-sonnet-4-6"]["price_model"], "Claude Sonnet 4.6")
        self.assertIsNone(by_model["model-a"]["price"])

    def test_report_empty_when_all_models_excluded(self):
        rep = server.report(self.refresh(), exclude={"model-a", "model-b"}, group="model")
        self.assertEqual(rep["rows"], [])
        self.assertEqual(rep["totals"]["messages"], 0)
        self.assertIsNone(rep["totals"]["price"])


class PricingTest(DBFixture):
    def test_resolve_maps_db_slugs_to_labels(self):
        cases = {
            "claude-sonnet-4-6": "Claude Sonnet 4.6",
            "swe-2-high": "SWE-2 High",
            "gpt-5-6-luna-high": "GPT-5.6 Luna High Thinking",
            "gpt-6-luna-high-priority": "GPT-6 Luna High Thinking Fast",
            "gpt-6-luna-none": "GPT-6 Luna No Thinking",
            "MODEL_GOOGLE_GEMINI_3_0_FLASH_MEDIUM": "Gemini 3 Flash Medium",
            "glm-5-2": "GLM-5.2 High",
            "swe-1-7": "SWE-1.7 Medium",
            "claude-opus-5-5-medium": "Claude Opus 5.5 Medium",
            "claude-sonnet-5-5-medium": "Claude Sonnet 5.5 Medium",
            "gpt-5-5-high": "GPT-5.5 High Thinking",
            "gpt-6-1-sol-medium": "GPT-6.1 Sol Medium Thinking",
            "gpt-5-3-codex-medium": "GPT-5.3-Codex Medium",
        }
        for slug, label in cases.items():
            self.assertEqual(pricing.resolve(slug), label, slug)

    def test_resolve_unpriced_slugs(self):
        for slug in ("summarizer", "compactor", "MODEL_SWE_1_5", "model-a"):
            self.assertIsNone(pricing.resolve(slug), slug)

    def test_manual_price_with_long_prompt_tier(self):
        self.assertEqual(pricing.resolve("claude-haiku-5-5-high"), "Claude Haiku 5.5")
        r, lr = pricing.RATES["Claude Haiku 5.5"], pricing.LONG_RATES["Claude Haiku 5.5"]
        c = sqlite3.connect(self.db)
        nodes = [  # prompt = input + cache write + cache read; only the second exceeds 100K
            msg("assistant", "h1", "h1", "claude-haiku-5-5-high", inp=10, out=100, cw=1_000, cr=50_000),
            msg("assistant", "h2", "h2", "claude-haiku-5-5-high", inp=20, out=200, cw=2_000, cr=150_000),
        ]
        c.executemany("INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES ('s3',?,?,?)",
                      [(i, n, 300 + i) for i, n in enumerate(nodes)])
        c.commit()
        c.close()
        m = server.with_price(server.aggregate_session(self.conn, "s3")[0][0])
        short = 10 * r[0] + 100 * r[1] + 1_000 * r[2] + 50_000 * r[3]
        long = 20 * lr[0] + 200 * lr[1] + 2_000 * lr[2] + 150_000 * lr[3]
        self.assertAlmostEqual(m["price"], (short + long) / 1e6)
        self.assertEqual(m["price_model"], "Claude Haiku 5.5")

    def test_long_tier_ignored_for_models_without_it(self):
        parts = {"input": 10, "output": 10, "cache_write": 0, "cache_read": 200_000}
        _, flat = pricing.cost("claude-sonnet-4-6", parts)
        _, tiered = pricing.cost("claude-sonnet-4-6", parts, parts)
        self.assertAlmostEqual(flat, tiered)

    def test_resolve_prefers_published_uid(self):
        self.assertEqual(pricing.resolve("claude-5-fable-low"), "Claude Fable 5 Low")
        self.assertEqual(pricing.resolve("CLAUDE-OPUS-5-5-MEDIUM-FAST"), "Claude Opus 5.5 Medium Fast")

    def test_session_price_from_token_parts(self):
        res = {m["model"]: server.with_price(m) for m in server.aggregate_session(self.conn, "s2")[0]}
        m = res["claude-sonnet-4-6"]
        ir, orr, cwr, crr = pricing.RATES["Claude Sonnet 4.6"]
        expected = (4 * ir + 100 * cwr + 10 * orr + 1000 * crr + 2 * ir + 0 * cwr + 3 * orr + 7 * crr) / 1e6
        self.assertAlmostEqual(m["price"], expected)
        self.assertEqual(m["price_model"], "Claude Sonnet 4.6")

    def test_unpriced_model_has_null_price(self):
        res = {m["model"]: server.with_price(m) for m in server.aggregate_session(self.conn, "s1")[0]}
        self.assertIsNone(res["model-a"]["price"])
        self.assertIsNone(res["model-a"]["price_model"])


PAGE = r"""<script>self.__next_f.push([1,"...modelCostData=[{\"tier\":`TEAMS_TIER_ENTERPRISE_SAAS`,\"model_uid\":`m-a`,\"label\":`Model A`,\"model_provider\":`P`,\"input_cost_per_million_usd\":2.5,\"output_cost_per_million_usd\":10,\"cache_write_cost_per_million_usd\":0,\"cache_read_cost_per_million_usd\":.25,\"credit_multiplier\":1,\"is_recommended\":false},{\"tier\":`TEAMS_TIER_PRO`,\"model_uid\":`m-a`,\"label\":`Model A`,\"input_cost_per_million_usd\":9,\"output_cost_per_million_usd\":9,\"cache_write_cost_per_million_usd\":9,\"cache_read_cost_per_million_usd\":9},{\"tier\":`TEAMS_TIER_PRO`,\"model_uid\":`m-b`,\"label\":`Model B`,\"input_cost_per_million_usd\":1,\"output_cost_per_million_usd\":2,\"cache_write_cost_per_million_usd\":3,\"cache_read_cost_per_million_usd\":4}]"])</script>
<script>modelCostData=[{\"tier\":`TEAMS_TIER_ENTERPRISE_SAAS`,\"model_uid\":`m-a`,\"label\":`Model A`,\"input_cost_per_million_usd\":2.5,\"output_cost_per_million_usd\":10,\"cache_write_cost_per_million_usd\":0,\"cache_read_cost_per_million_usd\":.25}]</script>"""


class UpdatePricesTest(unittest.TestCase):
    def test_parse_and_build_with_pro_fallback(self):
        import update_prices
        entries = update_prices.parse(PAGE)
        self.assertEqual(len(entries), 3)  # duplicate page copy de-duplicated
        data = update_prices.build(entries, fetched="2026-01-01")
        models = {m["label"]: m for m in data["models"]}
        self.assertEqual(set(models), {"Model A", "Model B"})
        a, b = models["Model A"], models["Model B"]
        self.assertEqual((a["uid"], a["tier"], a["input"], a["output"], a["cache_write"], a["cache_read"]),
                         ("m-a", "Enterprise SaaS", 2.5, 10.0, 0.0, 0.25))
        self.assertEqual((b["tier"], b["cache_read"]), ("Pro", 4.0))

    def test_diff_reports_changes(self):
        import update_prices
        m = lambda label, i: {"label": label, "input": i, "output": 1, "cache_write": 0, "cache_read": 0}
        lines = update_prices.diff({"models": [m("A", 1), m("B", 1)]}, {"models": [m("A", 2), m("C", 1)]})
        self.assertEqual([l[0] for l in lines], ["+", "-", "~"])

    def test_pricing_reloads_when_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            write = lambda rate, mtime: (path.write_text(json.dumps({"models": [
                {"uid": "x-1", "label": "X 1", "input": rate, "output": 0, "cache_write": 0, "cache_read": 0}]})),
                os.utime(path, (mtime, mtime)))
            orig = pricing.DATA_PATH
            try:
                pricing.DATA_PATH = path
                write(1.0, 1000)
                self.assertEqual(pricing.price_info("x-1"), ("X 1", (1.0, 0, 0, 0)))
                write(2.0, 2000)
                self.assertEqual(pricing.price_info("x-1"), ("X 1", (2.0, 0, 0, 0)))
            finally:
                pricing.DATA_PATH = orig
                pricing._load()


if __name__ == "__main__":
    unittest.main()
