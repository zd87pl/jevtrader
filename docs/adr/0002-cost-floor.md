# ADR-0002: A documented cost floor for evidence (invariant I-2)

| | |
|---|---|
| Status | **Proposed** (2026-09-29). Becomes Accepted when the owner signs off after A8 (QA) review; this changes metrics code |
| Deciders | Owner (accepts), A3 Validation (author) |
| Reviewers | A8 QA & Evaluation (blocking on metrics), A0 Lead Architect (`common.py`) |
| Evidence | [Phase 0 audit](../audit/phase0.md) gap P0-02 (issue #6); invariant I-2 in [audit §8](../audit/phase0.md#8-invariants) |
| Supersedes | nothing. It tightens I-2 and leaves I-6 (the gate and its fingerprint) unchanged |

## Context

Refusal R2 (invariant I-2) says no call counts before costs. Before this ADR:
- `validate_strategy` accepted 0 for every cost, because it enforced only `minimum=0`.
- The gate nets each call with the costs frozen in that forecast's own strategy (`evidence.net_return`, `evidence.scoreboard`).

So a strategy with every cost at 0 validated, and its calls counted. Offline probe from the audit: a zero-cost `LONG` with a +1% target scored +1.00% net. The call was "after costs" only in name.

## Decision

**1. One documented floor per cost the gate nets.** `common.COST_FLOOR` holds the values. `tests/test_strategy.py` pins them exactly.

| Strategy field | Floor | Default | Default ÷ floor |
|---|---|---|---|
| `spread_bps` (once per round trip) | 2 bps | 10 bps | 5× |
| `slippage_bps_per_side` (entry and exit) | 1 bp | 5 bps | 5× |
| `short_borrow_bps_annual` (shorts only) | 25 bps a year | 300 bps a year | 12× |
| `min_edge_bps` (margin above costs before a call) | 5 bps | 20 bps | 4× |

- A floor is a sanity bound, not a cost estimate. It rules out "free" trading; it does not claim that a strategy at the floor is realistic.
- `commission_per_order` is not floored. It is in dollars, the gate's round trip is in basis points and excludes it, and zero-commission brokers exist.

**2. Rejected at the door.** `validate_strategy` rejects a strategy with any floored cost below its floor, naming the field and the floor. Every entry point validates: `load_strategy`, `engine.observe`, the daemon, the lab and `paper.plan_order`.

**3. Never counted from the ledger.** The gate reads frozen records, which older code or a hand-written ledger may have made. `common.meets_cost_floor` passes a frozen strategy only when every floored cost is a finite number (not a boolean) at or above its floor. A missing strategy or cost fails.
- `evidence.is_evidence` needs an evidence label **and** `meets_cost_floor`.
- `evidence.net_return` returns `None` for a sub-floor call: it has no net-of-cost return.
- `evidence.scoreboard` chooses each event's decision first and applies the floor after. Dropping a sub-floor first call therefore never promotes a later forecast of the same event, which would be a second try (see "Many tries" in [limitations](../limitations.md#what-can-fool-the-gate)).
- An `eligible` filter can only narrow; it cannot readmit a sub-floor call.
- Sub-floor forecasts are counted in `excluded_forecasts`, and `ELIGIBILITY_RULE` says so.

**4. Moving a floor needs a new ADR**, because it changes what may count as evidence.

## Migration

Invariants I-1 to I-6 change only through an ADR plus a migration under which `verify` passes on old ledgers.
- **No ledger rewrite.** The floor is applied when reading. The hash chain, stored records and forecast ids are untouched, so `verify` passes on old ledgers. `tests/test_evidence.py` pins this with a ledger holding a zero-cost forecast.
- **Old sub-floor forecasts stop counting.** They show as not evidence, with no net return.
- **A custom strategy file below a floor no longer loads.** The error names the field. Raise the cost to at least its floor.

## Consequences

- **Default results do not change.** The default strategy clears every floor with unchanged costs: a 20 bps round trip, 300 bps a year of borrow and a 20 bps edge. The `LONG` threshold stays 40 bps and the `SHORT` threshold 51.9 bps over 10 sessions (pinned in `tests/test_engine.py`). `GATE` and `gate_sha256` (I-6) are unchanged.
- **The fingerprint does not cover the floor.** `gate_sha256` hashes `GATE` only, so a moved floor would not change it. Only the pinning test and this ADR guard the values. Folding the floor into `GATE` would change I-6 and needs its own ADR.
- **`research.walk_forward` is not floored.** It takes explicit `cost_bps` and `min_edge_bps` (at least 0) and reports development results, not gate evidence. `engine.evaluate` passes it the validated strategy's costs.
- **Tests.** `tests/test_strategy.py` (floor values, rejection at and just below each floor, unprovable costs) and `tests/test_evidence.py` (`CostFloorEvidenceTests`).
