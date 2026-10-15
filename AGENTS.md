# Session Ledger (local Devin IDE token dashboard)

- Run: `run.bat` (starts `python server.py` on http://127.0.0.1:8765 and opens the browser). `--no-browser` to skip opening.
- Stop: `stop_server.bat` (kills the `server.py` process listening on `DASHBOARD_PORT`/8765; won't touch other programs on that port).
  The server also exits on its own ~90s after the dashboard tab/browser closes: `app.js` pings `/api/ping` every 20s and
  `server.watch_browser` shuts down once pings stop. The watchdog only arms after the first ping, so `--no-browser` stays up.
- Tests: `python -m unittest test_server` (requires Node.js for the model-filter unit test; uses a mocked DOM, no browser).
- Data source: `%APPDATA%\devin\cli\sessions.db` (opened read-only; override with `SESSIONS_DB`). Port override: `DASHBOARD_PORT`.
- Tokens come from `message_nodes.chat_message -> $.metadata.metrics` on assistant messages, deduped by `request_id`
  (forked branches duplicate nodes). Input = input_tokens + cache_creation_tokens; Cached = cache_read_tokens.
- `message_nodes.created_at` is rewritten on save; use `$.metadata.created_at` for real message times.
- Aggregates cached in `.cache/sessions_cache.json`, keyed by session `last_activity_at`; bump `CACHE_VERSION` when the aggregate shape changes.
  Each session stores `models` (per-model totals) and `daily` (per-(model, local server day) totals, same fields plus `long`).
- Report maker: **Report** button -> dialog backed by `GET /api/report?group=model|day|week|month&from=YYYY-MM-DD&to=YYYY-MM-DD&exc=slug,slug&hidden=1`.
  Groups the cached `daily` buckets server-side (price is computed per model inside each group so long-prompt tiers apply;
  `unpriced` counts models without rates). The model dropdown is Excel-style: all checked = included, "(Select all)" tri-state box,
  per-model uncheck = exclude (sent as `exc`). Sortable columns, Download CSV exports the currently shown rows.
- Prices: `update_prices.bat` (or `python update_prices.py [--dry-run] [--file page.html]`) fetches docs.devin.ai/desktop/models,
  parses the embedded `modelCostData` array (Enterprise SaaS tier, Pro-tier fallback for models only published there), prints a diff
  and writes `pricing_data.json`. `pricing.py` hot-reloads that file on mtime change; no restart or cache bump needed.
- `pricing_manual.json`: hand-entered rates for models Devin doesn't publish (e.g. Claude Haiku 5.5 from Anthropic's pricing page).
  Never overwritten by the updater; published rates win if the model appears later. Entries list DB slugs in `uids` and may set
  `long_prompt` rates for requests whose prompt (input + cache write + cache read) > 100K; the aggregator tracks those requests
  separately per model in the `long` bucket.
- `pricing.resolve()` maps DB model slugs: OVERRIDES -> published `model_uid` (most slugs) -> ordered-token label match ignoring
  "thinking" (order matters so opus-5-5 != Opus 5). Price is computed at serve time (`server.with_price`) from cached token parts.
  Unpriced slugs (summarizer, compactor, MODEL_SWE_1_5) show "—".
- Session details: double-click a table row (or focus it and press Enter/Space) to open the native dialog.
  Details aggregate all rows for that session from `state.rows`, independent of table filters; unpriced models make the cost partial.
  The dialog and model-filter unit tests use Node.js with a mocked DOM.
