# Session Ledger

A local, read-only dashboard for token usage across your Devin IDE sessions.
It reads the Devin CLI's `sessions.db` directly from your machine, aggregates
token metrics per session and model, estimates cost at published rates, and
serves a single-page dashboard on `127.0.0.1`. No data leaves your machine.

## Requirements

- Windows (`run.bat` / `stop_server.bat`; macOS/Linux users see below)
- Python 3.8+ on PATH — **no pip installs**, stdlib only
- The Devin CLI / Devin Desktop installed, so `%APPDATA%\devin\cli\sessions.db` exists
- Node.js — only needed to run the unit tests, not the dashboard

## Run

```bat
run.bat
```

Starts `python server.py` on http://127.0.0.1:8765 and opens your browser.
Use `run.bat --no-browser` to skip opening the browser.

Stop it with `stop_server.bat` (kills the `server.py` process on the port) or
Ctrl+C in the server window.

### macOS / Linux

The batch files are Windows-only, but the server itself is portable:

```sh
SESSIONS_DB=/path/to/sessions.db python server.py
```

`SESSIONS_DB` is required there because the default path uses `%APPDATA%`.

## Using the dashboard

- One row per **(session, model)**. Sessions with no metrics show a single empty row.
- **Input** = input tokens + cache-write tokens. **Cached** = cache-read tokens.
- Forked branches duplicate assistant responses; they're deduplicated by `request_id`.
- Filter by search text, model (multi-select), workspace, and date range.
  Toggle "Show hidden" to include hidden sessions. Click column headers to sort.
- **Double-click a row** (or focus it and press Enter) for full session details
  across all models, regardless of table filters.
- **$ Prices** shows the rate card; **Rescan database** re-reads `sessions.db`
  (only sessions whose `last_activity_at` changed are re-aggregated).

## Configuration

| Env var          | Default                                        | Purpose              |
|------------------|------------------------------------------------|----------------------|
| `SESSIONS_DB`    | `%APPDATA%\devin\cli\sessions.db`              | Path to the database |
| `DASHBOARD_PORT` | `8765`                                         | Port to listen on    |

Aggregates are cached in `.cache/sessions_cache.json`, keyed by each session's
`last_activity_at`, so rescans are incremental.

## Pricing

Estimated costs use the published rates from
[docs.devin.ai/desktop/models](https://docs.devin.ai/desktop/models)
(Enterprise SaaS tier, Pro-tier fallback), stored in `pricing_data.json`.

- `update_prices.bat` fetches the latest rates, prints a diff, and rewrites
  `pricing_data.json`. The file is hot-reloaded on change — no restart needed.
- `pricing_manual.json` holds hand-entered rates for models Devin doesn't
  publish. It's never overwritten by the updater, and it can define
  `long_prompt` rates for requests whose prompt exceeds 100K tokens.
- Models with no matching rate (e.g. summarizer/compactor models) show `—`
  instead of a price.

## Tests

```bat
python -m unittest test_server
```

Uses a mocked DOM via Node.js for the front-end logic tests; no browser needed.

## Project layout

```
server.py            HTTP server + sqlite aggregation + cache
pricing.py           Rate lookup, model-slug resolution, hot-reload
pricing_data.json    Published rates (written by update_prices.py)
pricing_manual.json  Hand-entered rates (never auto-overwritten)
update_prices.py     Fetches + parses the docs.devin.ai rate card
static/              Single-page dashboard (index.html, app.js, style.css)
run.bat / stop_server.bat / update_prices.bat   Windows helpers
test_server.py       Unit tests
```

## Privacy

The server binds `127.0.0.1` only, opens `sessions.db` read-only, and makes no
outbound calls except `update_prices.py` fetching the public pricing page.
Your session data stays in `%APPDATA%` and is never copied into this repo.
