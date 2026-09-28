# JEVTrader

A small Python research lab for testing whether changes in corporate disclosures help predict subsequent stock returns. It combines JEV semantic features, a ridge regression model, deterministic filters, and an immutable SQLite ledger.

**This is research software, not a trading bot.** It has no broker connection, order execution, or fill simulator. The optional background service ([Run it for you](#run-it-for-you-macos)) watches filings, records decisions and sends a brief; it never places an order. Paper order plans do not place orders or track a portfolio. No profitable strategy or statistically credible alpha has been established.

## Start offline

Requires Python 3.11 or newer. From the repository directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m jevtrader --db /tmp/NEWdemo.sqlite demo
pytest
```

Use a new, empty demo database; the demo refuses to modify a populated ledger. It creates synthetic disclosures and prices, extracts lexical features, fits a model, evaluates events, and prints a paper-plan example.

**The demo deliberately injects a relationship between text and future prices. Its results are a plumbing check, not evidence of alpha.** Its weekday calendar and prices are artificial. Setup, demo, and tests make no paid API calls. Provider integrations are tested with mocked responses, not live service verification.

Only `init`, `demo` and `setup` create a ledger; every other command needs an existing one. Without one, a research command names the service's ledger when setup has created it (use it with `--db`) instead of suggesting an `init` that would make an empty ledger here. Read-only commands (`status`, `show`, `evaluate` and the app views below) open it read-only. Research commands default to `--db data/jevtrader.sqlite`; the app commands in the next section default to the ledger named in the app config.

## Run it for you (macOS)

A background service can do the collecting for you: it watches SEC 8-K filings, fetches completed daily bars, freezes a point-in-time decision for each new filing and sends a calm pre-market brief. A localhost page and a read-only MCP server show the same code-built cards. **It never places orders and it is not investment advice.**

### Install and set up

```sh
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
jevtrader setup
```

`setup` asks, one question at a time (press Return to keep the value shown):

1. **Your name and email for SEC.** SEC's fair-access policy asks automated tools to identify themselves; the value goes only to sec.gov in the User-Agent header.
2. **Watchlist** symbols, and whether to score only those or every qualifying 8-K (`all`).
3. **Text features**: `rules` (fixed word lists, free, offline), `local` (a model served by Ollama or LM Studio on this Mac; setup runs a health check), `jev` or `openai` (paid; they receive selected filing text). Paid presets also ask for a monthly spend cap and the model's prices per million input and output tokens; the cap counts output (reasoning included) at the requested maximum when a response reports no count, and without both prices the service keeps paid extraction off.
4. **Alpaca bars**: completed daily bars from Alpaca's free market-data API (needs the keys of a free paper account). Without bars nothing can be scored: filings are collected but their cards say `not scored: no market data`, and setup and `doctor` say so.
5. **Brief time** (New York, weekdays) and whether to show it as a macOS notification.
6. **Keys** for the choices above, typed hidden and stored in the macOS Keychain. Blank skips.
7. Whether to **start the service** now (`jevtrader up`).

Nothing is saved until the last answer is in: cancelling (Ctrl-C or end of input) before then stores no key and writes no file. By the final question, keys, config and both ledgers are saved; cancelling it only leaves the service stopped.

Settings live in `~/Library/Application Support/jevtrader/` (`config.json`, owner-only), next to `forward.sqlite` (the service's ledger), `research.sqlite` (for backfills) and `logs/`. Set `JEVTRADER_HOME` to an absolute path to use another folder; `up` passes it on to the service. Config holds no secrets. `model_overrides` in `config.json` declares facts the model registry lacks, for example `{"local:qwen3:32b": {"training_cutoff": "2024-10-01"}}` or `{"openai:MODEL": {"usd_per_million_input_tokens": 1.25, "usd_per_million_output_tokens": 10}}`.

### The daily loop

| Job | When (New York time) | What it does |
|---|---|---|
| `poll` | every 60 s on weekdays 06:00–22:00, else every 15 min | New 8-K/8-K/A filings from SEC's current feed that list Item 7.01 or 8.01 and not 2.02; `first_seen_at` is actual receipt |
| `bars` | weekdays after 16:30, catching up if missed | Completed sessions (close + 20 min), stamped with actual receipt: the strategy's benchmark, then open forecasts' symbols and benchmarks, the watchlist and recent filers |
| `observe` | after new filings or bars | One frozen forward decision per new filing with the configured provider; a batch starts no new filing after 2 minutes, so polling keeps up |
| `settle` | after bars | Next-open to tenth-close labels once they have matured |
| `brief` | weekdays at your brief time | Notification of the filings first seen since the previous brief (the `brief` command, page and MCP show the last 3 days) |
| `reconcile` | 22:45 | Compares the day's EDGAR index with what was collected |

Every run, skip and failure is recorded in the ledger as a `runs` record. If the Mac slept, the service was stopped, or jobs held up polling for more than 5 minutes past its interval, a `coverage_gap` record says so; filings that left SEC's 100-entry feed meanwhile are not reconstructed. `jevtrader poll` and `jevtrader bars` run one job by hand (they refuse while the service or another such job runs); a service starting meanwhile waits up to 10 minutes for them, then exits with an error so launchd tries again. `jevtrader daemon` runs the schedule in the foreground.

### What the brief means

Each card shows the symbol, form and items, when this system first saw the filing, the code-computed action and its reasons, and at most two short quotes copied verbatim from the filing (300 characters per card, never the full text). A sentence that addresses the reader or an AI agent, gives orders or names a tool is never quoted. Quotes are labeled as the company's text. `WATCH` means observation only: **without a fitted calibrator every decision is `WATCH`,** and no call counts toward the evidence gate (see [From WATCH to calls](#from-watch-to-calls)). `LONG`/`SHORT` appear only when the calibrator's predicted excess return clears costs and the minimum edge. A card without a decision says `not scored: no market data` when its symbol has no bars. A filing's outcome is the stock's move minus the benchmark's over the label window; no position is ever taken.

The evidence line comes from a pre-registered gate: it counts matured `LONG`/`SHORT` calls net of assumed costs, with a 90% Student-t interval over decision dates. Status is `collecting` until 100 matured calls, then `supported` (whole interval above zero), `no_edge` (whole interval below +0.10%) or `inconclusive`. The health line names jobs that failed or need attention, and names `service` when no run has been recorded for 30 minutes: the service polls at least every 15 minutes, so silence means it is not running.

### From WATCH to calls

The service decides with the calibrator named in `config.json` (`"calibrator": null` at first). Once enough forward decisions have matured labels (30 by default), fit one on the service's ledger:

```sh
LEDGER="$HOME/Library/Application Support/jevtrader/forward.sqlite"
jevtrader --db "$LEDGER" status          # extractor_keys; models once fitted
jevtrader --db "$LEDGER" fit --extractor-key KEY --mode forward
```

Set `"calibrator": "MODEL_ID"` in `config.json` and restart the service (`jevtrader down`, then `jevtrader up`). `doctor` checks that the model is in that ledger, fit by the current evaluator and on the service's provider and model; otherwise the service's observe runs fail and health names them. Forward filings it has not yet decided with this calibrator are decided again when the service next runs: live, at that time, never backdated.

### Evidence labels

Every new forecast records an `eligibility` label from the model registry. Only three labels count as evidence:

| Label | Meaning | Counts |
|---|---|---|
| `forward` | Decided live, when the filing was first seen | yes |
| `no_model_knowledge` | Replay with the `rules` baseline, which learned nothing | yes |
| `post_cutoff` | Replay of a filing dated more than 92 days after the model's published training cutoff | yes |
| `contaminated` | Replay of a filing the model may have seen in training | no |
| `unknown_cutoff` | Replay with a model whose cutoff is undisclosed (JEV) or undeclared | no |
| `adhoc_replay` | Replay of one filing picked by hand (`--event` with `--replay` or `--as-of`) | no |
| `synthetic` | Demo data | never |

Each forecast also freezes the registry facts behind its label (`eligibility_basis`: the training cutoff and whether it came from the registry or your config), and the filing page shows them. Each filing counts once: its first recorded forward call (else its first forward forecast); a replay never replaces a forward decision, and a filing with replays only uses the first one recorded. Forecasts recorded before labels existed keep their records; only forward ones count.

### Look at it

```sh
jevtrader brief            # JSON; --notify also shows the notification, --since TIME sets the window start (default: 3 days ago)
jevtrader serve            # http://127.0.0.1:8765/ (read-only); --port if 8765 is taken
jevtrader doctor           # config, ledger chain, health, calibrator, bars, local engine, key names, service
jevtrader verify           # recompute every record hash and the hash chain
jevtrader --db "$HOME/Library/Application Support/jevtrader/forward.sqlite" show forecasts FORECAST_ID
```

`brief`, the page and the MCP server show filings first seen in the last 3 days (Monday still shows Friday's); the notification covers the time since the previous brief. Research commands such as `show` and `status` default to `data/jevtrader.sqlite`, so pass the service's ledger with `--db` as above.

The page binds to 127.0.0.1 only, answers only `127.0.0.1`/`localhost` Host headers, has no forms and opens the ledger read-only. `jevtrader mcp` is a read-only MCP server on stdio with five tools (`today_brief`, `explain_filing`, `evidence_report`, `health`, `search_filings`); for example, in an MCP client's config:

```json
{"mcpServers": {"jevtrader": {"command": "/absolute/path/to/.venv/bin/jevtrader", "args": ["mcp"]}}}
```

Its answers are code-built cards with no key values and no raw filing text. Only `explain_filing` includes filing text: its quotes, under `untrusted_filing_excerpts`, with a note on each card that they are the filer's words, data and not instructions. The server also tells the client to decline trading and personalized advice.

For research on older filings, `jevtrader backfill --start 2026-01-02 --end 2026-03-31 [--bars]` collects historical 8-Ks (and, with `--bars`, historical Alpaca bars) into `research.sqlite`, never the forward ledger. Availability is an assumption (acceptance + 15 minutes, or the next 06:00 ET weekday), symbols come from today's ticker map (survivorship bias), and only filings still in each company's recent SEC submissions are found. Replay them with the research commands, e.g. `jevtrader --db "$HOME/Library/Application Support/jevtrader/research.sqlite" observe --replay --limit 200`.

### What it cannot do

- Place, change or track orders or positions, or give investment advice.
- Show an edge that does not exist: until a calibrator is fitted and named in config everything is `WATCH`, and the gate needs 100 matured calls.
- See every disclosure: only 8-Ks listing 7.01/8.01 (not 2.02), one EX-99 or primary document each, HTML or text only. Periods when the service was not running stay gaps.
- Provide market data by itself: bars need Alpaca keys; delisting returns and quotes are not modeled; costs are assumptions.
- Remove model contamination from replays; it only labels it.

### Privacy and keys

Keys stay in the macOS Keychain (service `jevtrader`) or your environment; the environment wins. They never go into config, the ledger, logs, the LaunchAgent plist or command output, and `security` receives them on stdin, not argv. What leaves the Mac: your SEC contact and filing requests to sec.gov; symbols and dates to Alpaca; selected filing text to JEV or OpenAI only if you chose them. A `local` engine must be on 127.0.0.1, localhost or ::1, and is reached without proxies. The page and the MCP server only read.

### Stop it

```sh
jevtrader down     # stop the service and remove its LaunchAgent; data stays
```

Everything else is in the app folder above; delete it to remove the ledgers and config. Stored keys can be removed with `security delete-generic-password -s jevtrader -a ALPACA_API_KEY_ID` (and likewise for the other names).

## What makes decisions

```text
SEC disclosure + earlier observed disclosure
                   ↓
JEV / OpenAI / fixed lexical rules → four semantic features
                   +
completed stock and benchmark bars → four numerical features
                   ↓
ridge model → predicted benchmark-relative return
                   ↓
evidence, liquidity, cost and risk checks → WATCH / PASS / LONG / SHORT
                   ↓
optional whole-share paper order plan
```

JEV extracts direction, materiality, novelty, and uncertainty. Code calculates reaction, momentum, volatility, and an assumed spread. **JEV's probabilities are not calibrated profit probabilities.** The fitted ridge model estimates subsequent excess return; deterministic code applies the policy. Without a fitted calibrator, forecasts remain `WATCH`.

The `rules` provider is a fixed lexical baseline that ignores strategy questions. OpenAI can extract the same features or propose changes to the three semantic questions. It does not change risk limits, execution assumptions, or the evaluator.

Default research settings: SPY benchmark, 21 visible history sessions, and entry at the next available session open through the tenth session close. Default paper settings are:

| Setting | Default |
|---|---:|
| Example account equity | $3,000 |
| Planned risk per trade | 0.25% ($7.50 at $3,000) |
| Maximum single position | 15% of equity |
| Maximum gross exposure | 100% of equity |
| Maximum positions | 4 |
| Pause new plans at drawdown | 8% from supplied peak equity |
| Short plans | Disabled |

These are conservative prototype choices, not optimized settings. Stops, pauses, and planned risk do **not** guarantee a maximum loss: gaps, halts, and short borrow recalls can exceed them. Positions, equity, cash, and peak equity are user-supplied; nothing is synchronized with a broker. A short plan additionally requires an enabled strategy and explicit `--shortable` simulation assumption.

**Paper planning is stateless with respect to your account.** Supply current positions, equity, cash, and peak equity on each invocation. Repeated plans do not reserve cash or create holdings. The 8% pause only reflects drawdown when the correct `--peak-equity` is supplied; omitting it defaults the peak to current equity. This is not a portfolio simulation.

## Ledger and data

```sh
python -m jevtrader --db data/research.sqlite init
python -m jevtrader --db data/research.sqlite status
```

Records are immutable. Repeating an identical insert is harmless; conflicting content requires a new version ID. Every record is also committed to a SHA-256 hash chain; `python -m jevtrader --db data/research.sqlite verify` recomputes all record hashes and the chain (older ledgers are upgraded to the chain once, on their first read-write open). Repeated SEC collection preserves the original stored `first_seen_at`. Keep synthetic demonstrations separate from real research. Historical LLM extraction can contain knowledge learned after the event; only a newly frozen forward record establishes what this system actually observed at that time.

### Disclosures

`import-disclosures` accepts JSONL: one object per line. Required fields are `id`, `symbol`, `text`, `source_url`, `mode`, `published_at`, and `first_seen_at`. Imported modes are `historical` or `synthetic`; only collection can create forward disclosures. Example historical record:

```json
{"id":"example-20260106","symbol":"ABC","published_at":"2026-01-06T21:05:00Z","first_seen_at":"2026-01-06T21:10:00Z","source_url":"https://example.com/disclosures/example-20260106","mode":"historical","text":"The company reports an operating update with specific changes in demand."}
```

```sh
python -m jevtrader --db data/research.sqlite import-disclosures disclosures.jsonl
```

For public SEC data, provide your real contact-bearing User-Agent; no SEC API key is needed:

```sh
export SEC_USER_AGENT='Your Name your-real-contact@your-domain.com'
python -m jevtrader --db data/forward.sqlite collect --cik 0000320193 --symbol AAPL --limit 5
```

Collection accepts 8-K/8-K/A filings listing 7.01 or 8.01 while excluding Item 2.02. It prefers one EX-99 HTML/text exhibit and marks primary-document fallbacks. Filename/link heuristics cannot guarantee an exhibit's type or that content is non-earnings. Unknown item metadata is excluded; PDFs and complete attachment coverage are unsupported. Requests are bounded and rate-limited; external links and redirects are not followed.

`published_at` is SEC acceptance time, **not guaranteed public availability**. The collector records actual receipt as `first_seen_at`. Polling an old filing now does not create a historical forward observation or reconstruct what a historical observer had seen.

### Raw session bars

Supply raw, unadjusted OHLCV for each stock **and SPY**, with actual session timestamps and explicit time zones. CSV example:

```csv
symbol,session,open_at,close_at,open,high,low,close,volume,split_ratio,cash_dividend,available_at
ABC,2026-01-06,2026-01-06T14:30:00Z,2026-01-06T21:00:00Z,100,103,99,102,1000000,1,0,2026-01-06T21:01:00Z
SPY,2026-01-06,2026-01-06T14:30:00Z,2026-01-06T21:00:00Z,600,603,598,602,50000000,1,0,2026-01-06T21:01:00Z
```

These two rows illustrate format only. Observations need at least 21 aligned prior sessions; labels need the full future horizon. Use the correct UTC offset for daylight saving and actual early-close times.

```sh
python -m jevtrader --db data/research.sqlite import-bars bars.csv --mode historical
python -m jevtrader --db data/forward.sqlite import-bars completed-bars.csv --mode forward
```

Historical `available_at` defaults to session close if omitted: that is an assumption, not verified receipt. Forward imports ignore supplied availability and stamp actual import time. Importing past bars today cannot make them visible to yesterday's decision. Use a separate database when changing provenance; immutable bar IDs identify symbol/session.

`split_ratio` means new shares per prior share at that session's open; default 1. `cash_dividend` is cash per **post-split** share on the ex-date; default 0. Labels credit dividends only when the position was held before that open and carry split-adjusted share counts. Do not supply adjusted prices and then apply corporate actions again. Prior cash dividends are not reinvested in holding-period labels.

## Observe, label, and evaluate

Begin with the free offline rules baseline:

```sh
python -m jevtrader --db data/research.sqlite observe --replay --provider rules --model rules-v1 --limit 200
python -m jevtrader --db data/research.sqlite settle
python -m jevtrader --db data/research.sqlite status
```

Replay uses each disclosure's first-seen timestamp and remains historical. `--as-of TIMESTAMP --event ID` is another explicit replay. Without either, `observe` decides only collected forward disclosures at the current time; pending historical and synthetic records are skipped (counted under `skipped.requires_replay`) rather than paired with today's market, and naming one with `--event` alone is reported under `errors` (exit status 1). Observation excludes future documents and unavailable bars. Features and provider responses are cached; historical repeats at the same decision time are idempotent. Forward decisions include the time required to finish extraction.

Pass `--provider local` (default model `gpt-oss:120b`) to use a local engine at the config's `local_base_url`. Nothing is billed: an engine that is down stops the run without recording an attempt, while a model that answers invalidly twice is recorded under `attempts` like a failed paid request and later queued runs skip it.

Extraction sends at most 40,000 characters: the start of the current document, with up to 10,000 characters reserved for the start of the previous one; any budget the current document leaves unused also goes to the previous one. A truncated extraction records its `text_excerpt` sizes; the ledger and the extraction cache key keep the full collected text. Queued events whose symbol or benchmark has fewer stored bars than `min_history_sessions` are skipped up front (counted under `skipped.no_market_data`). Other events rejected by local checks (too little history at decision time, stale data, a calibrator that cannot score them) are reported under `errors` without blocking later events or counting against `--limit`; a run stops scanning after `--max-scan` (default 200) such rejections and reports `skipped.scan_truncated`. With a paid provider, a failed or interrupted request stops the run because it may have been billed. The failure is recorded under `attempts` (`status` lists them under `failed_attempts`; inspect one with `show attempts ID`), and later queued runs skip that event for the same provider, model and questions (counted under `skipped.failed_before`) unless you pass `--retry-failed` or name it with `--event`; once such a retry succeeds, the event is queued normally again.

Forward and replay observations remain separate. If an extractor has records in multiple modes, `fit` and `evaluate` require an explicit `--mode forward`, `--mode historical`, or `--mode synthetic`; a backdated replay cannot replace a forward observation in that cohort.

Use the actual extractor key printed by `status`; model schemas and resolved provider models must match:

```sh
EXTRACTOR_KEY='paste-extractor-key-from-status'
python -m jevtrader --db data/research.sqlite fit --extractor-key "$EXTRACTOR_KEY" --cutoff 2026-09-01T00:00:00Z
python -m jevtrader --db data/research.sqlite evaluate --extractor-key "$EXTRACTOR_KEY" --before 2026-09-01T00:00:00Z
```

Choose cutoffs appropriate to your dataset. Only labels received strictly before the training cutoff enter training. Thirty samples is a **computational minimum, not evidence of an edge**. Walk-forward evaluation freezes each test-block model and excludes overlapping, not-yet-mature training labels. It compares semantic-plus-numerical features against numerical-only, semantic-direction, and all-long baselines. The report key `semantic` applies to JEV, OpenAI, or rules features, depending on the chosen extractor.

To use JEV or OpenAI, explicitly choose the provider/model and export `TYPESAFE_API_KEY` or `OPENAI_API_KEY` (or store it in the Keychain with `jevtrader setup`; commands that can use keys read it from there). `.env.example` documents variables; `.env` files are **not automatically loaded**. A key is needed only for extractions that are not already cached; without one, `observe` stops at the first such event with exit status 2, still printing the forecasts it already recorded, with that event under `errors` and `skipped.missing_credentials` set. These commands can incur provider charges and send selected disclosure text to that provider:

```sh
python -m jevtrader --db data/forward.sqlite observe --provider jev --model jev-1.13.0 --limit 5
# Alternative: --provider openai --model YOUR_AVAILABLE_OPENAI_MODEL
```

Once a compatible model exists, pass `--calibrator MODEL_ID` on new observations. Its cutoff must precede the decision; training events cannot be scored as new events, and synthetic-trained models cannot score real disclosures. Models fit before the current evaluator (`ridge-event-v2`) are refused, as are paper plans for forecasts they scored; re-fit them with `fit` (`status` shows each model's `version`). Without `--event`, the queue leaves out events the model can never score and counts them under `skipped.calibrator_ineligible`: its training events, real events for a synthetic-trained model and, with `--replay` (which decides at first sight), events first seen at or before its cutoff. A live run decides now, so it also scores forward events collected before the cutoff. Inspect returned forecast IDs and plan a simulation only:

```sh
python -m jevtrader --db data/forward.sqlite paper-plan --forecast FORECAST_ID --equity 3000 --cash 3000 --peak-equity 3000
python -m jevtrader --db data/forward.sqlite show forecasts FORECAST_ID
```

## Bounded question research

This adapts autoresearch's experiment pattern; it does not run Karpathy's GPU-training repository. First build enough matured baseline observations. With the default training floor, the development universe needs at least 40 events, and purging must leave at least 10 evaluated events.

```sh
python -m jevtrader --db data/research.sqlite autoresearch --provider jev --model jev-1.13.0 --proposal-model YOUR_AVAILABLE_OPENAI_MODEL --development-until 2026-09-01T00:00:00Z --rounds 3
```

The first round evaluates the baseline; later rounds ask OpenAI to change only question text/name. The ledger locks the development cutoff, baseline, provider, model, and event universe. Before locking, it rejects a universe that mixes real and synthetic events or that purging would leave with fewer than 10 evaluated events. Missing API keys fail before the research settings are locked or any trial or proposal budget is reserved (with `--rounds` above 1, both the OpenAI key and the extraction provider's key are checked first); a completed trial is returned again without keys. A completed trial is reused only if the current evaluator (`ridge-event-v2`) scored it; running one scored by an earlier evaluator uses a new trial slot. A paid proposal that fails local strategy validation is recorded as `proposal_rejected` with its response and still counts as a proposal attempt; `autoresearch` lists it under `rejected_proposals`, and that round produces no trial. The limit is **five candidate trials per ledger, at most 200 events each**, plus at most **four proposal API attempts**, including failures and manual `propose` calls. Failed started trials are retained and are not automatically retried, even under a later evaluator. This bounds calls, not a dollar amount. The resolved provider model is pinned; changes during comparison are rejected. `experiment` tests a candidate JSON; `propose` writes a candidate from a completed development trial. Run either with `--help` for arguments.

The development winner is never automatically promoted. All adaptive results remain development results; freeze a chosen version and collect new forward observations before judging it. Repeatedly changing questions against the same holdout contaminates that holdout.

## What the numbers do not establish

- Evaluation reports **event-level benchmark-relative returns**, not portfolio P&L, an executable hedged portfolio, an equity curve, or Sharpe. Events overlap and are not independent. The research comparison omits paper sizing and several policy gates.
- Spread, slippage, commissions, and borrow costs are configurable assumptions, not observed quotes or actual fills. Event evaluation uses fixed round-trip basis points and excludes dollar commissions and short borrow costs; paper plans include configured commissions and borrow estimates. Paper plans use the stored reference price, not a live executable quote. Short results are conditional on borrow availability and costs.
- No exchange calendar, delisting-return feed, point-in-time universe, or market-data subscription is bundled. Stock/benchmark gaps are checked against each other, but sessions missing from **both** can remain undetected. Delistings and missing future data remain unresolved; inspect unresolved counts instead of silently treating those events as losses, wins, or exclusions.
- Historical text may be contaminated by model pretraining. Chronological splits cannot remove that contamination, survivorship bias, bad timestamps, revised source data, or repeated-search overfitting.
- More trials or more complex models do not establish statistical validity. The next useful evidence is a frozen strategy observed forward with realistic data, costs, and a comparison against simple alternatives.
