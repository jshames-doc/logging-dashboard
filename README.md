# Session Ledger

A local, read-only dashboard for token usage across your Devin IDE sessions.
It reads the Devin `sessions.db` files directly from your machine — both the
CLI's (`%APPDATA%\devin\cli`) and the IDE's (`%APPDATA%\devin\cli-next`) when
present — aggregates token metrics per session and model, estimates cost at
published rates, and serves a single-page dashboard on `127.0.0.1`.
No data leaves your machine.

## Requirements

- Windows (`run.bat` / `stop_server.bat`; macOS/Linux users see below)
- Python 3.8+ on PATH — **no pip installs**, stdlib only
- The Devin CLI / Devin Desktop installed, so a `sessions.db` exists under
  `%APPDATA%\devin\cli` or `%APPDATA%\devin\cli-next`
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

`SESSIONS_DB` is required there because the default paths use `%APPDATA%`.
It accepts several paths joined by `os.pathsep` (`;` on Windows, `:` elsewhere).

## Using the dashboard

- One row per **(session, model)**. Sessions with no metrics show a single empty row.
- **Input** = input tokens + cache-write tokens. **Cached** = cache-read tokens.
- Forked branches duplicate assistant responses; they're deduplicated by `request_id`.
- Filter by search text, model (multi-select), workspace, and date range.
  Toggle "Show hidden" to include hidden sessions. Click column headers to sort.
  The **Source** column shows which database a session came from (`cli` vs
  `cli-next`); hide it via the Columns picker.
- **Double-click a row** (or focus it and press Enter) for full session details
  across all models, regardless of table filters.
- **$ Prices** shows the rate card; **Rescan database** re-reads `sessions.db`
  (only sessions whose `last_activity_at` changed are re-aggregated).
- **Report** opens the report maker: Excel-style model checkboxes (all included
  by default, uncheck to exclude), a response-date range, and grouping by
  model, day, week, or month. Results are sortable and exportable to CSV.

## Configuration

| Env var          | Default                                                    | Purpose                              |
|------------------|------------------------------------------------------------|--------------------------------------|
| `SESSIONS_DB`    | `%APPDATA%\devin\cli\sessions.db` and `...\cli-next\…`     | Database paths (`os.pathsep`-joined) |
| `DASHBOARD_PORT` | `8765`                                                     | Port to listen on                    |

Each configured database that exists is scanned; sessions are tagged with a
**Source** label taken from the database's parent folder. Aggregates are cached
in `.cache/sessions_cache.json`, keyed by `source:session-id` and each session's
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
