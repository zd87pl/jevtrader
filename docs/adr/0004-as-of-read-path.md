# ADR-0004: Indexed bitemporal `as_of(t)` read path, one timestamp type, injectable clock

| | |
|---|---|
| Status | **Proposed** (2026-09-29). Becomes Accepted when the owner signs off after A8 (QA) review |
| Deciders | Owner (accepts), A1 Data & PIT (author) |
| Reviewers | A8 QA & Evaluation, A0 Lead Architect |
| Evidence | [Phase 0 audit](../audit/phase0.md) gap P0-17 (issue #21); ADR-0001 D5; invariants I-3, I-8, I-17 in [audit §8](../audit/phase0.md#8-invariants) |
| Supersedes | nothing. It adds a read path beside `Ledger.all` and `Ledger.prefix` |

## Context

Readers call `ledger.all(kind)`, which re-hashes every row, and each caller repeats its own point-in-time filter. Timestamps are parsed in several places with slightly different rules, and the ledger stamps `recorded_at` from the wall clock directly, so tests and replays cannot control it.

## Decision

1. **One timestamp type.** `jevtrader.pit.Instant` is an aware UTC moment. `Instant.parse` rejects naive and malformed values; `iso()` is fixed-width (`YYYY-MM-DDTHH:MM:SS.ffffffZ`), so string order equals time order. `common.instant`, `common.timestamp` and `common.utc_now` now go through it.
2. **Injectable clock.** `pit.Clock` is a protocol with `now() -> Instant`; `SYSTEM_CLOCK` and `FixedClock` implement it. `Ledger(..., clock=...)` uses it for `recorded_at` and for the "forward observation cannot be in the future" check (I-8). Without a clock the ledger behaves as before.
3. **Knowledge time.** `pit.knowledge_time(kind, payload, recorded_at)` is when the ledger could first know a record: `first_seen_at` (disclosures), `available_at` (bars), `decision_at` (forecasts), `created_at` (extractions), the later of `outcome_at` and `label_available_at` (outcomes). Other kinds, and payloads whose field is missing or malformed, use `recorded_at`.
4. **Derived index.** A new table `knowledge(kind, id, knowledge_time)` with index `knowledge_by_time(kind, knowledge_time, id)` is written in the same transaction as each record and backfilled when a writable ledger opens. It is additive: it sits outside the hash chain, `records`, `chain` and the schema version are unchanged, and `verify` passes on old ledgers.
5. **`Ledger.as_of(kind, t, prefix=None)`** returns records whose knowledge time is at or before `t`, ordered by id, reading through the index. Every returned row is hash-checked and its knowledge time recomputed from the payload. A row whose index entry disagrees raises; rows without an index entry (a read-only ledger opened before backfill) are filtered by scan. So a stale or edited index can hide a row but never leak one known after `t`.

Pinned by `tests/test_pit.py`, including a seeded property test (40 seeds, random kinds, clocks and query instants) asserting no returned row has knowledge time after `t` and that the result equals the brute-force answer.

## Consequences

- The decision path reads through `as_of` (issue #21): `engine.observe` picks the previous disclosure from `as_of("disclosures", decision_at)`, and `market.snapshot` and `market.outcome` read bars through `market.bars_as_of`, a thin sorted helper over `as_of("bars", t, prefix=...)`, keyed by the decision or settlement time. The filters they replaced (`first_seen_at <= decision_at`, `available_at <= t`) are the same knowledge fields, so results are unchanged. One corner differs only if availability is non-monotone: a label bar still unknown at `t` while a later session's bar is known is now skipped rather than blocking `outcome`; the session-alignment checks still apply.
- Later migrations (still on `all()`/`prefix()`/`bars_for`): `engine.settle`, `engine.training_rows` and the forecast scans in `observe`; `evidence.py`; `brief.py` and `pipeline.py` (`bars_for`); `app.py`, `cli.py`, `daemon.py`, `feeds.py`, `lab.py`, `bars.py`; plus the remaining direct `utc_now()` calls and local parsers (`research._time`, `sec._published_at`).
- Opening a writable ledger that predates this ADR takes one write transaction to backfill the index.
