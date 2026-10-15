"""Local dashboard server for Devin IDE session token usage.

Reads the IDE's sessions.db read-only, aggregates token metrics per
(session, model), caches results on disk, and serves a static dashboard.
"""
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pricing
from pricing import LONG_PROMPT_TOKENS, cost

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SESSIONS_DB") or Path(os.environ.get("APPDATA", "")) / "devin" / "cli" / "sessions.db")
CACHE_PATH = ROOT / ".cache" / "sessions_cache.json"
HOST, PORT = "127.0.0.1", int(os.environ.get("DASHBOARD_PORT", "8765"))
CACHE_VERSION = 7

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


def connect(db_path=DB_PATH):
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, check_same_thread=False)


def parse_ts(iso, fallback):
    """'2026-10-12T15:14:21.969281100Z' -> epoch seconds (node created_at is rewritten on save, so prefer this)."""
    try:
        return int(datetime.fromisoformat(iso[:19]).replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return fallback


def _empty_day(day, name):
    return {"day": day, "model": name, "messages": 0, "input": 0, "output": 0, "cached": 0,
            "cache_write": 0, "long": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}}


def aggregate_session(conn, session_id, fallback_model=""):
    """Return (per-model totals, per-(model, local day) totals) for one session,
    de-duplicating forked copies of the same assistant response by request_id
    (or message_id). The daily buckets back /api/report."""
    seen, models, daily = set(), {}, {}
    for rid, mid, gm, metrics, iso, node_ts in conn.execute(ASSISTANT_SQL, (session_id,)):
        if not metrics:
            continue
        key = rid or mid
        if key in seen:
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
        seen.add(key)
        name = gm or fallback_model or "unknown"
        agg = models.setdefault(name, {"model": name, "first": created, "last": created, "messages": 0,
                                       "input": 0, "output": 0, "cached": 0, "cache_write": 0,
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
        if inp + cw + cr > LONG_PROMPT_TOKENS:  # long-prompt requests, for models with prompt-size price tiers
            for k, v in (("input", inp), ("output", out), ("cache_write", cw), ("cache_read", cr)):
                agg["long"][k] += v
                d["long"][k] += v
    return (sorted(models.values(), key=lambda a: a["first"]),
            sorted(daily.values(), key=lambda a: (a["day"], a["model"])))


def with_price(m):
    """Price is computed at serve time from the token parts, so price updates need no cache rebuild."""
    cw = m.get("cache_write", 0)
    parts = {"input": m["input"] - cw, "output": m["output"], "cache_write": cw, "cache_read": m["cached"]}
    label, price = cost(m["model"], parts, m.get("long"))
    return {**m, "price": price, "price_model": label}


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


def refresh_sessions(conn, cache, progress=lambda done, total: None):
    """Re-aggregate only sessions whose last_activity_at changed. Returns new cache dict."""
    rows = conn.execute("SELECT id, title, working_directory, model, created_at, last_activity_at, hidden "
                        "FROM sessions ORDER BY last_activity_at DESC").fetchall()
    out = {}
    for i, (sid, title, wd, model, created, last, hidden) in enumerate(rows):
        prev = cache.get(sid)
        if prev and prev["last_activity"] == last:
            models, daily = prev["models"], prev.get("daily", [])
        else:
            models, daily = aggregate_session(conn, sid, model)
        out[sid] = {"id": sid, "title": title or "", "workspace": wd, "created": created,
                    "last_activity": last, "hidden": bool(hidden), "models": models, "daily": daily}
        progress(i + 1, len(rows))
    return out


def flatten(sessions):
    """One row per (session, model). Sessions without any metrics get a single empty row."""
    rows = []
    for s in sessions.values():
        base = {k: s[k] for k in ("id", "title", "workspace", "created", "last_activity", "hidden")}
        for m in s["models"] or [{"model": "—", "first": s["created"], "last": s["last_activity"],
                                   "messages": 0, "input": 0, "output": 0, "cached": 0, "cache_write": 0}]:
            rows.append({**base, **with_price(m)})
    return rows


REPORT_GROUPS = ("model", "day", "week", "month")


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
            key, label = (b["model"], b["model"]) if group == "model" else _period(b["day"], group)
            g = meta.setdefault(key, {"key": key, "label": label, "sessions": set(), "models": set(),
                                      "messages": 0, "input": 0, "output": 0, "cached": 0,
                                      "cache_write": 0, "price": 0.0, "priced": 0, "unpriced": 0})
            g["sessions"].add(s["id"])
            g["models"].add(b["model"])
            all_sessions.add(s["id"])
            all_models.add(b["model"])
            for f in ("messages", "input", "output", "cached", "cache_write"):
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
    rows = [{"key": g["key"], "label": g["label"], "sessions": len(g["sessions"]),
             "models": len(g["models"]), "messages": g["messages"], "input": g["input"],
             "output": g["output"], "cached": g["cached"], "cache_write": g["cache_write"],
             "price": g["price"] if g["priced"] else None, "unpriced": g["unpriced"],
             "price_model": g.get("price_model")} for g in meta.values()]
    rows.sort(key=lambda r: r["key"])
    priced = [r["price"] for r in rows if r["price"] is not None]
    totals = {"sessions": len(all_sessions), "models": len(all_models),
              "messages": sum(r["messages"] for r in rows),
              "input": sum(r["input"] for r in rows), "output": sum(r["output"] for r in rows),
              "cached": sum(r["cached"] for r in rows), "cache_write": sum(r["cache_write"] for r in rows),
              "price": sum(priced) if priced else None,
              "unpriced": sum(r["unpriced"] for r in rows)}
    return {"group": group, "rows": rows, "totals": totals, "generated": int(time.time())}


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
            conn = connect()
            try:
                result = refresh_sessions(conn, self.sessions, lambda d, t: self.status.update(done=d, total=t))
            finally:
                conn.close()
            self.sessions = result
            save_cache(result)
            self.status["updated"] = int(time.time())
        except Exception as e:  # surfaced to the UI
            self.status["error"] = str(e)
        finally:
            self.status["running"] = False


STATE = State()

PING_TIMEOUT = 90  # page pings every 20s; Chrome throttles background tabs to ~1/min
last_ping = [0.0]  # watchdog arms on the first /api/ping, so --no-browser stays up


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
            return self._json({"status": STATE.status, "db": str(DB_PATH), "rows": flatten(STATE.sessions)})
        if self.path.startswith("/api/status"):
            return self._json(STATE.status)
        if self.path.startswith("/api/ping"):
            last_ping[0] = time.time()
            return self._json({"ok": True})
        if self.path.startswith("/api/pricing"):
            return self._json(pricing.snapshot())
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
    if not DB_PATH.exists():
        sys.exit(f"sessions.db not found at {DB_PATH} (set SESSIONS_DB to override)")
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
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
