# Limitations

**No edge has been established.** The demo's signal is injected on purpose, so its results are a plumbing check. This page lists what JEVTrader cannot do, what its numbers do not establish, and the ways its evidence gate can still be fooled.

- [What it cannot do](#what-it-cannot-do)
- [What the numbers do not establish](#what-the-numbers-do-not-establish)
- [What can fool the gate](#what-can-fool-the-gate)
- [Platform and project status](#platform-and-project-status)

## What it cannot do

- Place, change or track orders or positions, or give investment advice. It has no broker connection, order execution, or fill simulator. Paper plans are stateless simulations; you supply equity and positions each time.
- Show an edge that does not exist: until a calibrator is fitted (after at least 30 matured forward labels by default) and named in config, everything is `WATCH`, and the gate needs 100 matured evidence-eligible calls. Expect weeks to months of forward collection before any `LONG`/`SHORT`, and longer before the gate can decide.
- See every disclosure: only 8-Ks listing 7.01/8.01 (not 2.02, so no earnings releases), one EX-99 or primary document each, HTML or text only, no PDFs. Only SEC's 100-entry current feed is polled. Periods when the service was not running stay gaps; they are recorded as `coverage_gap` records but not reconstructed.
- Provide market data by itself: bars need Alpaca keys (a free paper account; only the `sip` feed is supported); delisting returns and quotes are not modeled; costs are assumptions. There is no real-time or intraday data: it uses completed daily bars and polls SEC about once a minute during weekday hours.
- Scope the Alpaca keys to data only. The paper-account keys it uses for bars can probably place paper orders; the app never calls an order, position or account route, and a test fails if any such route or a broker trading host (other than the calendar read from `paper-api.alpaca.markets`) appears outside `jevtrader/execution/`. Whether Alpaca offers data-only keys is unverified (P0-04).
- Remove model contamination from replays; it only labels it.
- Guarantee that quotes are safe to show an AI agent: the filter that drops sentences addressing the reader or an AI, giving orders or naming tools is best effort.
- Prove that the ledger was never rebuilt: the hash chain is tamper-evident, not tamper-proof, and nothing anchors it publicly. See [Checking the ledger](data-and-ledger.md#checking-the-ledger).

## What the numbers do not establish

- Evaluation reports **event-level benchmark-relative returns**, not portfolio P&L, an executable hedged portfolio, an equity curve, or Sharpe. Events overlap and are not independent. The research comparison omits paper sizing and several policy gates.
- Spread, slippage, commissions, and borrow costs are configurable assumptions, not observed quotes or actual fills. Event evaluation uses fixed round-trip basis points and excludes dollar commissions and short borrow costs; paper plans include configured commissions and borrow estimates. Paper plans use the stored reference price, not a live executable quote. Short results are conditional on borrow availability and costs.
- No exchange calendar, delisting-return feed, point-in-time universe, or market-data subscription is bundled. Stock/benchmark gaps are checked against each other, but sessions missing from **both** can remain undetected. Delistings and missing future data remain unresolved; inspect unresolved counts instead of silently treating those events as losses, wins, or exclusions.
- Historical text may be contaminated by model pretraining. Chronological splits cannot remove that contamination, survivorship bias, bad timestamps, revised source data, or repeated-search overfitting.
- **Jev's probabilities are not calibrated profit probabilities.** Jev's training cutoff is undisclosed, so Jev counts as evidence only when forward. OpenAI models have no built-in cutoffs.
- More trials or more complex models do not establish statistical validity. The next useful evidence is a frozen strategy observed forward with realistic data, costs, and a comparison against simple alternatives.

## What can fool the gate

The gate is fixed in advance and fingerprinted, which stops thresholds from moving quietly. It does not stop these:

- **Self-reported cutoffs.** Evidence labels trust the training cutoffs that vendors publish. Names match exactly, so a fine-tune or renamed build gets `unknown_cutoff`, but a wrong published cutoff would mislabel replays as `post_cutoff`. A cutoff you declare in config is trusted as declared; for a registered model it can't be set earlier than the published one.
- **Overlapping windows.** Each outcome runs from the next open to the tenth close, so calls made on nearby dates share market moves. The interval treats each decision date as one observation; it does not model that overlap, so it understates uncertainty.
- **Many tries.** Within one ledger each filing counts once, as its first recorded call, so re-scoring can't swap in a better call. Nothing links separate ledgers, though: trying several providers, question sets or autoresearch trials and keeping the best-looking scoreboard inflates the chance of a false `supported`.
- **Backfills.** On a research ledger, `rules` replays of backfilled filings are labelled `no_model_knowledge` and count toward that ledger's gate, even though backfills use assumed availability times (acceptance + 15 minutes, or the next 06:00 ET weekday), symbols from today's ticker map (survivorship bias), and only filings still in each company's recent SEC submissions. The rules baseline's word lists were also chosen by hand, with today's knowledge.
- **Costs.** The default 20 bps round trip and 300 bps a year of short borrow are assumptions, not observed fills. The [cost floor](adr/0002-cost-floor.md) only rules out near-free trading; a strategy at the floor is not realistic either.

If you find another way to make it count something it shouldn't, please [open an issue](https://github.com/zd87pl/jevtrader/issues).

## Platform and project status

- **The background service is macOS-only.** It depends on launchd, the Keychain and notifications. Linux gets the CLI, research tools, brief, page and MCP server, with keys from environment variables. Windows is unsupported because the CLI imports `fcntl`. See [Linux and Windows](service.md#linux-and-windows).
- **Provider integrations are tested with mocked responses,** not verified against the live Jev, OpenAI, Ollama or LM Studio services.
- **The default local model (`gpt-oss:120b`) is very large.** `gpt-oss:20b` is also registered. The project states no hardware requirements.
- **The demo shows no filings in the brief or in search,** on the page or over MCP, because both exclude synthetic filings. A filing opened by its id (`/filing/<id>` or `explain_filing`) still shows, labelled synthetic. Its scoreboard stays at "Collecting evidence: 0 of 100 matured calls", with all 93 synthetic forecasts excluded.
- **It installs from source only.** There is no PyPI release, Homebrew formula, Docker image or desktop app.
- **The project is young:** version 0.2.0, one author, and CI tolerates a baseline of pre-existing mypy errors.
