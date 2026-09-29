"""The one timestamp type and the injectable clock used by point-in-time reads.

An ``Instant`` is an aware UTC moment with microsecond precision. Its ``iso()`` form is
fixed-width (``YYYY-MM-DDTHH:MM:SS.ffffffZ``), so string order equals time order; the
ledger's knowledge index relies on that (ADR-0004)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True, order=True)
class Instant:
    moment: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.moment, datetime) or self.moment.utcoffset() is None:
            raise ValueError("Instant needs a timezone-aware datetime")
        object.__setattr__(self, "moment", self.moment.astimezone(timezone.utc))

    @classmethod
    def parse(cls, value: str) -> Instant:
        """Parse ISO 8601 with an explicit offset or ``Z``; naive values are rejected."""
        if not isinstance(value, str):
            raise ValueError("Timestamp must be an ISO 8601 string with an explicit timezone")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid timestamp: {value!r}") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"Timestamp needs an explicit timezone: {value!r}")
        return cls(parsed)

    @classmethod
    def coerce(cls, value: Instant | str | datetime) -> Instant:
        if isinstance(value, Instant):
            return value
        if isinstance(value, datetime):
            return cls(value)
        return cls.parse(value)

    def iso(self) -> str:
        return self.moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def __str__(self) -> str:
        return self.iso()


class Clock(Protocol):
    def now(self) -> Instant: ...


class SystemClock:
    def now(self) -> Instant:
        return Instant(datetime.now(timezone.utc))


@dataclass(frozen=True)
class FixedClock:
    """A clock that always reads the same instant, for tests and historical replays."""

    at: Instant

    def now(self) -> Instant:
        return self.at


SYSTEM_CLOCK: Clock = SystemClock()
