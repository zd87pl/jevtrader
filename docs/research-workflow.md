# Research workflow

The research commands test whether changes in corporate disclosures help predict subsequent stock returns. They combine text features (from Jev, OpenAI, a local model or fixed lexical rules), a ridge regression model, deterministic filters, and an immutable SQLite ledger. **No profitable strategy or statistically credible edge has been established.**

- [Start offline](#start-offline)
- [What makes decisions](#what-makes-decisions)
- [Observe, label and evaluate](#observe-label-and-evaluate)
- [Fit, evaluate and score new filings](#fit-evaluate-and-score-new-filings)
- [Paper plans](#paper-plans)
- [Bounded question research](#bounded-question-research)
- [Backfills](#backfills)

## Start offline

Requires Python 3.11 or newer. From the repository directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m jevtrader --db /tmp/NEWdemo.sqlite demo
pytest
```

Use a new, empty demo database; the demo refuses to modify a populated ledger (`Demo requires a new, empty ledger; existing records were preserved`, exit status 2). It creates synthetic disclosures and prices, extracts lexical features, fits a model, evaluates events, and prints a paper-plan example.

**The demo deliberately injects a relationship between text and future prices. Its results are a plumbing check, not evidence of an edge.** Its output says so (`"warning": "Artificial signal was injected. Results do not establish alpha."`). Its weekday calendar and prices are artificial. Setup, demo, and tests make no paid API calls. Provider integrations are tested with mocked responses, not live service verification.

Only `init`, `demo` and `setup` create a ledger; every other command needs an existing one. Without one, a research command names the service's ledger when setup has created it (use it with `--db`) instead of suggesting an `init` that would make an empty ledger here. Read-only commands (`status`, `show`, `evaluate` and the app views) open it read-only. Research commands default to `--db data/jevtrader.sqlite`; the app commands (`brief`, `serve`, `mcp`, `doctor`, `verify`) default to the ledger named in the app config. Global options such as `--db` and `--strategy` go before the command.

## What makes decisions

```text
SEC disclosure + earlier observed disclosure
                   ↓
Jev / OpenAI / local model / fixed lexical rules → four semantic features
                   +
completed stock and benchmark bars → four numerical features
                   ↓
ridge model → predicted benchmark-relative return
                   ↓
evidence, liquidity, cost and risk checks → WATCH / PASS / LONG / SHORT
                   ↓
optional whole-share paper order plan
```

The text provider extracts direction, materiality, novelty, and uncertainty. Code calculates reaction, momentum, volatility, and an assumed spread. **Jev's probabilities are not calibrated profit probabilities.** The fitted ridge model (`alpha` 10, evaluator version `ridge-event-v2`) estimates subsequent excess return; deterministic code applies the policy. Without a fitted calibrator, forecasts remain `WATCH`.

The actions: `WATCH` means no calibrator (observation only). `PASS` means a calibrator scored the event, but a filter stopped it (price below $5, average daily dollar volume over the last 20 sessions below $5M, uncertainty above 0.6, or novelty or materiality below 0.5) or the predicted excess return does not clear costs plus the minimum edge. `LONG` needs a predicted excess return above the round-trip cost (20 bps by default: a 10 bps spread plus 5 bps of slippage per side) plus the 20 bps minimum edge. `SHORT` additionally needs `allow_short` in the strategy (off by default) and clears a higher bar, because shorts add 300 bps a year of assumed borrow cost over the holding period.

The `rules` provider is a fixed lexical baseline (9 positive and 10 negative phrases) that ignores strategy questions. OpenAI can extract the same features or propose changes to the three semantic questions. It does not change risk limits, execution assumptions, or the evaluator.

Default research settings (`jevtrader/default_strategy.json`): SPY benchmark, 21 visible history sessions, and entry at the next available session open through the tenth session close. Pass `--strategy FILE` to use another strategy file.

## Observe, label and evaluate

Begin with the free offline rules baseline:

```sh
python -m jevtrader --db data/research.sqlite observe --replay --provider rules --model rules-v1 --limit 200
python -m jevtrader --db data/research.sqlite settle
python -m jevtrader --db data/research.sqlite status
```

Replay uses each disclosure's first-seen timestamp and remains historical. `--as-of TIMESTAMP --event ID` is another explicit replay. Without either, `observe` decides only collected forward disclosures at the current time; pending historical and synthetic records are skipped (counted under `skipped.requires_replay`) rather than paired with today's market, and naming one with `--event` alone is reported under `errors` (exit status 1). Observation excludes future documents and unavailable bars. Features and provider responses are cached; historical repeats at the same decision time are idempotent. Forward decisions include the time required to finish extraction.

Every forecast records an evidence label; see [Evidence labels and the gate](evidence.md).

### Local models

Pass `--provider local` (default model `gpt-oss:120b`) to use a local engine at the config's `local_base_url` (Ollama `http://127.0.0.1:11434/v1` by default; LM Studio serves `:1234/v1`). The engine must be on 127.0.0.1, localhost or ::1; it is reached without system proxies, redirects are refused, and it speaks the OpenAI-compatible `/chat/completions` API. Nothing is billed: an engine that is down stops the run without recording an attempt, while a model that answers invalidly twice (a malformed answer is retried once) is recorded under `attempts` like a failed paid request and later queued runs skip it. `experiment` and `autoresearch` do not offer the local provider.

### Extraction limits and skips

Extraction sends at most 40,000 characters: the start of the current document, with up to 10,000 characters reserved for the start of the previous one; any budget the current document leaves unused also goes to the previous one. A truncated extraction records its `text_excerpt` sizes; the ledger and the extraction cache key keep the full collected text.

Queued events whose symbol or benchmark has fewer stored bars than `min_history_sessions` are skipped up front (counted under `skipped.no_market_data`). Other events rejected by local checks (too little history at decision time, stale data, a calibrator that cannot score them) are reported under `errors` without blocking later events or counting against `--limit`; a run stops scanning after `--max-scan` (default 200) such rejections and reports `skipped.scan_truncated`.

With a paid provider, a failed or interrupted request stops the run because it may have been billed. The failure is recorded under `attempts` (`status` lists them under `failed_attempts`; inspect one with `show attempts ID`), and later queued runs skip that event for the same provider, model and questions (counted under `skipped.failed_before`) unless you pass `--retry-failed` or name it with `--event`; once such a retry succeeds, the event is queued normally again.

### Paid providers and keys

To use Jev or OpenAI, explicitly choose the provider/model and export `TYPESAFE_API_KEY` or `OPENAI_API_KEY` (or store it in the Keychain with `jevtrader setup`; commands that can use keys read it from there). `.env.example` documents variables; `.env` files are **not automatically loaded**. A key is needed only for extractions that are not already cached; without one, `observe` stops at the first such event with exit status 2, still printing the forecasts it already recorded, with that event under `errors` and `skipped.missing_credentials` set. These commands can incur provider charges and send selected disclosure text to that provider:

```sh
python -m jevtrader --db data/forward.sqlite observe --provider jev --model jev-1.13.0 --limit 5
# Alternative: --provider openai --model YOUR_AVAILABLE_OPENAI_MODEL
```

Jev receives the text at `https://api.typesafe.ai/v1/systemone` as typed questions. OpenAI receives it through the Responses API with a strict JSON schema and `store: false`; OpenAI has no default model, so choose one with `--model` or `OPENAI_MODEL`.

## Fit, evaluate and score new filings

Forward and replay observations remain separate. If an extractor has records in multiple modes, `fit` and `evaluate` require an explicit `--mode forward`, `--mode historical`, or `--mode synthetic`; a backdated replay cannot replace a forward observation in that cohort.

Use the actual extractor key printed by `status`; model schemas and resolved provider models must match:

```sh
EXTRACTOR_KEY='paste-extractor-key-from-status'
python -m jevtrader --db data/research.sqlite fit --extractor-key "$EXTRACTOR_KEY" --cutoff 2026-09-01T00:00:00Z
python -m jevtrader --db data/research.sqlite evaluate --extractor-key "$EXTRACTOR_KEY" --before 2026-09-01T00:00:00Z
```

Choose cutoffs appropriate to your dataset. Only labels received strictly before the training cutoff enter training. Thirty samples is a **computational minimum, not evidence of an edge**. Walk-forward evaluation freezes each test-block model and excludes overlapping, not-yet-mature training labels. It compares semantic-plus-numerical features against numerical-only, semantic-direction, and all-long baselines. The report key `semantic` applies to Jev, OpenAI, local or rules features, depending on the chosen extractor.

Once a compatible model exists, pass `--calibrator MODEL_ID` on new observations. Its cutoff must precede the decision; training events cannot be scored as new events, and synthetic-trained models cannot score real disclosures. Models fit before the current evaluator (`ridge-event-v2`) are refused, as are paper plans for forecasts they scored; re-fit them with `fit` (`status` shows each model's `version`). Without `--event`, the queue leaves out events the model can never score and counts them under `skipped.calibrator_ineligible`: its training events, real events for a synthetic-trained model and, with `--replay` (which decides at first sight), events first seen at or before its cutoff. A live run decides now, so it also scores forward events collected before the cutoff. Inspect returned forecast IDs and plan a simulation only:

```sh
python -m jevtrader --db data/forward.sqlite paper-plan --forecast FORECAST_ID --equity 3000 --cash 3000 --peak-equity 3000
python -m jevtrader --db data/forward.sqlite show forecasts FORECAST_ID
```

`show KIND ID` accepts `attempts`, `bars`, `disclosures`, `experiments`, `extractions`, `forecasts`, `models`, `outcomes`, `paper_plans` and `runs`. An extraction keeps the raw provider response for audit.

## Paper plans

Paper order plans do not place orders or track a portfolio; every plan is marked `simulation_only`. `paper-plan` also accepts `--positions FILE.json` and `--shortable`. Default paper settings are:

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

## Bounded question research

This adapts autoresearch's experiment pattern; it does not run Karpathy's GPU-training repository. First build enough matured baseline observations. With the default training floor, the development universe needs at least 40 events, and purging must leave at least 10 evaluated events.

```sh
python -m jevtrader --db data/research.sqlite autoresearch --provider jev --model jev-1.13.0 --proposal-model YOUR_AVAILABLE_OPENAI_MODEL --development-until 2026-09-01T00:00:00Z --rounds 3
```

The first round evaluates the baseline; later rounds ask OpenAI to change only question text/name. The ledger locks the development cutoff, baseline, provider, model, and event universe. Before locking, it rejects a universe that mixes real and synthetic events or that purging would leave with fewer than 10 evaluated events. Missing API keys fail before the research settings are locked or any trial or proposal budget is reserved (with `--rounds` above 1, both the OpenAI key and the extraction provider's key are checked first); a completed trial is returned again without keys. A completed trial is reused only if the current evaluator (`ridge-event-v2`) scored it; running one scored by an earlier evaluator uses a new trial slot. A paid proposal that fails local strategy validation is recorded as `proposal_rejected` with its response and still counts as a proposal attempt; `autoresearch` lists it under `rejected_proposals`, and that round produces no trial.

The limit is **five candidate trials per ledger, at most 200 events each**, plus at most **four proposal API attempts**, including failures and manual `propose` calls. Failed started trials are retained and are not automatically retried, even under a later evaluator. This bounds calls, not a dollar amount. The resolved provider model is pinned; changes during comparison are rejected. `experiment` tests a candidate JSON; `propose` writes a candidate from a completed development trial. Run either with `--help` for arguments.

The development winner is never automatically promoted. All adaptive results remain development results; freeze a chosen version and collect new forward observations before judging it. Repeatedly changing questions against the same holdout contaminates that holdout.

## Backfills

For research on older filings, `jevtrader backfill --start 2026-01-02 --end 2026-03-31 [--symbols A,B] [--max-filings 500] [--bars]` collects historical 8-Ks (and, with `--bars`, historical Alpaca bars) into `research.sqlite` in the app folder, never the forward ledger. `--symbols` defaults to the config's scope.

Availability is an assumption (acceptance + 15 minutes, or the next 06:00 ET weekday), symbols come from today's ticker map (survivorship bias), and only filings still in each company's recent SEC submissions are found. Replay them with the research commands, for example:

```sh
jevtrader --db "$HOME/Library/Application Support/jevtrader/research.sqlite" observe --replay --limit 200
```

Replays with the `rules` baseline are labelled `no_model_knowledge` and count toward that ledger's evidence gate, even though backfills carry the biases above. Read a research ledger's scoreboard with that in mind; see [Limitations](limitations.md#what-can-fool-the-gate).
