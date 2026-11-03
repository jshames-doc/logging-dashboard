"""Local dashboard server for Devin IDE session token usage.

Reads the IDE's sessions.db read-only, aggregates token metrics per
(session, model), caches results on disk, and serves a static dashboard.
"""
import bisect
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pricing
from pricing import LONG_PROMPT_TOKENS, cost

ROOT = Path(__file__).resolve().parent
APP_TITLE = "Session Ledger"  # matches <title> in static/index.html and the installed web-app shortcut


def _configured_dbs():
    """SESSIONS_DB may hold several paths joined by os.pathsep (';' on Windows).
    By default both Devin databases are read: the CLI's and the IDE's (cli-next),
    each included only if the file exists at refresh time."""
    env = os.environ.get("SESSIONS_DB")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p.strip()]
    appdata = Path(os.environ.get("APPDATA", "")) / "devin"
    return [appdata / "cli" / "sessions.db", appdata / "cli-next" / "sessions.db"]


def _db_label(path, paths):
    """Short per-database tag for the Source column: the parent folder name
    ('cli', 'cli-next'), unless two configured DBs share it."""
    name = path.parent.name or path.stem
    return name if sum(p.parent.name == name for p in paths) == 1 else str(path)


DB_PATHS = _configured_dbs()
DB_SOURCES = [(_db_label(p, DB_PATHS), p) for p in DB_PATHS]
CACHE_PATH = ROOT / ".cache" / "sessions_cache.json"
HOST, PORT = "127.0.0.1", int(os.environ.get("DASHBOARD_PORT", "8765"))
CACHE_VERSION = 11  # 11: daily buckets include timestamped usage for allowance windows

ASSISTANT_SQL = """
SELECT json_extract(chat_message, '$.metadata.request_id'),
       json_extract(chat_message, '$.message_id'),
       json_extract(chat_message, '$.metadata.generation_model'),
       json_extract(chat_message, '$.metadata.metrics'),
       json_extract(chat_message, '$.metadata.created_at'),
       created_at
FROM message_nodes
WHERE session_id = ?
  AND chat_message LIKE '%"role":"assistant"%'
  AND json_extract(chat_message, '$.role') = 'assistant'
"""


def connect(db_path):
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, check_same_thread=False)


def parse_ts(iso, fallback):
    """'2026-10-12T15:14:21.969281100Z' -> epoch seconds (node created_at is rewritten on save, so prefer this)."""
    try:
        return int(datetime.fromisoformat(iso[:19]).replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return fallback


def _empty_day(day, name):
    return {"day": day, "model": name, "messages": 0, "input": 0, "output": 0, "cached": 0,
            "cache_write": 0, "gen_ms": 0, "usage": [],
            "long": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}}


def aggregate_session(conn, session_id, fallback_model=""):
    """Return (per-model totals, per-(model, local day) totals) for one session,
    de-duplicating forked copies of the same assistant response by request_id
    (or message_id). The daily buckets back /api/report."""
    seen, models, daily = set(), {}, {}
    for rid, mid, gm, metrics, iso, node_ts in conn.execute(ASSISTANT_SQL, (session_id,)):
        if not metrics:
            continue
        key = rid or mid
        if key is not None and key in seen:
            continue
        try:
            m = json.loads(metrics)
            created = parse_ts(iso, node_ts)
            inp, cw = m.get("input_tokens") or 0, m.get("cache_creation_tokens") or 0
            out, cr = m.get("output_tokens") or 0, m.get("cache_read_tokens") or 0
            if not all(isinstance(v, (int, float)) for v in (inp, cw, out, cr)):
                continue
            day = datetime.fromtimestamp(created).date().isoformat()
        except (TypeError, ValueError, AttributeError, OverflowError, OSError):
            continue  # malformed metrics or unusable timestamp: skip the row, not the whole scan
        if key is not None:  # no rid/mid: can't dedupe, so count every such row
            seen.add(key)
        name = gm or fallback_model or "unknown"
        agg = models.setdefault(name, {"model": name, "first": created, "last": created, "messages": 0,
                                       "input": 0, "output": 0, "cached": 0, "cache_write": 0, "gen_ms": 0,
                                       "long": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}})
        agg["first"], agg["last"] = min(agg["first"], created), max(agg["last"], created)
        agg["messages"] += 1
        agg["input"] += inp + cw
        agg["cache_write"] += cw
        agg["output"] += out
        agg["cached"] += cr
        d = daily.setdefault((name, day), _empty_day(day, name))
        d["messages"] += 1
        d["input"] += inp + cw
        d["cache_write"] += cw
        d["output"] += out
        d["cached"] += cr
        usage = {"at": created, "input": inp, "output": out, "cache_write": cw, "cache_read": cr}
        d["usage"].append(usage)
        tps = m.get("tokens_per_sec")  # generation time for the TPS metric: out / tps, ttft excluded
        if out > 0 and isinstance(tps, (int, float)) and tps > 0:
            gen_ms = out * 1000.0 / tps
            agg["gen_ms"] += gen_ms
            d["gen_ms"] += gen_ms
        if inp + cw + cr > LONG_PROMPT_TOKENS:  # long-prompt requests, for models with prompt-size price tiers
            usage["long"] = {k: usage[k] for k in ("input", "output", "cache_write", "cache_read")}
            for k, v in (("input", inp), ("output", out), ("cache_write", cw), ("cache_read", cr)):
                agg["long"][k] += v
                d["long"][k] += v
    return (sorted(models.values(), key=lambda a: a["first"]),
            sorted(daily.values(), key=lambda a: (a["day"], a["model"])))


def with_price(m):
    """Price and TPS are computed at serve time from the cached parts, so price updates need no cache rebuild."""
    cw = m.get("cache_write", 0)
    parts = {"input": m["input"] - cw, "output": m["output"], "cache_write": cw, "cache_read": m["cached"]}
    label, price = cost(m["model"], parts, m.get("long"))
    gen_ms = m.get("gen_ms") or 0
    return {**m, "price": price, "price_model": label,
            "tps": round(m["output"] * 1000.0 / gen_ms, 1) if gen_ms else None}


def load_cache(path=CACHE_PATH):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data["sessions"] if data.get("version") == CACHE_VERSION else {}
    except (OSError, ValueError, KeyError):
        return {}


def save_cache(sessions, path=CACHE_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": CACHE_VERSION, "sessions": sessions}), encoding="utf-8")
    tmp.replace(path)


def refresh_sessions(conn, cache, progress=lambda done, total: None, src=""):
    """Re-aggregate only sessions whose last_activity_at changed. Returns new cache dict.

    src labels which database the session came from; dict keys are prefixed
    with it so identical session ids in different databases can't collide."""
    rows = conn.execute("SELECT id, title, working_directory, model, created_at, last_activity_at, hidden "
                        "FROM sessions ORDER BY last_activity_at DESC").fetchall()
    out = {}
    for i, (sid, title, wd, model, created, last, hidden) in enumerate(rows):
        key = f"{src}:{sid}" if src else sid
        prev = cache.get(key)
        if prev and prev["last_activity"] == last:
            models, daily = prev["models"], prev.get("daily", [])
        else:
            models, daily = aggregate_session(conn, sid, model)
        out[key] = {"id": sid, "src": src, "title": title or "", "workspace": wd, "created": created,
                    "last_activity": last, "hidden": bool(hidden), "models": models, "daily": daily}
        progress(i + 1, len(rows))
    return out


def flatten(sessions):
    """One row per (session, model). Sessions without any metrics get a single empty row.
    Quota columns show the account sample in effect at the session's last activity."""
    rows = []
    for s in sessions.values():
        base = {k: s[k] for k in ("id", "src", "title", "workspace", "created", "last_activity", "hidden")}
        qs = _sample_at(_quota_hist, s["last_activity"])
        base.update(quota_day=qs[1] if qs else None, quota_week=qs[2] if qs else None,
                    quota_t=qs[0] if qs else None)
        for m in s["models"] or [{"model": "—", "first": s["created"], "last": s["last_activity"],
                                   "messages": 0, "input": 0, "output": 0, "cached": 0, "cache_write": 0, "gen_ms": 0}]:
            rows.append({**base, **with_price(m)})
    return rows


REPORT_GROUPS = ("model", "workspace", "day", "week", "month")


def _period(day, group):
    """ISO day 'YYYY-MM-DD' -> (sortable key, display label) for a report period."""
    d = date.fromisoformat(day)
    if group == "week":
        iso = d.isocalendar()
        mon, sun = d - timedelta(days=iso[2] - 1), d - timedelta(days=iso[2] - 1) + timedelta(days=6)
        if mon.year != sun.year:
            label = f"{mon:%b} {mon.day}, {mon.year} – {sun:%b} {sun.day}, {sun.year}"
        elif mon.month != sun.month:
            label = f"{mon:%b} {mon.day} – {sun:%b} {sun.day}, {sun:%Y}"
        else:
            label = f"{mon:%b} {mon.day}–{sun.day}, {sun:%Y}"
        return f"{iso[0]}-W{iso[1]:02d}", label
    if group == "month":
        return day[:7], f"{d:%B} {d:%Y}"
    return day, f"{d:%b} {d.day}, {d:%Y}"


def report(sessions, exclude=(), from_day=None, to_day=None, group="model", hidden=False):
    """Group cached per-(model, day) buckets into report rows.

    exclude is a set of model slugs to drop (the UI's checkbox list: checked =
    included). from_day/to_day are inclusive ISO dates compared against the
    bucket's local day. Price is computed per model inside each group so
    long-prompt tiers still apply; rows report how many models were unpriced.
    """
    exclude = set(exclude)
    meta, cells, all_sessions, all_models = {}, {}, set(), set()
    for s in sessions.values():
        if s["hidden"] and not hidden:
            continue
        for b in s.get("daily") or []:
            if b["model"] in exclude or (from_day and b["day"] < from_day) or (to_day and b["day"] > to_day):
                continue
            if group == "model":
                key, label = b["model"], b["model"]
            elif group == "workspace":
                key = label = s["workspace"] or "—"
            else:
                key, label = _period(b["day"], group)
            g = meta.setdefault(key, {"key": key, "label": label, "sessions": set(), "models": set(),
                                      "messages": 0, "input": 0, "output": 0, "cached": 0,
                                      "cache_write": 0, "gen_ms": 0, "price": 0.0, "priced": 0,
                                      "unpriced": 0, "series": []})
            g["sessions"].add(s["id"])
            g["models"].add(b["model"])
            all_sessions.add(s["id"])
            all_models.add(b["model"])
            for f in ("messages", "input", "output", "cached", "cache_write", "gen_ms"):
                g[f] += b[f]
            c = cells.setdefault((key, b["model"]),
                                 {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
                                  "long": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}})
            c["input"] += b["input"] - b["cache_write"]
            c["output"] += b["output"]
            c["cache_write"] += b["cache_write"]
            c["cache_read"] += b["cached"]
            for f, v in (b.get("long") or {}).items():
                c["long"][f] += v
    for (key, model), c in cells.items():
        label, usd = cost(model, c, c["long"])
        g = meta[key]
        if usd is None:
            g["unpriced"] += 1
        else:
            g["priced"], g["price"] = g["priced"] + 1, g["price"] + usd
        if group == "model":
            g["price_model"] = label
        g["series"].append({"model": model, "price": usd,
                            "tokens": c["input"] + c["output"] + c["cache_write"] + c["cache_read"]})
    rows = [{"key": g["key"], "label": g["label"], "sessions": len(g["sessions"]),
             "models": len(g["models"]), "messages": g["messages"], "input": g["input"],
             "output": g["output"], "cached": g["cached"], "cache_write": g["cache_write"],
             "tps": round(g["output"] * 1000.0 / g["gen_ms"], 1) if g["gen_ms"] else None,
             "price": g["price"] if g["priced"] else None, "unpriced": g["unpriced"],
             "price_model": g.get("price_model"),
             "series": sorted(g["series"], key=lambda s: s["model"])} for g in meta.values()]
    rows.sort(key=lambda r: r["key"])
    priced = [r["price"] for r in rows if r["price"] is not None]
    tot_gen = sum(g["gen_ms"] for g in meta.values())
    totals = {"sessions": len(all_sessions), "models": len(all_models),
              "messages": sum(r["messages"] for r in rows),
              "input": sum(r["input"] for r in rows), "output": sum(r["output"] for r in rows),
              "cached": sum(r["cached"] for r in rows), "cache_write": sum(r["cache_write"] for r in rows),
              "tps": round(sum(g["output"] for g in meta.values()) * 1000.0 / tot_gen, 1) if tot_gen else None,
              "price": sum(priced) if priced else None,
              "unpriced": sum(r["unpriced"] for r in rows)}
    return {"group": group, "rows": rows, "totals": totals, "generated": int(time.time())}


def allowance(samples, sessions, exclude=(), from_day=None, to_day=None, hidden=False):
    """Implied USD value of the weekly/monthly quota allowance.

    Consecutive quota samples inside the range that share a weekly_reset form
    windows in which the weekly-% drop is attributable to usage in (t_a, t_b].
    Priced requests are matched to windows by their message timestamps;
    weekly_usd = cost * 100 / weekly_delta.
    """
    exclude = set(exclude)
    lo = int(datetime.fromisoformat(from_day).timestamp()) if from_day else None
    hi = int((datetime.fromisoformat(to_day) + timedelta(days=1)).timestamp()) if to_day else None
    ss = [s for s in samples if (lo is None or s[0] >= lo) and (hi is None or s[0] < hi)]
    windows = []
    for a, b in zip(ss, ss[1:]):
        w0, wr0, w1, wr1 = a[2], a[4], b[2], b[4]
        if (w0 is None or w1 is None or wr0 is None or wr1 is None or wr0 != wr1
                or not a[0] < b[0] < wr0 or w0 - w1 <= 0):
            continue  # missing value, weekly reset inside the window, or no drop
        windows.append((a[0], b[0], w0 - w1))
    used, unpriced = 0.0, set()
    ends = [t1 for _, t1, _ in windows]
    for s in sessions.values():
        if s["hidden"] and not hidden:
            continue
        for b in s.get("daily") or []:
            if b["model"] in exclude:
                continue
            for usage in b.get("usage") or []:
                at = usage["at"]
                i = bisect.bisect_left(ends, at)
                if i == len(windows) or not windows[i][0] < at:
                    continue
                _, usd = cost(b["model"], usage, usage.get("long"))
                if usd is None:
                    unpriced.add(b["model"])
                else:
                    used += usd
    measured = sum(t1 - t0 for t0, t1, _ in windows)
    delta = sum(w[2] for w in windows)
    weekly = used * 100.0 / delta if delta else None
    return {"from": from_day, "to": to_day, "samples_in_range": len(ss),
            "windows": len(windows), "measured_hours": round(measured / 3600.0, 1),
            "weekly_delta": delta, "cost": used,
            "weekly_usd": round(weekly, 2) if weekly is not None else None,
            "monthly_usd": round(weekly * 4.33, 2) if weekly is not None else None,
            "unpriced": len(unpriced), "generated": int(time.time())}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = load_cache()
        self.status = {"running": False, "done": 0, "total": 0, "error": None, "updated": None}

    def refresh_async(self):
        with self.lock:
            if self.status["running"]:
                return
            self.status.update(running=True, done=0, total=0, error=None)
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self):
        try:
            merged, done, total = {}, 0, 0
            for src, path in DB_SOURCES:
                if not path.exists():
                    continue
                conn = connect(path)
                try:
                    n0 = len(merged)
                    merged.update(refresh_sessions(
                        conn, self.sessions,
                        lambda d, t, d0=done, t0=total: self.status.update(done=d0 + d, total=t0 + t),
                        src=src))
                    done = total = total + len(merged) - n0
                finally:
                    conn.close()
            self.sessions = merged
            save_cache(merged)
            self.status["updated"] = int(time.time())
        except Exception as e:  # surfaced to the UI
            self.status["error"] = str(e)
        finally:
            self.status["running"] = False


STATE = State()

CREDENTIALS_PATH = Path(os.environ.get("APPDATA", "")) / "devin" / "credentials.toml"
QUOTA_TTL = 300  # seconds between upstream GetUserStatus calls
_quota_lock = threading.Lock()
_quota = {"at": 0.0, "data": None}


def _credentials(path):
    """(api_key, api_server_url) from the Devin CLI's credentials.toml, or None."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    vals = dict(re.findall(r'(?m)^(\w+)\s*=\s*"([^"]*)"', text))
    key, base = vals.get("windsurf_api_key", ""), vals.get("api_server_url", "")
    return (key, base) if key and base else None


def fetch_quota(creds):
    """The same GetUserStatus call the Devin CLI's /usage panel makes.

    Returns the account's plan quota (daily/weekly percent remaining and reset
    times); fields absent under the account's billing strategy are omitted."""
    key, base = creds
    meta = {"api_key": key, "ide_name": "devin-cli", "ide_version": "0.0.0",
            "extension_name": "devin-cli", "extension_version": "0.0.0"}  # all required; api_key alone is a 400
    body = json.dumps({"metadata": meta}).encode()
    req = urllib.request.Request(
        base + "/exa.seat_management_pb.SeatManagementService/GetUserStatus",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        ps = json.loads(r.read()).get("userStatus", {}).get("planStatus", {})
    out = {"plan": (ps.get("planInfo") or {}).get("planName"), "fetched": int(time.time())}
    for src, dst in (("dailyQuotaRemainingPercent", "daily"),
                     ("weeklyQuotaRemainingPercent", "weekly"),
                     ("dailyQuotaResetAtUnix", "daily_reset"),
                     ("weeklyQuotaResetAtUnix", "weekly_reset")):
        try:
            if ps.get(src) is not None:
                out[dst] = int(ps[src])
        except (TypeError, ValueError):
            pass
    return out


def quota(path=CREDENTIALS_PATH):
    """Account quota snapshot, refetched at most every QUOTA_TTL seconds.
    Returns {'error': ...} when credentials or the upstream call fail."""
    now = time.time()
    with _quota_lock:
        if _quota["data"] is not None and now - _quota["at"] < QUOTA_TTL:
            return _quota["data"]
        creds = _credentials(path)
        try:
            data = fetch_quota(creds) if creds else {"error": "Devin credentials not found"}
        except Exception as e:
            data = {"error": str(e)}
        _quota.update(at=now, data=data)
    record_quota(data)
    return data


QUOTA_HISTORY_PATH = ROOT / ".cache" / "quota_history.json"
_hist_lock = threading.Lock()


def load_quota_history(path=QUOTA_HISTORY_PATH):
    """[(t, daily, weekly, daily_reset, weekly_reset)] sorted by t. Any field
    but t may be None; shorter legacy tuples are padded."""
    def num(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return sorted((tuple(num(x) for x in (list(s) + [None] * 5)[:5]) for s in data["samples"]),
                      key=lambda s: s[0] or 0)  # key avoids comparing Nones on a timestamp tie
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return []


_quota_hist = load_quota_history()


def record_quota(data, path=QUOTA_HISTORY_PATH):
    """Append a (fetched, daily, weekly, daily_reset, weekly_reset) sample when
    the values changed; this history backs the per-row quota columns. Errors
    aren't recorded. Reset times dedupe too, so an identical-looking percent in
    a new cycle still extends validity."""
    daily, weekly = data.get("daily"), data.get("weekly")
    if data.get("error") or (daily is None and weekly is None):
        return
    t = int(data.get("fetched") or time.time())
    sample = (t, daily, weekly, data.get("daily_reset"), data.get("weekly_reset"))
    with _hist_lock:
        if _quota_hist and _quota_hist[-1][1:] == sample[1:]:
            return
        _quota_hist.append(sample)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"samples": _quota_hist}), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass


def _sample_at(samples, t):
    """(t, daily, weekly) of the most recent sample at or before unix time t.

    Each value is kept only while t precedes the sample's reset for that counter
    (a missing reset leaves it unbounded): quota consumed after a reset makes an
    older percent meaningless, so expired values come back as None and the UI
    shows unknown. The sample time itself is always returned so the UI can tell
    'expired sample' apart from 'no sample'."""
    i = bisect.bisect_right([s[0] for s in samples], t) - 1
    if i < 0:
        return None
    st, d, w, dr, wr = samples[i]
    return (st,
            d if dr is None or t < dr else None,
            w if wr is None or t < wr else None)


PING_TIMEOUT = 90  # page pings every 20s; Chrome throttles background tabs to ~1/min
last_ping = [0.0]  # watchdog arms on the first /api/ping, so --no-browser stays up


def _shortcut_dirs():
    """Where Chrome/Edge drop shortcuts for installed web apps (PWAs)."""
    for base in (os.environ.get("APPDATA"), os.environ.get("PROGRAMDATA")):
        if base:
            yield Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    home = Path.home()
    yield home / "Desktop"
    yield home / "OneDrive" / "Desktop"


def find_app_shortcut(name=APP_TITLE, roots=None):
    """Path of the installed web-app shortcut named after the dashboard, or None.
    Browsers name the shortcut after the page title, so 'Session Ledger.lnk'
    under Start Menu\\Programs\\Chrome Apps is the installed PWA."""
    for root in _shortcut_dirs() if roots is None else roots:
        try:
            for lnk in root.rglob(name + ".lnk"):
                return lnk
        except OSError:
            continue
    return None


def open_dashboard(url):
    """Open the dashboard window: the installed web app when one is found (so
    run.bat launches the PWA rather than a browser tab), else the default browser.
    DASHBOARD_APP overrides the shortcut/exe to launch; DASHBOARD_APP=browser forces
    the browser."""
    pref = os.environ.get("DASHBOARD_APP", "").strip()
    target = Path(pref).expanduser() if pref and pref.lower() != "browser" else None
    if target is None and pref.lower() != "browser":
        target = find_app_shortcut()
    if target and target.exists():
        try:
            os.startfile(str(target))
            return
        except (AttributeError, OSError):
            pass
    webbrowser.open(url)


def watch_browser(server):
    while True:
        time.sleep(5)
        if last_ping[0] and time.time() - last_ping[0] > PING_TIMEOUT:
            print("Browser closed - shutting down.")
            server.shutdown()
            return


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT / "static"), **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/data"):
            dbs = " + ".join(str(p) for _, p in DB_SOURCES if p.exists())
            q = quota()  # fetch first so the fresh sample lands in history before rows are stamped
            return self._json({"status": STATE.status, "db": dbs, "rows": flatten(STATE.sessions),
                               "quota": q})
        if self.path.startswith("/api/status"):
            return self._json(STATE.status)
        if self.path.startswith("/api/ping"):
            last_ping[0] = time.time()
            return self._json({"ok": True, "quota": quota()})
        if self.path.startswith("/api/pricing"):
            return self._json(pricing.snapshot())
        if self.path.startswith("/api/allowance"):
            q = parse_qs(urlsplit(self.path).query)
            exc = {m for v in q.get("exc", []) for m in v.split(",") if m}
            quota()  # land a fresh sample before measuring
            return self._json(allowance(_quota_hist, STATE.sessions, exclude=exc,
                                        from_day=q.get("from", [None])[0] or None,
                                        to_day=q.get("to", [None])[0] or None,
                                        hidden=q.get("hidden") == ["1"]))
        if self.path.startswith("/api/report"):
            q = parse_qs(urlsplit(self.path).query)
            group = q.get("group", ["model"])[0]
            if group not in REPORT_GROUPS:
                return self._json({"error": f"group must be one of {REPORT_GROUPS}"}, 400)
            exc = {m for v in q.get("exc", []) for m in v.split(",") if m}
            return self._json(report(STATE.sessions, exclude=exc,
                                     from_day=q.get("from", [None])[0] or None,
                                     to_day=q.get("to", [None])[0] or None,
                                     group=group, hidden=q.get("hidden") == ["1"]))
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/refresh"):
            STATE.refresh_async()
            return self._json(STATE.status)
        self.send_error(404)


class Server(ThreadingHTTPServer):
    # Windows SO_REUSEADDR lets a second process bind the same port, leaving a stale server answering requests.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    if not any(p.exists() for _, p in DB_SOURCES):
        sys.exit("no sessions.db found (looked in "
                 + ", ".join(str(p) for _, p in DB_SOURCES)
                 + "; set SESSIONS_DB to override)")
    url = f"http://{HOST}:{PORT}/"
    try:
        server = Server((HOST, PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} is already in use - the dashboard may already be running at {url}. "
                 "Close that window first (or set DASHBOARD_PORT).")
    STATE.refresh_async()
    threading.Thread(target=watch_browser, args=(server,), daemon=True).start()
    print(f"Session dashboard running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        open_dashboard(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
