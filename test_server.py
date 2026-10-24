import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import server
import pricing

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT, model TEXT, created_at INTEGER,
  last_activity_at INTEGER, title TEXT, hidden INTEGER DEFAULT 0);
CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, node_id INTEGER,
  chat_message TEXT, created_at INTEGER);
CREATE INDEX idx_message_nodes_session ON message_nodes(session_id);
"""


def msg(role, rid=None, mid="m", model=None, inp=0, out=0, cw=0, cr=0, at=None, tps=None):
    meta = {"request_id": rid, "generation_model": model, "created_at": at}
    if role == "assistant" and rid:
        meta["metrics"] = {"input_tokens": inp, "output_tokens": out, "cache_creation_tokens": cw, "cache_read_tokens": cr}
        if tps is not None:
            meta["metrics"]["tokens_per_sec"] = tps
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
            msg("assistant", "r1", "a1", "model-a", inp=4, out=10, cw=100, cr=1000, tps=250),
            msg("assistant", "r1", "a1", "model-a", inp=4, out=10, cw=100, cr=1000, tps=250),  # forked duplicate
            msg("tool", mid="t1"),
            msg("assistant", "r2", "a2", "model-b", inp=1, out=5, cw=None, cr=50, tps=100),
            msg("assistant", "r3", "a3", "model-a", inp=2, out=3, cw=0, cr=7),  # no tokens_per_sec
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

    def test_tps_generation_time_accumulated_and_served(self):
        res = {m["model"]: m for m in server.aggregate_session(self.conn, "s1")[0]}
        # gen_ms = output / tokens_per_sec * 1000: r1 10/250 -> 40 ms, r2 5/100 -> 50 ms; r3 has no tps
        self.assertAlmostEqual(res["model-a"]["gen_ms"], 40.0)
        self.assertAlmostEqual(res["model-b"]["gen_ms"], 50.0)
        served = {k: server.with_price(m) for k, m in res.items()}
        self.assertEqual(served["model-a"]["tps"], 325.0)  # 13 output tokens / 0.04 s
        self.assertEqual(served["model-b"]["tps"], 100.0)

    def test_tps_ignores_missing_or_invalid_speed(self):
        c = sqlite3.connect(self.db)
        nodes = [
            msg("assistant", "t1", "t1", "model-a", inp=1, out=0, tps=5000),  # no output: no generation time
            msg("assistant", "t2", "t2", "model-a", inp=1, out=4, tps=0),     # zero speed: unusable
            msg("assistant", "t3", "t3", "model-a", inp=1, out=4, tps=-5),    # negative speed: unusable
            msg("assistant", "t4", "t4", "model-a", inp=1, out=4, tps=200),   # 20 ms
        ]
        c.executemany("INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES ('s9',?,?,?)",
                      [(i, n, 400 + i) for i, n in enumerate(nodes)])
        c.commit()
        c.close()
        (m,) = server.aggregate_session(self.conn, "s9")[0]
        self.assertEqual((m["messages"], m["output"], m["gen_ms"]), (4, 12, 20.0))
        self.assertEqual(server.with_price(m)["tps"], 600.0)  # 12 tokens / 0.02 s

    def test_untracked_speed_yields_null_tps(self):
        res = {m["model"]: server.with_price(m) for m in server.aggregate_session(self.conn, "s2")[0]}
        self.assertIsNone(res["claude-sonnet-4-6"]["tps"])

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

    def test_aggregation_skips_malformed_rows(self):
        c = sqlite3.connect(self.db)
        rows = [
            (0, json.dumps({"message_id": "b1", "role": "assistant",
                            "metadata": {"request_id": "bad1", "metrics": 5}}), 500),  # metrics not an object
            (1, json.dumps({"message_id": "b2", "role": "assistant",
                            "metadata": {"request_id": "bad2",
                                         "metrics": {"input_tokens": "lots", "output_tokens": 2}}}), 500),
            (2, msg("assistant", "bad3", "b3", "model-a", inp=9, out=9), None),  # no usable timestamp
            (3, msg("assistant", "good1", "g1", "model-a", inp=3, out=2), 500),
        ]
        c.executemany("INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES ('s7',?,?,?)", rows)
        c.commit()
        c.close()
        models, daily = server.aggregate_session(self.conn, "s7")
        res = {m["model"]: m for m in models}
        self.assertEqual((res["model-a"]["messages"], res["model-a"]["input"], res["model-a"]["output"]), (1, 3, 2))
        self.assertEqual((len(daily), daily[0]["messages"], daily[0]["input"]), (1, 1, 3))

    def test_metrics_without_request_or_message_id_all_counted(self):
        bare = lambda i: json.dumps({"role": "assistant", "content": "",
                                     "metadata": {"generation_model": "model-a",
                                                  "metrics": {"input_tokens": i, "output_tokens": i}}},
                                    separators=(",", ":"))
        mid_only = lambda i: json.dumps({"message_id": "x1", "role": "assistant", "content": "",
                                         "metadata": {"generation_model": "model-a",
                                                      "metrics": {"input_tokens": i, "output_tokens": i}}},
                                        separators=(",", ":"))
        nodes = [bare(1), bare(2), mid_only(4), mid_only(4)]  # mid_only pair is a forked duplicate
        c = sqlite3.connect(self.db)
        c.executemany("INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES ('s8',?,?,?)",
                      [(i, n, 600 + i) for i, n in enumerate(nodes)])
        c.commit()
        c.close()
        (m,) = server.aggregate_session(self.conn, "s8")[0]
        self.assertEqual((m["messages"], m["input"], m["output"]), (3, 7, 7))


class MultiDBTest(unittest.TestCase):
    def _mk(self, folder, sid, model, wd):
        db = Path(self.tmp.name) / folder / "sessions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(db)
        c.executescript(SCHEMA)
        c.execute("INSERT INTO sessions VALUES (?,?,'',100,300,'T',0)", (sid, wd))
        c.execute("INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES (?,?,?,?)",
                  (sid, 0, msg("assistant", "r1", "a1", model, inp=4, out=10), 150))
        c.commit()
        c.close()
        return db

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_refresh_merges_dbs_with_source_labels(self):
        # same session id in both DBs must not collide; rows carry the DB label
        a = self._mk("cli", "shared", "model-a", "C:\\ws\\one")
        b = self._mk("cli-next", "shared", "model-b", "C:\\ws\\two")
        ca, cb = server.connect(a), server.connect(b)
        try:
            merged = server.refresh_sessions(ca, {}, src="cli")
            merged.update(server.refresh_sessions(cb, merged, src="cli-next"))
        finally:
            ca.close()
            cb.close()
        self.assertEqual(set(merged), {"cli:shared", "cli-next:shared"})
        rows = server.flatten(merged)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["src"] for r in rows}, {"cli", "cli-next"})
        self.assertEqual({r["id"] for r in rows}, {"shared"})  # raw id kept for display

    def test_cache_reuse_is_scoped_per_source(self):
        db = self._mk("cli", "s1", "model-a", "C:\\ws\\one")
        conn = server.connect(db)
        try:
            cache = server.refresh_sessions(conn, {}, src="cli")
            self.assertEqual(set(cache), {"cli:s1"})
            out = server.refresh_sessions(conn, cache, src="cli")
            self.assertEqual(out["cli:s1"]["models"], cache["cli:s1"]["models"])
        finally:
            conn.close()


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

    def test_column_visibility_dropdown(self):
        self.run_script(r"""
vm.runInContext(`
  setData({ rows: [
    { id: "a", title: "A", model: "model-a", workspace: "ws1", created: 100, last_activity: 200,
      messages: 3, input: 10, output: 5, cached: 7, price: 1.5, tps: 87.5, first: 100, last: 200 },
  ], db: "db" });
`, context);
assert.ok(element("rows").innerHTML.includes('class="model"'));
assert.ok(element("rows").innerHTML.includes("41.2%"));  // cache hit = cached / (input + cached)
assert.ok(element("rows").innerHTML.includes("87.5"));   // TPS cell
assert.ok(element("f-cols-options").innerHTML.includes('value="hit"'));
assert.equal(element("f-cols-label").textContent, "Columns");
element("f-cols-options").listeners.change({ target: { type: "checkbox", value: "model", checked: false } });
assert.ok(!element("rows").innerHTML.includes('class="model"'));
assert.ok(element("rows").innerHTML.includes('class="ws"'));
assert.equal(element("f-cols-label").textContent, "12 of 13 columns");
const target = { type: "checkbox", value: "hit", checked: false };
for (const k of ["created", "last_activity", "title", "workspace", "src", "messages", "input", "output", "tps", "cached", "price"])
  element("f-cols-options").listeners.change({ target: { type: "checkbox", value: k, checked: false } });
element("f-cols-options").listeners.change({ target });
assert.equal(target.checked, true);  // last visible column can't be hidden
assert.equal(vm.runInContext("state.hiddenCols.size", context), 12);
element("f-cols-all").listeners.click();
assert.equal(element("f-cols-label").textContent, "Columns");
assert.ok(element("rows").innerHTML.includes('class="model"'));
""")

    def test_row_cap_notice_expands_on_click(self):
        self.run_script(r"""
vm.runInContext(`
  state.rows = Array.from({ length: 4100 }, (_, i) => ({ id: "s" + i, model: "m", workspace: "", created: i, last_activity: i }));
  render();
`, context);
assert.equal(element("more").hidden, false);
assert.ok(element("more").textContent.includes("of 4,100"), element("more").textContent);
assert.ok(!element("rows").innerHTML.includes('data-session-id="s2099"'));
element("more").listeners.click();
assert.equal(element("more").hidden, false);
assert.ok(element("rows").innerHTML.includes('data-session-id="s2099"'));
assert.ok(element("more").textContent.includes("100 more"), element("more").textContent);
element("more").listeners.click();
assert.equal(element("more").hidden, true);
assert.ok(element("rows").innerHTML.includes('data-session-id="s4099"'));
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

    def test_report_workspace_group_shows_basename(self):
        self.run_script(r"""
vm.runInContext(`
  report.group = "workspace";
  report.rows = [
    { key: "C:\\\\ws\\\\proj", label: "C:\\\\ws\\\\proj", sessions: 2, models: 1, messages: 5,
      input: 10, output: 5, cached: 3, cache_write: 0, tps: 50, price: 1.2, unpriced: 0 },
    { key: "/home/u/lib", label: "/home/u/lib", sessions: 1, models: 1, messages: 2,
      input: 6, output: 2, cached: 1, cache_write: 0, tps: null, price: null, unpriced: 1 },
  ];
  report.totals = { sessions: 3, models: 2, messages: 7, input: 16, output: 7, cached: 4,
    cache_write: 0, tps: 50, price: 1.2, unpriced: 1 };
  renderReport();
`, context);
const html = element("report-rows").innerHTML;
assert.ok(element("report-head").innerHTML.includes("Workspace"));
assert.ok(element("report-head").innerHTML.includes("Models"));
assert.ok(html.includes('title="C:\\ws\\proj"'));  // full path on hover
assert.ok(html.includes(">proj<"));               // basename shown
assert.ok(html.includes('title="/home/u/lib"'));
assert.ok(html.includes(">lib<"));
assert.ok(html.includes("—"));                    // unpriced row
""")

    def test_session_details_include_all_models_and_handle_unpriced_usage(self):
        self.run_script(r"""
vm.runInContext(`
  state.rows = [
    { id: 's"1', title: '<script>title</script>', workspace: 'C:/work/<demo>', created: 100,
      last_activity: 400, hidden: true, model: 'model-a', messages: 2, input: 100,
      cache_write: 40, output: 20, cached: 300, price: 1.25, price_model: 'Model A', tps: 500, gen_ms: 40, first: 110, last: 200 },
    { id: 's"1', title: '<script>title</script>', workspace: 'C:/work/<demo>', created: 100,
      last_activity: 400, hidden: true, model: '<model-b>', messages: 3, input: 50,
      cache_write: 10, output: 30, cached: 150, price: null, price_model: null, tps: null, first: 210, last: 350 },
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
assert.ok(element("session-stats").innerHTML.includes('Avg TPS'));
assert.ok(element("session-stats").innerHTML.includes('1250.0'));  // 50 output / 0.04 s, model-b untracked
assert.ok(element("session-cost-note").textContent.includes('1 model without published rates'));
assert.ok(element("session-models").innerHTML.includes('&lt;model-b&gt;'));
assert.ok(element("session-models").innerHTML.includes('500.0'));  // model-a TPS cell
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
            msg("assistant", "d1", "d1", "model-a", inp=10, out=5, at=self.D12, tps=100),
            msg("assistant", "d2", "d2", "model-b", inp=20, out=6, at=self.D12, tps=200),
            msg("assistant", "d3", "d3", "model-a", inp=30, out=7, at=self.D13, tps=70),
            msg("assistant", "d1", "d1", "model-a", inp=10, out=5, at=self.D12, tps=100),  # forked duplicate
        ])
        sessions = self.refresh()
        self.assertEqual(len(sessions["s4"]["daily"]), 3)
        days = {r["key"]: r for r in server.report(sessions, group="day")["rows"]}
        d12, d13 = day_of(self.D12), day_of(self.D13)
        self.assertEqual((days[d12]["messages"], days[d12]["input"], days[d12]["output"]), (2, 30, 11))
        self.assertEqual((days[d12]["sessions"], days[d12]["models"]), (1, 2))
        self.assertEqual((days[d13]["messages"], days[d13]["input"]), (1, 30))
        # token-weighted: 11 output / (5/100 + 6/200 s) and 7 / (7/70 s)
        self.assertEqual((days[d12]["tps"], days[d13]["tps"]), (137.5, 70.0))

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

    def test_report_groups_by_workspace(self):
        self.add_session("s4", [
            msg("assistant", "w1", "w1", "model-a", inp=10, out=5, at=self.D12),
            msg("assistant", "w2", "w2", "model-b", inp=4, out=2, at=self.D13),
        ])
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO sessions VALUES ('sn',NULL,'',100,300,'T',0)")  # no working_directory
        c.execute("INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES ('sn',0,?,700)",
                  (msg("assistant", "n1", "n1", "model-a", inp=3, out=1, at=self.D12),))
        c.commit()
        c.close()
        sessions = self.refresh()
        rows = {r["key"]: r for r in server.report(sessions, group="workspace")["rows"]}
        self.assertEqual(set(rows), {"C:\\ws\\a", "C:\\ws\\s4", "—"})
        ws = rows["C:\\ws\\s4"]
        self.assertEqual((ws["label"], ws["sessions"], ws["models"]), ("C:\\ws\\s4", 1, 2))
        self.assertEqual((ws["input"], ws["output"]), (14, 7))
        self.assertEqual(rows["—"]["input"], 3)
        self.assertEqual(server.report(sessions, group="workspace")["totals"]["sessions"], 3)

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
        self.assertIsNone(row["tps"])  # this day's buckets have no tokens_per_sec
        by_model = {r["key"]: r for r in server.report(sessions, group="model")["rows"]}
        self.assertEqual(by_model["claude-sonnet-4-6"]["price_model"], "Claude Sonnet 4.6")
        self.assertIsNone(by_model["model-a"]["price"])
        self.assertEqual(by_model["model-a"]["tps"], 450.0)  # group includes s1's model-a: 18 tokens / 0.04 s

    def test_report_tps_is_token_weighted(self):
        self.add_session("s4", [
            msg("assistant", "d1", "d1", "model-x", inp=10, out=5, at=self.D12, tps=100),
            msg("assistant", "d2", "d2", "model-y", inp=20, out=6, at=self.D12, tps=200),
            msg("assistant", "d3", "d3", "model-x", inp=30, out=7, at=self.D13, tps=70),
        ])
        sessions = self.refresh()
        models = {r["key"]: r for r in server.report(sessions, group="model")["rows"]}
        self.assertEqual((models["model-x"]["tps"], models["model-y"]["tps"]), (80.0, 200.0))
        # from_day drops the shared fixture's 1970-era buckets, isolating s4: 18 tokens / 0.18 s
        tot = server.report(sessions, group="model", from_day=day_of(self.D12))["totals"]
        self.assertEqual(tot["tps"], 100.0)

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

    def test_resolve_ignores_mmdd_build_stamp(self):
        self.assertEqual(pricing.resolve("deepseek-v4-1-flash-1017-high"), "DeepSeek V4.1 Flash High")
        self.assertEqual(pricing.resolve("deepseek-v4-1-flash-high"), "DeepSeek V4.1 Flash High")
        self.assertEqual(pricing.resolve("deepseek-v4-1-flash-1232-high"), None)  # not a date: stays unpriced

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

    def test_pricing_survives_malformed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_path, manual_path = Path(tmp) / "d.json", Path(tmp) / "m.json"
            orig = pricing.DATA_PATH, pricing.MANUAL_PATH
            write = lambda p, text, mtime: (p.write_text(text), os.utime(p, (mtime, mtime)))
            try:
                pricing.DATA_PATH, pricing.MANUAL_PATH = data_path, manual_path
                write(data_path, json.dumps({"models": [
                    {"uid": "x-9", "label": "X 9", "input": "3", "output": 0,
                     "cache_write": 0, "cache_read": 0}, 42, {"label": "No Rates"}]}), 1000)
                self.assertEqual(pricing.price_info("x-9"), ("X 9", (3.0, 0.0, 0.0, 0.0)))
                self.assertIsNone(pricing.resolve("claude-haiku-5-5-high"))  # manual file absent
                write(manual_path, "{ not json ]", 1500)
                self.assertIsNone(pricing.resolve("claude-haiku-5-5-high"))
                write(data_path, "[1, 2, 3]", 2000)  # valid JSON, not an object
                write(manual_path, json.dumps({"models": [{"label": "Claude Haiku 5.5",
                    "uids": ["claude-haiku-5-5-high"], "input": 0.1, "output": 0.5,
                    "cache_write": 0.125, "cache_read": 0.01}]}), 2500)
                self.assertIsNone(pricing.resolve("x-9"))
                self.assertEqual(pricing.resolve("claude-haiku-5-5-high"), "Claude Haiku 5.5")
            finally:
                pricing.DATA_PATH, pricing.MANUAL_PATH = orig
                pricing._load()

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


class OpenDashboardTest(unittest.TestCase):
    def test_find_app_shortcut_searches_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = Path(tmp) / "Chrome Apps"
            apps.mkdir()
            lnk = apps / "Session Ledger.lnk"
            lnk.write_text("")
            self.assertEqual(server.find_app_shortcut(roots=[Path(tmp) / "missing", Path(tmp)]), lnk)
            self.assertIsNone(server.find_app_shortcut("Other App", roots=[Path(tmp)]))

    def test_open_dashboard_prefers_installed_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            lnk = Path(tmp) / "Session Ledger.lnk"
            lnk.write_text("")
            with mock.patch.dict(os.environ, {"DASHBOARD_APP": str(lnk)}), \
                 mock.patch.object(server.os, "startfile", create=True) as startfile, \
                 mock.patch.object(server.webbrowser, "open") as browser:
                server.open_dashboard("http://127.0.0.1:8765/")
            startfile.assert_called_once_with(str(lnk))
            browser.assert_not_called()

    def test_open_dashboard_falls_back_to_browser(self):
        with mock.patch.dict(os.environ, {"DASHBOARD_APP": "browser"}), \
             mock.patch.object(server.webbrowser, "open") as browser:
            server.open_dashboard("http://127.0.0.1:8765/")
        browser.assert_called_once_with("http://127.0.0.1:8765/")

    def test_open_dashboard_falls_back_when_launch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            lnk = Path(tmp) / "Session Ledger.lnk"
            lnk.write_text("")
            with mock.patch.dict(os.environ, {"DASHBOARD_APP": str(lnk)}), \
                 mock.patch.object(server.os, "startfile", side_effect=OSError, create=True), \
                 mock.patch.object(server.webbrowser, "open") as browser:
                server.open_dashboard("http://127.0.0.1:8765/")
            browser.assert_called_once_with("http://127.0.0.1:8765/")


if __name__ == "__main__":
    unittest.main()
