"""Local dashboard server for Devin IDE session token usage.

Reads the IDE's sessions.db read-only, aggregates token metrics per
(session, model), caches results on disk, and serves a static dashboard.
"""
import json
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SESSIONS_DB") or Path(os.environ.get("APPDATA", "")) / "devin" / "cli" / "sessions.db")
CACHE_PATH = ROOT / ".cache" / "sessions_cache.json"
HOST, PORT = "127.0.0.1", int(os.environ.get("DASHBOARD_PORT", "8765"))
CACHE_VERSION = 2

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


def aggregate_session(conn, session_id, fallback_model=""):
    """Return per-model token totals for one session, de-duplicating forked
    copies of the same assistant response by request_id (or message_id)."""
    seen, models = set(), {}
    for rid, mid, gm, metrics, iso, node_ts in conn.execute(ASSISTANT_SQL, (session_id,)):
        if not metrics:
            continue
        key = rid or mid
        if key in seen:
            continue
        seen.add(key)
        m = json.loads(metrics)
        created = parse_ts(iso, node_ts)
        name = gm or fallback_model or "unknown"
        agg = models.setdefault(name, {"model": name, "first": created, "last": created, "messages": 0,
                                       "input": 0, "output": 0, "cached": 0})
        agg["first"], agg["last"] = min(agg["first"], created), max(agg["last"], created)
        agg["messages"] += 1
        agg["input"] += (m.get("input_tokens") or 0) + (m.get("cache_creation_tokens") or 0)
        agg["output"] += m.get("output_tokens") or 0
        agg["cached"] += m.get("cache_read_tokens") or 0
    return sorted(models.values(), key=lambda a: a["first"])


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
            models = prev["models"]
        else:
            models = aggregate_session(conn, sid, model)
        out[sid] = {"id": sid, "title": title or "", "workspace": wd, "created": created,
                    "last_activity": last, "hidden": bool(hidden), "models": models}
        progress(i + 1, len(rows))
    return out


def flatten(sessions):
    """One row per (session, model). Sessions without any metrics get a single empty row."""
    rows = []
    for s in sessions.values():
        base = {k: s[k] for k in ("id", "title", "workspace", "created", "last_activity", "hidden")}
        for m in s["models"] or [{"model": "—", "first": s["created"], "last": s["last_activity"],
                                   "messages": 0, "input": 0, "output": 0, "cached": 0}]:
            rows.append({**base, **m})
    return rows


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
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/refresh"):
            STATE.refresh_async()
            return self._json(STATE.status)
        self.send_error(404)


def main():
    if not DB_PATH.exists():
        sys.exit(f"sessions.db not found at {DB_PATH} (set SESSIONS_DB to override)")
    STATE.refresh_async()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Session dashboard running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
