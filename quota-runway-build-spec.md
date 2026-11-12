# Session Ledger — Quota Runway & Background Sampler Build Spec

Implementation-ready spec for adding a quota runway widget and a scheduled quota sampler to **Session Ledger**.

## 1. What You Are Building

The dashboard already samples daily/weekly quota percent-remaining via `GetUserStatus` and stores changed samples in `.cache/quota_history.json`, shown only as per-session table columns. This adds:

1. A **runway widget** in the header: quota remaining, reset countdown, projected exhaustion at the current burn rate, and a confidence label. Computed server-side, exposed as an additive `runway` field on `/api/data` and `/api/ping`.
2. A **background sampler** (`python server.py --sample-only`) that records one quota sample and exits, so history accrues while the dashboard is closed. This also improves Allowance value coverage.

In scope: `record_quota` merge+lock rewrite, `quota_runway()`, `run_sample_only()`, endpoint fields, the `#runway` widget, scheduler scripts, tests, docs.
Out of scope: notifications, budget settings, history charts, changes to allowance/report/aggregation/pricing/table code, writes to `sessions.db`, new dependencies.

## 2. Current System Orientation (verified)

`server.py`:

- `QUOTA_TTL = 300`, `_quota = {"at": 0.0, "data": None}`, `_quota_lock` guards it.
- `fetch_quota(creds)` → `{"plan", "fetched", "daily", "weekly", "daily_reset", "weekly_reset"}`; any field but `fetched` may be absent; `{"error": ...}` on failure.
- `quota(path=CREDENTIALS_PATH)`: on cache hit returns early **without** calling `record_quota`; on fetch it calls `record_quota(data)` **after releasing** `_quota_lock`. Preserve both orderings.
- `QUOTA_HISTORY_PATH = ROOT / ".cache" / "quota_history.json"`; `_hist_lock` is an existing in-process mutex; `_quota_hist = load_quota_history()` at import.
- `load_quota_history(path)` → sorted list of 5-tuples `(t, daily, weekly, daily_reset, weekly_reset)`; any field but `t` may be `None`; legacy 3-tuples are padded; `[]` on bad/missing file.
- `record_quota(data, path=...)` currently appends to `_quota_hist` when `(daily, weekly, daily_reset, weekly_reset)` differ from the last sample, then rewrites the file via `path.with_suffix(".tmp")` + `tmp.replace(path)`; OSError is swallowed; returns `None`.
- `_sample_at(samples, t)` backs the per-row quota columns. Do not change.
- `Handler.do_GET`: `/api/data` returns `{status, db, rows, quota}`; `/api/ping` sets `last_ping[0]` and returns `{ok, quota}`. Also `/api/status`, `/api/pricing`, `/api/allowance`, `/api/report`; POST `/api/refresh`.
- `main()`: sessions.db existence check → `Server` → `STATE.refresh_async()` → `watch_browser` thread → optional `open_dashboard` → `serve_forever()`.

`static/index.html`: `</header>` (line ~29) is followed by three `<dialog>` blocks, then `<section class="totals" id="totals">`. Dialogs are out of flow, so insert `#runway` right after `</header>`.

`static/app.js`: `$ = id => getElementById`; helpers `esc`, `fmt`, `compact`, `fmtPrice` exist; `state` is the client store; `setData(data)` sets `state.quota`/rows/filters then calls `render()`; line ~694: `setInterval(() => fetch("/api/ping").catch(() => {}), 20000)` — response currently ignored.

`static/style.css`: dark palette via `:root` vars (`--bg --bg-2 --panel --line --line-2 --ink --ink-2 --ink-3 --in --out --ca`, fonts `--serif --sans --mono`).

`test_server.py`: `unittest` + `unittest.mock`; `QuotaTest.setUp` **rebinds** `server._quota_hist = []` and restores in `tearDown` — this is why `record_quota` must slice-assign, not rebind. Front-end tests run `app.js` in Node's `vm` with a mocked DOM.

## 3. Constraints

- Python 3.8+ stdlib only; vanilla JS; no build step. No new dependencies.
- 127.0.0.1 only; `sessions.db` read-only; the only outbound call remains `GetUserStatus`.
- API consumers: add fields only, never remove/rename. Preserve `_sample_at` and the `quota_day`/`quota_week` columns.
- Windows is primary. Epoch seconds everywhere; local time only where existing code uses it.
- Style: small functions, type-agnostic dicts, comments only for non-obvious rules.

## 4. Files

```
server.py               # merge/lock record_quota, quota_runway(), run_sample_only(), --sample-only, endpoint fields
static/index.html       # <section id="runway"> after </header>
static/app.js           # setRunway(), renderRunway(), ping handler, 30s ticker
static/style.css        # .runway styles
test_server.py          # RunwayTest, merge/lock tests, sampler tests, one JS test
sample_quota.bat        # new: manual sampler invocation
register_sampler.bat    # new: registers 15-min task running pythonw.exe
unregister_sampler.bat  # new: deletes the task
README.md, AGENTS.md    # document widget + sampler
```

No other files.

## 5. Schemas

Quota snapshot (existing) and history file (existing) are unchanged: samples are `[t, daily, weekly, daily_reset, weekly_reset]`, values are **percent remaining** 0–100, resets are epoch seconds; any field but `t` may be `null`. Keep the file format so older versions read it.

New `runway` object on `/api/data` and `/api/ping`:

```json
{"runway": {
  "generated": 1760000000, "plan": "Pro", "error": null, "sample_age_s": 120,
  "history": {"samples_24h": 12, "samples_7d": 84, "oldest": 1759392000, "newest": 1759999880},
  "daily":  {"remaining": 62, "reset": 1760080000, "cycle_start": 1759993600,
             "rate_per_hr": 5.2, "hours_left": 11.92, "exhausts_at": 1760042912,
             "status": "ok", "confidence": "high", "samples": 7, "sampled_at": 1759999880},
  "weekly": { "...same shape..." }
}}
```

Top-level rules:

- `generated`: the clock for all projections/countdowns (epoch seconds).
- `plan`/`error`: copied from the snapshot; `error` is null on success.
- `sample_age_s` = `max(0, generated - history.newest)`, or null with no history.
- `history` counts measure sampling coverage over the **full** history (count tuples even when both values are null): `samples_24h`/`samples_7d` = `t >= generated - 86400/604800`; `oldest`/`newest` = min/max `t`, null when empty.

Counter rules (`daily`/`weekly` identical):

- `remaining`: percent remaining clamped 0–100; null when unknown/expired/missing.
- `reset`: current cycle reset, null when unknown. `cycle_start`: previous reset, else `reset - period`; null when unknown.
- `rate_per_hr`: points/hour consumed, `>= 0`, 2 decimals; `0.0` = flat; null when unknown.
- `hours_left` = `remaining / rate_per_hr` (2 decimals) and `exhausts_at` = `generated + round(hours_left*3600)`; non-null **only** when `status == "ok"` (except `exhausted`: `hours_left=0`, `exhausts_at=generated`).
- `status` ∈ `unknown|exhausted|flat|lasts|ok`; `confidence` ∈ `none|low|medium|high`.
- `samples`: usable samples in the current cycle; `sampled_at`: newest usable sample `t` for that counter.

No other keys.

## 6. Core Logic

### 6.1 `record_quota` — merge under a lock

```
record_quota(data, path=QUOTA_HISTORY_PATH, lock_timeout=5.0) -> bool
```

1. If `data.get("error")` truthy, or both `daily` and `weekly` are `None` → return `False`.
2. `sample = (int(data.get("fetched") or time.time()), daily, weekly, daily_reset, weekly_reset)`.
3. Acquire the in-process `_hist_lock`, then the lock file `path.with_suffix(".lock")` via `os.open(O_CREAT | O_EXCL | O_WRONLY)` (write the PID, close).
   - On `FileExistsError`: if lock mtime older than 30 s, delete and retry (stale recovery); else sleep 0.05 s and retry until `lock_timeout` → then return `False`. Losing one sample is OK; racy writes are not.
4. Read history **from disk** (`load_quota_history(path)`); disk is authoritative — never merge the possibly-stale `_quota_hist` (that would resurrect an intentional reset).
5. **Skip rule** (preserves the old append-on-change contract plus coverage thinning): if disk is non-empty and `disk[-1][1:] == sample[1:]` **and** `sample[0] - disk[-1][0] < 600` → set `_quota_hist[:] = disk` (absorb external samples) and return `False`. The 600 s gap means an unchanged quota still records about one sample per 10 min so runway coverage and "as of" stay fresh; a reset change always differs at `[1:]` so it records immediately (existing test `test_record_quota_rerecords_same_percent_after_reset` keeps passing).
6. Otherwise `merged = sorted(dedupe_exact(disk + [sample]), key=t)` (stable sort). Write `{"samples": merged}` via `with_suffix(".tmp")` + `replace`, then `_quota_hist[:] = merged` — **slice-assign, never rebind** (tests and other threads hold the reference; also update `_quota_hist` in every early-return/absorb path).
7. Return `True`. Release the lock file in `finally`; ignore `OSError` on unlink. On `OSError` during the write, still update `_quota_hist` and return `False`. This function never raises.

Callers keep working unchanged (they ignore the return value). Do not hold `_quota_lock` during file I/O — `quota()` already calls `record_quota` outside it.

### 6.2 Counter mapping

| Counter | value key | reset key | history idx (value/reset) | period s |
|---|---|---|---|---|
| daily | `daily` | `daily_reset` | 1 / 3 | 86,400 |
| weekly | `weekly` | `weekly_reset` | 2 / 4 | 604,800 |

### 6.3 Current cycle (per counter, independently)

1. `now = generated`.
2. `current_reset` = max of (snapshot reset if non-null) ∪ (every sample's reset for this counter with `t <= now`). Max wins so a sampler-recorded newer cycle beats the dashboard's 5-min-cached snapshot. No candidates → `unknown`.
3. `current_reset <= now` → `unknown` (snapshot past reset; next fetch refreshes).
4. `prev_reset` = largest reset `< current_reset` from the same pool. `cycle_start = prev_reset` if it exists and `current_reset - prev_reset <= 1.5 * period`, else `current_reset - period`. `cycle_start >= now` → `unknown`.
5. `cycle_samples` = samples with non-null value, `reset == current_reset`, `t <= now`, sorted by `t`.
6. Merge the snapshot: if it has a non-null value for this counter, `snapshot.reset == current_reset`, and `fetched <= now`, add `(fetched, value)` (replacing any same-`t` entry) and re-sort — even when `cycle_samples` is non-empty, so the freshest point survives a record_quota write that lost its lock race.
7. Still empty → `unknown`. Else `remaining = clamp(latest_value, 0, 100)`, `sampled_at = latest.t`.

Malformed tuples are skipped, not fatal: pad short tuples to 5 (like `load_quota_history`); skip non-sequences or non-int `t`. Wrap each counter's computation so one bad counter can't take down the other.

### 6.4 Burn rate

Using `cycle_samples` newest→oldest adjacent pairs (`latest` is the last element):

- `dt = t[i] - t[i-1]`; skip pair if `dt < 60`.
- `value[i] > value[i-1]` (top-up/plan change) → `rate_per_hr = 0.0`, stop → `flat`.
- `value[i] < value[i-1]` → `rate_per_hr = (value[i-1] - value[i]) / (dt/3600)`, stop.
- equal → continue to the older pair.

Fallback (only if no pair produced a rate): if `remaining >= 100` → `0.0`; else `elapsed = latest_t - cycle_start`; `elapsed < 60` → unknown; else `rate_per_hr = max(0, (100 - remaining) / (elapsed/3600))`. This assumes 100% at `cycle_start` (true at a quota reset) → always `low` confidence. Round to 2 decimals; null rate → `unknown`.

### 6.5 Status (evaluate in order)

| Condition | status | hours_left / exhausts_at |
|---|---|---|
| `remaining`/`rate_per_hr`/`reset` is null | `unknown` | null / null |
| `remaining <= 0` | `exhausted` | 0 / `generated` |
| `rate_per_hr <= 0` | `flat` | null / null |
| `generated + (remaining/rate)*3600 >= reset` | `lasts` | null / null |
| otherwise | `ok` | `remaining/rate` (2 dp) / `generated + round(h*3600)` |

Use `generated`, never wall-clock, so tests are deterministic.

### 6.6 Confidence

`span = cycle_samples[-1].t - cycle_samples[0].t`:

- `unknown` or no samples → `none`
- ≥3 samples, `span >= 3600`, newest sample age `<= 900` → `high`
- ≥2 samples, `span >= 1800` → `medium`
- otherwise → `low` (single sample and the cycle-average fallback always land here)

### 6.7 `quota_runway()` contract

```
quota_runway(samples=None, quota_data=None, now=None) -> dict
```

- `samples=None` → `load_quota_history()`; if empty, fall back to `list(_quota_hist)` (read-only resilience if the file is briefly unreadable).
- `quota_data=None` → `{}`; `now=None` → `int(time.time())`. `now` becomes `generated`.
- Never raises; uncomputable counter → its `unknown` object; exactly the §5 schema.
- `quota_data.error` → copy into `runway.error` but still compute counters from history.
- Call sites: in `/api/data` and `/api/ping`, after `q = quota()`, add `"runway": quota_runway(quota_data=q)`. Reloading the small history file on each call also picks up sampler writes. Not on `/api/status|report|allowance`. Do not touch `flatten`/`_sample_at`.

### 6.8 `--sample-only`

```
run_sample_only() -> int
```

1. `data = quota()` (fresh process → always fetches and records).
2. Build one line: `quota sampled at 2026-11-12 15:04:05 plan=Pro daily=62 daily_reset=2026-11-13 00:00 weekly=41 weekly_reset=2026-11-18 00:00` — omit a `counter=… counter_reset=…` pair when the value is null; `time.strftime` local time, `"%Y-%m-%d %H:%M:%S"` for `fetched`, `"%Y-%m-%d %H:%M"` for resets. Never print the API key or URL.
3. Append the line to `QUOTA_HISTORY_PATH.parent / "sampler.log"` (mkdir parents first) — the scheduled task runs under `pythonw` with no console, so the log is written by Python, not a shell redirect.
4. On error: line is `quota sample failed: <error>`; append it, print to **stderr**, return `1`. Success: print to stdout, return `0`.

Wire into `main()` **before** the sessions.db check:

```python
if "--sample-only" in sys.argv:
    sys.exit(run_sample_only())
```

Must not: start the server, open a browser, refresh the sessions cache, arm the watchdog, or require sessions.db. The import-time `load_cache()` cost is acceptable.

### 6.9 Edge cases

| Case | Behavior |
|---|---|
| Plan lacks a counter | that counter `unknown`; both unknown → "not reported" note |
| Fetch fails, history usable | `error` set, counters still compute; UI warns the sample is stale |
| Value 100 / 0 | `flat` / `exhausted` |
| Top-up between samples | most-recent increase → `rate_per_hr = 0.0` → `flat` |
| One usable sample | cycle-average fallback, `low` |
| Pair `dt < 60` s | skip pair |
| Reset missing, or `now >= reset` | `unknown` |
| Legacy 3-tuples / bad JSON | padded / `[]` via `load_quota_history`; no crash |
| Lock timeout | `False`, file unchanged |
| Error payload to `record_quota` | `False`, no write |
| Clock skew | UI uses `runway.generated` + client elapsed, never raw client clock |

## 7. UI

### 7.1 Markup

After `</header>` in `index.html`:

```html
<section class="runway" id="runway" hidden aria-label="Quota runway"></section>
<span class="sr-only" id="runway-status" aria-live="polite"></span>
```

Hidden until data arrives (no flash on older servers).

### 7.2 Card

One card per usable counter, daily then weekly:

```
DAILY QUOTA                                   62% left
[==================--------------------------]
Resets in 4h 12m · 8:00 PM
At current pace: runs out in 6h 40m · 3:40 AM
High confidence · 7 samples this cycle · as of 2 min ago
```

| status | big value | reset line | projection line |
|---|---|---|---|
| `unknown` | `—` | `Resets in …` if future, `Reset time passed — waiting for a fresh sample` if past, `Reset time unknown` if null | `Not enough samples for a projection` |
| `exhausted` | `0% left` | `Resets in …` | `Depleted · resets in …` |
| `flat` | `N% left` | `Resets in …` | `At current pace: no recent drop` |
| `lasts` | `N% left` | `Resets in …` | `At current pace: lasts until reset` |
| `ok` | `N% left` | `Resets in …` | `At current pace: runs out in {dur} · {clock}` |

- `{dur}` compact: `3h 20m` / `12m` / `<1m`; `{clock}` local `3:40 AM`. If `ok` but `exhaust_in == 0` → `Runs out now`.
- Meter fill width = `remaining`%; `remaining == null` → `—`, no fill. Classes: `good` >50, `warn` 20–50, `low` <20, `empty` 0.
- Meta line: `high` → `High confidence`, `medium` → `Estimated`, `low` → `Rough estimate`, each ` · {n} sample(s) this cycle · as of {ago}`; `none` → omit. `{ago}`: `just now` / `2 min ago` / `1h 12m ago`. Pluralize `sample`.

### 7.3 Error / absent states

- `runway.error` + both counters `unknown` → show section, one line `Quota unavailable — {error}`, no cards.
- `runway.error` + ≥1 usable counter → cards + muted `Quota fetch failed — showing the last sample from {ago}.`
- no error + both `unknown` → `Quota is not reported on this plan.` if `runway.plan`, else hide section.
- no `runway` key → hide, never throw.

### 7.4 Updates

- `setData(data)` → `setRunway(data.runway)`. Ping handler consumes the response:
  ```js
  setInterval(async () => {
    try {
      const res = await fetch("/api/ping");
      if (!res.ok) return;
      const data = await res.json();
      if (data.runway) setRunway(data.runway);
    } catch {}
  }, 20000);
  ```
- `setRunway(r)`: `state.runway = r; state.runwayReceivedAt = Date.now()`; if JSON identical to previous, skip the DOM rebuild; else `renderRunway()`. `$("runway").hidden` toggles per §7.3.
- 30 s ticker calls `renderRunway()` so countdowns/`as of` advance. All countdowns use `now = runway.generated + (Date.now() - state.runwayReceivedAt)/1000`; `reset_in`/`exhaust_in` = `max(0, target - now)`.
- Never re-render the table or reset filters/scroll.

### 7.5 A11y & responsive

- Meter: `role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="62"` + `aria-valuetext="62 percent remaining; projected to run out in 6 hours 40 minutes"`; unknown → omit `aria-valuenow`, `aria-valuetext="Quota unknown"`.
- `#runway-status` live region updates only when a counter's `status` changes or hits `exhausted`.
- `prefers-reduced-motion`: no meter/entrance transitions. Never color-only information.
- Two cards side-by-side, stack <640 px, ~70–90 px tall, errors wrap.

### 7.6 CSS tokens

`.runway`: `display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); border-bottom:1px solid var(--line-2)`. `.runway-card`: padding `10px 14px 12px 0`, `border-right:1px solid var(--line)` (none on last). `.rw-lbl` mono 10px uppercase ls .14em `--ink-3`; `.rw-val` serif 22px `--ink` (`left` suffix mono 10px `--ink-3`); `.rw-reset` `--ink-2`, `.rw-proj` `--ink`, `.rw-meta` `--ink-3` (mono 10–11px). `.meter` 6px `var(--line)`, `> i` fill `var(--ca)` width inline, `.warn`→`--in`, `.low/.empty`→`--out`. Add the reduced-motion and 640 px media queries. Don't touch masthead/totals.

## 8. Tests

`python -m unittest test_server` — all existing tests must still pass.

`RunwayTest` (fixed `NOW`, explicit `samples`/`quota_data`, `assertAlmostEqual` for floats):

| Test | Expect |
|---|---|
| unknown_without_samples | both `unknown`, nulls |
| single_sample_cycle_average | `(NOW-3600, 80, …, NOW+3600, …)` → `lasts`, rate ≈0.91, `low` |
| projects_early_exhaustion | 90→70 over 1 h, reset `NOW+28800` → rate 20.0, `ok`, 3.5 h, `exhausts_at=NOW+12600`, `medium` |
| lasts_until_reset | `(NOW-14400, 80)` reset `NOW+7200` → `lasts`, nulls |
| flat_at_full_quota / top_up_is_flat / exhausted / expired / missing_reset | per §6.5 |
| uses_previous_reset_for_cycle_start | prev reset `NOW-82800` → `cycle_start=NOW-82800` |
| weekly_period_fallback | weekly-only sample → `cycle_start = reset - 604800` |
| confidence_levels | 4 samples/2 h span/5-min-old → `high`; shrink → `medium`/`low` |
| history_counts | samples at `NOW-100` & `NOW-90000` → 24h=1, 7d=2, oldest/newest |
| skips_malformed_samples | `None`, 3-tuple, valid 5-tuple → dict, no crash |
| error_exposed_counters_compute | `{"error":"boom"}` → error set, counter computed |

Merge/lock tests (always `tempfile.TemporaryDirectory`, `path=tmp/"h.json"`, never the real cache):

| Test | Expect |
|---|---|
| preserves_external_samples | disk A, call C → file `[A,C]` |
| deduplicates_last_sample | same values+resets, `t` within 600 s → one sample, second call `False` |
| thins_unchanged_samples | same values, `t` gap ≥600 s → appended (coverage) |
| ignores_stale_in_memory | disk A, `_quota_hist` has B, call C → `[A,C]`, B gone |
| updates_global_from_disk | disk A, empty global, call B → global `[A,B]` |
| lock_timeout_skips_write | fresh `.lock`, `lock_timeout=0.05` → `False`, unchanged |
| recovers_stale_lock | `.lock` mtime −60 s → succeeds, lock removed |
| removes_lock_on_success | no `.lock` afterward |
| does_not_record_errors | error payload → `False`, no file |

Sampler tests: patch `server.quota` → success prints `daily=… weekly=…`, returns 0, appends to `sampler.log`; error → rc 1, stderr has the error, stdout empty. `main()` with `argv=["server.py","--sample-only"]`, patched `run_sample_only`/`Server` → `SystemExit(0)`, `Server` not called; with `DB_SOURCES` pointed at nonexistent paths → still exits 0 (use `assertRaises(SystemExit)` + `cm.exception.code`).

One JS test in the existing Node/`vm` harness: `setRunway` with `daily.status="ok"` (`remaining 62`, `confidence "high"`) + `weekly.status="flat"` → HTML contains `62%`, `runs out in`, `no recent drop`, `High confidence`, `role="meter"`, `aria-valuenow="62"`; both-`unknown` renders the not-enough-samples state without throwing. If the harness can't reach `setRunway`, keep card markup in a pure `runwayCardHTML(counter, now)` and test that.

Manual acceptance: `--sample-only` prints one line & exits 0; rerun doesn't grow history beyond the thinning rule; widget appears after first sample; sampler run updates the widget within 20 s, no reload; fabricated history yields the expected projection; `stop_server.bat` and the watchdog still work.

## 9. Scheduling

Console windows must not flash every 15 min — register `pythonw.exe` directly, not the .bat.

`sample_quota.bat` (manual runs / debugging):

```bat
@echo off
cd /d "%~dp0"
python server.py --sample-only
```

`register_sampler.bat` — resolve `pythonw` via `where pythonw.exe` (error out with instructions if missing), then:

```bat
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '<pythonw.exe path>' -Argument '\"%~dp0server.py\" --sample-only' -WorkingDirectory '%~dp0';" ^
  "$t = New-ScheduledTaskTrigger -Daily -At 12:00AM -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 1);" ^
  "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew;" ^
  "Register-ScheduledTask -TaskName 'Session Ledger quota sampler' -Action $a -Trigger $t -Settings $s -Description 'Samples Devin quota for Session Ledger runway/allowance.' -Force"
```

Runs as the current user (needs `%APPDATA%\devin\credentials.toml` + network). If registration needs elevation, document the fallback: `schtasks /Create /TN "Session Ledger quota sampler" /TR "\"<pythonw>\" \"%~dp0server.py\" --sample-only" /SC MINUTE /MO 15 /F`.

`unregister_sampler.bat`: `@echo off` + `schtasks /Delete /TN "Session Ledger quota sampler" /F`.

Verify: `schtasks /Query /TN "Session Ledger quota sampler" /V /FO LIST` and `type .cache\sampler.log` — one line per run. The sampler uses the same credentials file and calls only Devin's `GetUserStatus` endpoint; the key never leaves the machine.

## 10. Docs & Acceptance

`README.md`: what the meters/projection/confidence mean (a pace estimate, not a guarantee); why background sampling matters (widget + Allowance are only as good as coverage); the three .bat files and verification; privacy note.
`AGENTS.md` quota bullet: `--sample-only` records one sample and exits; `record_quota` merges under `quota_history.lock` with 10-min thinning for unchanged values; `quota_runway()` projects from same-cycle samples, additive `runway` on `/api/data` + `/api/ping`, widget is `#runway`.

Done when: full suite passes; `runway` is additive on both endpoints; all counter states render cleanly; ping updates the widget without touching table/filters/scroll; countdowns tick locally; `--sample-only` exits 0/1 correctly and never starts the server; concurrent writers never lose samples (merge test); stale lock recovers, busy lock skips; sampler works with no sessions.db; task registers/removes; `role="meter"` + reduced-motion honored; no new deps, DB writes, or endpoints; docs updated.

Suggested order: 1) `record_quota` merge/lock + tests → 2) `quota_runway` + tests → 3) sampler + tests → 4) endpoint fields → 5) widget JS/HTML → 6) CSS → 7) batch files + docs → 8) full suite + manual checklist.
