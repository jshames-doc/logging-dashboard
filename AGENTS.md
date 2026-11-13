# Session Ledger (local Devin IDE token dashboard)

- Run: `run.bat` (starts `python server.py` on http://127.0.0.1:8765 and opens the dashboard). `--no-browser` to skip opening.
  `server.open_dashboard` prefers the installed PWA: it finds the web-app shortcut the browser created in
  `%APPDATA%\...\Start Menu\Programs\Chrome Apps\Session Ledger.lnk` (also searched: machine Start Menu, Desktop) and
  launches it via `os.startfile`, so the app window opens instead of a browser tab; falls back to `webbrowser.open`.
  `DASHBOARD_APP` overrides the shortcut/exe path, `DASHBOARD_APP=browser` forces the browser. The shortcut is named after
  the page `<title>` in `static/index.html`, so keep `APP_TITLE` in sync when the title changes.
- Stop: `stop_server.bat` (kills the `server.py` process listening on `DASHBOARD_PORT`/8765; won't touch other programs on that port).
  The server also exits on its own ~90s after the dashboard tab/browser closes: `app.js` pings `/api/ping` every 20s and
  `server.watch_browser` shuts down once pings stop. The watchdog only arms after the first ping, so `--no-browser` stays up.
- Tests: `python -m unittest test_server` (requires Node.js for the model-filter unit test; uses a mocked DOM, no browser).
- Data source: every existing `sessions.db` under `%APPDATA%\devin\cli` (CLI) and `%APPDATA%\devin\cli-next` (IDE),
  opened read-only and merged; override with `SESSIONS_DB` (accepts an `os.pathsep`-joined list). Port override: `DASHBOARD_PORT`.
  Each session is tagged `src` = the DB's parent folder name (the **Source** column); cache keys are `src:session-id` so
  identical session ids across DBs can't collide.
- Tokens come from `message_nodes.chat_message -> $.metadata.metrics` on assistant messages, deduped by `request_id`
  (forked branches duplicate nodes; falls back to `message_id`, and rows with neither are all counted — only non-null keys dedupe).
  Input = input_tokens + cache_creation_tokens; Cached = cache_read_tokens.
- TPS = output tokens ÷ generation time, token-weighted (sum(output) ÷ sum(gen_ms)). The CLI stores `tokens_per_sec`
  per message (= output / (total_time_ms − ttft_ms); ttft_ms is null in ~⅓ of rows), so the aggregator derives
  `gen_ms = output_tokens / tokens_per_sec * 1000`, skipping rows without positive output/tps (zero-output responses).
  TPS is computed at serve time in `with_price` and per report group; shown as a table column (after Output), an
  "Avg TPS" totals card, and a report column.
- `message_nodes.created_at` is rewritten on save; use `$.metadata.created_at` for real message times.
- Aggregates cached in `.cache/sessions_cache.json`, keyed by `src:session-id` and invalidated on `last_activity_at` change; bump `CACHE_VERSION` when the aggregate shape changes.
  Each session stores `models` (per-model totals) and `daily` (per-(model, local server day) totals, same fields plus `long`
  and `usage`: deduplicated request timestamps and token parts, including long-prompt parts when applicable).
- Report maker: **Report** button -> dialog backed by `GET /api/report?group=model|workspace|day|week|month&from=YYYY-MM-DD&to=YYYY-MM-DD&exc=slug,slug&hidden=1`.
  Groups the cached `daily` buckets server-side (workspace groups by the session's `working_directory`, showing the basename with the
  full path as tooltip; sessions without one fold into "—"). Price is computed per model inside each group so long-prompt tiers apply;
  `unpriced` counts models without rates; TPS is token-weighted per group). Each row also carries `series`: per-model
  `{model, tokens, price}` inside the group, which drives the stacked usage-over-time chart shown for day/week/month
  groups (columns in chronological key order regardless of table sort; top 7 models by the metric get palette colors,
  the rest fold into "other"; Cost/Tokens toggle, Cost disabled when nothing is priced). A View toggle swaps the chart
  for the sortable list (`report.view`, persisted in localStorage `ledger.reportView`, default chart). The model dropdown is Excel-style: all checked = included, "(Select all)" tri-state box,
  per-model uncheck = exclude (sent as `exc`). Sortable columns, Download CSV exports the currently shown rows.
- Table view state (search text, model set, workspace, dates, show-hidden, sort) persists in the URL hash via
  `saveView()`/`applyView()` in `app.js` (`models` is a JSON array; stale model/workspace values are pruned on restore),
  so filtered views survive reload and can be bookmarked. Column visibility stays in localStorage `ledger.cols`.
  **Allowance value** button -> `GET /api/allowance?from&to&exc&hidden`: `server.allowance` pairs consecutive quota
  samples inside the range that share a known, unexpired `weekly_reset` into windows where `weekly` dropped, sums priced
  requests whose timestamps fall in `(start, end]`, and returns `weekly_usd = cost × 100 ÷ Δweekly%` (`monthly_usd = × 4.33`);
  `None` when no usable windows (too few samples or no drop — note tells the user why). Quota is account-wide while cost
  is local-only, so other clients' and excluded models' drain stay in the denominator.
- Prices: `update_prices.bat` (or `python update_prices.py [--dry-run] [--file page.html]`) fetches docs.devin.ai/desktop/models,
  parses the embedded `modelCostData` array (Self-serve tab = `TEAMS_TIER_PRO`, the rates self-serve plans pay;
  Enterprise-ACU tier fallback — accepts the legacy `TEAMS_TIER_ENTERPRISE_SAAS` name too — for models only published there), prints a diff
  and writes `pricing_data.json`. Models dropped from the page are kept at their last-known rates marked tier `"Legacy"`
  (`merge_legacy`), so historical sessions stay priced. `pricing.py` hot-reloads that file on mtime change; no restart or cache bump needed.
- `pricing_manual.json`: hand-entered rates for models Devin doesn't publish (e.g. Claude Haiku 5.5 from Anthropic's pricing page;
  now only covers the plain `claude-haiku-5-5` slug — the effort-suffixed slugs are published and take precedence).
  Never overwritten by the updater; published rates win if the model appears later (a manual entry whose `uids` intersect
  published uids is dropped entirely). Entries list DB slugs in `uids` and may set
  `long_prompt` rates for requests whose prompt (input + cache write + cache read) > 100K; the aggregator tracks those requests
  separately per model in the `long` bucket.
- `pricing.resolve()` maps DB model slugs: OVERRIDES -> published `model_uid` (most slugs) -> ordered-token label match ignoring
  "thinking" (order matters so opus-5-5 != Opus 5), then also ignoring a 4-digit MMDD build stamp
  (`deepseek-v4-1-flash-1017-high` -> DeepSeek V4.1 Flash High). Price is computed at serve time (`server.with_price`) from
  cached token parts. Unpriced slugs (summarizer, compactor, MODEL_SWE_1_5) show "—".
- Session details: double-click a table row (or focus it and press Enter/Space) to open the native dialog.
  Details aggregate all rows for that session from `state.rows`, independent of table filters; unpriced models make the cost partial.
  Shows an Avg TPS stat and a per-model TPS column (token-weighted; "—" when no response had measurable speed).
  The dialog and model-filter unit tests use Node.js with a mocked DOM.
- Column picker: **Columns** dropdown in the filter bar toggles `#tbl` columns (`COLS`/`CELLS` maps in `app.js`;
  `state.hiddenCols` persisted in localStorage `ledger.cols`, all visible by default, at least one column stays shown).
  Dropdown rows drag to reorder (`state.colOrder` -> `ledger.colOrder`; `applyOrder` re-appends the `th`s, body cells
  follow in `render`); **Reset order & widths** restores defaults. Header right-edge grips (`initColResize`, `.rsz`) resize
  columns into `state.colWidths` -> `ledger.colWidths`, painted through `#colg` under `table-layout: fixed` (`.sized` class
  on `#tbl`) once the first render with rows seeds widths measured from auto layout — `title` stays widthless to absorb
  leftover table width (a resized `title` switches the leftover to proportional distribution).
- Quota columns (Daily % / Weekly %): `server.quota()` calls the CLI's `GetUserStatus` endpoint
  (`{api_server_url}/exa.seat_management_pb.SeatManagementService/GetUserStatus`, POST JSON `{"metadata": {...}}`;
  api_key + api_server_url come from `%APPDATA%\devin\credentials.toml` — the metadata block must include ide_name,
  ide_version, extension_name, extension_version or it's a 400; the key never leaves the server). Cached `QUOTA_TTL`=300s;
  invoked by `/api/data` and `/api/ping` so samples accrue while the dashboard is open.
  `record_quota` merges (fetched, daily, weekly, daily_reset, weekly_reset) samples into `.cache/quota_history.json` under a
  persistent `quota_history.lock` OS-backed file lock (`msvcrt` on Windows, `flock` elsewhere; never unlink it, and process
  exit releases ownership automatically). The cache directory is created before opening the lock (disk is authoritative —
  the in-memory `_quota_hist` is only slice-assigned from disk, never
  rebind it); unchanged values are still appended once per ~10 min for coverage, while any change (including a reset advance)
  records immediately. `python server.py --sample-only` records one sample and exits (the "Session Ledger quota sampler"
  scheduled task runs it every 15 min via `register_sampler.bat`; `unregister_sampler.bat` removes it, `sample_quota.bat`
  runs it manually; each run appends one line to `.cache/sampler.log`). It never opens sessions.db or starts the server.
  `flatten` stamps each row with the latest sample at or before `last_activity` (bisect on sample time only — values may be
  None), so columns show quota in effect when the session ended. A value is only valid until that sample's reset for its
  counter; past the reset the cell shows "—" (tooltip explains), and identical percents re-record when a reset advances.
  Legacy 3-field samples load padded with unbounded resets. Percent fields only exist on quota-billed plans.
  `server.quota_runway()` projects per-counter burn rate and exhaustion from same-cycle samples (status unknown|exhausted|
  flat|lasts|ok; confidence none|low|medium|high; cycle-average fallback is always low). It's an additive `runway` field on
  `/api/data` and `/api/ping`, rendered by the `#runway` widget after the header (`setRunway`/`renderRunway`/`runwayCardHTML`
  in app.js; the ping response feeds it and a 30 s ticker advances countdowns against `runway.generated`, not the client
  clock).
