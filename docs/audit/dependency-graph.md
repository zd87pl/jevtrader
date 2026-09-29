# Workstream dependency graph (Phases 1 to 6)

Owner: A0 Lead Architect. Companion to the [Phase 0 audit](phase0.md) and [ADR-0001](../adr/0001-evidence-engine-architecture.md).
- Nodes are workstreams, named by the agent that owns them, with the gap issues each one closes (`P0-nn`, [audit §10](phase0.md#10-gap-register-issues-to-file)).
- An arrow means "must land before". Bold arrows mark the critical path.
- Each phase's definition of done is in master prompt §5. Owner decisions O1 to O4 in ADR-0001 apply throughout. In particular, live orders need per-order human approval, and there is no live-bounded-auto rung.

```mermaid
flowchart TD
  P0["Phase 0: audit, ADR-0001 accepted<br/>A0, A7 and A8 review"]

  subgraph PH1["Phase 1: foundations and packaging"]
    A0C["A0: contracts v1, merge gates<br/>P0-23, P0-25"]
    A1L["A1: ledger v3 (identity, hashed recorded_at)<br/>ADR + migration, P0-18"]
    A1P["A1: bitemporal as_of store, one time type<br/>P0-17"]
    A1S["A1: security master<br/>P0-16"]
    A1T["A1: EDGAR acceptance, global rate limiter,<br/>reconcile, bar parity, P0-11 to P0-15"]
    A3F["A3: cost floor and replay stop-gap<br/>P0-02, P0-31"]
    A6S["A6 + A7: declared SEC contact, alias<br/>P0-03"]
    A6K["A6: compose, PyPI, systemd, fixture ledger,<br/>doctor, P0-24"]
    A7T["A7: threat model, secrets policy,<br/>secret scanning, P0-22"]
    A7Z["A7: sanitizer and provenance<br/>P0-05, P0-07, P0-08"]
    A8C["A8 + A6: CI gates, golden ledgers, guard gaps,<br/>pinned tests, P0-10, P0-19 to P0-21, P0-26"]
    A7N["A7 + A4: no-order guard test, data-only key check,<br/>broker-key isolation, P0-04, P0-42"]
    A2A["A2 + A7: autoresearch runs only human-approved<br/>question sets, P0-06"]
    A7R["A7 + A8: red-team corpus in CI<br/>P0-09"]
  end

  subgraph PH2["Phase 2: honest validation"]
    A3R["A3: global trial registry<br/>P0-27"]
    A3G["A3: gate v2 per hypothesis,<br/>full-spec fingerprint, P0-29, P0-30, P0-32"]
    A3M["A3: DSR, PBO, SPA, HAC, purged CV,<br/>reports, P0-28, P0-33, P0-34"]
    A3C["A3: simulator and one cost model<br/>P0-36"]
    A8L["A8: look-ahead suite, placebo,<br/>planted leak, P0-35"]
    A3I["A3 + A7: local model digest<br/>P0-37"]
  end

  subgraph PH3["Phase 3: hypothesis library"]
    A2S["A2: hypothesis spec, typed features<br/>H-001 is the existing strategy, P0-40"]
    A2Q["A2: span-evidence readers<br/>P0-39"]
    A2X["A2 + A3 + A4: split monolith functions<br/>P0-38"]
    A2H["A2: at least 5 hypotheses pre-registered<br/>and in forward collection"]
    A2P["A2: paper-to-backtest pipeline, sandboxed"]
  end

  subgraph PH4["Phase 4: chat-first UX"]
    A5A["A5 + A0: service API, chat UI,<br/>numbers from tool results, P0-41"]
    A5M["A5 + A7: MCP profiles, read-only by default;<br/>propose profile sees typed outputs only"]
  end

  subgraph PH5["Phase 5: execution and risk"]
    A4K["A4: risk kernel, manipulation guards<br/>(a library the execution service re-runs)"]
    A4E["A4: execution service, BrokerPort, isolated keys,<br/>approval tokens with a physical-presence factor, kill switch"]
    FWD(["Forward-evidence clock per hypothesis:<br/>at least 100 matured calls and 3 months forward paper"])
    A4L["Promotion: forward-paper, paper-auto, live-approval<br/>user-signed promotion records, per-order human approval,<br/>no live-bounded-auto"]
  end

  subgraph PH6["Phase 6: hosted and forecast ledger"]
    H1["A6 + A7: hosted multi-tenant, OIDC,<br/>per-tenant ledgers, compliance modes"]
    H2["A3 + A1: probabilistic forecast ledger,<br/>external chain anchoring"]
    A7C["A7: compliance and data-licensing review<br/>publisher's exclusion, SIP redistribution, EU"]
    H3["Public weekly gate page"]
  end

  P0 ==> A0C
  A0C ==> A1L ==> A1P
  A1P --> A1S
  A0C --> A1T
  A0C --> A3F
  P0 --> A6S
  P0 --> A6K
  P0 --> A7T --> A7Z
  P0 --> A8C
  P0 --> A7N
  P0 --> A2A
  A7Z --> A7R
  A2A --> A7R

  A1P ==> A3R
  A3F --> A3G
  A3R ==> A3G ==> A3M
  A1P --> A3C
  A8C --> A8L
  A1T --> A3I

  A0C --> A2S
  A1P --> A2S
  A7Z --> A2Q
  A2S --> A2X
  A3M ==> A2H
  A2S ==> A2H
  A2Q --> A2H
  A2A --> A2H
  A8L --> A2H
  A1S --> A2H
  A2H --> A2P

  A0C --> A5A
  A6K --> A5A
  A5A --> A5M
  A7R --> A5M

  A3C --> A4K
  A2X --> A4K
  A4K --> A4E
  A7N --> A4E
  A8C --> A4E
  A7R --> A4E
  A7T --> A4E
  A6K --> A4E
  A2H ==> FWD
  FWD ==> A4L
  A3M --> A4L
  A4E --> A4L

  A6K --> H1
  A5A --> H1
  A1L --> H2
  A3M --> H2
  FWD --> H3
  A7T --> A7C
  A7C --> H1
  A7C --> H3
```

## Critical path

**The engineering path.** Phase 0 → A0 contracts → A1 ledger v3 and `as_of` store → A3 trial registry → gate v2 → validation statistics → A2 hypotheses pre-registered.
- **Why A1 comes first.** Nothing about counting can be trusted until each ledger has an identity and transaction time sits inside the hash (P0-18). Until then, trials cannot be counted across ledgers.
- **Why statistics come before hypotheses.** A2 must not pre-register new hypotheses against gate v1, whose fingerprint omits costs and the selection rule (P0-30). The gate each hypothesis registers against must already report DSR, PBO and trial counts.

**The calendar path runs past engineering.** A hypothesis starts accruing counted calls only once it is pre-registered. Live-approval then needs, per hypothesis, all of the following:
- at least 100 matured calls;
- DSR ≥ 0.95 and PBO ≤ 0.2;
- at least 3 months of forward paper, with realized costs within 1.5× of the model (master prompt §5, Phase 5).

So the earliest live-approval date is fixed by when Phase 3 pre-registers hypotheses and how often each one fires, not by when the execution service is ready. A4's risk kernel and execution service are off the critical path: they can finish in Phase 5 and wait for the evidence.

Three consequences:
- **Keep the existing forward ledger running unchanged under gate v1.** H-001's clock never restarts.
- **Land A3's registry and gate v2 as early as Phase 2 allows,** and pre-register hypotheses the day their spec and gate exist.
- **Don't count paper-auto time before a hypothesis's gate is `supported`.** It does not shorten the 3-month forward-paper requirement.

**Parallel tracks.**
- A6 packaging, A7 threat model and A8 CI gates start on day one and depend only on Phase 0.
- A5's chat UI depends on the service-API contract, not on validation.
- **Phase-1 security items land in Phase 1.** Human-approved question sets for autoresearch (P0-06) and the red-team corpus (P0-09) carry the `phase-1` label, so they sit in Phase 1 here. Span-evidence readers (P0-39) stay in Phase 3.
- **Five hard prerequisites guard execution work.** Before any broker adapter merges:
  - the isolation-guard gaps must be closed (P0-10, in A8C);
  - the no-order guard test must be in CI (P0-04);
  - the red-team corpus must be in CI (P0-09);
  - A7's threat model and secrets policy must be accepted (A7T);
  - the secret store must keep broker keys away from agents, LLM hosts and child processes (P0-24, P0-42).
- **A7's compliance and data-licensing review gates Phase 6** (A7C; ADR-0001 §D10). The public gate page (H3) waits for both that review and the forward clock. Hosted mode (H1) waits for the review.
