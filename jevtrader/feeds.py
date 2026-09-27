"""Universe-wide EDGAR 8-K collection that never backdates what this system saw.

``poll`` stamps each filing with the time its selected document was actually received
(forward mode). ``backfill`` is historical only: availability is an explicit conservative
assumption (``first_seen_basis: "backfill_assumed"``), never evidence of a forward
observation, and it refuses the forward ledger. Symbols come from SEC's *current* ticker
map, so a backfill is survivorship-biased: delisted or renamed companies are missing or
carry today's ticker. Every request goes through sec.py's allowlist, byte bounds, a
per-call request budget and the shared 5 requests/second limiter.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from datetime import date, datetime, time as clock_time, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from xml.etree import ElementTree

from . import paths, sec
from .common import EASTERN, instant, ledger_file, sec_symbol

TIMEOUT = 20.0
FEED_COUNTS = (10, 20, 40, 80, 100)
MAX_POLL_FILINGS = 100
MAX_BACKFILL_FILINGS = 5_000
MAX_BACKFILL_DAYS = 366
MAX_FILING_ATTEMPTS = 10
MAX_REMEMBERED = 10_000
TICKER_TTL_SECONDS = 6 * 3600
REQUESTS_PER_FILING = 4  # submissions + directory + cover + exhibit
FIRST_SEEN_DELAY = timedelta(minutes=15)
MORNING = clock_time(6, 0)  # EDGAR opens; nothing is disseminated earlier.
EVENING = sec.AFTER_HOURS
FIRST_INDEX_YEAR = 1994
_ATOM = "{http://www.w3.org/2005/Atom}"
_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb="
    "&owner=include&start=0&count={count}&output=atom"
)
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_TICKER = re.compile(r"[A-Z][A-Z0-9.-]{0,11}", re.A)  # what common.symbol and the ledger accept
_INDEX_ROW = re.compile(
    r"(8-K(?:/A)?)\s+(.*?)\s+(\d{1,10})\s+(\d{8}|\d{4}-\d{2}-\d{2})\s+"
    r"edgar/data/\d{1,10}/(\d{10}-\d{2}-\d{6})\.txt\s*\Z",
    re.A,
)
_SKIPS = (
    "not_qualifying",
    "unmapped",
    "not_watched",
    "not_indexed",
    "failed_before",
    "deferred",
)


def _client(user_agent: str, max_requests: int, transport: Callable | None) -> sec._SECClient:
    return sec._SECClient(user_agent, TIMEOUT, max_requests, transport=transport)


def _bounded(value: object, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _watched(symbols: Iterable[str] | None) -> set[str] | None:
    if symbols is None:
        return None
    if isinstance(symbols, str):
        raise ValueError("symbols must be a collection of symbols, not one string")
    # SEC's ticker map writes share classes as BRK-B; users and brokers write BRK.B.
    return {sec_symbol(value) for value in symbols}


def _feed_entry(entry: ElementTree.Element) -> dict | None:
    def text(tag: str) -> str:
        return (entry.findtext(_ATOM + tag) or "").strip()

    category = entry.find(_ATOM + "category")
    title = text("title")
    form = (category.get("term") or "").strip() if category is not None else ""
    form = form or title.split(" - ", 1)[0].strip()
    accession = re.search(r"accession-number=(\d{10}-\d{2}-\d{6})\Z", text("id"), re.A)
    cik = re.search(r"\((\d{10})\)", title, re.A)
    if form not in sec.FORMS or not accession or not cik or int(cik[1]) == 0:
        return None
    try:
        updated = sec._published_at(text("updated"))
    except sec.SECError:
        return None
    return {
        "cik": cik[1],
        "accession": accession[1],
        "form": form,
        "updated": updated,
        # Empty means the feed did not list items; SEC submissions stay authoritative.
        "items": sorted(set(re.findall(r"\bItem\s+(\d{1,2}\.\d{2})\b", text("summary"), re.A))),
    }


def _latest_8k(client: sec._SECClient, count: int) -> list[dict]:
    payload = client.get(_FEED_URL.format(count=count))
    # Decode first so the DTD check sees exactly what the parser will: a UTF-16 document
    # would slip a byte-level check. Atom needs no DTD; refusing one rules out entity tricks.
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise sec.SECError("SEC Atom feed is not UTF-8") from None
    # Without a BOM, UTF-16 also decodes as UTF-8 (NULs between the characters), and expat
    # then detects UTF-16 by itself. XML never allows NUL, so refusing it closes that path.
    if "\x00" in text:
        raise sec.SECError("SEC Atom feed is not UTF-8")
    if not text.lstrip().startswith("<") or re.search(r"<!(?:DOCTYPE|ENTITY)", text, re.I):
        raise sec.SECError("SEC Atom feed must be plain XML without a DTD or entities")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise sec.SECError("Invalid SEC Atom feed") from exc
    if root.tag != _ATOM + "feed":
        raise sec.SECError("SEC response is not an Atom feed")
    rows, seen = [], set()
    for entry in root.findall(_ATOM + "entry")[:count]:
        row = _feed_entry(entry)
        if row is not None and (row["cik"], row["accession"]) not in seen:
            seen.add((row["cik"], row["accession"]))
            rows.append(row)
    return rows


def latest_8k(
    user_agent: str, *, transport: Callable | None = None, count: int = 100
) -> list[dict]:
    """Newest 8-K and 8-K/A filings, newest first: one request.

    Rows are {"cik" (10 digits), "accession", "form", "updated" (UTC), "items"}; one row per
    registrant, so a co-registrant filing appears once per CIK.
    """
    if count not in FEED_COUNTS:
        raise ValueError(f"count must be one of {FEED_COUNTS}")
    return _latest_8k(_client(user_agent, 1, transport), count)


def _ticker_table(client: sec._SECClient) -> dict[str, list[str]]:
    data = client.get_json(_TICKERS_URL, max_bytes=sec.MAX_BULK_BYTES)

    def rank(key: str) -> tuple[int, int, str]:
        return (0, int(key), key) if key.isascii() and key.isdigit() else (1, 0, key)

    table: dict[str, list[str]] = {}
    for key in sorted(data, key=rank):
        entry = data[key]
        if not isinstance(entry, dict):
            continue
        cik, ticker = entry.get("cik_str"), entry.get("ticker")
        if isinstance(cik, bool) or not isinstance(cik, (int, str)) or not isinstance(ticker, str):
            continue
        ticker = ticker.strip().upper()
        if (
            not re.fullmatch(r"\d{1,10}", str(cik), re.A)
            or int(cik) == 0
            or not _TICKER.fullmatch(ticker)
        ):
            continue
        tickers = table.setdefault(str(int(cik)).zfill(10), [])
        if ticker not in tickers:
            tickers.append(ticker)
    if not table:
        raise sec.SECError("SEC ticker map contains no usable entries")
    return table


def ticker_map(user_agent: str, *, transport: Callable | None = None) -> dict[str, str]:
    """CIK (10 digits) -> SEC's first-listed ticker today; one request. Not point-in-time."""
    return {
        cik: tickers[0] for cik, tickers in _ticker_table(_client(user_agent, 1, transport)).items()
    }


def _index_url(day: date) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/"
        f"QTR{(day.month - 1) // 3 + 1}/form.{day:%Y%m%d}.idx"
    )


def _check_day(value: object, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{name} must be a date")
    if value.year < FIRST_INDEX_YEAR:
        raise ValueError(f"{name} precedes EDGAR daily indexes")
    return value


def _daily_index(client: sec._SECClient, day: date) -> list[dict]:
    if day.weekday() >= 5:
        return []
    try:
        payload = client.get(_index_url(day), max_bytes=sec.MAX_BULK_BYTES)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        exc.close()  # A holiday, or an index SEC has not published yet.
        return []
    lines = payload.decode("latin-1").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if re.fullmatch(r"-{20,}\s*", line, re.A))
    except StopIteration:
        raise sec.SECError("Malformed SEC daily form index") from None
    rows, seen = [], set()
    for line in lines[start + 1 :]:
        match = _INDEX_ROW.match(line)
        if not match or int(match[3]) == 0:
            continue
        cik = match[3].zfill(10)
        if (cik, match[5]) in seen:
            continue
        seen.add((cik, match[5]))
        filed = match[4] if "-" in match[4] else f"{match[4][:4]}-{match[4][4:6]}-{match[4][6:]}"
        rows.append(
            {
                "cik": cik,
                "accession": match[5],
                "form": match[1],
                "company": match[2].strip(),
                "date_filed": filed,
            }
        )
    return rows


def daily_index(user_agent: str, day: date, *, transport: Callable | None = None) -> list[dict]:
    """8-K and 8-K/A rows of one day's form.idx; weekends make no request, 404 gives [].

    Rows are {"cik" (10 digits), "accession", "form", "company", "date_filed"}. An empty list
    can also mean SEC has not yet published that day's index.
    """
    return _daily_index(_client(user_agent, 1, transport), _check_day(day, "day"))


def assumed_first_seen(acceptance: str) -> str:
    """Conservative availability for a backfilled filing, from SEC's raw acceptanceDateTime.

    Acceptance + 15 minutes on weekdays from 06:00 to before 17:30 ET; otherwise the next
    06:00 ET that falls on a weekday. Holidays are not modeled. The acceptance is read at its
    latest plausible instant (sec.latest_acceptance), so the assumption never runs early.
    """
    accepted = sec.latest_acceptance(acceptance)
    local = accepted.astimezone(EASTERN)
    if local.weekday() < 5 and MORNING <= local.time() < EVENING:
        seen = accepted + FIRST_SEEN_DELAY
    else:
        day = local.date() if local.time() < MORNING else local.date() + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        seen = datetime.combine(day, MORNING, tzinfo=EASTERN)
    return seen.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class PollMemory:
    """What one long-running poller learned; bounded, per process, never persisted.

    It keeps the ticker map for TICKER_TTL_SECONDS, accessions SEC showed do not qualify,
    and failure counts so a broken filing is not refetched every minute forever.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.tickers: dict[str, list[str]] | None = None
        self.fetched_at = 0.0
        self.rejected: dict[str, None] = {}
        self.attempts: dict[str, int] = {}

    def table(self, client: sec._SECClient) -> dict[str, list[str]]:
        now = self.clock()
        if self.tickers is None or not 0 <= now - self.fetched_at < TICKER_TTL_SECONDS:
            self.tickers = _ticker_table(client)
            self.fetched_at = now
        return self.tickers

    @staticmethod
    def _remember(store: dict, key: str, value) -> None:
        store.pop(key, None)
        while len(store) >= MAX_REMEMBERED:
            store.pop(next(iter(store)))
        store[key] = value

    def reject(self, accession: str) -> None:
        self.attempts.pop(accession, None)
        self._remember(self.rejected, accession, None)

    def fail(self, accession: str) -> None:
        self._remember(self.attempts, accession, self.attempts.get(accession, 0) + 1)

    def exhausted(self, accession: str) -> bool:
        return self.attempts.get(accession, 0) >= MAX_FILING_ATTEMPTS


_MEMORY = PollMemory()


def _may_qualify(items: list[str]) -> bool:
    return "2.02" not in items and bool({"7.01", "8.01"}.intersection(items))


def _choose(
    rows: list[dict], table: dict[str, list[str]], wanted: set[str] | None
) -> tuple[str, str] | str:
    """(cik, symbol) for a filing's registrants, or the skip reason."""
    mapped = False
    for row in rows:
        tickers = table.get(row["cik"], [])
        mapped = mapped or bool(tickers)
        for ticker in tickers:
            if wanted is None or ticker in wanted:
                return row["cik"], ticker
    return "not_watched" if mapped else "unmapped"


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def _stops_batch(exc: BaseException) -> bool:
    """SEC throttling, an SEC outage, or a network failure: continuing would only hammer."""
    if isinstance(exc, HTTPError):
        return exc.code in (403, 429) or exc.code >= 500
    return isinstance(exc, (OSError, HTTPException))


def _refuse(ledger, target: Path, message: str) -> None:
    location = ledger_file(ledger)
    if location is not None and location == target.resolve():
        raise ValueError(message)


def _result(**extra) -> dict:
    return {
        "seen": 0,
        "new": 0,
        "added": [],
        "skipped": dict.fromkeys(_SKIPS, 0),
        "errors": [],
        "stopped": None,
        "requests": 0,
        **extra,
    }


def _failure(result: dict, where: dict, exc: BaseException) -> bool:
    """Record one failure (never filing text); True when the batch must stop."""
    result["errors"].append({**where, "error": _describe(exc)})
    stop = _stops_batch(exc)
    if stop:
        result["stopped"] = _describe(exc)
    if isinstance(exc, HTTPError):
        exc.close()
    return stop


def _store(ledger, result: dict, record: dict) -> None:
    try:
        if ledger.disclosure(record, imported=False):
            result["added"].append(record["id"])
    except ValueError as exc:
        where = {key: record[key] for key in ("accession", "cik", "symbol")}
        _failure(result, where, exc)


def poll(
    ledger,
    user_agent: str,
    *,
    symbols: set[str] | None,
    transport: Callable | None = None,
    max_filings: int = 25,
    memory: PollMemory | None = None,
) -> dict:
    """Collect new qualifying 8-Ks from the current feed into the forward ledger.

    Oldest first, so filings about to leave the 100-entry feed are not starved. Accessions
    already in the ledger cost no request; the ticker map is fetched only when something
    new needs mapping. At most ``max_filings`` filings are fetched (<= 4 requests each);
    the rest are counted as ``deferred`` for the next poll. One bad filing is recorded in
    ``errors``; SEC throttling (403/429), 5xx or a network failure stops the batch
    (``stopped``). -> {"seen", "new", "added", "skipped", "errors", "stopped", "requests"}
    """
    max_filings = _bounded(max_filings, MAX_POLL_FILINGS, "max_filings")
    wanted = _watched(symbols)
    _refuse(
        ledger, paths.research_ledger_path(), "poll writes forward records; not the research ledger"
    )
    memory = _MEMORY if memory is None else memory
    client = _client(user_agent, 2 + REQUESTS_PER_FILING * max_filings, transport)
    registrants: dict[str, list[dict]] = {}
    for row in _latest_8k(client, FEED_COUNTS[-1]):
        registrants.setdefault(row["accession"], []).append(row)
    result = _result(seen=len(registrants))
    fresh = [
        (accession, rows)
        for accession, rows in registrants.items()
        if not ledger.prefix("disclosures", f"sec:{accession}:")
    ]
    result["new"] = len(fresh)
    skipped, examined = result["skipped"], 0
    for accession, rows in reversed(fresh):
        if accession in memory.rejected or (
            rows[0]["items"] and not _may_qualify(rows[0]["items"])
        ):
            skipped["not_qualifying"] += 1
            continue
        if memory.exhausted(accession):
            skipped["failed_before"] += 1
            continue
        choice = _choose(rows, memory.table(client), wanted)
        if isinstance(choice, str):
            skipped[choice] += 1
            continue
        if examined >= max_filings:
            skipped["deferred"] += 1
            continue
        examined += 1
        cik, ticker = choice
        try:
            record = sec.collect_filing(client, cik, accession, ticker)
        except sec.FilingNotFound:
            memory.fail(accession)
            skipped["not_indexed"] += 1
            continue
        except (ValueError, OSError, HTTPException) as exc:
            memory.fail(accession)
            if _failure(result, {"accession": accession, "cik": cik, "symbol": ticker}, exc):
                break
            continue
        if record is None:
            memory.reject(accession)
            skipped["not_qualifying"] += 1
            continue
        _store(ledger, result, record)
    result["requests"] = client.requests
    return result


def _days(start: object, end: object) -> list[date]:
    first, last = _check_day(start, "start"), _check_day(end, "end")
    if first > last:
        raise ValueError("start must not be after end")
    span = (last - first).days + 1
    if span > MAX_BACKFILL_DAYS:
        raise ValueError(f"Backfill at most {MAX_BACKFILL_DAYS} days per call")
    return [first + timedelta(days=offset) for offset in range(span)]


def _require_research_ledger(ledger) -> None:
    _refuse(
        ledger, paths.ledger_path(), "backfill writes historical records; use the research ledger"
    )
    db = getattr(ledger, "db", None)
    if db is None:
        return
    forward = db.execute(
        "SELECT 1 FROM records WHERE kind='disclosures' "
        "AND json_extract(payload, '$.mode')='forward' LIMIT 1"
    ).fetchone()
    if forward is not None:
        raise ValueError("This ledger holds forward disclosures; backfill into a research ledger")


def backfill(
    ledger,
    user_agent: str,
    start: date,
    end: date,
    *,
    symbols: set[str] | None = None,
    transport: Callable | None = None,
    max_filings: int = 500,
) -> dict:
    """Historical 8-K collection from daily form indexes into a separate research ledger.

    Never forward: ``first_seen_at`` is assumed_first_seen(acceptance), marked
    ``first_seen_basis: "backfill_assumed"``; filings whose assumed availability is still in
    the future are skipped. Covers filings within each company's recent SEC submissions.
    ``max_filings`` bounds filings examined (<= 4 requests each); ``truncated`` means a
    candidate was left unexamined, so the range was not finished. -> poll's keys plus "days", "truncated".
    """
    days = _days(start, end)
    max_filings = _bounded(max_filings, MAX_BACKFILL_FILINGS, "max_filings")
    wanted = _watched(symbols)
    _require_research_ledger(ledger)
    budget = 1 + sum(day.weekday() < 5 for day in days) + REQUESTS_PER_FILING * max_filings
    client = _client(user_agent, budget, transport)
    now = client.now()
    if days[-1] > now.astimezone(EASTERN).date():
        raise ValueError("Backfill cannot cover a future day")
    result = _result(days=0, truncated=False)
    skipped = result["skipped"]
    skipped.update(not_in_submissions=0, not_yet_available=0)
    table: dict[str, list[str]] | None = None
    submissions: dict[str, dict] = {}
    examined, stop = 0, False
    for day in (day for day in days if day.weekday() < 5):
        if stop:
            break
        try:
            index = _daily_index(client, day)
        except (ValueError, OSError, HTTPException) as exc:
            stop = _failure(result, {"day": day.isoformat()}, exc)
            continue
        result["days"] += 1
        registrants: dict[str, list[dict]] = {}
        for row in index:
            registrants.setdefault(row["accession"], []).append(row)
        for accession, rows in registrants.items():
            result["seen"] += 1
            if ledger.prefix("disclosures", f"sec:{accession}:"):
                continue
            result["new"] += 1
            table = _ticker_table(client) if table is None else table
            choice = _choose(rows, table, wanted)
            if isinstance(choice, str):
                skipped[choice] += 1
                continue
            if examined >= max_filings:
                result["truncated"] = True
                stop = True
                break
            examined += 1
            cik, ticker = choice
            try:
                if cik not in submissions:
                    submissions[cik] = client.get_json(
                        f"https://data.sec.gov/submissions/CIK{cik}.json"
                    )
                record = sec.collect_filing(
                    client,
                    cik,
                    accession,
                    ticker,
                    submissions=submissions[cik],
                    mode="historical",
                    first_seen=assumed_first_seen,
                )
            except sec.FilingNotFound:
                skipped["not_in_submissions"] += 1
                continue
            except (ValueError, OSError, HTTPException) as exc:
                stop = _failure(result, {"accession": accession, "cik": cik, "symbol": ticker}, exc)
                if stop:
                    break
                continue
            if record is None:
                skipped["not_qualifying"] += 1
                continue
            if instant(record["first_seen_at"]) > now:
                skipped["not_yet_available"] += 1
                continue
            record["first_seen_basis"] = "backfill_assumed"
            record["symbol_basis"] = "sec_ticker_map_at_backfill"
            _store(ledger, result, record)
    result["requests"] = client.requests
    return result
