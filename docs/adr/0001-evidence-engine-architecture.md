# ADR-0001: Evidence Engine architecture

| | |
|---|---|
| Status | **Proposed** (2026-09-29). Becomes Accepted when the owner signs off after A7 (Security) and A8 (QA) review |
| Deciders | Owner (accepts), A0 Lead Architect (author) |
| Reviewers | A7 Security & Compliance (§D2 to §D4, §D10), A8 QA & Evaluation (§D5 to §D7) |
| Evidence | [Phase 0 audit](../audit/phase0.md) at `fa0c0d3`. Every `path:line` below refers to that tree |
| Supersedes | nothing. This is the first ADR |

## Context

**What exists today.** JEVTrader is a single Python package:
- 27 modules and 10,294 lines, with numpy as the only runtime dependency (`pyproject.toml:12`).
- It collects selected SEC 8-Ks and Alpaca daily bars, then extracts four text features with a word list, a local model, Jev or OpenAI.
- It freezes each decision in an append-only SQLite ledger with a SHA-256 hash chain (`jevtrader/store.py:28`, `store.py:31-77`, `store.py:80-82`, `store.py:224-312`).
- It scores matured calls against one fingerprinted gate (`jevtrader/evidence.py:24`, `evidence.py:238`).

It makes four promises, the refusals at `README.md:44-47`:
- R1: no counting replays inside a training window.
- R2: no counting before costs.
- R3: no quiet rewrites of history.
- R4: no trading.

R4 holds today because no broker code exists. The only broker hosts are Alpaca's data and calendar routes (`jevtrader/bars.py:35-36`, `bars.py:68-88`). The MCP tools are all read-only (`jevtrader/mcp_server.py:85-91`, `mcp_server.py:449-451`), the web view accepts GET and HEAD only (`jevtrader/web.py:326-328`), and paper plans are `simulation_only` (`jevtrader/paper.py:119`).

**What the program asks for.** Master prompt §3 describes an Evidence Engine in three zones:
- **Untrusted:** collectors, a raw store, a sanitizer and tool-less reader LLMs.
- **Trusted:** a bitemporal point-in-time store, a hypothesis library, a signal engine, a trial registry, a validator, a forecast ledger, a simulator, a risk kernel and an execution service.
- **Interaction:** an orchestrator, chat, MCP and the brief, gated by human approval.

**What Phase 0 found** ([audit §0](../audit/phase0.md#0-summary)). The core is sound and well tested: 742 tests and 1313 subtests, fully offline. Several parts of the target do not exist yet:
- **Trials.** No trial counting, DSR or PBO, and one pooled gate per ledger (audit §6).
- **Sanitizing and spans.** No sanitizer before any model, and no span evidence (audit §5).
- **Point-in-time reads.** No indexed as-of read path; every read is a full-table scan (`store.py:325-349`).
- **Costs.** No cost floor (`jevtrader/common.py:123`), and two different cost formulas (`common.py:143-147`, `paper.py:159-182`).
- **Ledger identity.** `GENESIS` is the same constant in every ledger, so a copied ledger verifies (`store.py:29`).
- **Contracts.** No typed contracts between modules.
- **Secrets.** Every known key is exported into the process environment, and the Keychain items are probably readable by any process of the user (audit §1.10, P0-42).

**Owner decisions that override the master prompt:**
- **O1.** Every live order needs per-order human approval in the app's own UI. The "live-bounded-auto" rung is dropped. Paper-auto is allowed.
- **O2.** The name stays "jevtrader".
- **O3.** The owner's name and email are never sent to SEC or any other service. No fabricated User-Agent is ever used. The product sends a contact the user declares in config, and the docs recommend a dedicated alias.
- **O4.** The assistant never places orders, paper or live. Broker code is tested with mocks and recorded fixtures. Live-paper contract tests run only behind environment variables that the owner sets and runs.

## Decision

### D1. Grow the existing Python core into a modular monolith; do not rewrite it

- **Keep Python, the ledger and the test suite.** New components are subpackages of `jevtrader/` so they ship in the wheel (`pyproject.toml:20-21`), for example `jevtrader/collectors/`, `jevtrader/pit/` and `jevtrader/validate/`. Non-Python assets live at the repo root: `ui/` (TypeScript), `deploy/` and `evals/`.
- **Contracts come first.** Typed schemas live in `jevtrader/contracts/` as TypedDicts or frozen dataclasses, with JSON Schema exported for non-Python clients. Each schema gets a contract test in `tests/contracts/`. A contract merges before any implementation that depends on it.
- **Trust zones are enforced by imports.** An import-boundary test (stdlib `ast`, owned by A8) fails when:
  - untrusted-zone code (collectors, sanitizer, readers) imports risk or execution;
  - execution or risk imports a provider or any LLM client;
  - the interaction layer imports a ledger write path.
- **Modules move by strangler extraction.** A module moves into its component behind a re-export shim. The move PR changes no behaviour and no test, and callers migrate in later PRs. See the migration path below.
- **Only the execution service is a separate process (D2).** Everything else stays in-process, so `pipx install` and `docker compose up` remain one command.

### D2. Execution: the research core never trades, and every live order needs a human

This restates refusal R4 for Phase 5. **Until that ADR is accepted, R4 stays exactly as written at `README.md:47`.**

- **Research core.** Collectors, readers, the signal engine, validation and every LLM-facing surface have no order path. An A7 guard test asserts that the package contains no order, position-changing or account-changing endpoint outside `jevtrader/execution/` (P0-04).
- **Execution service** (Phase 5, A4).
  - It is a separate process. It talks to brokers only through one `BrokerPort` interface (Alpaca and IBKR adapters). Paper is the default.
  - It uses idempotent client order ids, fill reconciliation and a kill switch.
  - **It alone holds the trading keys and the key that verifies approval tokens.** The Phase 5 ADR must name the mechanism that keeps them unreadable by the core, by coding agents and by every process that hosts or calls an LLM. The candidates are a separate OS user with its own keychain, a Keychain access-control list bound to a signed execution binary, or container secrets mounted only into the execution container. Trading keys are never exported into the environment of such a process.
  - **Today's secret store cannot provide this** (P0-42):
    - Items are written with `/usr/bin/security` (`jevtrader/secrets.py:79-81`) and read back with `security find-generic-password -w` (`secrets.py:50`). Such items probably list `security` itself as a trusted application, so any process running as the user, a coding agent included, can probably read them without a prompt. This is unverified: nothing here touched the real Keychain.
    - `export_to_environ` copies every known key into `os.environ` (`secrets.py:99-114`). The key-using commands call it (`jevtrader/cli.py:55-58`, `cli.py:492-493`), and so does `doctor` (`jevtrader/app.py:363`, `app.py:476`). Child processes inherit that environment, for example the notifier's `osascript` (`jevtrader/notify.py:32-35`).
  - **It verifies every approval token itself:** the signature, the hash of the exact ticket, single use and expiry. It never trusts a verdict from the core.
  - **It re-enforces the risk-kernel limits in its own process** before every order. The kernel is a pure library that both processes import. A check in the core only refuses early in the UI and is never the binding check.
  - **Exclusions** (master prompt §1 and the Do-NOT list). The kernel refuses order patterns that could be manipulative: wash trades, orders that would cross the account's own resting orders, and quote-and-cancel bursts (spoofing, layering). No strategy may use material non-public information.
- **Promotion ladder: research → forward-paper → paper-auto → live-approval.** There is no live-bounded-auto rung (O1).
  - **Every promotion is a user-signed ledger record.** This covers the step into paper-auto as well as the step into live-approval (master prompt §1 and §5). The user signs with the same second factor as approvals. Demotions are automatic (drawdown > 2σ of the paper expectation, or a gate change). They are recorded, and they need no signature.
  - **Paper-auto:** only for strategies whose pre-registered gate is `supported`, and always inside risk-kernel limits. It uses **paper-only credentials** held by the execution service. The paper adapter cannot reach a live host, and live trading uses separate keys.
  - **Live-approval:** every live order needs a **per-order approval token** minted by the app's own UI after a second factor. The token is:
    - bound to a hash of the exact ticket (account, symbol, side, quantity, order type, limit, expiry);
    - single-use and short-lived.
  - **The second factor needs the user's physical presence**, for example WebAuthn user verification with Touch ID or a security key. A typed code or a click is not enough, because an agent with browser or computer-use tools could supply either.
  - **The approval route checks** authentication, CSRF, `Origin` and `Host`, and binds to loopback (as the web view checks `Host` today, `jevtrader/web.py:323-325`).
  - **No standing, batch or delegated live approvals.**
- **LLM tools.**
  - No LLM or agent gets a tool that approves or places an order.
  - `propose_trade_ticket` exists only in a separate, opt-in MCP profile. It takes a forecast id, not free-form trade fields. The symbol, side and size come from that code-produced forecast and from the risk kernel (D3). It creates a pending ticket that the risk kernel checks and a human approves.
  - The read-only MCP profile never exposes it.
- **Coding agents never place orders (O4).** Broker code is tested with mocks and recorded fixtures. Live-paper contract tests are skipped unless an opt-in environment variable is set, and only the owner runs them.
- **Market-data credentials are separate from trading credentials.** Today bars and the calendar use the account keys of an Alpaca paper account (`bars.py:35-38`), and `paper-api.alpaca.markets` is allowlisted for `/v2/calendar` (`bars.py:36`, `bars.py:87`). Those keys can probably place paper orders. It is unverified whether Alpaca can scope a key pair to data only, so P0-04 checks that first.

### D3. Untrusted text never reaches an instruction channel or an agent with write tools

The untrusted-text flow in the target:
1. **Raw store.** Immutable and content-hashed, with `first_seen` recorded. **None exists today.**
   - The ledger keeps only parsed text, cut to 200,000 characters (`jevtrader/sec.py:37`, `sec.py:311-321`, `sec.py:531`). The text of hidden HTML elements is already merged into it. Only the acceptance string is kept raw (`sec.py:542`).
   - So existing records cannot be re-sanitized for hidden HTML.
   - From Phase 1, the original bytes of every fetched document are content-hashed into the raw store. Ledger text written before the sanitizer is marked legacy and unsanitized.
2. **Sanitizer** (A7). Versioned. It applies NFKC, a confusables skeleton, zero-width, bidi and control stripping, removal of hidden HTML (`display:none`, `hidden`, `ix:hidden`, `<head>`, `<template>`) and section parsing. The raw text stays unchanged; the sanitized text and a diff are stored next to it. Today only `script/style/noscript` is dropped (`sec.py:265`, `sec.py:311-321`) (P0-05).
3. **Quarantined readers** (A2). No tools. They emit only schema-validated values plus verbatim span offsets. Today's extractors already have no tools and return four bounded numbers (`jevtrader/providers.py:203-217`, `providers.py:330-337`, `jevtrader/local.py:215`), but record no spans (P0-39).

Rules around that flow:
- **Prompt text is trusted only after human approval.** Prompt templates and question sets are versioned into the extraction spec. LLM-written questions are drafts until a human approves the diff (P0-06, P0-07).
- **Orchestrator agent, by profile.**
  - **Read-only profile** (read tools only). It may receive verbatim filing excerpts, as MCP clients do today, but only as provenance-tagged data under an untrusted label (`jevtrader/mcp_server.py:41-48`), never in its instruction channel.
  - **Propose profile** (read tools plus `propose_trade_ticket`, a write tool). It sees **only quarantined typed outputs:** numbers, enums, record ids and span offsets. It never sees verbatim excerpts or any other external-derived free text, such as filer filenames, source URLs, provider-reported model names or error text. Excerpts stay in read-only sessions. Master prompt §1 and Do-NOT item 1 require this split.
  - **Ticket arguments come from code.** `propose_trade_ticket` takes a forecast id. The symbol, side and size are read from that code-produced forecast and from the risk kernel.
  - **Tool calls whose arguments derive from untrusted spans are rejected** (master prompt §9). The tool layer tags every value it hands an agent with its provenance. It refuses, and logs, any argument that is neither a code-produced id nor an enum value, or that matches untrusted text.
- **Provenance on every external-derived field.** This includes text that reaches MCP or web hosts (P0-08).
- **Red-team corpus in CI** (P0-09). 100% of its cases must yield no ticket and no state change, with one exception. The ledger must record what the reader returned (R3), so an extraction record may be written. That record must be **flagged as quarantined**, and it is then excluded from counting, from training and from paper plans. The corpus asserts the flag and each exclusion.
  - This deviates, on purpose, from the literal "no state change" of master prompt §9. It keeps §8's requirement that the test "flags the item".
  - Without the flag, an adversarial but in-range extraction is exactly the poisoning path in audit §5.2 (rows B1 to B4).

### D4. Secrets, identity and outbound access

- **Secrets** live in the OS secret store:
  - today, the macOS Keychain (`jevtrader/secrets.py:14-18`);
  - planned, libsecret, Windows Credential Manager and Docker secrets (P0-24).

  Secrets never appear in config, plists, argv, the ledger, logs, error text, prompts or MCP results (audit invariant I-25).
- **Where the Keychain is read today.** More commands than the ones that need keys read it:
  - the key-using commands (`jevtrader/cli.py:55-58`) export every known key into the environment (`cli.py:492-493`);
  - `doctor` does the same (`jevtrader/app.py:363`, `app.py:476`), and it also runs `launchctl print` (`app.py:367`, `app.py:493`, `jevtrader/launchd.py:137`);
  - `setup` reads keys (`app.py:758`) and writes them through stdin (`app.py:775`, `secrets.py:79-81`).

  The secrets and identity data flow is in [audit §1.10](../audit/phase0.md#110-setup-launchd-daemon-and-secrets). Trading keys need stricter isolation than this (D2, P0-42).
- **SEC identity** (O3):
  - The User-Agent is a contact the user declares in config. There is no default (`jevtrader/config.py:21`), and one containing an email is required (`sec.py:194-201`).
  - It is never fabricated, and tooling never fills it in with the owner's identity.
  - Setup must ask for a "contact for SEC" and recommend a dedicated alias, instead of "your name and email" (`jevtrader/app.py:584-588`) (P0-03).
  - The contact belongs in config. Today's docs also suggest passing it as `--user-agent` (`jevtrader/cli.py:92`, `docs/data-and-ledger.md:72`), where `ps` shows it to every local process.
  - One-off sec.gov checks during development use WebFetch or the in-app browser, never a scripted client with an invented header.
- **Rate limits are global.** SEC stays at or below 10 requests per second, and Alpaca within its tier, **across processes**. Today the limiter is per process (`sec.py:41`, `sec.py:175-177`) (P0-13).
- **Least privilege.**
  - Broker scopes are read-only by default; trade scope is requested only at promotion.
  - Every local server binds to loopback, as `web.py:417-418` does today.
  - Any UI route that changes state needs auth and CSRF protection.

### D5. Point-in-time data is bitemporal; the SQLite ledger stays the system of record

- **Timestamps.** Every stored fact carries:
  - `event_time`, `first_seen_time` and `knowledge_time`;
  - the transaction time `recorded_at`, which moves inside the hash in ledger schema v3 (P0-18). Today it sits outside the link hash (`store.py:46`, `store.py:80-82`).
- **Reads.** Decision code reads only through `as_of(t)`, which never returns a row whose `knowledge_time` is after `t` (P0-17). A property test proves this.
- **Ledger identity.** Schema v3 gives each ledger a creation record and nonce. Old v1 and v2 ledgers must still verify after migration, proven by golden files (P0-19).
- **Analytical store.** DuckDB and Parquet may be added as a **derived, rebuildable** store. It never becomes a source of truth.
- **Security master.** It holds CIK↔ticker history, delistings and corporate actions, and replaces today's ticker map (`jevtrader/feeds.py:6-8`, P0-16).

### D6. Validation counts every trial, and gates are per hypothesis and fingerprinted in full

- **Trial registry.** One global, append-only registry spans ledgers and records abandoned trials. Its fields are hypothesis id, params hash, window, model id and label (P0-27).
- **Gates.** Each hypothesis is pre-registered with its own gate (P0-29). Gate v2's fingerprint covers the whole evaluation spec: costs, eligibility rule, buffer, method and selection rule (P0-30).
  - A changed gate is a new gate.
  - Gate v1 (`evidence.py:24`, `c895567c…bbac`) stays reproducible for existing ledgers.
- **Costs.** One versioned cost model with a floor is used everywhere (P0-02, P0-36).
- **Reports** always show DSR, PBO, trial count, label mix, cost sensitivity at 0.5×, 1× and 2×, and the baselines (P0-28).
- **Ownership.** A3 owns metrics code and A2 owns signal code. A8 must approve any metric change.

### D7. Today's strategy becomes the first hypothesis

The existing 8-K 7.01/8.01 strategy becomes hypothesis H-001 (`jevtrader/default_strategy.json`, the ridge calibrator at `jevtrader/research.py:113-175`, the decision rule at `jevtrader/engine.py:192-212`).
- Its forward ledger keeps collecting under gate v1.
- New hypotheses are versioned specs with named, typed features, replacing the fixed 8-slot vector (`research.py:23-32`) (P0-40).
- The signal engine is deterministic code. LLMs supply typed features only.

### D8. Interaction surfaces stay read-only views over the ledger

- **Brief, web and MCP stay read-only views over the ledger.** MCP stays on stdio and read-only.
- **Chat UI (Phase 4)** is a client of a typed service API. Every number it shows comes from a tool result.
- **A5 code never writes to the ledger.**
- **Later additions**, each under its own ADR: Streamable HTTP MCP for hosted use, and the opt-in propose profile (D3).
- **Co-installed trading servers.** A host that loads this server next to a broker MCP server with order tools gives filing excerpts a path to an agent with write tools today. `docs/mcp.md` and the server instructions (`mcp_server.py:50-59`) must warn against it (P0-08).

### D9. Invariants

Audit [§8](../audit/phase0.md#8-invariants) lists 29 invariants.
- **I-1 to I-6** (the four refusals, the hash chain, the evidence labels and the gate fingerprint) change only through an ADR plus a migration under which `verify` passes on old ledgers.
- **I-7 to I-29** change only through an ADR.
- Every PR lists the invariants it touches.

### D10. Regulatory posture (not legal advice)

This mirrors master prompt §9. It is a design posture, not legal advice.
- **Personal use is the default.** The default deployment is self-hosted and for the user's own research. Every surface says it is a research tool, not investment advice, and MCP tells its host to decline trading and personalized advice (invariant I-29).
- **Publishing to others.** Anything published to others, such as the public weekly gate page (N4), is designed for the publisher's exclusion: impersonal, bona fide, and of general and regular circulation (*Lowe v. SEC*).
  - It shows aggregate gate statistics computed by code.
  - It never makes individualized recommendations.
- **The hosted default** offers no individualized recommendations and no discretionary execution.
- **Registration questions.** Personalized advice, discretion or custody for others likely raises investment-adviser and possibly broker-dealer registration questions. Counsel must be consulted before any such feature.
- **Other duties remain.** The master prompt records that the SEC's 2023 predictive-data-analytics proposal was withdrawn on June 12, 2025. Fiduciary, marketing-rule and anti-fraud duties still apply.
- **Data licences.** A personal data licence, such as the one covering Alpaca SIP bars, does not permit redistribution. A hosted service needs vendor agreements or per-user keys that users bring themselves. The public page never republishes licensed bars.
- **EU.** MiFID II's definition of investment advice, the AI Act, GDPR and, for crypto, MiCA apply. The EU is a separate launch.
- **Exclusions.** No material non-public information, and no manipulative order patterns (the kernel scope in D2). No acquisition that breaks a site's terms, and SEC traffic stays at or below 10 requests per second with a declared contact (D4).
- **Gate.** A7 completes a compliance and data-licensing review before the public gate page or hosted mode ships (dependency graph, Phase 6). Compliance mode disables personalized language (master prompt §5, Phase 6).

## Migration path: today's modules to the target components

The rules for every move:
- Each move is a shim-first PR that changes no behaviour and no test. It lands with the offline suite green.
- The target paths are subpackages of `jevtrader/` (D1).
- Line counts are from `wc -l` at `fa0c0d3`.

| Today (lines) | Target component | Zone | Owner | Phase | First gaps |
|---|---|---|---|---|---|
| `sec.py` (579), `feeds.py` (566) | `collectors/edgar` + raw store | untrusted | A1 | 1 | P0-11 to P0-14, P0-16 |
| `bars.py` (724); `market.py` bar import (`market.py:60-69`) | `collectors/bars` | untrusted | A1 | 1 | P0-13, P0-15 |
| `store.py` (412); `market.snapshot` (`market.py:86-121`) | ledger (kept) + `pit/` (`as_of`, security master) | trusted | A1, with A0 on the schema ADR | 1 | P0-17, P0-18 |
| text extraction in `sec.py:311-321`; filters at `brief.py:70-75`, `notify.py:42-44`, `web.py:403`, `daemon.py:544`, `launchd.py:246` | `security/sanitize` | boundary | A7 | 1 | P0-05 |
| `providers.py` (515), `local.py` (377) | `extractors/` (quarantined readers) | boundary | A2 | 1–3 | P0-06, P0-07, P0-37, P0-39 |
| `research.py` fit/predict (`research.py:113-215`); decision rule (`engine.py:192-212`) | `signal/` | trusted | A2 | 3 | P0-38, P0-40 |
| `registry.py` (261); `engine.settle` and `market.outcome` (`engine.py:314-326`, `market.py:132-178`) | `validate/` (labels, outcomes) | trusted | A3 | 2 | P0-37 |
| `evidence.py` (253); `research.walk_forward` (`research.py:254-405`) | `validate/` (gate, statistics) | trusted | A3 (A8 approves) | 2 | P0-28 to P0-35 |
| `lab.py` (326) | `trials/` (registry) + `hypotheses/` (autoresearch) | trusted | A3 + A2 | 1–3 | P0-06 (Phase 1), P0-27, P0-28 |
| `default_strategy.json`; `common.validate_strategy` (`common.py:91-140`) | `hypotheses/` specs (H-001) | trusted | A2 | 3 | P0-02, P0-40 |
| `common.round_trip_bps` (`common.py:143-147`); cost block `paper.py:159-182` | `validate/costs` (one versioned model) | trusted | A3 | 2 | P0-36 |
| `paper.py` (225) | `risk/` (kernel seed, a pure library) + `simulator/` | trusted; the binding kernel checks run again inside the execution process (D2) | A4 (A3 for the simulator) | 5 | P0-20 |
| none | `execution/` (BrokerPort, approval-token verification, kill switch, trading keys) | separate process | A4 (A7 reviews) | 5 | P0-04, P0-10, P0-42 |
| `pipeline.py` (289), `daemon.py` (775) | core service and scheduler | trusted | A0 (integration); A6 (deployment) | 1+ | P0-25 |
| `brief.py` (756), `web.py` (452), `notify.py` (69) | brief and `ui/` | interaction | A5 | 4 | P0-08, P0-41 |
| `mcp_server.py` (516) | MCP read-only profile (+ opt-in propose profile that sees typed outputs only, D3) | interaction | A5 + A7 | 1, 4–5 | P0-08 (Phase 1) |
| `app.py` (780), `cli.py` (508), `config.py` (218), `paths.py` (47), `launchd.py` (255), `secrets.py` (120), `demo.py` (110) | `deploy/`, setup, doctor, service units, secret store, fixture ledger | devex | A6 (A7 owns secrets policy) | 1 | P0-03, P0-24, P0-42 |
| `common.py` (147) | `contracts/` + shared primitives (one time type) | shared | A0 | 1 | P0-17, P0-23 |

**Order of work.** The detailed graph is in [dependency-graph.md](../audit/dependency-graph.md).
- **Phase 1:**
  - contracts and ledger v3;
  - the `as_of` read path;
  - the sanitizer;
  - a global rate limiter;
  - packaging;
  - CI gates;
  - the phase-1 security items: human-approved question sets for autoresearch (P0-06), the red-team corpus in CI (P0-09) and broker-key isolation (P0-42);
  - the three principle-breaking fixes: P0-02, P0-03 and P0-31 (the P0-31 fix is a stop-gap label; the full fix comes in Phase 2).
- **Phase 2:** trial registry, gate v2, statistics, one cost model, the look-ahead suite.
- **Phase 3:** hypothesis specs, span-evidence readers, the monolith-function split (P0-38), at least five hypotheses in forward collection.
- **Phase 4:** service API and chat UI.
- **Phase 5:** risk kernel and execution service.
- **Phase 6:** hosted mode and the probabilistic forecast ledger, after A7's compliance and data-licensing review (D10).

## Consequences

**Positive**
- The 742 tests, the ledger format and the four refusals carry over. Old ledgers keep verifying, and the forward-evidence clock of the existing strategy never restarts.
- Keeping one install unit meets the one-command demo goal (N1). The process split sits exactly at the security boundary, where N3 needs it.
- Every later decision has a home: its own ADR, a contract and a named owner.

**Negative and costs**
- Strangler moves add temporary re-export shims and churn in imports. Tests that patch private names (audit §9 rank 21) must move to public seams first (P0-26). Otherwise a move would force test rewrites, which the Do-NOT list forbids.
- Schema v3 and gate v2 are the first real migrations. They need golden-ledger fixtures before any code changes.
- Per-order live approval caps live throughput by design (O1). Strategies that need unattended live execution are out of scope.

**Risks and mitigations**
- **Scope creep in Phase 1.** Contracts gate implementation, and A0 holds merge gates.
- **The forward-evidence clock, not engineering, bounds live promotion.** At least 100 matured calls and at least 3 months of forward paper are needed per hypothesis. Hypotheses must therefore be pre-registered early (dependency graph, critical path).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Rewrite in a new stack (for example a TypeScript or Rust backend) | Loses the tested core, and invites breaking I-1 to I-6 without a migration. The master prompt says to keep Python and the tests |
| Microservices from the start | Conflicts with local-first, one-command install. Only execution needs process isolation |
| Make DuckDB or Postgres the system of record | Breaks the hash-chained ledger (I-3) and local-first use. Allowed only as derived stores (D5) or hosted metadata (a later ADR) |
| Keep the live-bounded-auto rung | Rejected by the owner (O1) |
| Let the orchestrator LLM approve or place orders behind guardrails | Violates N3 and Do-NOT item 1. Code decides and humans approve |
| Let the propose-profile orchestrator read filing excerpts, relying on provenance tags | Untrusted text would reach an agent with a write tool (Do-NOT item 1). Tags do not stop an injection. Excerpts stay in read-only sessions (D3) |
| A generic or placeholder SEC User-Agent, or the owner's identity by default | Violates O3 and SEC fair-access terms |
| Keep one pooled gate per ledger and count trials by convention | Audit §6 shows 15 trial routes, 4 of them high severity. A registry and per-hypothesis gates are required |

## Later ADRs

Numbers are assigned when each ADR opens.

| Topic | Owner | Phase | Invariants touched |
|---|---|---|---|
| Ledger schema v3: ledger identity, hashed `recorded_at`, migration | A1 + A0 | 1 | I-3 |
| Bitemporal store and `as_of` API; optional DuckDB/Parquet analytical store | A1 | 1 | I-8 to I-17 |
| Security master data sources and licensing | A1 + A7 | 1 | — |
| Threat model and secrets policy (Keychain, libsecret, Windows Credential Manager, Docker secrets; broker-key isolation) | A7 | 1 | I-25 |
| Packaging: Docker Compose, PyPI, systemd; later a Tauri desktop app with a Python sidecar | A6 | 1 / 4 | — |
| Sanitizer and quarantined-reader contract (spans, provenance) | A7 + A2 | 1–3 | I-26 |
| Trial registry and gate v2 (per-hypothesis gates, full-spec fingerprint) | A3 (A8 approves) | 2 | I-6, I-18, I-19 |
| Cost model v2 (floor, one model, sensitivity) | A3 | 2 | I-2 |
| Validation statistics (DSR, PBO/CSCV, SPA, HAC, purged CV) | A3 + A8 | 2 | I-22 |
| Hypothesis spec and feature schema v2 | A2 | 3 | I-5, I-13 |
| Model tiering and provider policy (rules baseline always, local or Jev, frontier, Chrono vintages) | A2 + A7 | 3 | I-1, I-21 |
| Agent runtime and backtest sandbox | A2 + A7 | 3 | — |
| Service API and frontend stack (TypeScript/React) | A5 + A0 | 4 | I-27 |
| MCP profiles and transports (stdio read-only; Streamable HTTP; opt-in propose with typed outputs only and untrusted-argument rejection) | A5 + A7 | 4–5 | I-27 |
| Execution service, BrokerPort, approval tokens (physical-presence second factor), risk kernel re-enforced in-process, trading-key isolation; R4 restated | A4 + A7 | 5 | I-4, I-25 |
| Promotion ladder (user-signed promotion records, paper-only credentials for paper-auto) and live limits | A4 + A3 + owner | 5 | I-4, I-6 |
| Hosted multi-tenant mode (OIDC, per-tenant ledgers, BYOK) and compliance modes | A6 + A7 | 6 | I-3, I-25, I-29 |
| Compliance and data-licensing review (publisher's exclusion, SIP redistribution, EU); gates the public gate page | A7 | 6 | I-29 |
| Probabilistic forecast ledger and external anchoring of the chain head | A3 + A1 | 6 | I-3 |
