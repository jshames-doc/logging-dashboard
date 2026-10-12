# Session Ledger (local Devin IDE token dashboard)

- Run: `run.bat` (starts `python server.py` on http://127.0.0.1:8765 and opens the browser). `--no-browser` to skip opening.
- Tests: `python -m unittest test_server`
- Data source: `%APPDATA%\devin\cli\sessions.db` (opened read-only; override with `SESSIONS_DB`). Port override: `DASHBOARD_PORT`.
- Tokens come from `message_nodes.chat_message -> $.metadata.metrics` on assistant messages, deduped by `request_id`
  (forked branches duplicate nodes). Input = input_tokens + cache_creation_tokens; Cached = cache_read_tokens.
- `message_nodes.created_at` is rewritten on save; use `$.metadata.created_at` for real message times.
- Aggregates cached in `.cache/sessions_cache.json`, keyed by session `last_activity_at`; bump `CACHE_VERSION` when the aggregate shape changes.
