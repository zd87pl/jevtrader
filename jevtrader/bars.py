"""Alpaca daily bars, trading calendar and corporate actions, stored point-in-time.

A session is stored only once its close is at least 20 minutes old, and a stored bar
is never replaced: the first receipt is kept. Forward bars carry their actual receipt
time. Historical bars assume availability at close + SETTLE_DELAY, the earliest a
forward run could have stored them (ADR-0003) — an assumption, not a verified
receipt — and belong in a separate research ledger. Prices are raw
(adjustment=raw); forward/reverse splits and cash dividends ride on the first session
on or after their ex-date. Stock dividends, spin-offs and mergers are not applied, and
an action Alpaca records only after a bar was stored cannot be added to it later.

Requests go only to allowlisted Alpaca HTTPS URLs with the keys from the environment
in headers; key values never appear in URLs, errors or results. Requests are paced
below the free tier's 200 per minute and bounded per call.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from bisect import bisect_left
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__, paths
from .common import EASTERN, instant, ledger_file, symbol, timestamp, utc_now
from .market import SETTLE_DELAY, normalize_bar
from .ratelimit import SharedLimiter

DATA_HOST = "data.alpaca.markets"
CALENDAR_HOST = "paper-api.alpaca.markets"
KEY_ID = "ALPACA_API_KEY_ID"
SECRET_KEY = "ALPACA_API_SECRET_KEY"
BENCHMARK = "SPY"
# IEX volume is a small slice of consolidated volume and would break liquidity gates.
FEEDS = ("sip",)
ACTION_TYPES = "forward_split,reverse_split,cash_dividend"
REQUEST_INTERVAL = 0.3  # The free tier allows 200 requests per minute.
REQUEST_TIMEOUT = 30.0
MAX_REQUESTS = 1_000
MAX_RESPONSE_BYTES = 10_000_000
MAX_SYMBOLS = 10_000
MAX_SYMBOLS_PER_REQUEST = 100
MAX_LOOKBACK_SESSIONS = 1_000
MAX_RANGE_DAYS = 366 * 30
BAR_PAGE_LIMIT = 10_000
ACTION_PAGE_LIMIT = 1_000
CALENDAR_PADDING_DAYS = 14
ACTION_PADDING_DAYS = 7  # Places an ex-date that fell on a holiday before the first session.
CHUNK_STATUSES = frozenset({400, 422})  # One rejected symbol list; later chunks may still work.

Transport = Callable[[str, dict[str, str], float], bytes]

_SYMBOL = r"[A-Z][A-Z0-9.]{0,11}"
_SYMBOLS = re.compile(rf"{_SYMBOL}(?:,{_SYMBOL}){{0,{MAX_SYMBOLS_PER_REQUEST - 1}}}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_LIMIT = re.compile(r"[1-9]\d{0,4}")
_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{1,1024}")
_KEY = re.compile(r"[A-Za-z0-9._~+/=-]{1,256}")
_CLOCK = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_ROUTES: dict[tuple[str, str], dict[str, re.Pattern[str]]] = {
    (DATA_HOST, "/v2/stocks/bars"): {
        "symbols": _SYMBOLS,
        "timeframe": re.compile("1Day"),
        "start": _INSTANT,
        "end": _INSTANT,
        "limit": _LIMIT,
        "adjustment": re.compile("raw"),
        "feed": re.compile("|".join(FEEDS)),
        "sort": re.compile("asc"),
    },
    (DATA_HOST, "/v1/corporate-actions"): {
        "symbols": _SYMBOLS,
        "types": re.compile(re.escape(ACTION_TYPES)),
        "start": _DAY,
        "end": _DAY,
        "limit": _LIMIT,
        "sort": re.compile("asc"),
    },
    (CALENDAR_HOST, "/v2/calendar"): {"start": _DAY, "end": _DAY},
}
_ACTION_GROUPS = {
    "forward_splits": "split",
    "reverse_splits": "split",
    "cash_dividends": "dividend",
}
_BAR_FIELDS = (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "v"))


class BarsError(ValueError):
    """Unsafe input, a rejected or malformed Alpaca response, or an exceeded bound."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status
        self.partial: dict | None = None  # What a fetch stored before it stopped.


class MissingKeys(BarsError):
    """Alpaca keys are not in the environment; nothing was requested."""


class _NoRedirect(HTTPRedirectHandler):
    """Never send the key headers to another location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen(url: str, headers: dict[str, str], timeout: float) -> bytes:
    request = Request(url, headers=headers)
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        if response.geturl() != url:
            raise BarsError("Alpaca redirects are not followed")
        return response.read(MAX_RESPONSE_BYTES + 1)


class _RateLimiter:
    def __init__(self, clock: Callable[[], float], sleep: Callable[[float], object]):
        self.clock = clock
        self.sleep = sleep
        self.last: float | None = None
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            if self.last is not None:
                delay = REQUEST_INTERVAL - (self.clock() - self.last)
                if delay > 0:
                    self.sleep(delay)
            self.last = self.clock()


# Shared by every thread and process using this app directory, so the daemon and a
# backfill together stay within the free tier's 200 requests per minute.
_LIMITER: _RateLimiter | SharedLimiter = SharedLimiter("alpaca", REQUEST_INTERVAL)


def validate_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise BarsError("Malformed Alpaca URL") from None
    if parts.scheme != "https" or parts.username or parts.password or port or parts.fragment:
        raise BarsError("Alpaca requests require an approved HTTPS URL")
    rules = _ROUTES.get((parts.netloc, parts.path))
    if rules is None:
        raise BarsError("URL is outside the approved Alpaca market-data paths")
    params = dict(pairs)
    extra = set(params) - set(rules) - {"page_token"}
    if len(params) != len(pairs) or extra or set(rules) - set(params):
        raise BarsError("Alpaca query parameters are not the approved set")
    for key, value in params.items():
        if not rules.get(key, _TOKEN).fullmatch(value):
            raise BarsError(f"Alpaca query parameter {key} has an unapproved value")


def _headers() -> dict[str, str]:
    values = {}
    for name in (KEY_ID, SECRET_KEY):
        value = os.environ.get(name, "").strip()
        if not value:
            raise MissingKeys(f"Set {KEY_ID} and {SECRET_KEY} to fetch Alpaca market data")
        if not _KEY.fullmatch(value):
            raise BarsError(f"{name} contains unsupported characters")
        values[name] = value
    return {
        "APCA-API-KEY-ID": values[KEY_ID],
        "APCA-API-SECRET-KEY": values[SECRET_KEY],
        "Accept": "application/json",
        "User-Agent": f"{paths.APP_NAME}/{__version__}",
    }


def _http_message(status: int, path: str) -> str:
    if status in (401, 403):
        return (
            f"Alpaca refused {path} (HTTP {status}); check the paper-trading API keys "
            "and the market-data subscription"
        )
    return f"Alpaca HTTP error {status} at {path}; not retried"


class _Client:
    """One call's session: keys read once, every URL checked, requests paced and counted."""

    def __init__(self, transport: Transport | None):
        self.headers = _headers()
        self.transport = transport
        self.remaining = MAX_REQUESTS

    def get(self, host: str, path: str, params: dict[str, str]) -> object:
        url = f"https://{host}{path}?{urlencode(params)}"
        validate_url(url)
        if self.remaining <= 0:
            raise BarsError(f"Alpaca request budget of {MAX_REQUESTS} per call exhausted")
        self.remaining -= 1
        _LIMITER.acquire()
        try:
            payload = (self.transport or urlopen)(url, dict(self.headers), REQUEST_TIMEOUT)
        except BarsError:
            raise
        except HTTPError as error:
            # Remote bodies and headers stay out of the message.
            status = error.code
            error.close()
            raise BarsError(_http_message(status, path), status=status) from None
        except Exception:
            # Transport exception text can echo request headers, which hold the keys.
            raise BarsError(f"Alpaca request to {path} failed; not retried") from None
        if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_RESPONSE_BYTES:
            raise BarsError(f"Alpaca response from {path} is not bytes or exceeds the size limit")
        try:
            return json.loads(payload)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise BarsError(f"Alpaca returned invalid JSON from {path}") from None


def _pages(client: _Client, host: str, path: str, params: dict[str, str]) -> list[dict]:
    pages: list[dict] = []
    seen: set[str] = set()
    token: object = None
    while True:
        query = params if token is None else {**params, "page_token": str(token)}
        page = client.get(host, path, query)
        if not isinstance(page, dict):
            raise BarsError(f"Alpaca {path} response must be an object")
        pages.append(page)
        token = page.get("next_page_token")
        if token is None or token == "":
            return pages
        if not isinstance(token, str) or not _TOKEN.fullmatch(token) or token in seen:
            raise BarsError(f"Alpaca {path} returned an invalid or repeated page token")
        seen.add(token)


def _dates(start: date, end: date) -> tuple[date, date]:
    if not all(isinstance(v, date) and not isinstance(v, datetime) for v in (start, end)):
        raise BarsError("start and end must be datetime.date values")
    if start > end:
        raise BarsError("start must not be after end")
    if (end - start).days > MAX_RANGE_DAYS:
        raise BarsError(f"Date range is limited to {MAX_RANGE_DAYS} days")
    return start, end


def _feed(feed: str) -> str:
    if feed not in FEEDS:
        raise BarsError(
            "feed must be 'sip': IEX volume is a small slice of consolidated volume "
            "and would break liquidity gates"
        )
    return feed


def _names(symbols: Iterable[str], *, first: tuple[str, ...] = ()) -> list[str]:
    if isinstance(symbols, (str, bytes)) or not isinstance(symbols, Iterable):
        raise BarsError("symbols must be a list of ticker symbols")
    values = [*first, *symbols]
    if not all(isinstance(value, str) for value in values):
        raise BarsError("symbols must be ticker symbols as text")
    names = list(dict.fromkeys(symbol(value) for value in values))
    if len(names) > MAX_SYMBOLS:
        raise BarsError(f"At most {MAX_SYMBOLS} symbols per call")
    return names


def _alpaca(name: str) -> str:
    # SEC tickers write share classes with a dash (BRK-B); Alpaca uses a dot (BRK.B).
    return name.replace("-", ".")


def _aliases(names: Iterable[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name in names:
        result.setdefault(_alpaca(name), []).append(name)
    return result


def _chunks(values: list[str]) -> list[list[str]]:
    size = MAX_SYMBOLS_PER_REQUEST
    return [values[index : index + size] for index in range(0, len(values), size)]


def _day(value: object) -> date:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise BarsError("Alpaca dates must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise BarsError("Alpaca returned an invalid date") from None


def _noon(day: date) -> str:
    # Daily bars are stamped at midnight; a noon bound can never be tied with one.
    moment = datetime(day.year, day.month, day.day, 12, tzinfo=EASTERN).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def calendar(start: date, end: date, *, transport: Transport | None = None) -> list[dict]:
    """Exchange sessions in [start, end] with UTC open/close times, early closes included."""
    start, end = _dates(start, end)
    return _calendar(_Client(transport), start, end)


def _calendar(client: _Client, start: date, end: date) -> list[dict]:
    params = {"start": start.isoformat(), "end": end.isoformat()}
    rows = client.get(CALENDAR_HOST, "/v2/calendar", params)
    if not isinstance(rows, list):
        raise BarsError("Alpaca calendar must be a list")
    sessions: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise BarsError("Malformed Alpaca calendar row")
        day = _day(row.get("date"))
        if not start <= day <= end or day.isoformat() in sessions:
            raise BarsError(f"Alpaca calendar returned an unrequested or repeated date {day}")
        open_at, close_at = (_session_time(day, row.get(key)) for key in ("open", "close"))
        if open_at >= close_at:
            raise BarsError(f"Alpaca calendar session {day} does not open before it closes")
        sessions[day.isoformat()] = {
            "session": day.isoformat(),
            "open_at": timestamp(open_at.isoformat()),
            "close_at": timestamp(close_at.isoformat()),
        }
    return [sessions[day] for day in sorted(sessions)]


def _session_time(day: date, value: object) -> datetime:
    match = _CLOCK.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise BarsError(f"Alpaca calendar times must be HH:MM (session {day})")
    local = datetime(day.year, day.month, day.day, int(match[1]), int(match[2]), tzinfo=EASTERN)
    return local.astimezone(timezone.utc)


def daily_bars(
    symbols: list[str],
    start: date,
    end: date,
    *,
    feed: str = "sip",
    transport: Transport | None = None,
) -> dict[str, list[dict]]:
    """Raw daily OHLCV per requested symbol, sorted by session.

    Refuses an ``end`` whose session may be unfinished: every session through ``end``
    must be at least SETTLE_DELAY past the latest regular close (16:00 New York).
    """
    names = _names(symbols)
    start, end = _dates(start, end)
    latest_close = datetime(end.year, end.month, end.day, 16, tzinfo=EASTERN)
    if instant(utc_now()) < latest_close + SETTLE_DELAY:
        raise BarsError(f"Session {end} may be unfinished; request only settled sessions")
    feed = _feed(feed)
    result: dict[str, list[dict]] = {name: [] for name in names}
    if not names:
        return result
    client = _Client(transport)
    aliases = _aliases(names)
    for chunk in _chunks(list(aliases)):
        for alpaca_symbol, raw in _bar_records(client, chunk, start, end, feed):
            for name in aliases[alpaca_symbol]:
                row = _bar(name, raw)
                if start.isoformat() <= row["session"] <= end.isoformat():
                    result[name].append(row)
    for name, rows in result.items():
        rows.sort(key=lambda row: row["session"])
        if len({row["session"] for row in rows}) != len(rows):
            raise BarsError(f"Alpaca returned more than one {name} bar for a session")
    return result


def _bar_records(
    client: _Client, chunk: list[str], start: date, end: date, feed: str
) -> list[tuple[str, object]]:
    params = {
        "symbols": ",".join(chunk),
        "timeframe": "1Day",
        "start": _noon(start - timedelta(days=1)),
        "end": _noon(end),
        "limit": str(BAR_PAGE_LIMIT),
        "adjustment": "raw",
        "feed": feed,
        "sort": "asc",
    }
    wanted = set(chunk)
    records: list[tuple[str, object]] = []
    for page in _pages(client, DATA_HOST, "/v2/stocks/bars", params):
        if "bars" not in page:
            raise BarsError("Alpaca bars response has no bars field")
        bars = page["bars"] if page["bars"] is not None else {}
        if not isinstance(bars, dict):
            raise BarsError("Alpaca bars must be an object keyed by symbol")
        for alpaca_symbol, rows in bars.items():
            if alpaca_symbol not in wanted or not isinstance(rows, list):
                raise BarsError("Alpaca returned bars for an unrequested symbol or not as a list")
            records.extend((alpaca_symbol, raw) for raw in rows)
    return records


def _bar(name: str, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise BarsError(f"Malformed Alpaca bar for {name}")
    session = _bar_session(raw.get("t"), name)
    row: dict = {"symbol": name, "session": session}
    for key, field in _BAR_FIELDS:
        value = _finite(raw.get(field))
        if value is None or value < 0 or (key != "volume" and value == 0):
            raise BarsError(f"Alpaca bar {name} {session} has an invalid {key}")
        row[key] = value
    if not (
        row["low"]
        <= min(row["open"], row["close"])
        <= max(row["open"], row["close"])
        <= row["high"]
    ):
        raise BarsError(f"Alpaca bar {name} {session} has an inconsistent OHLC range")
    return row


def _bar_session(value: object, name: str) -> str:
    try:
        moment = instant(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None:
        raise BarsError(f"Alpaca bar for {name} has an invalid timestamp")
    # Alpaca stamps daily bars at midnight New York time; accept a UTC-midnight stamp too.
    for candidate in (moment.astimezone(EASTERN), moment):
        if candidate == candidate.replace(hour=0, minute=0, second=0, microsecond=0):
            return candidate.date().isoformat()
    raise BarsError(f"Alpaca daily bar for {name} is not stamped at midnight")


def corporate_actions(
    symbols: list[str], start: date, end: date, *, transport: Transport | None = None
) -> dict[tuple[str, str], dict]:
    """(symbol, ex_date) -> {"split_ratio": new/old, "cash_dividend": per share}.

    Same-day splits multiply and dividends add. A split and a cash dividend on one
    ex-date is flagged ``"ambiguous": True``: the dividend's per-share basis is unknown.
    """
    names = _names(symbols)
    start, end = _dates(start, end)
    if not names:
        return {}
    client = _Client(transport)
    aliases = _aliases(names)
    records: list[tuple[tuple[str, str], str, float]] = []
    for chunk in _chunks(list(aliases)):
        found, broken = _action_records(client, chunk, start, end)
        if broken:
            raise BarsError(f"Alpaca corporate actions are malformed for {', '.join(broken)}")
        records.extend(
            ((name, ex_date.isoformat()), kind, value)
            for alpaca_symbol, ex_date, kind, value in found
            if start <= ex_date <= end
            for name in aliases[alpaca_symbol]
        )
    return _combine(records)


def _action_records(
    client: _Client, chunk: list[str], start: date, end: date
) -> tuple[list[tuple[str, date, str, float]], list[str]]:
    """Valid actions, plus symbols with a malformed action (their bars cannot be trusted)."""
    params = {
        "symbols": ",".join(chunk),
        "types": ACTION_TYPES,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": str(ACTION_PAGE_LIMIT),
        "sort": "asc",
    }
    wanted = set(chunk)
    records: list[tuple[str, date, str, float]] = []
    broken: set[str] = set()
    seen: set[str] = set()
    for page in _pages(client, DATA_HOST, "/v1/corporate-actions", params):
        groups = page.get("corporate_actions")
        # An unexpected group may be a renamed type; silently missing a split corrupts labels.
        if not isinstance(groups, dict) or set(groups) - set(_ACTION_GROUPS):
            raise BarsError("Alpaca corporate actions must group only splits and cash dividends")
        for group, items in groups.items():
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                raise BarsError(f"Alpaca corporate actions {group} must be a list of objects")
            for item in items:
                name = item.get("symbol")
                if not isinstance(name, str):
                    raise BarsError("Alpaca corporate action without a symbol")
                identity = item.get("id")
                if name not in wanted or (isinstance(identity, str) and identity in seen):
                    continue
                if isinstance(identity, str):
                    seen.add(identity)
                record = _action(name, _ACTION_GROUPS[group], item)
                if record is None:
                    broken.add(name)
                else:
                    records.append(record)
    return records, sorted(broken)


def _action(name: str, kind: str, item: dict) -> tuple[str, date, str, float] | None:
    try:
        ex_date = _day(item.get("ex_date"))
    except BarsError:
        return None
    if kind == "split":
        new, old = _finite(item.get("new_rate")), _finite(item.get("old_rate"))
        if new is None or old is None or new <= 0 or old <= 0:
            return None
        return name, ex_date, kind, new / old
    rate = _finite(item.get("rate"))
    return None if rate is None or rate < 0 else (name, ex_date, kind, rate)


def _combine(records: Iterable[tuple[tuple[str, str], str, float]]) -> dict[tuple[str, str], dict]:
    result: dict[tuple[str, str], dict] = {}
    kinds: dict[tuple[str, str], set[str]] = {}
    for key, kind, value in records:
        entry = result.setdefault(key, {"split_ratio": 1.0, "cash_dividend": 0.0})
        if kind == "split":
            entry["split_ratio"] *= value
        else:
            entry["cash_dividend"] += value
        kinds.setdefault(key, set()).add(kind)
    for key, found in kinds.items():
        if len(found) > 1:
            result[key]["ambiguous"] = True
    return result


def fetch_forward(
    ledger,
    symbols: list[str],
    *,
    now: str,
    lookback_sessions: int = 45,
    transport: Transport | None = None,
    feed: str = "sip",
    benchmark: str = BENCHMARK,
) -> dict:
    """Store the last completed sessions as forward bars stamped with their actual receipt."""
    moment = instant(now)
    if moment > instant(utc_now()):
        raise BarsError("now cannot be later than the current time; unfinished bars could be kept")
    if (
        isinstance(lookback_sessions, bool)
        or not isinstance(lookback_sessions, int)
        or not 1 <= lookback_sessions <= MAX_LOOKBACK_SESSIONS
    ):
        raise BarsError(f"lookback_sessions must be an integer from 1 to {MAX_LOOKBACK_SESSIONS}")
    feed = _feed(feed)
    names = _names(symbols, first=(benchmark,))
    location = ledger_file(ledger)
    if location is not None and location == paths.research_ledger_path().resolve():
        # Forward bars there would shadow the backfill's historical ones for good.
        raise BarsError("Forward bars belong in the forward ledger, not the research ledger")
    client = _Client(transport)
    _require_provenance(ledger, names[0], "forward")
    today = moment.astimezone(EASTERN).date()
    since = today - timedelta(days=2 * lookback_sessions + CALENDAR_PADDING_DAYS)
    sessions = _calendar(client, since, today)
    completed = [s for s in sessions if instant(s["close_at"]) + SETTLE_DELAY <= moment]
    targets = [s["session"] for s in completed[-lookback_sessions:]]
    return _fetch(ledger, names, sessions, targets, mode="forward", client=client, feed=feed)


def fetch_historical(
    ledger,
    symbols: list[str],
    start: date,
    end: date,
    *,
    transport: Transport | None = None,
    feed: str = "sip",
    benchmark: str = BENCHMARK,
) -> dict:
    """Store completed sessions in [start, end] as historical bars available at their close.

    Close-time availability is an assumption, not a verified receipt, so this refuses
    the forward ledger: keep these bars in the research ledger.
    """
    start, end = _dates(start, end)
    feed = _feed(feed)
    names = _names(symbols, first=(benchmark,))
    location = ledger_file(ledger)
    if location is not None and location == paths.ledger_path().resolve():
        raise BarsError("Historical bars belong in the research ledger, not the forward ledger")
    client = _Client(transport)
    _require_provenance(ledger, names[0], "historical")
    sessions = _calendar(client, start - timedelta(days=CALENDAR_PADDING_DAYS), end)
    cutoff = instant(utc_now())
    targets = [
        s["session"]
        for s in sessions
        if s["session"] >= start.isoformat() and instant(s["close_at"]) + SETTLE_DELAY <= cutoff
    ]
    return _fetch(ledger, names, sessions, targets, mode="historical", client=client, feed=feed)


def _require_provenance(ledger, benchmark: str, mode: str) -> None:
    # Bar ids are symbol:session, so mixed provenance would silently shadow one another.
    modes = {bar.get("mode") for bar in ledger.prefix("bars", f"{benchmark}:")}
    if modes - {mode}:
        raise BarsError(
            f"This ledger already holds non-{mode} {benchmark} bars; keep {mode} bars "
            "in a separate ledger"
        )


def _fetch(
    ledger,
    names: list[str],
    sessions: list[dict],
    targets: list[str],
    *,
    mode: str,
    client: _Client,
    feed: str,
) -> dict:
    result: dict = {"symbols": len(names), "added": 0, "skipped": 0, "missing": 0, "errors": []}
    needed: dict[str, set[str]] = {}
    for name in names:
        for day in targets:
            if ledger.get("bars", f"{name}:{day}") is None:
                needed.setdefault(name, set()).add(day)
            else:
                result["skipped"] += 1
    days = [s["session"] for s in sessions]
    by_day = {s["session"]: s for s in sessions}
    aliases = _aliases(needed)
    for chunk in _chunks(list(aliases)):
        wanted = {name: needed[name] for key in chunk for name in aliases[key]}
        first = min(min(pending) for pending in wanted.values())
        last = max(max(pending) for pending in wanted.values())
        try:
            since = date.fromisoformat(first) - timedelta(days=ACTION_PADDING_DAYS)
            found, broken = _action_records(client, chunk, since, date.fromisoformat(last))
            raw_bars = _bar_records(
                client, chunk, date.fromisoformat(first), date.fromisoformat(last), feed
            )
        except BarsError as exc:
            if exc.status not in CHUNK_STATUSES:
                exc.partial = result
                raise
            result["errors"].append({"symbols": sorted(wanted), "error": str(exc)})
            continue
        actions = _session_actions(found, aliases, days)
        rows: dict[tuple[str, str], dict | None] = {}
        for key, raw in raw_bars:
            for name in aliases[key]:
                try:
                    parsed = _bar(name, raw)
                except BarsError as exc:
                    result["errors"].append({"symbol": name, "error": str(exc)})
                    continue
                identity = (name, parsed["session"])
                rows[identity] = None if identity in rows else parsed  # None marks a duplicate
        for (name, day), row in rows.items():
            error: str | None = None
            if day in wanted[name]:
                wanted[name].discard(day)
                action = actions.get((name, day), {})
                if row is None:
                    error = "Alpaca returned more than one bar for this session"
                elif _alpaca(name) in broken:
                    error = "Alpaca corporate actions for this symbol are malformed"
                elif action.get("ambiguous"):
                    error = "A split and a cash dividend share this ex-date; dividend basis unknown"
                else:
                    error = _store(ledger, row, by_day[day], action, mode, result)
            elif first <= day <= last and day not in by_day:
                error = "Alpaca returned a bar for a date that is not a session"
            if error:
                result["errors"].append({"symbol": name, "session": day, "error": error})
        result["missing"] += sum(len(pending) for pending in wanted.values())
    return result


def _session_actions(
    found: list[tuple[str, date, str, float]], aliases: dict[str, list[str]], days: list[str]
) -> dict[tuple[str, str], dict]:
    """Attach each action to the first session on or after its ex-date."""
    placed: list[tuple[tuple[str, str], str, float]] = []
    for key, ex_date, kind, value in found:
        index = bisect_left(days, ex_date.isoformat())
        # Before the calendar window the right session is unknown; after it, not yet due.
        if index == len(days) or (index == 0 and days[0] != ex_date.isoformat()):
            continue
        placed.extend(((name, days[index]), kind, value) for name in aliases[key])
    return _combine(placed)


def _store(ledger, row: dict, session: dict, action: dict, mode: str, result: dict) -> str | None:
    row = {
        **row,
        "open_at": session["open_at"],
        "close_at": session["close_at"],
        "split_ratio": action.get("split_ratio", 1.0),
        "cash_dividend": action.get("cash_dividend", 0.0),
    }
    try:
        bar = normalize_bar(row, mode=mode)
        added = ledger.put("bars", bar["id"], bar)
    except ValueError as exc:
        if ledger.get("bars", f"{row['symbol']}:{row['session']}") is None:
            return str(exc)
        added = False  # Another writer stored it first; its receipt is kept.
    result["added" if added else "skipped"] += 1
    return None


# Private aliases kept until every caller patches the public seams (P0-26, #30).
# Patching an alias does not change what the module calls.
_urlopen = urlopen
_validate_url = validate_url
