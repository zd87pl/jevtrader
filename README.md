# JEVTrader

A small Python research lab for testing whether changes in corporate disclosures help predict subsequent stock returns. It combines JEV semantic features, a ridge regression model, deterministic filters, and an immutable SQLite ledger.

**This is research software, not a trading bot.** It has no broker connection, order execution, fill simulator, dashboard, or scheduler. Paper order plans do not place orders or track a portfolio. No profitable strategy or statistically credible alpha has been established.

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

Records are immutable. Repeating an identical insert is harmless; conflicting content requires a new version ID. Repeated SEC collection preserves the original stored `first_seen_at`. Keep synthetic demonstrations separate from real research. Historical LLM extraction can contain knowledge learned after the event; only a newly frozen forward record establishes what this system actually observed at that time.

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

Replay uses each disclosure's first-seen timestamp and remains historical. `--as-of TIMESTAMP --event ID` is another explicit replay. Without either, `observe` decides only collected forward disclosures at the current time; historical and synthetic records are skipped (counted under `skipped.requires_replay`) rather than paired with today's market. Observation excludes future documents and unavailable bars. Features and provider responses are cached; historical repeats at the same decision time are idempotent. Forward decisions include the time required to finish extraction.

Extraction sends at most 40,000 characters: the current document first, keeping up to 10,000 characters of the previous one for comparison. A truncated extraction records its `text_excerpt` sizes; the ledger keeps the full collected text. Events rejected by local checks (missing bars, stale data, an ineligible calibrator) are reported under `errors` without blocking later events or counting against `--limit`; with a paid provider, a failed request still stops the run.

Forward and replay observations remain separate. If an extractor has records in multiple modes, `fit` and `evaluate` require an explicit `--mode forward`, `--mode historical`, or `--mode synthetic`; a backdated replay cannot replace a forward observation in that cohort.

Use the actual extractor key printed by `status`; model schemas and resolved provider models must match:

```sh
EXTRACTOR_KEY='paste-extractor-key-from-status'
python -m jevtrader --db data/research.sqlite fit --extractor-key "$EXTRACTOR_KEY" --cutoff 2026-09-01T00:00:00Z
python -m jevtrader --db data/research.sqlite evaluate --extractor-key "$EXTRACTOR_KEY" --before 2026-09-01T00:00:00Z
```

Choose cutoffs appropriate to your dataset. Only labels received strictly before the training cutoff enter training. Thirty samples is a **computational minimum, not evidence of an edge**. Walk-forward evaluation freezes each test-block model and excludes overlapping, not-yet-mature training labels. It compares semantic-plus-numerical features against numerical-only, semantic-direction, and all-long baselines. The report key `semantic` applies to JEV, OpenAI, or rules features, depending on the chosen extractor.

To use JEV or OpenAI, explicitly choose the provider/model and export `TYPESAFE_API_KEY` or `OPENAI_API_KEY`. `.env.example` documents variables; `.env` files are **not automatically loaded**. These commands can incur provider charges and send selected disclosure text to that provider:

```sh
python -m jevtrader --db data/forward.sqlite observe --provider jev --model jev-1.13.0 --limit 5
# Alternative: --provider openai --model YOUR_AVAILABLE_OPENAI_MODEL
```

Once a compatible model exists, pass `--calibrator MODEL_ID` on new observations. Its cutoff must precede the decision; training events cannot be scored as new events, and synthetic-trained models cannot score real disclosures. Without `--event`, only disclosures first seen after the model's cutoff are queued. Inspect returned forecast IDs and plan a simulation only:

```sh
python -m jevtrader --db data/forward.sqlite paper-plan --forecast FORECAST_ID --equity 3000 --cash 3000 --peak-equity 3000
python -m jevtrader --db data/forward.sqlite show forecasts FORECAST_ID
```

## Bounded question research

This adapts autoresearch's experiment pattern; it does not run Karpathy's GPU-training repository. First build enough matured baseline observations. With the default training floor, the development universe needs at least 40 events, and purging must leave at least 10 evaluated events.

```sh
python -m jevtrader --db data/research.sqlite autoresearch --provider jev --model jev-1.13.0 --proposal-model YOUR_AVAILABLE_OPENAI_MODEL --development-until 2026-09-01T00:00:00Z --rounds 3
```

The first round evaluates the baseline; later rounds ask OpenAI to change only question text/name. The ledger locks the development cutoff, baseline, provider, model, and event universe. Before locking, it rejects a universe that mixes real and synthetic events or that purging would leave with fewer than 10 evaluated events. Missing API keys fail before any trial or proposal budget is reserved. The limit is **five candidate trials per ledger, at most 200 events each**, plus at most **four proposal API attempts**, including failures and manual `propose` calls. Failed started trials are retained and are not automatically retried. This bounds calls, not a dollar amount. The resolved provider model is pinned; changes during comparison are rejected. `experiment` tests a candidate JSON; `propose` writes a candidate from a completed development trial. Run either with `--help` for arguments.

The development winner is never automatically promoted. All adaptive results remain development results; freeze a chosen version and collect new forward observations before judging it. Repeatedly changing questions against the same holdout contaminates that holdout.

## What the numbers do not establish

- Evaluation reports **event-level benchmark-relative returns**, not portfolio P&L, an executable hedged portfolio, an equity curve, or Sharpe. Events overlap and are not independent. The research comparison omits paper sizing and several policy gates.
- Spread, slippage, commissions, and borrow costs are configurable assumptions, not observed quotes or actual fills. Event evaluation uses fixed round-trip basis points and excludes dollar commissions and short borrow costs; paper plans include configured commissions and borrow estimates. Paper plans use the stored reference price, not a live executable quote. Short results are conditional on borrow availability and costs.
- No exchange calendar, delisting-return feed, point-in-time universe, or market-data subscription is bundled. Stock/benchmark gaps are checked against each other, but sessions missing from **both** can remain undetected. Delistings and missing future data remain unresolved; inspect unresolved counts instead of silently treating those events as losses, wins, or exclusions.
- Historical text may be contaminated by model pretraining. Chronological splits cannot remove that contamination, survivorship bias, bad timestamps, revised source data, or repeated-search overfitting.
- More trials or more complex models do not establish statistical validity. The next useful evidence is a frozen strategy observed forward with realistic data, costs, and a comparison against simple alternatives.
