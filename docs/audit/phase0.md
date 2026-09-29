# Phase 0 audit: JEVTrader today, measured against the Evidence Engine program

| | |
|---|---|
| Status | Draft for review by A7 (Security) and A8 (QA). See [Acceptance](#11-acceptance) |
| Owner | A0 Lead Architect |
| Tree audited | `fa0c0d3` (branch `phase0-audit`, identical to `origin/main`) |
| Date | 2026-09-29 |
| Companion outputs | [ADR-0001](../adr/0001-evidence-engine-architecture.md), [dependency graph](dependency-graph.md), [CLAUDE.md](../../CLAUDE.md), [AGENTS.md](../../AGENTS.md) |

**Scope.** This audit covers every checklist item in section 2 of the master prompt ("Phase 0: Codebase audit"). Each item gets `path:line` evidence from the tree above, or a pointer to a gap in the [gap register](#10-gap-register-issues-to-file) (IDs `P0-01` to `P0-42`, to be filed as GitHub issues).

**Method.**
- Five read-only auditors covered modules, timing, the LLM boundary, trials and quality. A0 then re-read every citation used here against the tree.
- A0 re-ran the offline suite: 742 passed, 1313 subtests, 4.3 s, Python 3.14.6 on macOS.
- Coverage was measured once, by the quality auditor, with `python3 -m pytest -q --cov=jevtrader --cov-branch --cov-report=term-missing:skip-covered` (pytest 9.0.2, pytest-cov 7.1.0, coverage 7.13.5, Python 3.14.6), with the coverage data file kept outside the repo. The QA review could not re-run coverage under its brief, so the coverage figures in §0 and §7.1 are A0-side measurements until A8 repeats that command (§11).
- No product code or test was changed. The only change outside `docs/`, `CLAUDE.md` and `AGENTS.md` is two `.gitignore` lines (`.coverage` and `.mypy_cache/`, `.gitignore:13-14`), which are not in `fa0c0d3` and must be committed with these docs.
- Nothing called launchctl, the Keychain, `jevtrader up`, or live SEC, Alpaca or provider collection.
- The only network use was the timing auditor's WebFetch reads of public sec.gov pages (§3). No User-Agent was set or invented for them.
- Offline probes ran against in-memory or temporary ledgers only.

**Owner decisions that override the master prompt** (restated in [CLAUDE.md](../../CLAUDE.md)):
1. Every live order needs per-order human approval in the app's own UI, and there is no "live-bounded-auto" rung. Paper-auto is allowed.
2. The name stays "jevtrader".
3. The owner's name and email are never sent to SEC or any other service, and no User-Agent is fabricated.
4. The assistant never places orders, paper or live.

## Contents

0. [Summary](#0-summary)
1. [Module map](#1-module-map)
2. [Confirmations: calibrator, costs, 92 days, labels, gate](#2-confirmations)
3. [EDGAR acceptanceDateTime](#3-edgar-acceptancedatetime)
4. [Alpaca bars: `feed=sip` and request age](#4-alpaca-bars-feedsip-and-request-age)
5. [LLM boundary](#5-llm-boundary)
6. [Trial mechanisms](#6-trial-mechanisms)
7. [Tests, isolation guards and CI](#7-tests-isolation-guards-and-ci)
8. [Invariants](#8-invariants)
9. [Ranked tech-debt list](#9-ranked-tech-debt-list)
10. [Gap register (issues to file)](#10-gap-register-issues-to-file)
11. [Acceptance](#11-acceptance)

## 0. Summary

| §2 checklist item | Verdict | Where | Gaps |
|---|---|---|---|
| Module map | Complete: 27 modules (29 files counting `__init__` and `__main__`), 10,294 lines, stdlib + numpy (`pyproject.toml:12`), plus a secrets and identity data-flow table | §1 | P0-03, P0-42 |
| Daemon passes the calibrator | **Confirmed** end to end, and pinned by tests | §2.1 | P0-25 (latent) |
| Cost model location | One function, `common.round_trip_bps`. **It has no floor:** a zero-cost strategy validates, and its calls count | §2.2 | P0-02, P0-36 |
| 92-day logic | **Confirmed:** one constant, one comparison on the New York filing date | §2.3 | P0-37 |
| 7 labels | **Confirmed:** assigned in two functions and frozen with the decision | §2.4 | P0-31 |
| Gate statistics | **Confirmed** as documented. The fingerprint covers only the 4 `GATE` fields, and the interval ignores overlapping windows | §2.5 | P0-30, P0-33, P0-34 |
| EDGAR acceptanceDateTime UTC→New York, with real fixtures | **Partly met.** Availability is conservative, but `after_hours` and `published_at` are each wrong for one population of filers. **There are no real fixtures.** Applying "UTC" literally would backdate some filings | §3 | P0-11, P0-12 |
| Alpaca `feed=sip` and `end` ≥ 15 min old | **Met in product paths.** The public helper `daily_bars` is unguarded, with no callers | §4 | P0-15 |
| External text → LLM, and LLM output → decisions | **Inventoried:** 7 entry points, 8 output sinks, 5 MCP fields and 1 web endpoint. Readers have no tools, but there is no sanitizer, no span evidence and no red-team corpus | §5 | P0-05–P0-09, P0-37, P0-39 |
| Trial mechanisms and whether they are counted | **Inventoried: 15 mechanisms.** Only autoresearch is recorded, and only per ledger. There is no DSR, no PBO and no trial count, and the gate pools every configuration | §6 | P0-27–P0-33 |
| Test inventory, isolation guards, CI | 742 tests + 1313 subtests, ~5 s, 95% line+branch coverage (A0-side measurement, see Method), hermetic. CI is sound but lax on coverage, mypy and timeouts | §7 | P0-10, P0-19–P0-23 |
| Ranked tech-debt list | 22 items | §9 | — |
| Invariants list | 29 invariants, each with its enforcing code and pinning test | §8 | — |
| Outputs | CLAUDE.md, AGENTS.md, ADR-0001 and the dependency graph are drafted | links above | — |

**Three findings break a stated principle today and should be fixed first:**
- **P0-02, zero-cost strategies count** (refusal #2).
- **P0-03, setup asks for the owner's name for SEC** (owner decision 3).
- **P0-31, hand-picked imported and backfilled cohorts count as evidence when replayed through the queue** (the intent behind `adhoc_replay`).

## 1. Module map

Data flow: `feeds.poll` / `feeds.backfill` → `sec.collect_filing` → `store.Ledger.disclosure` → `pipeline.observe_queue` → `engine.observe` (text features from `providers`/`local`, market features from `market.snapshot`, prediction from `research.predict`) → a forecast record → `engine.settle` (`market.outcome`) → `evidence.scoreboard` → `brief` / `web` / `mcp_server`. The `daemon` schedules all of it, and `app` wires it from `config`.

### 1.1 Collectors: feed, index reconcile, backfill

| Unit | Entry points | Notes |
|---|---|---|
| SEC client | `jevtrader/sec.py:180-245` | URL allowlist (`sec.py:121-147`), no redirects (`sec.py:150-156`), byte bounds (`sec.py:35-36`). The rate limiter is shared **within one process** (`sec.py:41`, `sec.py:159-177`; the comment at `sec.py:175-176` says so). The User-Agent must contain an email (`sec.py:194-201`) |
| Filing selection | `sec.py:419-446` (`_qualifying`), `sec.py:377-390` (`_filing_document`) | Only 8-K and 8-K/A listing Item 7.01 or 8.01 and not 2.02. One EX-99 or primary document per filing. The id is `sec:{accession}:{document}` (`sec.py:524`) |
| Filing record | `sec.py:464-544` (`collect_filing`) | Forward mode stamps actual receipt and refuses a supplied `first_seen` (`sec.py:489-490`, `sec.py:516`, `sec.py:521`). Historical mode requires one (`sec.py:491-492`) |
| Feed polling | `jevtrader/feeds.py:374-445` (`poll`) | One Atom request for the newest 100 8-Ks (`feeds.py:40-43`). CIKs map to *today's* tickers (`feeds.py:148-181`). The per-process `PollMemory` is never persisted (`feeds.py:264-303`) |
| Daily-index reconcile | `feeds.py:199-242`, `jevtrader/daemon.py:406-419` | Runs at 22:45 ET (`daemon.py:49`). **It only counts** 8-K rows against stored accessions (P0-14) |
| Backfill | `feeds.py:473-566`, `feeds.py:245-261`; CLI `jevtrader/app.py:234-288` | Historical only. Assumed availability is marked `first_seen_basis: backfill_assumed` and `symbol_basis: sec_ticker_map_at_backfill` (`feeds.py:562-563`). Refuses the forward ledger (`feeds.py:458-470`, `app.py:252-253`). Survivorship-biased, as documented at `feeds.py:6-8` (P0-16) |

### 1.2 Alpaca bars

| Unit | Entry points | Notes |
|---|---|---|
| Route allowlist | `jevtrader/bars.py:68-88`, `bars.py:145-163` | Three routes: `/v2/stocks/bars` and `/v1/corporate-actions` on `data.alpaca.markets`, and `/v2/calendar` on **`paper-api.alpaca.markets`** (`bars.py:35-36`, `bars.py:87`). Every parameter is required and pattern-checked (`bars.py:157-163`) |
| Bar request | `bars.py:385-410` | `feed` is always sent (`bars.py:395`), with `adjustment=raw`. `start` and `end` are noon ET (`bars.py:302-305`, `bars.py:391-392`) |
| Forward fetch | `bars.py:546-579` | Fetches only sessions whose close + 20 min ≤ now (`bars.py:43`, `bars.py:577`). Refuses the research ledger (`bars.py:568-571`). Bars are stamped at actual receipt (`jevtrader/market.py:51-53`) |
| Historical fetch | `bars.py:582-612` | Refuses the forward ledger (`bars.py:600-602`). Availability is the close (`market.py:52`; see P0-15) |
| Corporate actions | `bars.py:475-543`, `bars.py:694-705` | Splits and cash dividends only. Actions Alpaca records after a bar is stored are never applied (`bars.py:8-9`) |

### 1.3 Text providers

| Provider | Entry points | Registry treatment |
|---|---|---|
| dispatch | `jevtrader/providers.py:422-457` | 40,000-character limit (`providers.py:19`). Output must be exactly four keys within their ranges (`providers.py:203-217`) |
| `rules` | `providers.py:374-419` | `knowledge: none`, so its replays are labelled `no_model_knowledge` (`jevtrader/registry.py:57-63`) |
| `local` | `jevtrader/local.py:259-295`, health check `local.py:312-377` | Loopback only (`local.py:73-94`). The model name is **self-reported** (`local.py:225`, P0-37) |
| `jev` | `providers.py:220-286` | The cutoff is undisclosed and cannot be declared (`registry.py:212-213`) |
| `openai` | `providers.py:327-371` | `store: False` and a strict JSON schema (`providers.py:330-337`). `unknown_cutoff` unless a cutoff is declared (`registry.py:105-107`) |
| proposer | `providers.py:460-515` via `jevtrader/lab.py:64-108` | Rewrites only the name and questions |

### 1.4 Feature schema

- `FEATURE_NAMES` has eight entries (`jevtrader/research.py:23-32`); the version tag is `disclosure-eight-v1` (`jevtrader/engine.py:114`).
- Text features: direction ∈ [-1, 1], and materiality, novelty and uncertainty ∈ [0, 1] (`providers.py:208-211`).
- Market features come from `market.snapshot` (`market.py:86-121`). The "spread" feature is a **configured constant**, labelled "configured assumption, not a quote" (`market.py:118`, `market.py:120`; P0-40).
- The vector is assembled by name (`engine.py:187`) and frozen on the forecast (`engine.py:244-245`).

### 1.5 Ridge calibrator

- The fit is a standardized ridge (`research.py:113-175`). Every outcome must be before the cutoff (`research.py:128-129`), and the id is content-addressed (`research.py:159-174`). The version is `ridge-event-v2` (`research.py:33`).
- Training rows and fitting are in `engine.py:329-394`.
- At decision time (`engine.py:78-92`) the calibrator must exist and be the current version, its cutoff must be before the decision, a synthetic-trained model is refused for real data, and it never scores an event it trained on. The extractor key must match (`engine.py:188-190`).
- Queue prefilter: `jevtrader/pipeline.py:128-136`, `pipeline.py:196-207`. Purged walk-forward: `research.py:254-405`.

### 1.6 Filters and actions

The logic is in `engine.py:192-212`:
- **Filters:** price < `min_price`, dollar volume < `min_dollar_volume`, uncertainty > `max_uncertainty`, novelty or materiality < 0.5 (hard-coded). Each failing filter adds a reason and forces `PASS`.
- **No calibrator:** `WATCH`.
- **LONG:** expected return > (round trip + `min_edge_bps`)/10⁴.
- **SHORT:** only when `allow_short` is set.
- **Defaults:** `jevtrader/default_strategy.json:13-21`.
- **Paper sizing:** `jevtrader/paper.py:24-225` re-applies its own checks and is always `simulation_only` (`paper.py:119`).

### 1.7 Ledger schema v2

- Tables:
  - `records(kind, id, payload, content_hash, recorded_at)`, keyed by (kind, id) (`jevtrader/store.py:44-48`).
  - `chain(seq, kind, id, content_hash, prev_hash, chain_hash)` (`store.py:56-60`).
  - `SCHEMA_VERSION = 2` (`store.py:28`), with ten record kinds (`store.py:15-26`).
- Eight immutability triggers (`store.py:31-42`, `store.py:49-76`).
- The link hash is `sha256(prev|kind|id|content_hash)` (`store.py:80-82`), appended under one write lock (`store.py:188-206`).
- Migration from v0/v1 runs in `recorded_at, kind, id` order (`store.py:125-141`).
- `verify` recomputes hashes, walks the chain, and detects truncation, missing triggers and anchor mismatch (`store.py:224-312`). Every read re-checks the digest (`store.py:314-349`).
- **`GENESIS` is the same constant for every ledger** (`store.py:29`), and `recorded_at` is outside the link hash (`store.py:46`, `store.py:80-82`). See P0-18.

### 1.8 Evidence labels and gate

- **Registry:** `registry.py:20` (`BUFFER_DAYS`), `registry.py:22-31` (labels), `registry.py:56-108` (models), `registry.py:111-157` (`lookup`, `eligibility`).
- **Scoreboard:** `jevtrader/evidence.py:24` (`GATE`), `evidence.py:38-43` (`is_evidence`), `evidence.py:139-154` (`decisions`), `evidence.py:157-182` (interval and status), `evidence.py:203-253` (`scoreboard`).

### 1.9 Brief, serve and MCP

- **Brief.** `jevtrader/brief.py` has `compose` (`brief.py:395-430`) and `filing_card` (`brief.py:496-525`).
  - Quotes are verbatim, and sentences that read like directives are dropped (`brief.py:53-66`, `brief.py:108-153`).
  - Cards show the **latest** forecast (`brief.py:164-169`, `brief.py:382`, `brief.py:420`; P0-41).
- **Serve.** `jevtrader/web.py` binds loopback only (`web.py:417-418`), checks the Host header (`web.py:323-325`) and accepts GET/HEAD only (`web.py:326-328`).
- **MCP.** `jevtrader/mcp_server.py` exposes five tools, all annotated `readOnlyHint` (`mcp_server.py:85-91`, `mcp_server.py:95-161`). Unknown tools are refused (`mcp_server.py:449-451`), and the server instructions are at `mcp_server.py:50-59`.

### 1.10 Setup, launchd, daemon and secrets

- **Setup** (`app.py:562-649`) asks for **"Your name and email for SEC"** (`app.py:584-588`; reason text at `app.py:48-52`; P0-03). The same wording recurs in several places:
  - errors and doctor (`app.py:251`, `app.py:313`, `app.py:356`);
  - the config validator (`config.py:134`) and the daemon skip reason (`daemon.py:435`);
  - the SEC client's error, "containing your contact email" (`sec.py:201`);
  - `.env.example:3` and `docs/service.md:28`;
  - `docs/data-and-ledger.md:64` ("your real contact-bearing User-Agent"), `:67`, and `:72`, which suggests passing `--user-agent "Name email"` on the command line.

  No default identity is shipped (`config.py:21`).
- **Service.** `app.up`/`down` (`app.py:303-319`) call `jevtrader/launchd.py:74-129`. The plist holds no secrets (`launchd.py:206-211`).
- **Daemon** (`daemon.py`).
  - Jobs are defined at `daemon.py:40`; the pure schedule is `daemon.py:126-163`.
  - Every run is recorded (`daemon.py:548-569`).
  - `flock` enforces a single writer (`daemon.py:647-712`), and the spend cap is at `daemon.py:306-353`.
- **Doctor:** `app.py:325-376`, `app.py:413-451`. It touches the real system: it reads the Keychain (`app.py:363`, then `app.py:476`) and runs `launchctl print` (`app.py:367`, then `app.py:493` and `launchd.py:137`).
- **Secrets** are read from the macOS Keychain via `/usr/bin/security` (`jevtrader/secrets.py:14-18`). The key list is fixed (`secrets.py:15`). The flows are in the table below.

**Secrets and identity data flow.**

| Flow | Where | Pinned by | Weak spot |
|---|---|---|---|
| The environment wins over the Keychain | `secrets.get` (`secrets.py:59-68`) | `tests/test_secrets.py:57-61` | a key exported in a shell reaches every process started from it |
| Shell export is the documented default | `.env.example:1-11` | — | keys sit in shell environments and history files; nothing warns about this |
| Keychain → `os.environ` for every known key | `export_to_environ` (`secrets.py:99-114`), called for key-using commands (`cli.py:55-58`, `cli.py:492-493`) and by `doctor` (`app.py:476`) | `tests/test_secrets.py:152-164` | all four keys are exported even when one is needed. Child processes inherit them, for example `osascript` (`notify.py:32-35`) (P0-42) |
| Keychain writes go through stdin, never argv | `secrets.py:79-81`; setup reads at `app.py:758` and writes at `app.py:775` | `tests/test_secrets.py:80-92` | the items probably trust `/usr/bin/security`, so any process of the user can probably read them without a prompt (unverified; P0-42) |
| Keys leave only in request headers | Alpaca `bars.py:166-179`; providers `providers.py:125-128`, `providers.py:153` | `tests/test_bars.py:365-443`; the key is a separate transport argument in `tests/test_providers.py:121-126` | no test asserts that a provider key never enters a prompt body |
| Keys are scrubbed from error text | `secrets.py:41-46`; `bars.py:211-218`; `providers.py:163-169`; `daemon.py:537-545`; `mcp_server.py:181-183` | `tests/test_secrets.py:94-112`, `:141-151`; `tests/test_providers.py:297-319`; `tests/test_mcp.py:544-549`, `:657-662` | — |
| Keys are refused in the plist | `launchd.py:206-210` | `tests/test_launchd.py:140` | — |
| SEC contact: config | `config.py:21` (empty default), `config.py:125-134`; written with mode 0600 (`config.py:69-85`); read by the daemon (`daemon.py:432-435`) | `tests/test_config.py:127-129` (no email or a header break is refused) | the prompt asks for the owner's name (P0-03) |
| SEC contact: environment and argv | `collect --user-agent`, which defaults to `SEC_USER_AGENT` (`cli.py:92`); argv use is suggested at `docs/data-and-ledger.md:72` | — | argv is visible to every local process through `ps` |
| SEC contact: outbound | the `User-Agent` header to allowlisted sec.gov hosts only (`sec.py:227`, `sec.py:121-147`); no request without a contact email (`sec.py:194-201`) | `tests/test_sec.py:162-180`, `:209-230` | — |

### 1.11 Config and strategy JSON

- **`config.json`:**
  - defaults at `jevtrader/config.py:19-39` (the SEC User-Agent defaults to empty, with no fabricated value, `config.py:21`);
  - unknown keys are rejected by `config.py:88-112`;
  - it is written atomically with mode 0600 (`config.py:69-85`).
- **Strategy:**
  - `common.load_strategy` (`jevtrader/common.py:81-88`) and `validate_strategy` (`common.py:91-140`) accept **exactly** the default key set (`common.py:93-94`);
  - numbers need only be ≥ 0 (`common.py:117-125`);
  - the strategy is frozen into every forecast (`engine.py:252`) and into the extraction spec (`engine.py:110-119`).

## 2. Confirmations

### 2.1 The daemon passes the configured calibrator: CONFIRMED

| Step | Code |
|---|---|
| Config field (`None` means `WATCH` only) | `config.py:37-38` |
| Production wiring: `observe = partial(pipeline.observe_queue, base_url=…, overrides=…)` | `app.py:78-93`, used at `app.py:116` (one-off jobs) and `app.py:138` (daemon) |
| The observe job passes `calibrator=ctx.config["calibrator"]` | `daemon.py:285-294` (argument on `daemon.py:291`) |
| The queue hands it to the engine as `calibrator_id=calibrator` | `pipeline.py:250-260` (`pipeline.py:257`) |
| The engine uses it to predict and to choose the action | `engine.py:78-92`, `engine.py:191`, `engine.py:201-212` |
| Tests | `tests/test_daemon.py:553-559`; `tests/test_app.py:456-464` (the real queue fails with "Unknown calibrator: ridge-missing"); doctor at `tests/test_app.py:801-836` |

Latent risk: `daemon.Context` defaults to the bare `pipeline.observe_queue`, with no `base_url` or `overrides` (`daemon.py:109`). Only `app.daemon_context` adds them. See P0-25.

### 2.2 Cost model: location and every use

- **Definition** (`common.py:143-147`). The round trip is `spread_bps + 2 × slippage_bps_per_side`. Shorts add `short_borrow_bps_annual × horizon_sessions / 252`.
- **Defaults** (`default_strategy.json:15-19`): 20 bps long round trip, 31.9 bps short, and a 20 bps minimum edge, so a `LONG` needs more than 40 bps. That matches `README.md:45`.

| Use | Where | Charge |
|---|---|---|
| LONG/SHORT thresholds | `engine.py:205-206` | round trip (with borrow for shorts) + min edge |
| Gate net return | `evidence.py:66-68`, called at `evidence.py:230` | the **forecast's own frozen strategy** costs |
| Walk-forward | `research.py:268-273` | long round trip for both sides; **no borrow for shorts**, which is disclosed |
| Paper plans | `paper.py:159-182` | **a second formula:** max(observed, configured spread)/2 + slippage per side, plus borrow and commission (P0-36) |
| Spread feature | `market.py:118` | not a cost: the configured constant copied in as feature 8 |

- **Gap P0-02.** `validate_strategy` enforces only `minimum=0` (`common.py:123`). A strategy with every cost at 0 validates, and the gate nets its calls at zero cost (`evidence.py:67`). Offline probe: a LONG with a +1% target scores +1.00% net.
- **Gap P0-20.** No test pins the 40 / 51.9 bps thresholds. The only related assertion is `assertNotEqual(action, "WATCH")` (`tests/test_engine.py:600`).
- **Pinned today:** `tests/test_evidence.py:204-226` and `tests/test_research.py:167-178`.

### 2.3 The 92-day buffer: CONFIRMED

- **Constant.** `BUFFER_DAYS = 92` (`registry.py:20`); the comment at `registry.py:18-19` explains it. It is referenced nowhere else in the package.
- **Rule.** The filing date is `published_at` taken as a New York date (`registry.py:142`). A replay counts only if `(filed − cutoff).days > 92` (`registry.py:154-156`), so day 92 itself is still contaminated.
- **Scope.** It applies only to historical replays of models with a known cutoff (`registry.py:146-153`).
- **Declared cutoffs** may only be later than the published one (`registry.py:216-220`), and Jev cannot declare one (`registry.py:212-213`).
- **Tests:** `tests/test_registry.py:30-43` (the constant and the labels), `:215-263` (boundaries, including DST and leap day), `:367-388`.

### 2.4 The seven labels: CONFIRMED

`LABELS` is defined at `registry.py:22-30`; the evidence labels are `forward`, `post_cutoff` and `no_model_knowledge` (`registry.py:31`). An unrecognized string is not evidence (`registry.py:160-162`).

| Label | Assigned at | Counts |
|---|---|---|
| `forward` | `registry.py:146-147`; fallback `engine.py:275` | yes |
| `synthetic` | `registry.py:148-149`; fallback `engine.py:275` | never (`evidence.py:39-40`) |
| `no_model_knowledge` | `registry.py:150-151` (rules baseline only, `registry.py:57-63`) | yes |
| `unknown_cutoff` | `registry.py:152-153`; `engine.py:274-275` when the extractor cannot be identified | no |
| `post_cutoff` | `registry.py:155-156` | yes |
| `contaminated` | `registry.py:157` | no |
| `adhoc_replay` | `engine.py:276-277`, set only for a named `--event` with `--replay`/`--as-of` (`pipeline.py:96-101`) | no |

The label and its `eligibility_basis` are frozen into the forecast (`engine.py:226-243`). Tests: `tests/test_registry.py:170-213`, `tests/test_engine.py:177-213`, `tests/test_pipeline.py:286-300`, `tests/test_evidence.py:127-163`.

### 2.5 Gate statistics: CONFIRMED

- **`GATE`** = `{"version": 1, "min_matured_calls": 100, "confidence": 0.90, "futility_upper_bps": 10.0}` (`evidence.py:24`). A deep copy is taken per board (`evidence.py:208`).
- **Counted:**
  - forecasts visible at `as_of` that are evidence (`evidence.py:209-210`);
  - one decision per event (`evidence.py:139-154`);
  - matured when `max(outcome_at, label_available_at)` ≤ as_of (`evidence.py:55-56`, `evidence.py:222`).
- **Interval.** Net returns are grouped by New York decision date (`evidence.py:231-232`). The interval is a Student-t 90% interval over the per-date means (`evidence.py:157-170`), with the t quantile from an exact incomplete beta (`evidence.py:71-131`).
- **Status.**
  - Fewer than 100 calls → `collecting`.
  - Fewer than 2 dates → `inconclusive`.
  - low > 0 → `supported`.
  - high < +10 bps → `no_edge`.
  - Otherwise `inconclusive` (`evidence.py:173-182`).
- **Fingerprint.** `gate_sha256 = sha256(canonical(GATE))` (`evidence.py:238`, `common.py:38-43`) = `c895567c3b76c20bd5d1485ca5ecb09b0c42337e113977a3146cd0116435bbac`, recomputed by A0 and pinned by `tests/test_evidence.py:18-21`.
- **Not covered by the fingerprint:**
  - the cost model;
  - `ELIGIBILITY_RULE` and `METHOD` (`evidence.py:28-35`);
  - `BUFFER_DAYS` and the model registry;
  - the decision-selection rule.

  See P0-30.
- **Overlap.** The interval ignores overlapping 10-session windows (`README.md:193`; P0-34). The gate is recomputed at every read with no sequential correction (P0-33).

## 3. EDGAR acceptanceDateTime

**Verdict: partly met, and unsafe if the checklist is applied literally.**

**How the code reads a timestamp:**
- `published_at` reads a trailing `Z` as UTC (`jevtrader/sec.py:85`). A naive value is read as Eastern (`sec.py:88-89`).
- `latest_acceptance` returns the **later** of the UTC reading and the Eastern wall-clock reading, under either DST fold (`sec.py:93-109`). Its docstring calls the Eastern reading "unverified" (`sec.py:96`).

**Where each reading is used:**
- Availability always uses the later reading: backfill `first_seen_at` (`jevtrader/feeds.py:252`) and the supplied-first_seen check (`sec.py:449-461`). **It can only run late, so no look-ahead path exists.** Forward records use actual receipt (`sec.py:516`, `sec.py:521`), and gates use `first_seen_at` (`engine.py:70`).
- `after_hours` uses the later reading compared against 17:30 ET (`sec.py:47`, `sec.py:522`, `sec.py:528`). Nothing reads it yet.
- The brief shows `published_at` as the "accepted" time (`brief.py:751`).

**What EDGAR actually returns.** The timing auditor compared each filing's index page ("Accepted", an Eastern wall-clock time) with the submissions JSON, using WebFetch only. WebFetch passes pages through a summarizing model, so confidence is marked per row.

| CIK / accession | Index "Accepted" (ET) | JSON `acceptanceDateTime` | Meaning of `Z` | Confidence |
|---|---|---|---|---|
| 320193 / 0000320193-26-000018 (8-K) | 2026-07-30 16:30:28 | `2026-07-30T20:30:28.000Z` | true UTC | high |
| 320193 / 0000320193-26-000005 (8-K) | 2026-01-29 16:30:33 | `2026-01-29T21:30:33.000Z` | true UTC | high |
| 320193 / 0000320193-19-000073 (8-K, pre-2020) | 2019-07-30 16:30:36 | `2019-07-30T20:30:36.000Z` | true UTC (provisional) | medium-low |
| 1137411 / 0001137411-18-000111 (10-K, pre-2020) | 2018-11-26 16:09:52 | `2018-11-26T16:09:52.000Z` | **Eastern wall clock labelled `Z`** | medium |

**Consequences for the current code:**
- For the true-UTC filer, `after_hours` is **True** for 16:30 ET filings, because the later reading pushes them to 20:30 ET.
- For the same filer, backfilled availability (`feeds.py:245-261`) runs late by an amount that depends on the time of day. The later reading is the true instant plus 4 h (EDT) or 5 h (EST):
  - a filing accepted before about 13:30 EDT (12:30 EST) still reads before 17:30 ET, so its availability is 4–5 h late;
  - a filing accepted from then until 17:30 ET reads at or after 17:30 ET, so `feeds.py:254-260` rolls it to 06:00 ET on the next weekday. The 16:30 ET example becomes available at 06:00 the next weekday instead of 16:45 ET: about 13 h late, or about 2.5 days on a Friday.
- For the stale-JSON filer, `published_at` is 5 h early, so the brief displays 11:09 ET.
- Neither error lets a decision see a filing early.

**Fixtures.**
- Every existing fixture is synthetic (`tests/test_sec.py:156-158`, `tests/test_feeds.py:392-411`), and most use explicit offsets that the submissions JSON never produces.
- There are **no real index-page fixtures and no pre-2020 cases** (P0-11).
- No filing accepted at or after 17:30 ET was found within the call budget.

Owner check, by browser and with no User-Agent needed:
1. Open each accession's `…-index.htm` and note "Accepted".
2. Read the same accession's `acceptanceDateTime` in `data.sec.gov/submissions/CIK##########.json`.
3. Record both in `tests/fixtures/edgar_acceptance.json`.

Fix direction (P0-12): keep the later reading for availability. Store a verified acceptance instant with an `acceptance_basis`, taken from the filing index or the `.hdr.sgml` header. Only the index page is reachable today: `{accession}-index.htm` matches `_DOCUMENT` (`sec.py:42`), which the URL validator accepts (`sec.py:144-146`). `_DOCUMENT` allows only `.htm`, `.html` and `.txt`, so fetching `.hdr.sgml` needs an allowlist change at `sec.py:42` or `sec.py:145`. Derive `after_hours` only from that verified instant.

## 4. Alpaca bars: `feed=sip` and request age

| Check | Verdict | Evidence |
|---|---|---|
| `feed=sip` passed explicitly | **Met** | always in the query (`bars.py:395`); the route allowlist requires it (`bars.py:76`, `bars.py:159`); `FEEDS = ("sip",)` (`bars.py:41`); `_feed` refuses anything else (`bars.py:255-261`); config allows only `sip` (`config.py:28`, `config.py:49`). Tests: `tests/test_bars.py:279-280`, `:332`, `:783`, `:1169` |
| `end` ≥ 15 min old | **Met in product paths** | only sessions whose close + 20 min ≤ now are targets (`bars.py:43`, `bars.py:577`, `bars.py:610`). `end` is noon ET of the last target session (`bars.py:392`, `bars.py:649`, `bars.py:653-655`), so it is ≥ 4 h 20 m old, or ≥ 1 h 20 m on 13:00 early closes. `now` cannot be in the future (`bars.py:557-559`). The daemon runs bars at 16:30 ET (`daemon.py:48`) |
| Unguarded helper | Gap (P0-15) | `daily_bars(end=today)` "the newest may be unfinished" (`bars.py:355-363`). There are no callers in `jevtrader/` (grep) |
| Replay vs forward availability | Gap (P0-15) | historical bars are available at the close (`market.py:52`), while forward bars are available at receipt, which is ≥ 20 min after the close. Replays can therefore see a bar up to 20 min earlier than a forward run |
| Rate limit | Gap (P0-13) | 0.3 s per process (`bars.py:44`). `backfill --bars` running alongside the daemon can exceed 200/min |

## 5. LLM boundary

**Verdict.**
- No LLM has tools, and no order endpoint exists.
- The extractors behave like quarantined readers: they return four bounded numbers under a strict schema.
- Most of the §9 controls around them are missing: no sanitizer, no span offsets, no provenance tags, no versioned or logged prompt, and no red-team corpus.
- LLM output drives gates, actions and future training. Autoresearch runs LLM-written questions with no human review.

Row IDs: `E` is text entering a model (§5.1), `B` is model output that changes state (§5.2), and `C` is a field reaching an MCP or web host (§5.3). They are unrelated to the agent IDs A0–A8.

### 5.1 External text entering an LLM context

| # | Entry point | Text | Channel | Guard before the model | Risk |
|---|---|---|---|---|---|
| E1 | `engine.observe` → `extract_features` (`engine.py:144-159`) | current and previous filing text (`engine.py:101-109`) | per provider (E2–E4) | character budgets only (`engine.py:24-39`, `providers.py:113-116`, `sec.py:531`). HTML→text drops only `script/style/noscript` (`sec.py:265`, `sec.py:311-321`). **No NFKC, zero-width, bidi, homoglyph or hidden-HTML handling** | High for data integrity (P0-05) |
| E2 | OpenAI (`providers.py:327-371`) | text + strategy questions | trusted `instructions`. `input` is one JSON string that holds **both the filing text and the questions** (`providers.py:366`) | strict schema, no tools, `store: False` (`providers.py:330-337`); non-message items and refusals rejected | a filing can imitate a `questions` block (P0-07) |
| E3 | Jev (`providers.py:220-286`) | text in `state` | questions concatenated into per-question `instructions` (`providers.py:235`, `providers.py:241`) | typed choice answers | LLM-authored questions **are** the instruction channel (P0-06) |
| E4 | Local engine (`local.py:232-295`) | text + questions | fixed system message; the user message is JSON holding text and questions (`local.py:242-248`) | loopback, no proxy or redirect, `temperature 0`, `tool_calls` rejected (`local.py:215`) | same as E2 |
| E5 | Local health probe (`local.py:312-377`) | fixed probe text | — | — | none |
| E6 | Strategy proposer (`providers.py:460-515`, `lab.py:64-108`) | code-computed metrics, plus earlier LLM-written questions | feedback carries an `"instruction"` key (`lab.py:60`) although the model is told to treat feedback as data (`providers.py:500-501`) | schema {name, questions}; length checks only (`common.py:111-113`) | channel mixing (P0-07) |
| E7 | Imported disclosures (`store.py:359-391`) | any user JSONL | becomes E1 | nonempty strings only; **no length or charset bound** (`store.py:361-363`) | P0-08 |

No other call site exists. The callers are `engine.py:151`, `providers.py:450`, `lab.py:85`, `app.py:458` and `app.py:708`. The brief, web, notify and MCP layers never call a model.

### 5.2 LLM output that affects a decision or state

| # | Output | Lands in | What validates it | Risk |
|---|---|---|---|---|
| B1 | the 4 features | `extractions`, including the raw response (`engine.py:173-183`), then the forecast (`engine.py:187`) | keys and ranges only (`providers.py:203-217`) | adversarial but in-range values pass (P0-39) |
| B2 | uncertainty, novelty, materiality | hard filters (`engine.py:197-200`); paper filter (`paper.py:144-145`) | deterministic thresholds | a filer can open or close the gates |
| B3 | all 4 features | ridge `expected_return` (`engine.py:191`), then the action (`engine.py:201-212`) | code decides and costs are included | this is the future N3 path to tickets |
| B4 | all 4 features, persistently | training rows and fits (`engine.py:329-394`) | purged walk-forward; nothing checks provenance | **poisoning persists** into calibrators (P0-39) |
| B5 | proposed questions | `experiments` (`lab.py:91-103`). **Autoresearch runs them immediately** (`lab.py:141-153`) and keeps the better one (`lab.py:155-156`) | only name and questions may change; 5-trial cap; `promoted: False` (`lab.py:163`) | LLM-authored instructions reach the next LLM with no human review (P0-06) |
| B6 | provider-reported `model` | `extractor_key` (`engine.py:174`) → evidence label (`engine.py:226-228`) → gate | exact-name match (`registry.py:118`) | renamed weights served under a registered name earn `post_cutoff` (P0-37) |
| B7 | token usage | spend estimate (`daemon.py:360-374`) | input tokens are lower-bounded by the characters sent | low |
| B8 | features, action, model name | brief/web (escaped) and notifications | code-built | low |

### 5.3 MCP and web output reaching a host LLM

- **C1: excerpts.** Filing excerpts go out as `untrusted_filing_excerpts` with a note (`mcp_server.py:41-48`, `mcp_server.py:254-263`). They pass the verbatim check and a directive regex (`brief.py:53-66`, `brief.py:131`).
  - **The offline probe bypassed the regex** with Cyrillic homoglyphs, fullwidth letters and synonyms such as "purchase" and "acquire" (P0-08).
- **C2–C5: unlabelled external-derived strings.** These fields carry no untrusted label:
  - `event_id`, which includes the **filer-chosen exhibit filename** (`sec.py:42`, `sec.py:524`) or, for imports, any string;
  - `source_url`;
  - `resolved_model` (`brief.py:465`);
  - health errors (`daemon.py:537-545`).
- **`/api/brief.json`** serves quotes with no untrusted label (`web.py:286-290`).
- **No co-install warning (security, P0-08).** Neither `docs/mcp.md` nor the server instructions (`mcp_server.py:50-59`) warn against loading a broker MCP server with order tools in the same host. That is today's concrete path from filing text to an agent with write tools: C1 excerpts pass a regex that the probe bypassed, and the host agent could then call another server's order tools (N3, Do-NOT item 1).

### 5.4 §9 conformance

| §9 control | Status |
|---|---|
| Per-agent tool allowlists | Met for MCP: five read-only tools, and unknown tools are refused (`mcp_server.py:85-91`, `mcp_server.py:449-451`). A host that also loads a broker server is not covered (§5.3, P0-08) |
| Reject tool calls whose arguments derive from untrusted spans | Not applicable yet, because no write tool exists. ADR-0001 §D3 requires it before the propose profile |
| Tool-less quarantined readers | Largely met for extraction (`providers.py:330-337`, `local.py:215`, `providers.py:203-217`). Not met for the proposer (B5) |
| Verbatim span offsets | Missing (P0-39) |
| Unicode and hidden-text sanitization before the model | Missing (P0-05) |
| Sanitization before display, with diffs logged | Partial: five ad-hoc filters (`brief.py:70-75`, `notify.py:42-44`, `web.py:403`, `daemon.py:544`, `launchd.py:246`), no NFKC and no diff log |
| Provenance tags | Only on MCP excerpts (P0-08) |
| Red-team corpus in CI | Missing; three unit tests only (`tests/test_brief.py:121-160`, `tests/test_local.py:289-296`, `tests/test_mcp.py:613-633`) (P0-09) |
| Prompts logged, append-only | Partial. The raw response is chained; the prompt and template are neither stored nor part of the spec (`engine.py:110-123`, `providers.py:32-38`) (P0-07) |

## 6. Trial mechanisms

**Verdict.**
- Nothing counts trials against the gate. There is no DSR, no PBO, no placebo and no trial count; the scoreboard's output keys are at `evidence.py:235-250`.
- Only autoresearch records its trials, in `experiments` (`lab.py:80`, `lab.py:103`, `lab.py:253-268`, `lab.py:313-325`). Its budget is 5 trials and 4 proposals **per ledger** (`lab.py:16`, `lab.py:66-70`, `lab.py:245-251`).
- One ledger has one gate, pooled over every provider, strategy and calibrator (`evidence.py:209-210`). The `eligible` hook exists, but no caller passes it (`app.py:194`, `brief.py:412`, `web.py:292`).
- A byte copy of a ledger verifies (offline probe), because `GENESIS` is constant (`store.py:29`).

| # | Mechanism | Recorded? | Counted? | How it can inflate a false `supported` | Severity | Gap |
|---|---|---|---|---|---|---|
| T1 | Autoresearch / `experiment` (`lab.py:111-165`, `lab.py:168-326`) | yes, per ledger | budget only | the winner is chosen on development data (`lab.py:155-156`) and deployed with `--strategy`, with no parent-trial link (`engine.py:229-253`). Experiment forecasts run without a calibrator, so they are `WATCH` and never calls themselves (`lab.py:273-280`, `engine.py:201-203`) | M | P0-27 |
| T2 | Proposal slots (`lab.py:64-108`) | yes | budget only | only the best trial is reported, never the median (`lab.py:157-165`) | M | P0-28 |
| T3 | Strategy JSON (`common.py:81-140`) | only via the forecasts it produced | no | zero costs (P0-02); horizons, benchmarks and edges pool in one gate; a reworded question forces a fresh LLM sample | **H** | P0-02, P0-29 |
| T4 | Provider/model switching (`cli.py:216-236`, `config.py:24-25`) | per forecast; config changes are not ledgered (`daemon.py:295-302`) | no | forward calls from all providers pool under "first call wins" (`evidence.py:147-151`) | M | P0-27, P0-29 |
| T5 | Calibrator fits (`engine.py:380-394`) | fits are stored in `models`; which one was deployed when is not recorded | no | try many fits against `evaluate`, deploy the best | M | P0-27 |
| T6 | `evaluate` runs (`engine.py:397-425`, read-only at `cli.py:38`) | **no** | no | unlimited, invisible search | M | P0-27 |
| T7 | Multiple ledgers and homes (`paths.py:10-39`, `cli.py:63-69`) | **no** | no | discard failing copies; every per-ledger budget resets | **H** | P0-18, P0-27 |
| T8 | Backfills (`app.py:234-288`) | marked on disclosures; the run itself is not recorded | yes: `rules`/`post_cutoff` replays count | windows and symbols can be chosen with hindsight | **H** | P0-31 |
| T9 | Import + queue replay (`store.py:359-372`, `pipeline.py:102-103`) | the selection criteria are not | yes, as `no_model_knowledge` | import only filings followed by moves, then replay the queue | **H** | P0-31 |
| T10 | `--event` replays | yes, as `adhoc_replay` | excluded | none | L | — |
| T11 | Forward re-decisions (`pipeline.py:149-156`, `engine.py:184-186`) | yes | yes | a new config re-decides old forward filings. A later forward call replaces an earlier forward `WATCH`/`PASS` (`evidence.py:150-151`; pinned by `tests/test_evidence.py:299-339`) | M | P0-32 |
| T12 | `--retry-failed` (`pipeline.py:104-105`) | yes | spend only | repeat failures drop out non-randomly | L | — |
| T13 | Repeated looks at the gate (`evidence.py:157-182`) | **no** | no | each read is a fresh 90% test; status history is not recorded | M | P0-33 |
| T14 | Demo and synthetic (`jevtrader/demo.py`) | yes | never | none | L | — |
| T15 | Extraction rerolls (`engine.py:120-123`) | yes | — | identical specs are cached. Any spec change is a fresh sample | L | P0-40 |

Already right, and worth keeping:
- first-recorded selection within a ledger (`evidence.py:139-154`);
- autoresearch reserves a slot before any paid call and records rejected proposals (`lab.py:72-81`, `lab.py:91-107`);
- every forecast freezes its strategy, provider, model, calibrator and label basis (`engine.py:229-253`).

## 7. Tests, isolation guards and CI

### 7.1 Test inventory

There are 26 test files plus `tests/conftest.py`: 742 tests, 1313 subtests and 14,732 lines (14,612 in the test files and 120 in `conftest.py`), against 10,294 package lines. The suite runs in about 5 s with **95% line+branch coverage** (5,490 statements, 211 missed; 1,988 branches, 128 partial). Coverage is an A0-side measurement; the command and tool versions are under Method at the top, and A8 has not yet repeated it. The suite has no fixture files, no property-based tests and no markers.

| File | Tests | Covers |
|---|---:|---|
| `tests/test_mcp.py` | 72 | read-only stdio MCP, output filter, one real subprocess |
| `tests/test_bars.py` | 66 | URL allowlist, calendar, corporate actions, fetch timing |
| `tests/test_app.py` | 51 | setup, doctor, service wiring, backfill |
| `tests/test_daemon.py` | 50 | schedule, run records, spend cap, loop |
| `tests/test_ledger_chain.py` | 49 | hash chain, tamper cases, v1→v2 migration, concurrency |
| `tests/test_local.py` | 40 | loopback-only engines, retry, health |
| `tests/test_feeds.py` | 39 | Atom, daily index, poll, backfill |
| `tests/test_registry.py` | 39 | cutoffs, 92-day boundary, overrides, labels |
| `tests/test_brief.py` | 38 | cards, verbatim quotes, render |
| `tests/test_lab.py` | 34 | experiment and autoresearch protocol |
| `tests/test_store_market.py` | 31 | PIT put/get, snapshot, outcome |
| `tests/test_engine.py` | 28 | observe/train/settle/evaluate causality |
| `tests/test_sec.py` | 27 | SEC client, URL validation, collection |
| `tests/test_providers.py` | 22 | provider request/response contracts |
| `tests/test_evidence.py` | 22 | scoreboard, pinned gate fingerprint, t quantile |
| `tests/test_web.py` | 21 | loopback, Host allowlist, CSP |
| `tests/test_cli.py` | 20 | offline commands, full demo |
| `tests/test_launchd.py` | 16 | plist and launchctl argv (never executed) |
| `tests/test_pipeline.py` | 15 | observe queue |
| `tests/test_config.py` | 14 | config validation |
| `tests/test_research.py` | 12 | ridge, purged walk-forward |
| `tests/test_paper.py` | 12 | paper sizing and refusals |
| `tests/test_secrets.py` | 11 | Keychain via an injected runner |
| `tests/test_notify.py` | 7 | osascript argv |
| `tests/test_isolation.py` | 5 | the guards themselves |
| `tests/test_e2e.py` | 1 | daemon loop with fake SEC/Alpaca → web, MCP, verify |

Coverage misses that matter (from the same coverage run; the grep checks can be repeated without coverage):
- `paper.py` is at 85%. The risk-refusal branches `paper.py:139`, `:145`, `:155`, `:163` and `:168-169` are uncovered.
- `market.py` is at 85%. `import_bars` (`market.py:60-69`) is untested; grep finds no reference to `import_bars` or `import-bars` in `tests/`.
- `common.py` is at 87%. Most `validate_strategy` rejections are uncovered (`common.py:110`, `:116`, `:122`, `:125`, `:136`, `:138`, `:140`); the only test that calls it directly checks an accepted strategy (`tests/test_paper.py:37-40`).

**Missing entirely** (grep finds 0 hits for placebo, planted or shuffle in `tests/`):
- planted-signal tests;
- placebo tests;
- one-bar-shift tests;
- recorded EDGAR fixtures;
- golden old-ledger files (the v1 ledger is rebuilt in code, `tests/test_ledger_chain.py:40`).

### 7.2 Isolation guards (`tests/conftest.py:37-120`, self-tested in `tests/test_isolation.py:51-120`)

| Guard | Where |
|---|---|
| Keychain access refused | `tests/conftest.py:48-49` |
| launchctl refused | `tests/conftest.py:50` |
| `Popen` of `security`/`launchctl`/`osascript` refused | `tests/conftest.py:20`, `:53-63` |
| DNS and TCP connect only to loopback | `tests/conftest.py:66-92` |
| Daemon loop needs `sleep=`; capped at 1,000 ticks | `tests/conftest.py:95-112` |
| Temporary `HOME` and `JEVTRADER_HOME` | `tests/conftest.py:114-118` |
| Violations fail in teardown even when swallowed | `tests/conftest.py:39-43`, `:119-120` |

Gaps, not exploited by today's code (P0-10):
- **Wrappers and other spawn routes.** The `Popen` check looks only at argv[0] (`tests/conftest.py:24-28`), so `['/usr/bin/env','security',…]` and `['sh','-c','launchctl …']` pass. `os.system`, `posix_spawn` and `exec*` are not patched.
- **UDP.** `sendto` is unguarded.
- **Child interpreters** run without guards (`tests/test_mcp.py:889`).
- **Environment.** Variables are scrubbed per file, not globally.
- **Broker hosts.** There is no broker-host deny rule.

### 7.3 CI matrix (`.github/workflows/ci.yml`)

| Job | Runs on | What | Lines |
|---|---|---|---|
| lint | ubuntu, Python 3.14 | `ruff check`, `ruff format --check`, mypy error count ≤ 9 | `.github/workflows/ci.yml:17-41` |
| test | ubuntu with 3.11–3.14; macOS with 3.11 and 3.14 | `pytest -q -o faulthandler_timeout=300` | `.github/workflows/ci.yml:43-69` |
| package | ubuntu, Python 3.14 | build the wheel, install it in a clean venv, run the synthetic demo | `.github/workflows/ci.yml:71-93` |

Good: actions are pinned to SHAs (`ci.yml:22`, `ci.yml:25`), `permissions: contents: read` (`ci.yml:9-10`), `persist-credentials: false`, and Dependabot covers actions (`.github/dependabot.yml:4`).

Weak (P0-21, P0-22):
- **mypy ratchet.** It is a bare count, and `|| true` swallows mypy's exit code, so an INTERNAL ERROR passes (`ci.yml:36-41`).
- **Coverage and tooling.** Coverage is not measured, and mypy and pytest-cov are not in the dev extra (`pyproject.toml:15`).
- **Rules and warnings.** Ruff runs with its default rules (`pyproject.toml:29-31`), and there is no `filterwarnings` (`pyproject.toml:26-27`).
- **Timeouts.** `faulthandler_timeout` dumps stacks but does not exit, so a hang runs to the 15-minute job timeout (`ci.yml:46`, `ci.yml:67-69`).
- **Dependencies.** Test dependencies are unpinned (`ci.yml:66`).
- **Scanning.** There is no secret scanning, CodeQL or pip-audit.
- **Platforms.** The wheel smoke test runs only on ubuntu (`ci.yml:73`).

mypy 1.20.1 reports 9 errors, matching the baseline; `--strict` reports 441.

## 8. Invariants

Invariants I-1 to I-6 are the ones master prompt §1 protects: the four refusals, the hash-chained ledger, the evidence labels and the gate fingerprint. **They may change only through an ADR plus a migration under which `verify` passes on old ledgers.** I-7 to I-29 are principles the code enforces today. Changing them also needs an ADR, and every PR must list the invariants it touches.

| ID | Invariant | Enforced by | Pinned by | Known weak spots |
|---|---|---|---|---|
| I-1 | **R1: replays inside a training window never count** (`README.md:44`) | `registry.py:135-157`, `registry.py:20`, `registry.py:160-162`; `evidence.py:38-43`; `engine.py:276-277` | `tests/test_registry.py:30-43`, `:215-263`, `:367-411`; `tests/test_engine.py:177-213`; `tests/test_evidence.py:127-155` | P0-37, P0-31 |
| I-2 | **R2: no call counts before costs** (`README.md:45`) | `common.py:143-147`; `engine.py:205-212`; `evidence.py:66-68` | `tests/test_evidence.py:204-226`; `tests/test_research.py:167-178` | P0-02, P0-20, P0-30 |
| I-3 | **R3: history cannot be rewritten quietly** (`README.md:46`) | `store.py:31-77`, `store.py:188-206`, `store.py:224-312`, `store.py:314-349`; forward disclosures only from the collector (`store.py:366-368`) | `tests/test_store_market.py:82-168`; `tests/test_ledger_chain.py:297-449`, `:570-686`, `:749-802` | tamper-evident, not tamper-proof; no anchor; no ledger identity (P0-18) |
| I-4 | **R4: it does not trade** (`README.md:47`) | no broker code; Alpaca allowlist (`bars.py:68-88`); read-only MCP (`mcp_server.py:85-91`, `mcp_server.py:449-451`); `simulation_only` (`paper.py:119`); GET-only web (`web.py:326-328`) | `tests/test_bars.py:295-357`; `tests/test_mcp.py:489-500`; `tests/test_paper.py:33` | no whole-package no-order test; `paper-api` is allowlisted, and the data keys can probably place paper orders (P0-04); those keys are probably readable by any process of the user (P0-42). Phase 5 restates R4 through ADR-0001 §D2 |
| I-5 | Seven evidence labels; three count; label and basis are frozen at decision time | `registry.py:22-31`; `engine.py:226-243` | `tests/test_registry.py:30-43`, `:265-272`, `:318-323`; `tests/test_engine.py:177-203` | — |
| I-6 | The gate is fixed and fingerprinted (`c895567c…bbac`) | `evidence.py:24`, `evidence.py:208`, `evidence.py:238` | `tests/test_evidence.py:18-21`, `:103-115` | covers only 4 fields (P0-30) |
| I-7 | Only the live collector creates forward disclosures | `store.py:366-368`; `sec.py:489-492` | `tests/test_store_market.py:165-168`; `tests/test_sec.py:368-400` | `imported=False` is a plain keyword (`cli.py:291`) (P0-32) |
| I-8 | published ≤ first_seen; a forward first_seen is not in the future; the first observation is kept | `store.py:372-389` | `tests/test_store_market.py:129-217` | — |
| I-9 | No decision before first_seen; the live clock never decides historical text; a forward `decision_at` is taken after extraction | `engine.py:64-71`, `engine.py:184-186` | `tests/test_engine.py:113-120`, `:225-238`, `:342-354` | — |
| I-10 | The previous filing is earlier and was seen by the decision | `engine.py:101-109` | `tests/test_engine.py:122-141` | — |
| I-11 | The snapshot uses only bars available and closed by the decision, in the same mode, and not stale | `market.py:86-107` | `tests/test_store_market.py:283-331` | — |
| I-12 | Entry is at the next open after the decision; the label waits for every bar; an outcome is never replaced | `market.py:132-178`; `engine.py:314-326` | `tests/test_store_market.py:350-426`; `tests/test_engine.py:245-275` | — |
| I-13 | A calibrator trains only on labels before its cutoff, its cutoff is before the decision, and it never scores its own training events | `engine.py:78-92`, `engine.py:329-377`; `research.py:128-129` | `tests/test_engine.py:263-315`, `:541-582` | — |
| I-14 | Walk-forward purges training rows whose outcome is at or after each block cutoff | `research.py:288-300` | `tests/test_research.py:119-153` | — |
| I-15 | Forward bars are stored ≥ 20 min after close with receipt time; forward and historical bars never mix | `bars.py:43`, `bars.py:568-571`, `bars.py:577`, `bars.py:600-602`, `bars.py:615-622`; `market.py:51-55` | `tests/test_bars.py:921-1001`, `:1224-1243` | historical availability is at the close (P0-15) |
| I-16 | Backfilled availability is conservative and never forward | `feeds.py:245-261`, `feeds.py:458-470`, `feeds.py:559-563` | `tests/test_feeds.py:392-430`, `:654-698` | — |
| I-17 | Readers see only records visible at `as_of` | `evidence.py:209`, `evidence.py:222`; `brief.py:156-161` | `tests/test_evidence.py:178-202` | — |
| I-18 | Each event counts once; a replay never replaces a forward decision | `evidence.py:139-154` | `tests/test_evidence.py:299-388` | a forward `WATCH`/`PASS` can be replaced (P0-32) |
| I-19 | `eligible` can only narrow the evidence set | `evidence.py:204`, `evidence.py:210` | `tests/test_evidence.py:165-176` | — |
| I-20 | Synthetic data never counts | `evidence.py:39-40`; `registry.py:148-149`; `engine.py:89-90` | `tests/test_registry.py:186-197`; `tests/test_engine.py:567-582` | — |
| I-21 | Declared cutoffs can only be more conservative; Jev and rules cannot gain one | `registry.py:188-220` | `tests/test_registry.py:367-470` | — |
| I-22 | No Sharpe and no equity curve; net figures always include costs | `research.py:392-397`; `evidence.py:66-68` | `tests/test_research.py:167-178` | — |
| I-23 | Filing text sent to a local model stays on loopback | `local.py:73-94`, `local.py:141-170` | `tests/test_local.py:118-201` | — |
| I-24 | SEC: allowlisted URLs, no redirects, ≤ 5 req/s per process, and a declared (never fabricated) contact | `sec.py:121-156`, `sec.py:159-177`, `sec.py:194-201`; `config.py:21` | `tests/test_sec.py:162-180`, `:209-230`, `:425-474`; `tests/test_config.py:127-129` | per-process only (P0-13); setup asks for a name (P0-03); the contact can travel in argv (`cli.py:92`, suggested at `docs/data-and-ledger.md:72`) |
| I-25 | Keys never appear in config, plist, argv, ledger, logs, error text, prompts or MCP results | argv: `secrets.py:79-81`. Headers only: `bars.py:166-179`, `providers.py:153`. Error text: `secrets.py:41-46`, `bars.py:211-218`, `providers.py:163-169`, `daemon.py:537-545`. Plist: `launchd.py:206-211`. MCP: `mcp_server.py:181-183` | `tests/test_secrets.py:80-92`, `:94-112`, `:141-151`; `tests/test_bars.py:365-443`; `tests/test_providers.py:297-319`; `tests/test_launchd.py:140`; `tests/test_mcp.py:544-549`, `:657-662` | no test that a provider key never enters a prompt body; every key is exported into `os.environ` and inherited by child processes (`secrets.py:99-114`); the Keychain items are probably readable by any process of the user (unverified). See the §1.10 data-flow table and P0-42 |
| I-26 | LLM readers have no tools and return exactly four bounded numbers | `providers.py:203-217`, `providers.py:330-337`; `local.py:215` | `tests/test_providers.py:201-216` | — |
| I-27 | MCP is stdio and read-only and refuses unknown tools; the web view is loopback and GET/HEAD only | `mcp_server.py:85-91`, `mcp_server.py:449-451`; `web.py:323-328`, `web.py:417-418` | `tests/test_mcp.py:308-321`, `:489-500`; `tests/test_web.py` | — |
| I-28 | Tests cannot reach the network, Keychain, launchctl, osascript or the real home, and cannot run an unbounded daemon loop | `tests/conftest.py:37-120` | `tests/test_isolation.py:51-120` | P0-10 |
| I-29 | No personalized investment advice: every surface says research, not advice, and MCP tells its host to decline trading and personalized advice | `mcp_server.py:50-59`, `mcp_server.py:63`; `brief.py:36`, `brief.py:552`; `web.py:194`; `app.py:582` | `tests/test_mcp.py:145-147`, `:320`; `tests/test_web.py:187`; `tests/test_brief.py:369`, `:610` | nothing yet covers publishing to others or hosted mode (ADR-0001 §D10) |

## 9. Ranked tech-debt list

The priority score is Impact (1–5) × Ease (small = 3, medium = 2, large = 1). A high rank means "pick this up first", not "most important". The architectural blockers (ranks 19–20) rank low only because they need ADRs and migrations.

| Rank | Score | Debt | Evidence | Gap |
|---:|---:|---|---|---|
| 1 | 12 | Risk and strategy refusal branches have no tests | `paper.py:139`, `:145`, `:155`, `:163`, `:168`; `common.py:110-140`; `market.py:60-69` | P0-20 |
| 2 | 10 | No central sanitizer: five ad-hoc control-character filters | `brief.py:70-75`, `notify.py:42-44`, `web.py:403`, `daemon.py:544`, `launchd.py:246`; `engine.py:145-159` | P0-05 |
| 3 | 10 | No planted-signal, placebo, one-bar-shift or look-ahead tests | grep: 0 hits in `tests/` | P0-35 |
| 4 | 10 | Trial counting is ad hoc and per ledger | `lab.py:16`, `lab.py:66-70`; `engine.py:397-425` | P0-27 |
| 5 | 9 | CI gates are lax (coverage, mypy ratchet, rules, hang handling, pins, scanning) | `ci.yml:36-41`, `ci.yml:66`, `ci.yml:69`; `pyproject.toml:15`, `pyproject.toml:29-31` | P0-21, P0-22 |
| 6 | 9 | Isolation guard gaps | `tests/conftest.py:24-28`, `:53-63`, `:66-92`; `tests/test_mcp.py:889` | P0-10 |
| 7 | 9 | Four timestamp parsers and three `number` validators with different strictness | `common.py:22-31`, `common.py:46-55`; `research.py:37-46`; `sec.py:78-90` | P0-17 |
| 8 | 8 | `recorded_at` (transaction time) is outside the hash, yet migration order depends on it | `store.py:46`, `store.py:80-82`, `store.py:136`, `store.py:203` | P0-18 |
| 9 | 8 | Closed, version-locked strategy schema hashed into forecast identity | `common.py:92-99`; `engine.py:110-122`, `engine.py:213-222` | P0-40 |
| 10 | 8 | Fixed 8-slot positional feature vector, including a constant "spread" | `research.py:23-32`; `paper.py:63-67`; `market.py:118` | P0-40 |
| 11 | 8 | Monolith functions: `engine.observe` (214 lines, C901=31), `paper.plan_order` (202, C901=35), `lab.experiment` (159, C901=25) | `engine.py:42-255`; `paper.py:24-225`; `lab.py:168-326` | P0-38 |
| 12 | 6 | `daemon.Context` defaults skip `base_url` and `overrides` | `daemon.py:109`; `app.py:78-93` | P0-25 |
| 13 | 6 | 9 baseline mypy errors | `common.py:50`, `providers.py:216`, `paper.py:201`, `market.py:35-48`, `research.py:44`, `demo.py:20` | P0-23 |
| 14 | 6 | Two cost models | `common.py:143-147`; `paper.py:159-182`; `research.py:268-273` | P0-36 |
| 15 | 6 | Duplicated HTTP plumbing; per-process limiters; stale provider UA `jevtrader/0.1` | `sec.py:150`, `sec.py:159-177`; `bars.py:44`; `providers.py:156` vs `jevtrader/__init__.py:3` | P0-13 |
| 16 | 6 | 23 direct `utc_now()` calls, with no injectable clock in store, engine or market | e.g. `store.py:203`, `engine.py:69`, `engine.py:186`, `market.py:52` | P0-17 |
| 17 | 6 | Fixture builders duplicated across tests; no golden ledgers | `tests/test_cli.py:22`, `tests/test_brief.py:714`, `tests/test_pipeline.py:16`; `tests/test_ledger_chain.py:40` | P0-19 |
| 18 | 6 | Secrets and service are macOS-only | `secrets.py:14-18`; `jevtrader/launchd.py` | P0-24 |
| 19 | 5 | No indexed as-of read path: 26 `ledger.all(kind)` full scans, each re-hashing every row, with PIT filters repeated in each caller | `store.py:325-349`; `engine.py:101-108`; `market.py:89-96`; `evidence.py:209`; `pipeline.py:151-155` | P0-17 |
| 20 | 4 | No typed domain models or ports (bare `dict` everywhere; no Protocol or TypedDict) | `paper.py:31`, `paper.py:79-99`; `bars.py:36-38` | P0-23 |
| 21 | 4 | Tests reach into at least 26 private names: 73 direct references, and about 90 counting string-based `patch.object` and `setattr` calls | `tests/test_providers.py`, `tests/test_bars.py`, `tests/test_local.py` | P0-26 |
| 22 | 4 | CLI dispatch is a 24-branch if-chain | `cli.py:239-391`, `cli.py:394-447` | tracked here; to be filed when the Phase 4 service layer starts |

## 10. Gap register (issues to file)

Each row below becomes one GitHub issue, with its evidence and acceptance criteria. Record the GitHub number next to each ID when it is filed. The labels come from the program's label set.

| ID | Title | Labels | Owner | Where in this audit |
|---|---|---|---|---|
| P0-01 | Correct docs and tool wording found by the Phase 0 audit | phase-0, docs | A0 | §3 (`sec.py:96`), §6 T11 |
| P0-02 | Enforce a cost floor: zero-cost strategies count as net-of-cost evidence | phase-1, validation | A3 | §2.2, §6 T3 |
| P0-03 | Setup asks for the owner's name for SEC; ask for a declared contact and recommend an alias | phase-1, security, ux | A6 + A7 | §1.10 (wording sites and data-flow table) |
| P0-04 | Guard test for "no order route anywhere"; data-only Alpaca credentials | phase-1, security, execution | A7 | §1.2, §8 I-4, §11 open items |
| P0-05 | Central sanitizer before any LLM and any display | phase-1, security | A7 | §5.1 E1, §5.4, §9 rank 2 |
| P0-06 | Autoresearch runs LLM-written questions without human review | phase-1, security | A2 + A7 | §5.1 E3, §5.2 B5 |
| P0-07 | Provider prompts: separate channels, version the template, log prompts | phase-1, security | A2 + A7 | §5.1 E2, E4, E6; §5.4 |
| P0-08 | Harden external-derived text reaching MCP and web hosts; warn against co-installed broker servers | phase-1, security, ux | A7 + A5 | §5.1 E7, §5.3 |
| P0-09 | Red-team injection corpus in CI | phase-1, security | A7 + A8 | §5.4 |
| P0-10 | Close isolation-guard gaps before any broker code exists | phase-1, security, tech-debt | A8 + A7 | §7.2 |
| P0-11 | Real EDGAR acceptance fixtures, including pre-2020 and after-17:30 cases | phase-1, pit | A1 | §3 |
| P0-12 | Store a verified acceptance instant; fix `after_hours` and `published_at` | phase-1, pit | A1 | §3 |
| P0-13 | Global cross-process rate limiting for SEC and Alpaca | phase-1, pit | A1 | §1.1, §4, §9 rank 15 |
| P0-14 | Reconcile should recover missed qualifying filings | phase-1, pit | A1 | §1.1 |
| P0-15 | Bar availability parity (historical vs forward); guard `daily_bars` | phase-1, pit | A1 | §4, §8 I-15 |
| P0-16 | Security master: point-in-time tickers, delistings, late corporate actions | phase-1, pit | A1 | §1.1 (`feeds.py:6-8`) |
| P0-17 | Bitemporal `as_of(t)` read path, one timestamp type, injectable clock | phase-1, pit, tech-debt | A1 | §9 ranks 7, 16, 19 |
| P0-18 | Ledger schema v3: per-ledger identity and hashed `recorded_at` (ADR + migration) | phase-1, pit, validation | A1 + A0 | §1.7, §6 T7, §9 rank 8 |
| P0-19 | Golden v1/v2 ledger files and shared test fixtures | phase-1, tech-debt | A8 | §7.1, §9 rank 17 |
| P0-20 | Pin engine thresholds and the risk/strategy refusal branches with tests | phase-1, validation, tech-debt | A8 | §2.2, §7.1, §9 rank 1 |
| P0-21 | CI merge gates: coverage floor, exact mypy baseline, hang exit, rules | phase-1, packaging, tech-debt | A6 + A8 | §7.3, §9 rank 5 |
| P0-22 | Secret scanning, dependency audit and pinned test dependencies in CI | phase-1, security, packaging | A7 + A6 | §7.3 |
| P0-23 | Fix the 9 mypy errors; typed contracts; strict typing on the core | phase-1, tech-debt | A0 | §7.3, §9 ranks 13, 20 |
| P0-24 | Cross-platform secret store and service (libsecret, Windows, Docker, systemd) | phase-1, packaging, security | A6 + A7 | §9 rank 18 |
| P0-25 | `daemon.Context` defaults silently skip config-aware wiring | phase-1, tech-debt | A0 | §2.1, §9 rank 12 |
| P0-26 | Public seams for internals that tests patch | phase-1, tech-debt | A8 | §9 rank 21 |
| P0-27 | Global append-only trial registry spanning ledgers | phase-2, validation | A3 | §6 T1–T7 |
| P0-28 | Reports: trial count, DSR, PBO, median trial, placebo, leak-audit trigger | phase-2, validation | A3 + A8 | §6 T1, T2, T13 |
| P0-29 | Per-hypothesis gates instead of one pooled gate per ledger | phase-2, validation | A3 | §6 T3, T4 |
| P0-30 | Gate v2: the fingerprint covers the full evaluation spec | phase-2, validation | A3 + A0 | §2.5 |
| P0-31 | Replays of imported or backfilled cohorts count without pre-registration | phase-1, validation, pit | A3 | §6 T8, T9 |
| P0-32 | Forward decisions: bound filing age; the first forward decision is final | phase-2, validation, pit | A3 | §6 T11, §8 I-18 |
| P0-33 | Record gate status over time; a sequential rule for repeated looks | phase-2, validation | A3 | §6 T13 |
| P0-34 | Overlap-aware interval (HAC or block bootstrap) | phase-2, validation | A3 | §2.5 |
| P0-35 | Look-ahead suite: planted leak, one-bar shift, placebo, LAP probe | phase-2, validation | A8 | §7.1, §9 rank 3 |
| P0-36 | One versioned cost model used by engine, evidence, research and paper | phase-2, validation, tech-debt | A3 | §2.2, §9 rank 14 |
| P0-37 | Bind local model identity to a weights digest | phase-2, validation, security | A3 + A7 | §5.2 B6, §8 I-1 |
| P0-38 | Split the monolith functions along signal and risk seams | phase-3, tech-debt | A2 + A3 + A4 | §9 rank 11 |
| P0-39 | Span evidence and cross-checks before LLM features count or train | phase-3, security, validation | A2 + A7 | §5.2 B1–B4, §5.4 |
| P0-40 | Versioned hypothesis spec and named, typed features | phase-3, tech-debt | A2 | §1.4, §9 ranks 9, 10 |
| P0-41 | Cards should show the decision that counts, not the latest forecast | phase-4, ux | A5 | §1.9 |
| P0-42 | Keep broker keys out of reach of agents, LLM hosts and child processes | phase-1, security, execution | A7 + A4 | §1.10 data-flow table, §8 I-25, ADR-0001 §D2 |

Each issue body carries the `path:line` evidence and the acceptance criteria. The last column points to the section of this audit that holds the evidence: §9 ranks refer to the tech-debt table, `T`*n* to the trial mechanisms in §6, `E`/`B`/`C`*n* to the LLM boundary rows in §5, and `I-`*n* to the invariants in §8.

## 11. Acceptance

Master prompt §2 accepts Phase 0 when it is **"reviewed by Security and QA; every item has evidence or a filed issue"**.

| # | Checklist item | Evidence | Status | Gap issues |
|---|---|---|---|---|
| 1 | Module map (collectors, bars, providers, features, calibrator, filters, ledger v2, gate, brief/serve/MCP, setup/launchd, config/strategy) | §1 | Evidence complete | P0-03, P0-42 |
| 2a | Daemon passes the calibrator | §2.1 | Confirmed | P0-25 |
| 2b | Cost model location | §2.2 | Located; floor missing | P0-02, P0-36 |
| 2c | 92-day logic | §2.3 | Confirmed | P0-37 |
| 2d | 7 labels | §2.4 | Confirmed | P0-31 |
| 2e | Gate statistics | §2.5 | Confirmed | P0-30, P0-33, P0-34 |
| 3 | EDGAR acceptanceDateTime, with real fixtures | §3 | Partly met; fixtures missing | P0-11, P0-12 |
| 4 | Alpaca `feed=sip`, `end` ≥ 15 min | §4 | Met in product paths | P0-15 |
| 5 | External text → LLM; LLM output → decisions | §5 | Inventoried | P0-05–P0-09, P0-37, P0-39 |
| 6 | Trial mechanisms and counting | §6 | Inventoried | P0-27–P0-33 |
| 7 | Test inventory, isolation guards, CI matrix | §7 | Inventoried | P0-10, P0-19–P0-23 |
| 7b | Ranked tech-debt list | §9 | Done | — |
| 7c | Invariants list | §8 | Done | — |
| 8 | Outputs: CLAUDE.md, AGENTS.md, ADR-0001, dependency graph | [CLAUDE.md](../../CLAUDE.md), [AGENTS.md](../../AGENTS.md), [ADR-0001](../adr/0001-evidence-engine-architecture.md), [graph](dependency-graph.md) | Drafted | — |

**Open items A0 could not close:**
- The WebFetch-derived acceptance values in §3 need the owner's browser confirmation (P0-11).
- It is unverified whether Alpaca revises daily SIP bars after 16:20 ET (P0-15).
- It is unverified whether the Alpaca keys used for data are trade-capable, and whether Alpaca can scope a key pair to data only (P0-04).
- It is unverified whether Keychain items written with `/usr/bin/security` can be read by any process of the user without a prompt. Checking it needs the real Keychain, so only the owner can (P0-42).

**Sign-off.** Phase 0 is accepted when all of the following are true:
- [ ] All 42 gap-register rows are filed as GitHub issues, and their numbers are recorded in §10.
- [ ] **A7 Security** has reviewed §1.10, §5, §7.2, §8 (I-4, I-23 to I-29), P0-03 to P0-10, P0-22, P0-24, P0-37, P0-39, P0-42 and ADR-0001 §D2 to §D4 and §D10.
- [ ] **A8 QA** has reviewed §2, §6, §7, §8, P0-11, P0-19 to P0-21 and P0-27 to P0-35, confirmed the test counts by re-running the suite, and confirmed the §7.1 coverage figures by re-running the coverage command under Method.
- [ ] **The owner** has moved ADR-0001 from Proposed to Accepted.

## Issue map

Every gap above is filed as a GitHub issue:

| Gap | Issue | Phase | Title |
|---|---|---|---|
| P0-01 | [#5](https://github.com/zd87pl/jevtrader/issues/5) | phase-0 | Correct docs and tool wording found by the Phase 0 audit |
| P0-02 | [#6](https://github.com/zd87pl/jevtrader/issues/6) | phase-1 | Enforce a cost floor: zero-cost strategies count as net-of-cost evidence |
| P0-03 | [#7](https://github.com/zd87pl/jevtrader/issues/7) | phase-1 | Setup asks for the owner's name for SEC; ask for a declared contact and recommend an alias |
| P0-04 | [#8](https://github.com/zd87pl/jevtrader/issues/8) | phase-1 | Guard test for 'no order route anywhere'; data-only Alpaca credentials |
| P0-05 | [#9](https://github.com/zd87pl/jevtrader/issues/9) | phase-1 | Central sanitizer before any LLM and any display |
| P0-06 | [#10](https://github.com/zd87pl/jevtrader/issues/10) | phase-1 | Autoresearch runs LLM-written questions without human review |
| P0-07 | [#11](https://github.com/zd87pl/jevtrader/issues/11) | phase-1 | Provider prompts: separate channels, version the template, log prompts |
| P0-08 | [#12](https://github.com/zd87pl/jevtrader/issues/12) | phase-1 | Harden external-derived text reaching MCP and web hosts; warn against co-installed broker servers |
| P0-09 | [#13](https://github.com/zd87pl/jevtrader/issues/13) | phase-1 | Red-team injection corpus in CI |
| P0-10 | [#14](https://github.com/zd87pl/jevtrader/issues/14) | phase-1 | Close isolation-guard gaps before any broker code exists |
| P0-11 | [#15](https://github.com/zd87pl/jevtrader/issues/15) | phase-1 | Real EDGAR acceptance fixtures, including pre-2020 and after-17:30 cases |
| P0-12 | [#16](https://github.com/zd87pl/jevtrader/issues/16) | phase-1 | Store a verified acceptance instant; fix after_hours and published_at |
| P0-13 | [#17](https://github.com/zd87pl/jevtrader/issues/17) | phase-1 | Global cross-process rate limiting for SEC and Alpaca |
| P0-14 | [#18](https://github.com/zd87pl/jevtrader/issues/18) | phase-1 | Reconcile should recover missed qualifying filings |
| P0-15 | [#19](https://github.com/zd87pl/jevtrader/issues/19) | phase-1 | Bar availability parity (historical vs forward); guard daily_bars |
| P0-16 | [#20](https://github.com/zd87pl/jevtrader/issues/20) | phase-1 | Security master: point-in-time tickers, delistings, late corporate actions |
| P0-17 | [#21](https://github.com/zd87pl/jevtrader/issues/21) | phase-1 | Bitemporal as_of(t) read path, one timestamp type, injectable clock |
| P0-18 | [#22](https://github.com/zd87pl/jevtrader/issues/22) | phase-1 | Ledger schema v3: per-ledger identity and hashed recorded_at (ADR + migration) |
| P0-19 | [#23](https://github.com/zd87pl/jevtrader/issues/23) | phase-1 | Golden v1/v2 ledger files and shared test fixtures |
| P0-20 | [#24](https://github.com/zd87pl/jevtrader/issues/24) | phase-1 | Pin engine thresholds and the risk/strategy refusal branches with tests |
| P0-21 | [#25](https://github.com/zd87pl/jevtrader/issues/25) | phase-1 | CI merge gates: coverage floor, exact mypy baseline, hang exit, rules |
| P0-22 | [#26](https://github.com/zd87pl/jevtrader/issues/26) | phase-1 | Secret scanning, dependency audit and pinned test dependencies in CI |
| P0-23 | [#27](https://github.com/zd87pl/jevtrader/issues/27) | phase-1 | Fix the 9 mypy errors; typed contracts; strict typing on the core |
| P0-24 | [#28](https://github.com/zd87pl/jevtrader/issues/28) | phase-1 | Cross-platform secret store and service (libsecret, Windows, Docker, systemd) |
| P0-25 | [#29](https://github.com/zd87pl/jevtrader/issues/29) | phase-1 | daemon.Context defaults silently skip config-aware wiring |
| P0-26 | [#30](https://github.com/zd87pl/jevtrader/issues/30) | phase-1 | Public seams for internals that tests patch |
| P0-27 | [#31](https://github.com/zd87pl/jevtrader/issues/31) | phase-2 | Global append-only trial registry spanning ledgers |
| P0-28 | [#32](https://github.com/zd87pl/jevtrader/issues/32) | phase-2 | Reports: trial count, DSR, PBO, median trial, placebo, leak-audit trigger |
| P0-29 | [#33](https://github.com/zd87pl/jevtrader/issues/33) | phase-2 | Per-hypothesis gates instead of one pooled gate per ledger |
| P0-30 | [#34](https://github.com/zd87pl/jevtrader/issues/34) | phase-2 | Gate v2: the fingerprint covers the full evaluation spec |
| P0-31 | [#35](https://github.com/zd87pl/jevtrader/issues/35) | phase-1 | Replays of imported or backfilled cohorts count without pre-registration |
| P0-32 | [#36](https://github.com/zd87pl/jevtrader/issues/36) | phase-2 | Forward decisions: bound filing age; the first forward decision is final |
| P0-33 | [#37](https://github.com/zd87pl/jevtrader/issues/37) | phase-2 | Record gate status over time; a sequential rule for repeated looks |
| P0-34 | [#38](https://github.com/zd87pl/jevtrader/issues/38) | phase-2 | Overlap-aware interval (HAC or block bootstrap) |
| P0-35 | [#39](https://github.com/zd87pl/jevtrader/issues/39) | phase-2 | Look-ahead suite: planted leak, one-bar shift, placebo, LAP probe |
| P0-36 | [#40](https://github.com/zd87pl/jevtrader/issues/40) | phase-2 | One versioned cost model used by engine, evidence, research and paper |
| P0-37 | [#41](https://github.com/zd87pl/jevtrader/issues/41) | phase-2 | Bind local model identity to a weights digest |
| P0-38 | [#42](https://github.com/zd87pl/jevtrader/issues/42) | phase-3 | Split the monolith functions along signal and risk seams |
| P0-39 | [#43](https://github.com/zd87pl/jevtrader/issues/43) | phase-3 | Span evidence and cross-checks before LLM features count or train |
| P0-40 | [#44](https://github.com/zd87pl/jevtrader/issues/44) | phase-3 | Versioned hypothesis spec and named, typed features |
| P0-41 | [#45](https://github.com/zd87pl/jevtrader/issues/45) | phase-4 | Cards should show the decision that counts, not the latest forecast |
| P0-42 | [#46](https://github.com/zd87pl/jevtrader/issues/46) | phase-1 | Keep broker keys out of reach of agents, LLM hosts and child processes |
