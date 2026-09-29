"""Shared test builders: synthetic market bars, disclosures and golden ledger files.

Import as ``from builders import ...``; pytest puts ``tests/`` on ``sys.path``. Existing tests
keep their own copies until an independent reviewer approves moving them here (#23).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from jevtrader.bars import normalize_bar
from jevtrader.common import canonical, digest
from jevtrader.store import Ledger

FIXTURES = Path(__file__).with_name("fixtures")
GOLDEN_V1 = FIXTURES / "ledger_v1.sql"
GOLDEN_V2 = FIXTURES / "ledger_v2.sql"

# The schema-1 DDL, restated from the code that wrote v1 ledgers (records only, no chain).
V1_SCHEMA = """
    CREATE TABLE IF NOT EXISTS records (
        kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
        content_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY (kind, id)
    );
    CREATE TRIGGER IF NOT EXISTS records_no_update BEFORE UPDATE ON records
    BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records
    BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS records_no_replace BEFORE INSERT ON records
    WHEN EXISTS (SELECT 1 FROM records WHERE kind=NEW.kind AND id=NEW.id)
    BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END;
    PRAGMA user_version=1;
"""

Row = tuple[str, str, dict[str, Any], str]


def sessions(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    """Weekday ISO dates from ``start``; a fixture calendar, not an exchange calendar."""
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def seed_market(
    ledger: Ledger,
    count: int = 40,
    symbols: Sequence[str] = ("ABC", "SPY"),
    slopes: dict[str, float] | None = None,
) -> list[str]:
    """Aligned historical daily bars; each symbol's open rises by its slope (default 1.0)."""
    days = sessions(count)
    for index, session in enumerate(days):
        for ticker in symbols:
            price = 100 + index * (slopes or {}).get(ticker, 1.0)
            bar = normalize_bar(
                {
                    "symbol": ticker,
                    "session": session,
                    "open_at": f"{session}T14:30:00Z",
                    "close_at": f"{session}T21:00:00Z",
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price + 0.5,
                    "volume": 1_000_000,
                },
                mode="historical",
            )
            ledger.put("bars", bar["id"], bar)
    return days


def disclosure(**changes: Any) -> dict[str, Any]:
    """A synthetic historical 8-K disclosure event."""
    event: dict[str, Any] = {
        "id": "sec-accession-001",
        "symbol": "ABC",
        "text": "The company disclosed a new material agreement.",
        "source_url": "https://www.sec.gov/Archives/example.htm",
        "published_at": "2026-01-05T20:00:00Z",
        "first_seen_at": "2026-01-05T20:05:00Z",
        "mode": "historical",
    }
    event.update(changes)
    return event


def make_v1(path: Path, rows: Iterable[Row] = (), *, version: int = 1) -> None:
    """A ledger as the schema-1 code wrote it: records only, no chain."""
    db = sqlite3.connect(str(path))
    try:
        db.executescript(V1_SCHEMA)
        for kind, identity, payload, recorded_at in rows:
            db.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
                (kind, identity, canonical(payload), digest(payload), recorded_at),
            )
        db.execute(f"PRAGMA user_version={version}")
        db.commit()
    finally:
        db.close()


# Golden contents: rows in recorded_at order, so v1 migration and v2 puts chain identically.
GOLDEN_ROWS: tuple[Row, ...] = (
    ("disclosures", "sec-accession-001", disclosure(), "2026-01-05T20:05:00.000000Z"),
    (
        "bars",
        "ABC:2026-01-06",
        {"symbol": "ABC", "session": "2026-01-06", "open": 100.0, "close": 101.5},
        "2026-01-06T21:05:00.000000Z",
    ),
    (
        "forecasts",
        "f-001",
        {"event_id": "sec-accession-001", "direction": "up", "score": 0.25},
        "2026-01-06T21:10:00.000000Z",
    ),
    ("runs", "r-001", {"job": "poll", "status": "ok", "note": "synthetic"}, "2026-01-07T00:00:00Z"),
)


def build_v1(path: Path) -> None:
    make_v1(path, GOLDEN_ROWS)


def build_v2(path: Path) -> None:
    """A schema-2 ledger written by the current code, with the golden recorded_at times."""
    times = iter(row[3] for row in GOLDEN_ROWS)
    with patch("jevtrader.store.utc_now", side_effect=lambda: next(times)), Ledger(path) as led:
        for kind, identity, payload, _ in GOLDEN_ROWS:
            led.put(kind, identity, payload)


def dump_sql(path: Path) -> str:
    """A text dump of a ledger file that ``load_sql`` restores, including its user_version."""
    db = sqlite3.connect(str(path))
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        body = "\n".join(db.iterdump())
    finally:
        db.close()
    return f"{body}\nPRAGMA user_version={version};\n"


def load_sql(source: Path, path: Path) -> Path:
    """Restore a dumped ledger into a new SQLite file at ``path``."""
    if path.exists():
        raise FileExistsError(path)
    db = sqlite3.connect(str(path))
    try:
        db.executescript(source.read_text(encoding="utf-8"))
    finally:
        db.close()
    return path


def write_golden() -> None:  # pragma: no cover - run by hand to regenerate the fixtures
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        for build, target in ((build_v1, GOLDEN_V1), (build_v2, GOLDEN_V2)):
            scratch = Path(temp) / f"{target.stem}.sqlite"
            build(scratch)
            target.write_text(dump_sql(scratch), encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    write_golden()
