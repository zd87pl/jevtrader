"""Checked-in golden v1 and v2 ledgers still open, verify and migrate under the current code."""

import hashlib
import sqlite3
from pathlib import Path

import pytest
from builders import (
    GOLDEN_ROWS,
    GOLDEN_V1,
    GOLDEN_V2,
    build_v1,
    build_v2,
    disclosure,
    dump_sql,
    load_sql,
    seed_market,
    sessions,
)

from jevtrader.common import canonical, digest
from jevtrader.store import GENESIS, TRIGGERS, Ledger

# Pinned so an edited fixture or a changed chain formula fails here, not silently.
V1_SHA256 = "c1cf8f0571a4cebb2b2f39dc9da0c571f0f618475aeea0bc83634e144de06da6"
V2_SHA256 = "7abf6713d11245f20fcb5d0da292935f07f4f4332b007d22d26ddfe7994cf626"
GOLDEN_HEAD = {
    "seq": 4,
    "chain_hash": "69789d0948c7e40db0888e0b83dbd91616597c321e98ede7c2efeacce114bd96",
}
CHAIN_ORDER = [(kind, identity) for kind, identity, _, _ in GOLDEN_ROWS]


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path: Path, query: str) -> list[tuple]:
    db = sqlite3.connect(str(path))
    try:
        return db.execute(query).fetchall()
    finally:
        db.close()


RECORDS = "SELECT kind, id, payload, content_hash, recorded_at FROM records ORDER BY kind, id"
SCHEMA = "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
CHAIN = "SELECT seq, kind, id, content_hash, prev_hash, chain_hash FROM chain ORDER BY seq"


def expected_records() -> list[tuple]:
    return sorted(
        (kind, identity, canonical(payload), digest(payload), recorded_at)
        for kind, identity, payload, recorded_at in GOLDEN_ROWS
    )


def test_golden_files_are_pinned():
    assert file_sha(GOLDEN_V1) == V1_SHA256
    assert file_sha(GOLDEN_V2) == V2_SHA256


def test_golden_v1_is_a_schema_one_ledger_without_a_chain(tmp_path):
    path = load_sql(GOLDEN_V1, tmp_path / "v1.sqlite")
    assert rows(path, "PRAGMA user_version") == [(1,)]
    assert rows(path, "SELECT name FROM sqlite_master WHERE name='chain'") == []
    assert rows(path, RECORDS) == expected_records()
    with Ledger(path, readonly=True) as ledger:
        report = ledger.verify()
    assert (report["ok"], report["records"], report["chain_length"]) == (False, 4, 0)
    assert "has no hash chain" in report["problems"][0]


def test_golden_v1_migrates_and_verifies_to_the_pinned_head(tmp_path):
    path = load_sql(GOLDEN_V1, tmp_path / "v1.sqlite")
    with Ledger(path) as ledger:
        report = ledger.verify()
        head = ledger.head()
    assert report == {
        "ok": True,
        "records": 4,
        "chain_length": 4,
        "head": GOLDEN_HEAD["chain_hash"],
        "problems": [],
    }
    assert head == GOLDEN_HEAD
    assert rows(path, "PRAGMA user_version") == [(2,)]
    assert rows(path, RECORDS) == expected_records()
    assert [row[1:3] for row in rows(path, CHAIN)] == CHAIN_ORDER
    with Ledger(path, readonly=True) as ledger:
        assert ledger.verify(anchor=GOLDEN_HEAD)["ok"]


def test_golden_v2_verifies_read_only_and_matches_the_migrated_v1_chain(tmp_path):
    v1 = load_sql(GOLDEN_V1, tmp_path / "v1.sqlite")
    Ledger(v1).close()
    v2 = load_sql(GOLDEN_V2, tmp_path / "v2.sqlite")
    assert rows(v2, "PRAGMA user_version") == [(2,)]
    names = {name for (name,) in rows(v2, "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert names == set(TRIGGERS)
    with Ledger(v2, readonly=True) as ledger:
        assert ledger.verify(anchor=GOLDEN_HEAD) == {
            "ok": True,
            "records": 4,
            "chain_length": 4,
            "head": GOLDEN_HEAD["chain_hash"],
            "problems": [],
        }
    assert rows(v2, CHAIN) == rows(v1, CHAIN)
    assert rows(v2, CHAIN)[0][4] == GENESIS
    assert rows(v2, RECORDS) == expected_records()


def test_golden_v2_reopens_writable_unchanged_and_still_appends(tmp_path):
    path = load_sql(GOLDEN_V2, tmp_path / "v2.sqlite")
    before = rows(path, CHAIN)
    with Ledger(path) as ledger:
        assert rows(path, CHAIN) == before
        assert ledger.put("runs", "r-002", {"job": "bars"})
        report = ledger.verify(anchor=GOLDEN_HEAD)
    assert report["ok"] and report["chain_length"] == 5
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        sqlite3.connect(str(path)).execute("DELETE FROM chain")


@pytest.mark.parametrize(("build", "golden"), [(build_v1, GOLDEN_V1), (build_v2, GOLDEN_V2)])
def test_golden_files_are_reproduced_by_their_builders(tmp_path, build, golden):
    # Compared as contents, not dump text: iterdump output differs across Python versions.
    built, loaded = tmp_path / "built.sqlite", load_sql(golden, tmp_path / "golden.sqlite")
    build(built)
    for query in ("PRAGMA user_version", SCHEMA, RECORDS):
        assert rows(built, query) == rows(loaded, query)
    if golden == GOLDEN_V2:
        assert rows(built, CHAIN) == rows(loaded, CHAIN)


def test_dump_sql_round_trips_a_ledger(tmp_path):
    built = tmp_path / "built.sqlite"
    build_v2(built)
    dumped = tmp_path / "dump.sql"
    dumped.write_text(dump_sql(built), encoding="utf-8")
    restored = load_sql(dumped, tmp_path / "restored.sqlite")
    for query in ("PRAGMA user_version", SCHEMA, RECORDS, CHAIN):
        assert rows(restored, query) == rows(built, query)


def test_load_sql_refuses_to_overwrite(tmp_path):
    path = load_sql(GOLDEN_V1, tmp_path / "v1.sqlite")
    with pytest.raises(FileExistsError):
        load_sql(GOLDEN_V1, path)


def test_shared_builders_seed_aligned_weekday_bars():
    assert sessions(3) == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert sessions(6)[-1] == "2026-01-12"
    with Ledger(":memory:") as ledger:
        days = seed_market(ledger, count=3, symbols=("ABC", "SPY"), slopes={"SPY": 0.5})
        bars = {(bar["symbol"], bar["session"]): bar for bar in ledger.all("bars")}
    assert days == sessions(3)
    assert len(bars) == 6
    assert bars[("ABC", "2026-01-07")]["open"] == 102
    assert bars[("SPY", "2026-01-07")]["open"] == 101
    assert bars[("SPY", "2026-01-07")]["close"] == 101.5
    assert disclosure(symbol="XYZ")["symbol"] == "XYZ"
    assert disclosure()["mode"] == "historical"
