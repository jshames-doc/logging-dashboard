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


def _absorb_history(path):
    """Refresh _quota_hist from disk so samples written by other processes
    (the --sample-only scheduled task) show up without a restart."""
    _quota_hist[:] = load_quota_history(path)


def _lock_quota_file(fd):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def record_quota(data, path=QUOTA_HISTORY_PATH, lock_timeout=5.0):
    """Record a (fetched, daily, weekly, daily_reset, weekly_reset) sample.

    Merges into the on-disk history under a lock file (the --sample-only
    sampler writes from another process, so disk is authoritative and the
    possibly-stale _quota_hist is never merged in — that would resurrect an
    intentional reset). An unchanged sample is still appended once every
    ~10 min so runway coverage and "as of" stay fresh; reset times dedupe
    too, so an identical percent in a new cycle records immediately.
    Returns True when a sample was written; never raises."""
    try:
        daily, weekly = data.get("daily"), data.get("weekly")
        if data.get("error") or (daily is None and weekly is None):
            return False
        sample = (int(data.get("fetched") or time.time()), daily, weekly,
                  data.get("daily_reset"), data.get("weekly_reset"))
    except (AttributeError, TypeError, ValueError):
        return False
    path = Path(path)
    lock = path.with_suffix(".lock")
    deadline = time.monotonic() + lock_timeout
    with _hist_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            while True:
                try:
                    _lock_quota_file(fd)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        _absorb_history(path)
                        return False  # losing one sample is fine; racy writes are not
                    time.sleep(0.05)
            disk = load_quota_history(path)
            if disk and disk[-1][1:] == sample[1:] and sample[0] - disk[-1][0] < 600:
                _quota_hist[:] = disk
                return False
            merged = sorted(set(disk + [sample]), key=lambda s: s[0] or 0)
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"samples": merged}), encoding="utf-8")
                tmp.replace(path)
            except OSError:
                _quota_hist[:] = merged
                return False
            _quota_hist[:] = merged
            return True
        except Exception:
            return False
        finally:
            os.close(fd)


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


_RUNWAY_UNKNOWN = {"remaining": None, "reset": None, "cycle_start": None,
                   "rate_per_hr": None, "hours_left": None, "exhausts_at": None,
                   "status": "unknown", "confidence": "none", "samples": 0,
                   "sampled_at": None}
# counter name -> (history value index, history reset index, cycle seconds)
_RUNWAY_COUNTERS = {"daily": (1, 3, 86400), "weekly": (2, 4, 604800)}


def _norm_samples(samples):
    """Validated [(t, daily, weekly, daily_reset, weekly_reset)] sorted by t:
    short tuples padded, non-sequences and non-int timestamps dropped."""
    out = []
    for s in samples or []:
        if not isinstance(s, (list, tuple)):
            continue
        row = (list(s) + [None] * 5)[:5]
        if not isinstance(row[0], int):
            continue
        out.append(tuple(row))
    return sorted(out, key=lambda s: s[0])


def _counter_runway(samples, quota_data, name, vidx, ridx, period, now):
    """Runway fields for one counter (daily or weekly): current remaining %,
    burn rate from same-cycle samples, projected exhaustion, confidence."""
    c = dict(_RUNWAY_UNKNOWN)
    resets = {s[ridx] for s in samples if isinstance(s[ridx], int) and s[0] <= now}
    snap_reset = quota_data.get(name + "_reset")
    if isinstance(snap_reset, int):
        resets.add(snap_reset)
    if not resets:
        return c
    current = max(resets)  # a sampler-recorded newer cycle beats the cached snapshot
    c["reset"] = current
    if current <= now:
        return c  # reset passed; the next fetch refreshes the cycle
    prev = max((r for r in resets if r < current), default=None)
    c["cycle_start"] = prev if prev is not None and current - prev <= 1.5 * period else current - period
    if c["cycle_start"] >= now:
        return c
    cyc = sorted((s[0], s[vidx]) for s in samples
                 if isinstance(s[vidx], (int, float)) and s[ridx] == current and s[0] <= now)
    snap_v, fetched = quota_data.get(name), quota_data.get("fetched")
    if (isinstance(snap_v, (int, float)) and snap_reset == current
            and isinstance(fetched, (int, float)) and fetched <= now):
        cyc = sorted([p for p in cyc if p[0] != fetched] + [(fetched, snap_v)])
    if not cyc:
        return c
    c["samples"], c["sampled_at"] = len(cyc), cyc[-1][0]
    c["remaining"] = min(100, max(0, cyc[-1][1]))
    rate, fallback = None, False
    for i in range(len(cyc) - 1, 0, -1):
        dt = cyc[i][0] - cyc[i - 1][0]
        if dt < 60:
            continue
        if cyc[i][1] > cyc[i - 1][1]:
            rate = 0.0  # top-up or plan change: recent trend is flat
            break
        if cyc[i][1] < cyc[i - 1][1]:
            rate = (cyc[i - 1][1] - cyc[i][1]) / (dt / 3600)
            break
    if rate is None:  # cycle-average fallback assumes 100% at cycle start -> always low confidence
        fallback = True
        elapsed = cyc[-1][0] - c["cycle_start"]
        if c["remaining"] >= 100:
            rate = 0.0
        elif elapsed >= 60:
            rate = max(0.0, (100 - c["remaining"]) / (elapsed / 3600))
    if rate is None:
        return c  # unknown
    c["rate_per_hr"] = round(rate, 2)
    if c["remaining"] <= 0:
        c.update(status="exhausted", hours_left=0, exhausts_at=now)
    elif c["rate_per_hr"] <= 0:
        c["status"] = "flat"
    elif now + (c["remaining"] / c["rate_per_hr"]) * 3600 >= current:
        c["status"] = "lasts"
    else:
        h = c["remaining"] / c["rate_per_hr"]
        c.update(status="ok", hours_left=round(h, 2), exhausts_at=now + round(h * 3600))
    if fallback:
        c["confidence"] = "low"
    else:
        span, age = cyc[-1][0] - cyc[0][0], now - cyc[-1][0]
        if len(cyc) >= 3 and span >= 3600 and age <= 900:
            c["confidence"] = "high"
        elif len(cyc) >= 2 and span >= 1800:
            c["confidence"] = "medium"
        else:
            c["confidence"] = "low"
    return c


def quota_runway(samples=None, quota_data=None, now=None):
    """Quota runway projection for the header widget.

    Burn rate comes from same-cycle samples with the freshest snapshot value
    merged in; 'generated' (now) is the clock for every projection so the UI
    is immune to client-clock skew. Additive on /api/data and /api/ping;
    never raises."""
    try:
        now = int(time.time()) if now is None else int(now)
    except (TypeError, ValueError):
        now = int(time.time())
    try:
        if samples is None:
            samples = load_quota_history()
            if not samples:
                samples = list(_quota_hist)  # the file may be briefly unreadable
        samples = _norm_samples(samples)
        quota_data = quota_data if isinstance(quota_data, dict) else {}
        newest = samples[-1][0] if samples else None
        runway = {"generated": now, "plan": quota_data.get("plan"),
                  "error": quota_data.get("error"),
                  "sample_age_s": max(0, now - newest) if newest is not None else None,
                  "history": {"samples_24h": sum(1 for s in samples if s[0] >= now - 86400),
                              "samples_7d": sum(1 for s in samples if s[0] >= now - 604800),
                              "oldest": samples[0][0] if samples else None,
                              "newest": newest}}
        for name, (vidx, ridx, period) in _RUNWAY_COUNTERS.items():
            try:
                runway[name] = _counter_runway(samples, quota_data, name, vidx, ridx, period, now)
            except Exception:
                runway[name] = dict(_RUNWAY_UNKNOWN)
        return runway
    except Exception:
        return {"generated": now, "plan": None, "error": "runway unavailable",
                "sample_age_s": None,
                "history": {"samples_24h": 0, "samples_7d": 0, "oldest": None, "newest": None},
                "daily": dict(_RUNWAY_UNKNOWN), "weekly": dict(_RUNWAY_UNKNOWN)}


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
                               "quota": q, "runway": quota_runway(quota_data=q)})
        if self.path.startswith("/api/status"):
            return self._json(STATE.status)
        if self.path.startswith("/api/ping"):
            last_ping[0] = time.time()
            q = quota()
            return self._json({"ok": True, "quota": q, "runway": quota_runway(quota_data=q)})
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


def run_sample_only():
    """One quota sample for the scheduled task: fetch, record, append a line
    to .cache/sampler.log, and exit. Written by Python rather than a shell
    redirect because the task runs under pythonw with no console. Never
    starts the server, touches sessions.db, or prints credentials."""
    data = quota()
    if data.get("error"):
        line, out, rc = f"quota sample failed: {data['error']}", sys.stderr, 1
    else:
        at = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.localtime(data.get("fetched") or time.time()))
        parts = [f"quota sampled at {at}"]
        if data.get("plan"):
            parts.append(f"plan={data['plan']}")
        for name in ("daily", "weekly"):
            if data.get(name) is None:
                continue
            seg = f"{name}={data[name]}"
            if data.get(name + "_reset"):
                seg += f" {name}_reset={time.strftime('%Y-%m-%d %H:%M', time.localtime(data[name + '_reset']))}"
            parts.append(seg)
        line, out, rc = " ".join(parts), sys.stdout, 0
    print(line, file=out)
    try:
        QUOTA_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(QUOTA_HISTORY_PATH.parent / "sampler.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return rc


def main():
    if "--sample-only" in sys.argv:
        sys.exit(run_sample_only())
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
