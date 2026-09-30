# ADR-0007: Replays of imported or backfilled filings need a pre-registered cohort

| | |
|---|---|
| Status | **Proposed** (2026-09-29). Becomes Accepted when the owner signs off after A8 review |
| Deciders | Owner (accepts), A3 Validation (author) |
| Reviewers | A8 QA & Evaluation, A0 Lead Architect |
| Evidence | [Phase 0 audit](../audit/phase0.md) gap P0-31 (issue #35); ADR-0001 D5, D9; invariants I-1, I-5 |
| Supersedes | nothing. The trial registry (P0-27, Phase 2) will replace this stop-gap |

## Context

Only a replay of one named event (`--event` with `--replay` or `--as-of`) was labelled
`adhoc_replay`. A queue replay of an imported filing (`Ledger.disclosure`) or a backfilled one
(`feeds.backfill`) got `no_model_knowledge` or `post_cutoff` and counted as evidence. The user
chooses which filings to import, so they could import only filings that were followed by large
moves. The model's training cutoff (I-1) says nothing about that selection.

## Decision

1. **A new ledger kind, `cohorts`** (`jevtrader/cohorts.py`). A record holds `id`, `rule` (how
   the filings were chosen), `event_ids` (sorted, unique, at most 10,000) and `registered_at`.
   Records are immutable; registering the same cohort again is a no-op.
2. **Pre-registered means appended first.** `cohorts.preregistered(ledger, event_id)` returns a
   cohort that lists the filing and whose hash-chain position (`Ledger.sequence`) is before the
   filing's. A cohort written after the import does not count, so a user cannot import winners
   and register them afterwards. Chain order is tamper-evident; `recorded_at` could tie.
3. **Otherwise the replay is `adhoc_replay`.** In `engine.observe`, a `historical` forecast
   whose registry label would count is labelled `adhoc_replay` unless its filing is in a
   pre-registered cohort. Every historical disclosure is imported or backfilled, since the live
   collector writes only `forward` ones (I-3). Forward, synthetic and already non-evidence labels
   are unchanged. The label is frozen at decision time as before.
4. **The label mix is shown.** `pipeline.observe_queue` returns `labels`, the count of each
   evidence label in the run, so a replay batch shows how much of it can count.

## Invariants

- **I-1** is tightened, not changed: a replay outside the 92-day window still needs a
  pre-registered cohort to count. A replay inside the window still never counts.
- **I-5** keeps its seven labels and its three evidence labels. `adhoc_replay` now also covers
  unregistered cohorts, since an import the user picked by hand is a hand-picked replay.
- **Old ledgers.** No migration is needed and `verify` passes unchanged: the label is frozen in
  each forecast, so existing forecasts keep their labels. Only new replays are affected. A
  ledger opened read-only at schema 1 has no chain, so it cannot answer `sequence`.

## Consequences

- Existing imported histories stop producing evidence until a cohort is registered before the
  next import. Re-importing the same filings does not help, because the disclosure record
  already exists; a new cohort needs new filings.
- Pre-registration does not prove the user had not seen the outcomes elsewhere; it fixes the
  sample before the ledger holds it. The trial registry (P0-27) adds selection rules, a
  multiple-testing count and a registry fingerprint.
- There is no CLI command yet; cohorts are registered through `cohorts.register`. A CLI command
  and a `--cohort` flag on `backfill` are follow-ups.
