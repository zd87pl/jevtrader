# Test fixtures

## Golden ledgers (#23)

- `ledger_v1.sql`: a schema-1 ledger (records table and its immutability triggers, no hash
  chain, `user_version=1`), written by `tests/builders.py:build_v1` from the documented v1 DDL.
- `ledger_v2.sql`: a schema-2 ledger with the same four records, written by the current
  `jevtrader.store.Ledger` through `tests/builders.py:build_v2` with fixed `recorded_at` times.

Source: synthetic, no external data (the `sec.gov` URL in the disclosure is a placeholder).
Captured: 2026-09-29, with Python 3.14 `sqlite3.Connection.iterdump()`.

They are SQL text dumps because `*.sqlite*` files are never committed (`.gitignore`).
`tests/test_golden_ledgers.py` pins each file's SHA-256 and the chain head, restores them with
`builders.load_sql`, and checks that v1 migrates to exactly the v2 chain and that both verify.
**Never regenerate these to make a test pass**: a changed file or head means old ledgers no
longer verify, which needs an ADR and a migration (invariants I-1 to I-6). To add a new
schema version, add a new golden file beside these.
