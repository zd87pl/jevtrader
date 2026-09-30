"""Point-in-time security master: CIK-ticker history, delistings and corporate actions.

Each fact is an immutable ``securities`` ledger record with two times (ADR-0006):

- ``effective``: the New York date the fact takes effect (valid time);
- ``known_at``: the instant this system could first know it (knowledge time), which is what
  ``Ledger.as_of`` filters on.

``known_at_basis`` says who set ``known_at``: ``observed`` when this system stamped it while
reading SEC (``ticker_snapshot``), ``asserted`` when an owner file or other import claims it.
An observed fact never reaches a read made before it was observed. An asserted ``known_at``
is only as honest as its file: a backdated one shows a late fact to earlier reads, so a read at
a past instant must exclude asserted facts (``from_ledger(..., observed_only=True)``) or label
its result with ``SecurityMaster.asserted``.

For one CIK, event type and effective date, the latest-known event wins, so a correction is
a new record, never an edit. A ticker event replaces the CIK's whole ticker set from its
effective date; a delisting ends it. Only public SEC data and owner-supplied files enter the
master; licensed vendor data is never redistributed (ADR-0006)."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, Literal, Protocol, TypedDict

from ..common import EASTERN, canonical
from .time import Instant

KIND = "securities"
EVENT_TYPES = ("ticker", "delisting", "split")
EventType = Literal["ticker", "delisting", "split"]
KnownAtBasis = Literal["observed", "asserted"]
KNOWN_AT_BASES = ("observed", "asserted")
_CIK = re.compile(r"\d{1,10}", re.A)
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.A)
_TICKER = re.compile(r"[A-Z][A-Z0-9.-]{0,11}", re.A)
_COMMON = ("type", "cik", "effective", "known_at", "source")
_FIELDS: dict[str, tuple[str, ...]] = {
    "ticker": (*_COMMON, "tickers"),
    "delisting": (*_COMMON, "delisting_return"),
    "split": (*_COMMON, "ratio"),
}


class SecurityEvent(TypedDict, total=False):
    type: EventType
    cik: str
    effective: str
    known_at: str
    known_at_basis: KnownAtBasis
    source: str
    tickers: list[str]
    delisting_return: float | None
    ratio: float


class _Ledger(Protocol):
    def as_of(self, kind: str, t: Instant | str, *, prefix: str | None = None) -> list[Any]: ...

    def put(self, kind: str, identity: str, payload: Any) -> bool: ...

    def now(self) -> str: ...


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Security event {name} must be a non-empty string")
    return value.strip()


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Security event {name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Security event {name} must be finite")
    return result


def _day(value: object) -> str:
    text = _text(value, "effective")
    if not _DAY.fullmatch(text):
        raise ValueError("Security event effective must be a YYYY-MM-DD date")
    return date.fromisoformat(text).isoformat()


def parse_event(value: object) -> SecurityEvent:
    """Validate and normalize one event; raises ValueError on anything unexpected."""
    if not isinstance(value, Mapping):
        raise ValueError("Security event must be a mapping")
    kind = value.get("type")
    if kind not in _FIELDS:
        raise ValueError(f"Security event type must be one of {EVENT_TYPES}")
    fields = _FIELDS[kind]
    if set(value) - {"known_at_basis"} != set(fields):
        raise ValueError(
            f"Security event {kind} needs exactly the fields {fields}, "
            "optionally with known_at_basis"
        )
    basis = value.get("known_at_basis", "asserted")
    if basis not in KNOWN_AT_BASES:
        raise ValueError(f"Security event known_at_basis must be one of {KNOWN_AT_BASES}")
    cik = _text(value["cik"], "cik")
    if not _CIK.fullmatch(cik) or int(cik) == 0:
        raise ValueError("Security event cik must be 1 to 10 digits and not zero")
    event: SecurityEvent = {
        "type": kind,
        "cik": cik.zfill(10),
        "effective": _day(value["effective"]),
        "known_at": Instant.parse(_text(value["known_at"], "known_at")).iso(),
        "known_at_basis": basis,
        "source": _text(value["source"], "source"),
    }
    if kind == "ticker":
        raw = value["tickers"]
        if not isinstance(raw, list) or not raw:
            raise ValueError("Security event tickers must be a non-empty list")
        tickers: list[str] = []
        for item in raw:
            symbol = _text(item, "ticker").upper()
            if not _TICKER.fullmatch(symbol):
                raise ValueError(f"Invalid ticker: {item!r}")
            if symbol not in tickers:
                tickers.append(symbol)
        event["tickers"] = tickers
    elif kind == "delisting":
        raw_return = value["delisting_return"]
        if raw_return is not None:
            raw_return = _finite(raw_return, "delisting_return")
            if raw_return < -1:
                raise ValueError("Security event delisting_return cannot be below -100%")
        event["delisting_return"] = raw_return
    else:
        ratio = _finite(value["ratio"], "ratio")
        if ratio <= 0:
            raise ValueError("Security event ratio must be positive")
        event["ratio"] = ratio
    return event


def event_id(event: Mapping[str, object]) -> str:
    """``cik:effective:type:`` plus a content hash, so a correction gets its own id."""
    content = hashlib.sha256(canonical(dict(event)).encode()).hexdigest()[:24]
    return f"{event['cik']}:{event['effective']}:{event['type']}:{content}"


def record_events(ledger: _Ledger, events: Iterable[object], *, observed: bool = False) -> int:
    """Validate every event first, then store them; returns how many were new.

    Only a caller that stamped ``known_at`` itself passes ``observed=True``; an ``observed``
    event from anywhere else (an owner file) is refused, as is one observed after
    ``ledger.now()``."""
    parsed = [parse_event(value) for value in events]
    now = Instant.parse(ledger.now())
    for event in parsed:
        if event["known_at_basis"] != "observed":
            continue
        if not observed:
            raise ValueError("Only the system may record observed security events")
        if Instant.parse(event["known_at"]) > now:
            raise ValueError("An observed security event cannot be known after now")
    return sum(ledger.put(KIND, event_id(event), dict(event)) for event in parsed)


def ticker_snapshot(table: Mapping[str, list[str]], at: Instant) -> list[SecurityEvent]:
    """Ticker events for SEC's current map, effective only from the day it was read.

    The map says nothing about the past, so its facts are never backdated."""
    day = at.moment.astimezone(EASTERN).date().isoformat()
    return [
        parse_event(
            {
                "type": "ticker",
                "cik": cik,
                "tickers": list(tickers),
                "effective": day,
                "known_at": at.iso(),
                "known_at_basis": "observed",
                "source": "sec_company_tickers",
            }
        )
        for cik, tickers in sorted(table.items())
    ]


class SecurityMaster:
    """A read-only view of the events known at one instant."""

    def __init__(self, events: Iterable[SecurityEvent]) -> None:
        latest: dict[tuple[str, str, str], SecurityEvent] = {}
        for event in events:
            key: tuple[str, str, str] = (event["cik"], event["type"], event["effective"])
            prior = latest.get(key)
            if prior is None or (prior["known_at"], event_id(prior)) < (
                event["known_at"],
                event_id(event),
            ):
                latest[key] = event
        self._events: dict[str, list[SecurityEvent]] = {}
        for key in sorted(latest):
            self._events.setdefault(key[0], []).append(latest[key])

    @property
    def asserted(self) -> bool:
        """True when a held fact's knowledge time is only asserted, not observed."""
        return any(
            event["known_at_basis"] == "asserted"
            for events in self._events.values()
            for event in events
        )

    @classmethod
    def from_ledger(
        cls,
        ledger: _Ledger,
        known_at: Instant | str | datetime,
        *,
        observed_only: bool = False,
    ) -> SecurityMaster:
        """Facts known at ``known_at``; ``observed_only`` drops those whose time is asserted."""
        rows = ledger.as_of(KIND, Instant.coerce(known_at))
        events = (parse_event(row) for row in rows)
        return cls(e for e in events if not observed_only or e["known_at_basis"] == "observed")

    def __bool__(self) -> bool:
        return bool(self._events)

    def _of(self, cik: str, kind: str) -> list[SecurityEvent]:
        return [event for event in self._events.get(cik, []) if event["type"] == kind]

    def delisting(self, cik: str) -> SecurityEvent | None:
        """The earliest known delisting of ``cik``, or None."""
        found = self._of(cik, "delisting")
        return min(found, key=lambda event: event["effective"]) if found else None

    def tickers(self, cik: str, on: date) -> tuple[str, ...]:
        day = on.isoformat()
        ended = self.delisting(cik)
        if ended is not None and ended["effective"] <= day:
            return ()
        current: tuple[str, ...] = ()
        for event in self._of(cik, "ticker"):
            if event["effective"] <= day:
                current = tuple(event["tickers"])
        return current

    def table(self, on: date) -> dict[str, list[str]]:
        """CIK -> tickers listed on ``on``, in the shape feeds' ticker table uses."""
        result = {cik: list(self.tickers(cik, on)) for cik in sorted(self._events)}
        return {cik: tickers for cik, tickers in result.items() if tickers}

    def cik_for(self, ticker: str, on: date) -> str | None:
        wanted = ticker.strip().upper()
        for cik in sorted(self._events):
            if wanted in self.tickers(cik, on):
                return cik
        return None

    def split_factor(self, cik: str, after: date, through: date) -> float:
        """Product of split ratios with ex-date in (after, through]: multiply share counts by
        it, divide prices by it, to bring a raw value from ``after`` to ``through`` terms."""
        if after > through:
            raise ValueError("after must not be later than through")
        factor = 1.0
        low, high = after.isoformat(), through.isoformat()
        for event in self._of(cik, "split"):
            if low < event["effective"] <= high:
                factor *= event["ratio"]
        return factor
