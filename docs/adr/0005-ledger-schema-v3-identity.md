# ADR-0005: Ledger schema 3, per-ledger identity and `recorded_at` inside the chain

| | |
|---|---|
| Status | **Proposed** (2026-09-29). Becomes Accepted when the owner signs off after A8 (QA) and A7 (Security) review |
| Deciders | Owner (accepts), A1 Data & PIT (author) |
| Reviewers | A8 QA & Evaluation, A7 Security & Compliance, A0 Lead Architect |
| Evidence | [Phase 0 audit](../audit/phase0.md) gap P0-18 (issue #22); ADR-0001 D5 and D9; invariant I-3 in [audit §8](../audit/phase0.md#8-invariants) |
| Supersedes | the schema-2 chain formula for entries written after the upgrade. Schema-2 entries keep their formula |

## Context

Schema 2 chains every record from the constant `GENESIS = "0" * 64`, so two ledgers holding the same records have the same chain, and nothing in a verify report tells one ledger from another. `recorded_at` sits outside the link hash, yet the v1-to-v2 migration orders the chain by it and `as_of` falls back to it as knowledge time (ADR-0004), so an edited `recorded_at` goes unnoticed by `verify`.

I-3 (history cannot be rewritten quietly) changes only through an ADR plus a migration under which `verify` passes on old ledgers. This is that ADR.

## Decision

1. **Creation record.** Schema 3 adds a single-row table `ledger_identity(nonce, created_at, migrated_from, legacy_seq, legacy_head, legacy_recorded_at, root)`, written once when a ledger is created or upgraded, and protected by triggers that refuse update, delete and a second insert.
   - `nonce` is 128 random bits (`secrets.token_hex(16)`), the ledger identity.
   - `created_at` is the ledger clock's reading at creation or upgrade.
   - `migrated_from` is the schema the file had before (0 for a new ledger).
   - `legacy_seq` and `legacy_head` are the chain head when the row was written (`0` and `GENESIS` for a new ledger).
   - `legacy_recorded_at` is SHA-256 over the canonical JSON list `[[kind, id, recorded_at], ...]` of the chain entries at or below `legacy_seq`, in `seq` order. It brings the `recorded_at` of pre-upgrade records into the chain.
   - `root` is SHA-256 over the canonical JSON of the other six fields.
2. **Schema-3 link formula.** Entries after `legacy_seq` use `sha256(prev|kind|id|content_hash|recorded_at|root)`, where `recorded_at` is the record's stored value. Every new link therefore commits to the ledger identity and to its own `recorded_at`. `prev` still starts at the previous entry (or `GENESIS`), so the `chain_extends_head` trigger is unchanged. `recorded_at` never contains `|`, so the preimage stays unambiguous.
3. **Migration.** Opening a writable schema 0, 1 or 2 ledger upgrades it in one transaction: schemas 0 and 1 first get the schema-2 chain exactly as before, then the identity row is inserted and `user_version` becomes 3. Existing chain rows are never rewritten, so every head published before the upgrade remains a valid `verify(anchor=...)` anchor. Read-only opens never migrate; a read-only schema-2 ledger still verifies under the schema-2 rules and reports no identity.
4. **Verify.** For schema 3, `verify` also checks that the identity row exists, is unique and recomputes to its `root`, that `legacy_head` is the chain hash at `legacy_seq`, that `legacy_recorded_at` recomputes, and that the identity triggers (including `chain_links_v3`) exist. The report gains `identity`: the nonce, or `None` for schemas 1 and 2. An anchor may carry `identity`; a mismatch is a problem.
5. **Scope of the identity.** A byte copy of a ledger still verifies and reports the same identity; that is inherent to a file. What changes is that two independently created ledgers never share a chain, and a published anchor that includes the identity cannot be satisfied by another ledger.
6. **Anchors carry the root (review PIT-4).** The nonce is public once an anchor is published, so for `seq` 0, or any `seq <= legacy_seq`, a ledger with the same legacy prefix and a hand-written identity row reusing that nonce satisfied a nonce-only anchor. `Ledger.anchor()` returns `head()` plus `identity` and `root`, and `verify(anchor=...)` compares an anchored `root` with the root recomputed from the identity row, which also commits to `created_at` and the legacy prefix. `head()` and the verify report keep their shape; publish `anchor()`, not `head()`.
7. **Pre-upgrade writers are refused (review PIT-1).** A process still running schema-2 code with a handle opened before another process upgraded the file used to append a schema-2 link after `legacy_seq`, so `verify` failed for good. The identity is now written with a `chain_links_v3` trigger that recomputes every new link through `jevtrader_link_v3`, a SQL function only schema-3 connections register. SQLite resolves a trigger's functions when the firing statement is prepared, so an old writer's next insert fails with "no such function" and writes nothing, and a raw insert with a schema-2 link is refused by the trigger itself. `put` also re-reads `user_version` inside its write transaction and refuses when it changed since the handle opened. A writable open of a schema-3 ledger written before this trigger existed adds it; a read-only `verify` reports it missing. Stopping the daemon before upgrading is still the recommended order, but correctness no longer depends on it.

Pinned by `tests/test_ledger_identity.py`, and by `tests/test_golden_ledgers.py`, where the golden v1 and v2 ledgers (issue #23) migrate to schema 3 and still verify against the pinned schema-2 head.

## Consequences

- `Ledger.counts()` and every kind API are unchanged; the identity lives outside `records`.
- Tests that pinned schema 2 for new ledgers (the user version, the verify report shape and the link formula for new entries) change with this ADR; the schema-2 formula stays tested through the golden ledgers and the v1 migration tests.
- `tests/builders.py:build_v2` now restates the schema-2 write path (schema-2 DDL and link) instead of calling `Ledger`, which writes schema 3; the golden v2 fixture and its pinned SHA-256 are unchanged.
- Unsupported schemas are now anything outside 0 to 3.
- A checked-in golden v3 ledger is a follow-up.
