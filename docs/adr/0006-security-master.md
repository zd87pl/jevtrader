# ADR-0006: Point-in-time security master, its data sources and licensing

| | |
|---|---|
| Status | **Proposed** (2026-09-29). Becomes Accepted when the owner signs off after A7 and A8 review |
| Deciders | Owner (accepts), A1 Data & PIT (author) |
| Reviewers | A7 Security & Compliance (licensing), A8 QA & Evaluation, A0 Lead Architect |
| Evidence | [Phase 0 audit](../audit/phase0.md) gap P0-16 (issue #20); ADR-0001 D5; ADR-0004 (`as_of`) |
| Supersedes | nothing. Backfill's `symbol_basis: "sec_ticker_map_at_backfill"` remains as the fallback |

## Context

Backfill mapped CIKs to SEC's *current* ticker map, so history was survivorship-biased:
delisted companies were missing and renamed ones carried today's ticker. Corporate actions that
arrive after a bar is stored were never applied, and delisting returns were not modeled.

## Decision

1. **One ledger kind, `securities`.** Each record is an immutable event (`jevtrader/pit/securities.py`):
   `ticker` (the CIK's full ticker set from a date), `delisting` (with an optional delisting
   return, never below -100%) or `split` (a positive ratio). Every event carries `cik`,
   `effective` (New York date, valid time), `known_at` (UTC instant, knowledge time) and `source`.
   Unknown fields are rejected. Ids are `cik:effective:type:` plus a content hash, so a correction
   is a new record, never an edit, and the hash chain is untouched.
2. **Bitemporal reads through `as_of`.** `SecurityMaster.from_ledger(ledger, t)` reads
   `Ledger.as_of("securities", t)`; `known_at` is the knowledge field (ADR-0004), so a fact
   learned late never reaches an earlier read. For the same CIK, type and effective date the
   latest-known event wins. `tickers(cik, on)`, `table(on)`, `cik_for(ticker, on)`,
   `delisting(cik)` and `split_factor(cik, after, through]` answer in valid time.
3. **Backfills use it.** `feeds.backfill` maps each index day's CIKs through the master known at
   the run's start (`symbol_basis: "security_master"`) and makes no ticker-map request. Only an
   empty master falls back to SEC's current map, still labeled `sec_ticker_map_at_backfill`.
4. **Never backdated.** `ticker_snapshot` turns SEC's current map into events effective only from
   the day it was read; the map says nothing about the past.

## Data sources and licensing

| Source | Status | Terms |
|---|---|---|
| SEC `company_tickers.json`, submissions (former names), EDGAR filings (8-K Items 3.01, 5.03) | Allowed | Public US government data; fair-access rules (declared contact, <= 10 req/s across processes) still apply |
| Owner-supplied files (`record_events`) | Allowed | The owner is responsible for the right to use them; `source` names the origin |
| Alpaca corporate actions | Local use only | Under the owner's Alpaca data agreement; never redistributed |
| CRSP, Compustat, Norgate and similar vendor masters | Not bundled | Licensed; may be loaded by an owner who holds a licence, for local use only |

Licensed data is never committed, bundled, published in artifacts, sent to an LLM provider or
exported from the read-only web view or MCP server. Test fixtures are synthetic. This PR adds no
new network source.

## Consequences

- Adding a ledger kind is additive: old ledgers verify unchanged and simply have no `securities`
  records, so backfill behaves as before until the owner loads some.
- Follow-ups: a CLI to import an owner file and record `ticker_snapshot` on each poll; deriving
  delistings and ticker changes from SEC filings; applying `split_factor` to stored bars and
  delisting returns to outcomes (`bars.py`, `market.outcome`, A3 review for metrics).
