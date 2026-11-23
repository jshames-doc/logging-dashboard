# Add cloud sessions to Session Ledger

Extend the Session Ledger dashboard to ingest Devin Cloud sessions via the v3 API as a third `src`, showing status, exact on-demand USD spend, and per-session ACU usage — with an optional, clearly-estimated quota-drain attribution for in-quota cloud usage.

## Summary

The dashboard currently merges two local `sessions.db` sources (`cli`, `cli-next`). Add a third source — `cloud` — fed by the Devin v3 API using a Personal Access Token (`cog_…`). Cloud sessions get real rows in the table with status, PR link, on-demand spend in USD (`on_demand_usage`), and ACU consumption (`acus_consumed`). In-quota cloud usage has **no dollar price anywhere** (verified below), so any in-quota dollar figure is derived from the existing allowance/quota-sampler machinery and always displayed as an estimate.

## Verified API facts (docs.devin.ai, Nov 2026)

- `GET /v3/organizations/{org_id}/sessions` — org-scoped list, `Authorization: Bearer cog_…`. PATs (`cog_` prefix) are created on the **PATs** tab of Settings → Devin API; org id (`org-…`) is on the same page. Non-enterprise accounts may issue non-expiring PATs. A PAT has the creating user's permissions — a user who can view org sessions needs no extra grants.
- `GET /v3/organizations/{org_id}/sessions/insights` — **same paginated list shape**, returning `SessionInsightsResponse` = full `SessionResponse` **plus** `num_devin_messages`, `num_user_messages`, `session_size` (xs–xl), `analysis`/`analysis_status`. One paginated call yields everything the dashboard needs; **no per-session detail fetches are required**.
- Pagination: `first` (default 100, **max 200**) + `after` (opaque cursor). Response: `{items, has_next_page, end_cursor, total?}`.
- Incremental syncs: `SessionsQueryParams` includes `updated_after` / `updated_before` / `created_after` (epoch ints), plus `devin_id`, `session_ids`, `parent_session_id`, `is_archived`, `origins`, `tags` filters. (The OpenAPI marks `qs` "required" — that's a flattened-params artifact; plain `?first=200` works, per the docs' curl examples.)
- `SessionResponse` fields relevant here: `session_id`, `title`, `status`, `status_detail`, `created_at`, `updated_at` (**integer epoch seconds** — same units the cache already uses), `pull_requests: [{pr_url, pr_state}]`, `on_demand_usage`, `acus_consumed` (**required**, always populated), `is_archived`, `url` (direct app.devin.ai link), `devin_mode` (`swe-2-high`, `fusion`, …), `origin` (`webapp|slack|cli|desktop|…`), `parent_session_id`, `child_session_ids`, `tags`, `structured_output`.
- **`status`** enum: `new | claimed | running | exit | error | suspended | resuming`. **`status_detail`** enum: `working | waiting_for_user | waiting_for_approval | finished` (when running) and suspend reasons `inactivity | user_request | usage_limit_exceeded | out_of_credits | out_of_quota | no_quota_allocation | payment_declined | org_usage_limit_exceeded | user_usage_limit_exceeded | total_session_limit_exceeded | contract_expired | error`. There is no `status_enum` field (that's v1); `blocked`/`expired` do not exist.
- **`on_demand_usage`**: USD overage for the session, **excluding child sessions**; `0` when fully covered by quota. Populated on **Teams plans** (which covers self-serve org endpoints); **omitted entirely** on other plans and on `/v3/enterprise/*` — so "—" means "not reported on this plan", not "pre-migration".
- **`acus_consumed`**: cumulative per-session usage in ACUs. ACU is an Enterprise billing unit priced per order form — **there is no public $/ACU rate**, and self-serve plans don't bill in ACUs at all (overage is USD "based on the underlying model cost of each run"). Treat ACUs as their own unit; do not apply a fixed dollar conversion.
- `GET .../sessions/{devin_id}/messages` returns `SessionMessage {event_id, source: devin|user, message, created_at, origin, user_id, username}` — **no token metrics**. The existing per-token pricing pipeline cannot price cloud usage; verified dead end, do not plan around it.
- `/v3/organizations/{org}/consumption/daily/sessions/{id}` (per-session daily ACU) exists but is **Enterprise-only** — 403 on self-serve. Skip it.
- Errors are RFC 9457 `application/problem+json`; **429 is a defined response** — back off and keep last-good data.
- Legacy `apk_user_`/`apk_` keys still work on `/v1/*` during the deprecation period (offset pagination, `status_enum` field names), but v1 is slated for removal — keep any fallback thin.

## Design

### Auth & config
- Env vars, documented and never committed: `DEVIN_API_TOKEN` (`cog_…` PAT), `DEVIN_ORG_ID` (`org-…`), optional `DEVIN_API_BASE` (default `https://api.devin.ai`). Unset → cloud source silently disabled; dashboard behaves exactly as today.
- `run.bat` has no env mechanism — document `set`/`setx`, or read an optional gitignored `cloud.env`/`cloud_config.json`. Pick whichever is least new machinery.
- Optional thin fallback: if only a legacy `apk_user_` key exists, `GET /v1/sessions` (offset pagination) yields metadata without `on_demand_usage`/`acus_consumed`/insights; rows show "—". Only build if the account actually lacks PAT access.

### New module: `cloud_sessions.py`
- `fetch_cloud_sessions()`: `GET /v3/organizations/{org_id}/sessions/insights?first=200[&after=…]`, following `end_cursor` while `has_next_page`. TTL-gated (~5 min, matching `QUOTA_TTL`=300) so `/api/data` polling doesn't hammer the API; on failure return last-good plus an error string.
- Normalize each `SessionInsightsResponse` to the **full** existing cache-entry shape — all keys are required because `flatten`/`report`/`allowance` read them directly (`s["hidden"]`, `s["models"]`, `s.get("daily")`):
  - `id` = `session_id`, `src` = `"cloud"`, `title`, `workspace` = repo derived from `pull_requests[].pr_url` (`github.com/owner/repo/…` → `owner/repo`), else `"—"`
  - `created` = `created_at`, `last_activity` = `updated_at` (epoch ints, no conversion)
  - `hidden` = `is_archived`, `models` = `[]`, `daily` = `[]` (cloud rows emit one placeholder model row in `flatten` → cost "—"; absent from reports/allowance by construction)
  - New fields: `status` (render `status_detail`, fall back to `status`), `pull_requests` (list), `ondemand_usd` (`on_demand_usage`, may be absent), `acus` (`acus_consumed`), `acus_snap` (`[t, acus]` history for Phase 2), `num_devin_messages`, `num_user_messages`, `session_size`, `devin_mode` (populate the Model column with it), `url`, `origin`, `parent_session_id`
- Persist to `.cache/cloud_sessions.json` under the same OS-lock discipline as `quota_history.json` (`msvcrt`/`flock`, never unlink, atomic tmp+replace). Also stores fetch bookkeeping (`last_sync`, `error`) and per-session `acus_snap` history for Phase 2.
- Cache-key discipline: entries keyed `cloud:{session_id}` — consistent with `src:` prefixing; no collisions with local ids.
- Note: `State._refresh` writes `save_cache(merged)` — cloud entries merged into `merged` will also land in `sessions_cache.json`. Acceptable redundancy (cloud file stays authoritative for bookkeeping/ACU snapshots); just don't rely on the main cache for cloud fetch state. Re-merge cloud entries at startup (alongside `load_cache()`) and inside `_refresh` so a refresh can't drop them; on fetch error keep the last-good entries and surface the error via `cloud.error`.
- Handle 429/other HTTP errors with backoff; never let a cloud failure fail the local refresh (`merged` of local DBs must still save).

### `server.py` integration
- Merge normalized cloud dict into `merged` in `State._refresh` and at startup; rows flow through `flatten`, filters, search, sort, CSV unchanged.
- `flatten` pass-through: add `status`, `ondemand_usd`, `acus`, `pull_requests`, `url`, `origin`, `parent_session_id`, `num_devin_messages`, `num_user_messages`, `session_size`, `devin_mode` to the row base for `src == "cloud"` entries (or unconditionally — keys absent on local rows render "—").
- New field on `/api/data`: `cloud: {enabled, last_sync, error}` for a sync-state badge. `POST /api/refresh` triggers a cloud re-sync (subject to TTL).
- `session_id` caveat: the `devin_id` path param is the `devin-`-prefixed id; `session_id` in the response may or may not carry the prefix — only needed if detail endpoints are used later; the `url` field is the authoritative link regardless.

### UI (`static/app.js`, `index.html`, `styles.css`)
- Source column renders `cloud` automatically.
- New columns — **Status** (`status_detail`, e.g. `working`, `waiting_for_user`, `finished`, `suspended: out_of_quota`), **Spend** (`$x.xx` on-demand; `$0` + "within quota" tooltip; `~$x.xx est.` when Phase 2 attribution exists; "—" when the field is omitted), and optionally **ACU** (`acus_consumed`, own unit). Each needs a `COLS` entry, a `CELLS` renderer, and a `<th data-k>` in `index.html` (~line 216). Sorting, the Columns picker, and hash/localStorage persistence then work automatically.
- Columns are **visible by default** (`hiddenCols` is opt-out). Decide: default-visible, or seed them hidden for "opt-in" as originally sketched.
- Session details dialog (`showSession(id, src)` already scoped by `src`): for `src === "cloud"` render status/status_detail, PR links (`pull_requests[]` + `pr_state`), `url` link to open in app.devin.ai, `origin`, `devin_mode`, `num_devin_messages`/`num_user_messages`, `session_size`, `acus`, `ondemand_usd`, `est_drain` — instead of token/model stats.
- `is_archived → hidden` means the existing "show hidden" toggle filters archived cloud sessions for free.

### Usage attribution (Phase 2 — in-quota cloud spend, estimate only)
- **No fixed $/ACU rate exists** — do not convert ACUs to dollars at a guessed rate.
- Instead, reuse the allowance machinery the other way: `acus_consumed` is cumulative per session, so snapshot `[t, acus]` at each sync (and/or alongside the quota sampler). For each allowance window `(t_a, t_b]` with weekly-% drop `d`:
  1. implied $/% = priced local cost in window ÷ `d` (the same assumption `allowance()` already makes);
  2. residual drain = `d` × implied$/% − priced local cost in window = unattributed usage (cloud + other clients);
  3. distribute the residual across cloud sessions **proportional to each session's ΔACU** in the window (`acus_snap` diffs; sessions with no snapshot pair get 0 or last-known interpolation).
- Display as `~$x.xx est.` per session with a tooltip explaining it's a quota-residual estimate — **never merged into `used`**, so the `weekly_usd`/`monthly_usd` allowance figures and their documented semantics are untouched. (Cloud drain is already silently in the allowance denominator today; this only *labels* part of it.)
- Optional refinement: windows with near-zero local usage yield an empirical implied $/ACU for the account (residual ÷ ΣΔACU) — self-calibrating, still estimate-tier.
- Ship behind its own function so the base feature is independent.

### Files
- `cloud_sessions.py` — new module (PAT auth, insights list + cursor pagination, normalize, `.cache/cloud_sessions.json` persistence + lock, TTL, ACU snapshot history, Phase-2 ΔACU attribution helper)
- `server.py` — merge at startup + `_refresh`, `flatten` pass-through, `/api/data` `cloud` field, refresh hook
- `static/app.js`, `static/index.html`, `static/styles.css` — Status/Spend/ACU columns (`COLS`/`CELLS`/`<th data-k>`), cloud detail rendering, sync-state badge, `~$ est.` tooltips
- `test_server.py` — unit tests: normalization shape (all required keys incl. `hidden`/`models`/`daily`), `pull_requests[]` → workspace derivation, TTL + last-good-on-error, flatten pass-through, disabled-when-unconfigured, ΔACU window attribution math (mocked `urllib.request.urlopen`, no network)
- `AGENTS.md` — document env vars, `src: cloud`, `on_demand_usage` semantics (USD overage only; omitted off-Teams), `acus_consumed` (ACUs, no public $ rate), estimate labeling

## Verification
- `python -m unittest test_server` — existing + new tests must pass
- Manual: set `DEVIN_API_TOKEN`/`DEVIN_ORG_ID`, `run.bat`, confirm cloud rows appear with `src: cloud`, status, spend, ACUs; `POST /api/refresh` re-syncs; details dialog shows PR/url/insights; archived sessions hide under the existing toggle

## Risks
- `on_demand_usage` presence depends on plan (Teams-populated); verify the field on a live response before wiring the Spend column — first real fetch should dump one raw item to a debug log for field-name confirmation.
- `session_id` prefix (`devin-`) and exact `url` format: verify on live response before using either for detail calls or links.
- PATs require the user to have org-session visibility; a user-level PAT on an org where they can't view all sessions will 403 — surface via `cloud.error`, don't fail refresh.
- Legacy v1 fallback: `apk_` keys are deprecated and may be removed — treat as best-effort only.
- Estimates in Phase 2 inherit the allowance assumptions (whole-% quantization, account-wide denominator); always labeled `~est.`, never in totals.
