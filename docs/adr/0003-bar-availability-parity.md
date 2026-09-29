# ADR-0003: Bar availability parity between historical and forward bars (invariant I-15)

| | |
|---|---|
| Status | **Proposed** (2026-09-29). Becomes Accepted when the owner signs off after A8 (QA) review |
| Deciders | Owner (accepts), A1 Data & PIT (author) |
| Reviewers | A8 QA & Evaluation, A0 Lead Architect |
| Evidence | [Phase 0 audit](../audit/phase0.md) gap P0-15 (issue #19); invariant I-15 in [audit §8](../audit/phase0.md#8-invariants) |
| Supersedes | nothing. It tightens I-15 |

## Context

A forward run stores a daily bar only once its close is at least `SETTLE_DELAY` (20 minutes) old, and stamps the actual receipt time (`bars.fetch_forward`). Before this ADR, a historical bar without a receipt was assumed available **at the close** (`market.normalize_bar`, which `bars.fetch_historical` and `market.import_bars` use). A replay could therefore see a bar up to 20 minutes before any forward run could have, so replayed decisions had information a forward decision at the same instant would not.

Separately, the public `bars.daily_bars` could request a session that had not finished; its docstring said "the newest may be unfinished".

## Decision

1. **One constant.** `SETTLE_DELAY = timedelta(minutes=20)` lives in `jevtrader/market.py`; `bars.SETTLE_DELAY` is the same object.
2. **Historical availability = close + `SETTLE_DELAY`.** A historical bar without an explicit `available_at` gets `close_at + SETTLE_DELAY`. An explicit `available_at` (a recorded receipt) is kept, and must still be at or after the close. Forward bars still carry the actual receipt. Synthetic (demo) bars keep the close; they never count as evidence.
3. **`daily_bars` is guarded.** It raises `BarsError` before any request unless `now >= end 16:00 New York + SETTLE_DELAY`. 16:00 is the latest regular close, so early-close sessions are covered too. "Now" comes from `bars.utc_now`, the same seam `fetch_historical` uses.

I-15 now reads: *forward bars are stored ≥ `SETTLE_DELAY` after close with receipt time; historical bars without a receipt are available at close + `SETTLE_DELAY`; forward and historical bars never mix; `daily_bars` never returns an unfinished session.*

Pinned by `tests/test_bars.py::BarAvailabilityParityTests` and `tests/test_store_market.py::HistoricalAvailabilityTests` (boundary at exactly close + 20 min, one second before, and across the March DST change).

## Consequences

- Historical bars already in a research ledger keep their stored `available_at` (the ledger is append-only and a stored bar is never replaced). Only newly stored bars use the new default. A research ledger rebuilt from scratch gets the new availability.
- Replays with a decision between close and close + 20 min now see the previous session. This is intended.
- `daily_bars` callers asking for today before 16:20 New York get an error instead of a partial bar.

## Open question: does Alpaca revise daily SIP bars?

**Not checked.** Answering it needs network access to Alpaca (and the owner's keys or docs pages), which this change did not have. If Alpaca revises a daily SIP bar after the first receipt (for example late trade corrections folded into the consolidated volume or close), then:
- a forward bar stored at close + 20 min may differ from the historical bar for the same session fetched later, so parity of *timing* does not imply parity of *values*;
- `SETTLE_DELAY` may need to grow, or historical bars may need a later assumed availability.

Follow-up for the owner: compare a forward-stored bar with the same session fetched a day later over a sample of symbols, or confirm from Alpaca's documentation, and record the answer here before this ADR is Accepted.
