# Running the service (macOS)

A background service can do the collecting for you. It watches SEC 8-K filings, fetches completed daily bars, freezes a point-in-time decision for each new filing and sends a calm pre-market brief. A localhost page and a read-only MCP server show the same code-built cards. **It never places orders and it is not investment advice.**

The service needs macOS (launchd, the Keychain and notifications). On Linux, see [Linux and Windows](#linux-and-windows) below.

- [Install and set up](#install-and-set-up)
- [The daily loop](#the-daily-loop)
- [What the brief means](#what-the-brief-means)
- [From WATCH to calls](#from-watch-to-calls)
- [Look at it](#look-at-it)
- [Privacy and keys](#privacy-and-keys)
- [Linux and Windows](#linux-and-windows)
- [Stop it](#stop-it)

## Install and set up

Requires Python 3.11 or newer. macOS's built-in `python3` is 3.9, so use one from python.org or Homebrew.

```sh
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
jevtrader setup
```

`setup` asks, one question at a time (press Return to keep the value shown):

1. **A contact for SEC.** SEC's fair-access policy asks automated tools to declare a contact. Use a dedicated alias (for example `jevtrader sec-alias@your-domain.example`) rather than your personal name or address. The value is kept in `config.json` and goes only to sec.gov in the User-Agent header. There is no default.
2. **Watchlist** symbols, and whether to score only those or every qualifying 8-K (`all`).
3. **Text features**: `rules` (fixed word lists, free, offline), `local` (a model served by Ollama or LM Studio on this Mac; setup runs a health check), `jev` or `openai` (paid; they receive selected filing text). With `local`, setup asks for the engine URL (Ollama `:11434/v1`, LM Studio `:1234/v1`) and then the model; the default model is `gpt-oss:120b`, so type `gpt-oss:20b` (or whatever your engine serves) if that is what you pulled. Paid presets also ask for a monthly spend cap and the model's prices per million input and output tokens; the cap counts output (reasoning included) at the requested maximum when a response reports no count, and without both prices the service keeps paid extraction off.
4. **Alpaca bars**: completed daily bars from Alpaca's free market-data API (needs the keys of a free paper account; nothing trades through Alpaca). Without bars nothing can be scored: filings are collected but their cards say `not scored: no market data`, and setup and `doctor` say so.
5. **Brief time** (New York, weekdays; 08:45 by default) and whether to show it as a macOS notification.
6. **Keys** for the choices above, typed hidden and stored in the macOS Keychain. Blank skips.
7. Whether to **start the service** now (`jevtrader up`).

Nothing is saved until the last answer is in: cancelling (Ctrl-C or end of input) before then stores no key and writes no file. By the final question, keys, config and both ledgers are saved; cancelling it only leaves the service stopped.

Settings live in `~/Library/Application Support/jevtrader/` (`config.json`, owner-only), next to `forward.sqlite` (the service's ledger), `research.sqlite` (for backfills) and `logs/`. Set `JEVTRADER_HOME` to an absolute path to use another folder; `up` passes it on to the service. Config holds no secrets. `model_overrides` in `config.json` declares facts the model registry lacks, for example `{"local:qwen3:32b": {"training_cutoff": "2024-10-01"}}` or `{"openai:MODEL": {"usd_per_million_input_tokens": 1.25, "usd_per_million_output_tokens": 10}}`. See [Evidence labels and the gate](evidence.md#declaring-a-cutoff) for the rules on declared cutoffs.

## The daily loop

| Job | When (New York time) | What it does |
|---|---|---|
| `poll` | every 60 s on weekdays 06:00–22:00, else every 15 min | New 8-K/8-K/A filings from SEC's current feed that list Item 7.01 or 8.01 and not 2.02; `first_seen_at` is actual receipt |
| `bars` | weekdays after 16:30, catching up if missed | Completed sessions (close + 20 min), stamped with actual receipt: the strategy's benchmark, then open forecasts' symbols and benchmarks, the watchlist and recent filers |
| `observe` | after new filings or bars | One frozen forward decision per new filing with the configured provider; a batch starts no new filing after 2 minutes, so polling keeps up |
| `settle` | after bars | Next-open to tenth-close labels once they have matured |
| `brief` | weekdays at your brief time | Notification of the filings first seen since the previous brief (the `brief` command, page and MCP show the last 3 days) |
| `reconcile` | 22:45 | Classifies each 8-K in the day's EDGAR index, recovers missed qualifying filings as forward records stamped with their late receipt (`first_seen_basis: "reconcile_late"`), and records the rest as a `filing_gap` run |

Every run, skip and failure is recorded in the ledger as a `runs` record. If the Mac slept, the service was stopped, or jobs held up polling for more than 5 minutes past its interval, a `coverage_gap` record says so; filings that left SEC's 100-entry feed meanwhile are not reconstructed. `jevtrader poll` and `jevtrader bars` run one job by hand (they refuse while the service or another such job runs); a service starting meanwhile waits up to 10 minutes for them, then exits with an error so launchd tries again. `jevtrader daemon` runs the schedule in the foreground.

## What the brief means

Each card shows the symbol, form and items, when this system first saw the filing, the code-computed action and its reasons, and at most two short quotes copied verbatim from the filing (300 characters per card, never the full text). A sentence that addresses the reader or an AI agent, gives orders or names a tool is never quoted; this filter is best effort. Quotes are labeled as the company's text.

The actions:

- `WATCH` means observation only: **without a fitted calibrator every decision is `WATCH`,** and no call counts toward the evidence gate (see [From WATCH to calls](#from-watch-to-calls)).
- `PASS` means a calibrator scored the filing, but the predicted excess return does not clear costs and the minimum edge, or a filter (price, dollar volume, uncertainty, novelty or materiality) stopped it.
- `LONG`/`SHORT` appear only when the calibrator's predicted excess return clears costs and the minimum edge. `SHORT` also needs shorts enabled in the strategy; they are off by default.

A card without a decision says `not scored: no market data` when its symbol has no bars. A filing's outcome is the stock's move minus the benchmark's over the label window; no position is ever taken.

The evidence line comes from a gate that is fixed in advance and fingerprinted (nothing is registered externally). It counts matured `LONG`/`SHORT` calls whose evidence labels allow them to count (on the service's ledger, normally live forward calls), net of assumed costs, with a 90% Student-t interval over decision dates. Status is `collecting` until 100 such calls have matured, then `supported` (whole interval above zero), `no_edge` (whole interval below +0.10%) or `inconclusive`. Details: [Evidence labels and the gate](evidence.md).

The health line names jobs that failed or need attention, and names `service` when no run has been recorded for 30 minutes: the service polls at least every 15 minutes, so silence means it is not running.

## From WATCH to calls

The service decides with the calibrator named in `config.json` (`"calibrator": null` at first). Once enough forward decisions have matured labels (30 by default), fit one on the service's ledger:

```sh
LEDGER="$HOME/Library/Application Support/jevtrader/forward.sqlite"
jevtrader --db "$LEDGER" status          # extractor_keys; models once fitted
jevtrader --db "$LEDGER" fit --extractor-key KEY --mode forward
```

Set `"calibrator": "MODEL_ID"` in `config.json` and restart the service (`jevtrader down`, then `jevtrader up`). `doctor` checks that the model is in that ledger, fit by the current evaluator and on the service's provider and model; otherwise the service's observe runs fail and health names them. Forward filings it has not yet decided with this calibrator are decided again when the service next runs: live, at that time, never backdated.

## Look at it

```sh
jevtrader brief            # JSON; --notify also shows the notification, --since TIME sets the window start (default: 3 days ago)
jevtrader serve            # http://127.0.0.1:8765/ (read-only); --port if 8765 is taken
jevtrader doctor           # config, ledger chain, health, calibrator, bars, local engine, key names, service
jevtrader verify           # recompute every record hash and the hash chain
jevtrader --db "$HOME/Library/Application Support/jevtrader/forward.sqlite" show forecasts FORECAST_ID
```

`brief`, the page and the MCP server show filings first seen in the last 3 days (Monday still shows Friday's); the notification covers the time since the previous brief. Research commands such as `show` and `status` default to `data/jevtrader.sqlite`, so pass the service's ledger with `--db` as above.

The page has three views: the brief (`/`), the scoreboard (`/scoreboard`) and health (`/health`), plus one page per filing and JSON at `/api/brief.json` and `/api/scoreboard.json`. It binds to 127.0.0.1 only, answers only `127.0.0.1`/`localhost` Host headers, has no forms and opens the ledger read-only.

The JSON routes wrap external-derived fields the same way the MCP server does: filing `quotes`, `items`, `source_url`, document filenames, provider `resolved_model` names and job `error` text arrive as `{"untrusted": true, "source": "sec-filing" | "provider" | "job-error", "value": ...}` (`jevtrader/security/provenance.py`). The HTML views mark the same text with `class="untrusted"` and a `data-source` attribute: quotes as `blockquote`, job errors as `q`, filing items and source links as `span`.

`jevtrader mcp` is a read-only MCP server on stdio; see [MCP server](mcp.md).

For research on older filings, `jevtrader backfill` collects historical 8-Ks into `research.sqlite`, never the forward ledger; see [Backfills](research-workflow.md#backfills) for its assumptions and biases.

## Privacy and keys

Keys stay in the macOS Keychain (service `jevtrader`) or your environment; the environment wins. They never go into config, the ledger, logs, the LaunchAgent plist or command output, and `security` receives them on stdin, not argv.

What leaves the Mac:

- your SEC contact and filing requests, to sec.gov;
- symbols and dates, to Alpaca;
- selected filing text, to Jev (TypeSafe) or OpenAI, only if you chose them;
- if you connect a cloud-hosted MCP client, the short excerpts that `explain_filing` returns.

A `local` engine must be on 127.0.0.1, localhost or ::1, and is reached without proxies. The page and the MCP server only read.

## Linux and Windows

**Linux.** CI runs the full test suite on Ubuntu with Python 3.11–3.14. The research CLI, `demo`, `brief`, `serve`, `mcp` and `verify` work. Differences from macOS:

- There is no Keychain: export keys as environment variables (see `.env.example`; `.env` files are not loaded automatically).
- There is no service installer: `jevtrader up` refuses on anything but macOS, and there is no systemd unit yet. A foreground `jevtrader daemon` has not been tried live on Linux; its unit tests, like the rest of the suite, do run there.
- Notifications are macOS-only.
- The app folder is `$XDG_DATA_HOME/jevtrader`, or `~/.local/share/jevtrader` when that is unset. `JEVTRADER_HOME` overrides it, as on macOS.

**Windows is not supported.** The CLI imports `fcntl`, which Windows lacks, and CI does not run on Windows.

## Stop it

```sh
jevtrader down     # stop the service and remove its LaunchAgent; data stays
```

Everything else is in the app folder above; delete it to remove the ledgers and config. Stored keys can be removed with `security delete-generic-password -s jevtrader -a ALPACA_API_KEY_ID` (and likewise for the other names).

For what the service cannot do, see [Limitations](limitations.md#what-it-cannot-do).
