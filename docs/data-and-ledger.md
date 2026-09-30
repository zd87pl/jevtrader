# Ledger and data formats

- [The ledger](#the-ledger)
- [Checking the ledger](#checking-the-ledger)
- [Disclosures](#disclosures)
- [Collecting from SEC](#collecting-from-sec)
- [Raw session bars](#raw-session-bars)

## The ledger

```sh
python -m jevtrader --db data/research.sqlite init
python -m jevtrader --db data/research.sqlite status
```

Records are immutable. Repeating an identical insert is harmless; conflicting content requires a new version ID. Every record is also committed to a SHA-256 hash chain; `python -m jevtrader --db data/research.sqlite verify` recomputes all record hashes and the chain (older ledgers are upgraded to the chain once, on their first read-write open). Repeated SEC collection preserves the original stored `first_seen_at`. Keep synthetic demonstrations separate from real research. Historical LLM extraction can contain knowledge learned after the event; only a newly frozen forward record establishes what this system actually observed at that time.

How the rules are enforced (`jevtrader/store.py`):

- SQLite triggers abort any `UPDATE`, `DELETE` or `REPLACE` on the `records` table and on the `chain` table.
- Each record's content hash is linked into the chain as `sha256(prev_hash | kind | id | content_hash)`, starting from a genesis hash of zeros. Triggers also refuse a chain row that does not extend the current head or does not match a stored record.
- Only the live collector can create `forward` disclosures ("Only the live collector may create forward disclosures"). `first_seen_at` is the actual receipt time, and repeat polls keep the original. Forward bar imports are stamped with the import time.
- Record kinds: `attempts`, `bars`, `disclosures`, `experiments`, `extractions`, `forecasts`, `models`, `outcomes`, `paper_plans` and `runs`. Inspect any one with `show KIND ID`.

## Checking the ledger

`jevtrader verify` recomputes every content hash and chain link, and reports missing triggers and a truncated tail. It opens the ledger read-only and exits 1 if it finds a problem.

To see it work, tamper with a throwaway demo ledger (this needs the `sqlite3` command-line tool):

```sh
jevtrader --db /tmp/tamper-demo.sqlite demo > /dev/null
sqlite3 /tmp/tamper-demo.sqlite "DELETE FROM records"
# Error: stepping, Ledger records are immutable (19)

sqlite3 /tmp/tamper-demo.sqlite <<'SQL'
DROP TRIGGER records_no_update;
UPDATE records SET payload = replace(payload, '"action":"WATCH"', '"action":"LONG"')
 WHERE rowid = (SELECT min(rowid) FROM records
                WHERE kind = 'forecasts' AND payload LIKE '%"action":"WATCH"%');
SQL
jevtrader --db /tmp/tamper-demo.sqlite verify
# "ok": false, with problems:
#   "Record content does not match its hash: forecasts/…"
#   "Immutability trigger is missing: records_no_update"
```

**Tamper-evident, not tamper-proof.** Anyone with write access could rebuild the whole file with a consistent chain. Only a chain head saved somewhere else exposes that: keep a copy of the `chain_length` and `head` that `verify` prints, and later run `jevtrader verify --anchor-seq CHAIN_LENGTH --anchor-hash HEAD`. It reports a problem if that position is missing from the chain or holds a different hash. Nothing anchors the chain publicly.

## Disclosures

`import-disclosures` accepts JSONL: one object per line. Required fields are `id`, `symbol`, `text`, `source_url`, `mode`, `published_at`, and `first_seen_at`. Imported modes are `historical` or `synthetic`; only collection can create forward disclosures. Example historical record:

```json
{"id":"example-20260106","symbol":"ABC","published_at":"2026-01-06T21:05:00Z","first_seen_at":"2026-01-06T21:10:00Z","source_url":"https://example.com/disclosures/example-20260106","mode":"historical","text":"The company reports an operating update with specific changes in demand."}
```

```sh
python -m jevtrader --db data/research.sqlite import-disclosures disclosures.jsonl
```

## Collecting from SEC

For public SEC data, declare a contact for SEC; no SEC API key is needed. Use a dedicated alias that reaches you, not your personal name or address. `collect` reads it from `config.json` (set it with `jevtrader setup`), or from `SEC_USER_AGENT` when the config has none. There is no default. `collect` needs an existing ledger, so create one first:

```sh
export SEC_USER_AGENT='jevtrader sec-alias@your-domain.example'   # or set it in config via setup
python -m jevtrader --db data/forward.sqlite init
python -m jevtrader --db data/forward.sqlite collect --cik 0000320193 --symbol AAPL --limit 5
```

`--limit` (1 to 20) caps how many qualifying filings are collected from the company's recent submissions. Many companies rarely file under Items 7.01 or 8.01, so a run can collect few filings or none. The `--user-agent` option is deprecated and prints a warning: command-line arguments are visible to other processes (`ps`), so keep the contact in config. Collected filings are `forward` disclosures stamped with the time you collected them; they have no decision until you run `observe`, and cannot be scored until the ledger has bars for the symbol and benchmark.

Collection accepts 8-K/8-K/A filings listing 7.01 or 8.01 while excluding Item 2.02. It prefers one EX-99 HTML/text exhibit and marks primary-document fallbacks. Filename/link heuristics cannot guarantee an exhibit's type or that content is non-earnings. Unknown item metadata is excluded; PDFs and complete attachment coverage are unsupported. Requests are bounded and rate-limited (at most five requests a second); external links and redirects are not followed.

`published_at` is SEC acceptance time, **not guaranteed public availability**. The collector records actual receipt as `first_seen_at`. Polling an old filing now does not create a historical forward observation or reconstruct what a historical observer had seen.

## Raw session bars

Supply raw, unadjusted OHLCV for each stock **and SPY**, with actual session timestamps and explicit time zones. CSV example:

```csv
symbol,session,open_at,close_at,open,high,low,close,volume,split_ratio,cash_dividend,available_at
ABC,2026-01-06,2026-01-06T14:30:00Z,2026-01-06T21:00:00Z,100,103,99,102,1000000,1,0,2026-01-06T21:01:00Z
SPY,2026-01-06,2026-01-06T14:30:00Z,2026-01-06T21:00:00Z,600,603,598,602,50000000,1,0,2026-01-06T21:01:00Z
```

These two rows illustrate format only. Observations need at least 21 aligned prior sessions; labels need the full future horizon. Use the correct UTC offset for daylight saving and actual early-close times.

```sh
python -m jevtrader --db data/research.sqlite import-bars bars.csv --mode historical
python -m jevtrader --db data/forward.sqlite import-bars completed-bars.csv --mode forward
```

Historical `available_at` defaults to session close if omitted: that is an assumption, not verified receipt. Forward imports ignore supplied availability and stamp actual import time. Importing past bars today cannot make them visible to yesterday's decision. Use a separate database when changing provenance; immutable bar IDs identify symbol/session.

`split_ratio` means new shares per prior share at that session's open; default 1. `cash_dividend` is cash per **post-split** share on the ex-date; default 0. Labels credit dividends only when the position was held before that open and carry split-adjusted share counts. Do not supply adjusted prices and then apply corporate actions again. Prior cash dividends are not reinvested in holding-period labels.

The background service fetches completed daily bars from Alpaca instead (the `sip` feed; keys from a free paper account); see [Running the service](service.md).
