"""Schema 3 (ADR-0005): a per-ledger identity and recorded_at inside the hash chain."""

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from builders import GOLDEN_ROWS, GOLDEN_V1, GOLDEN_V2, load_sql

from jevtrader import store
from jevtrader.common import digest
from jevtrader.pit import FixedClock, Instant
from jevtrader.store import GENESIS, IDENTITY_TRIGGERS, SCHEMA_VERSION, Ledger, link_hash

GOLDEN_HEAD = {
    "seq": 4,
    "chain_hash": "69789d0948c7e40db0888e0b83dbd91616597c321e98ede7c2efeacce114bd96",
}
CLOCK = FixedClock(Instant.parse("2026-02-02T15:00:00Z"))
CHAIN = "SELECT seq, kind, id, content_hash, prev_hash, chain_hash FROM chain ORDER BY seq"
IDENTITY = (
    "SELECT nonce, created_at, migrated_from, legacy_seq, legacy_head, legacy_recorded_at, root "
    "FROM ledger_identity"
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def expected_root(nonce, created_at, migrated_from, legacy_seq, legacy_head, legacy_recorded):
    """Independent restatement of the ADR-0005 root formula."""
    fields = {
        "created_at": created_at,
        "legacy_head": legacy_head,
        "legacy_recorded_at": legacy_recorded,
        "legacy_seq": legacy_seq,
        "migrated_from": migrated_from,
        "nonce": nonce,
    }
    return sha(json.dumps(fields, sort_keys=True, separators=(",", ":")))


def expected_v3_link(prev, kind, identity, payload, recorded_at, root):
    return sha(f"{prev}|{kind}|{identity}|{digest(payload)}|{recorded_at}|{root}")


def legacy_digest(rows) -> str:
    return sha(json.dumps([list(row) for row in rows], separators=(",", ":")))


def raw(path: Path, query: str) -> list[tuple]:
    db = sqlite3.connect(str(path))
    try:
        return db.execute(query).fetchall()
    finally:
        db.close()


def fresh(nonce: str = "ab" * 16) -> Ledger:
    with patch("jevtrader.store.new_nonce", return_value=nonce):
        return Ledger(":memory:", clock=CLOCK)


def test_new_ledger_is_schema_three_with_a_creation_record():
    assert SCHEMA_VERSION == 3
    ledger = fresh()
    assert ledger.db.execute("PRAGMA user_version").fetchone()[0] == 3
    row = ledger.db.execute(IDENTITY).fetchone()
    created = CLOCK.now().iso()
    root = expected_root("ab" * 16, created, 0, 0, GENESIS, legacy_digest([]))
    assert row == ("ab" * 16, created, 0, 0, GENESIS, legacy_digest([]), root)
    assert ledger.identity() == "ab" * 16
    assert ledger.verify() == {
        "ok": True,
        "records": 0,
        "chain_length": 0,
        "head": GENESIS,
        "identity": "ab" * 16,
        "problems": [],
    }


def test_new_nonces_are_random_and_128_bits():
    first, second = store.new_nonce(), store.new_nonce()
    assert first != second
    assert len(first) == 32 and int(first, 16) >= 0


def test_v3_links_commit_to_recorded_at_and_the_root():
    ledger = fresh()
    root = ledger.db.execute("SELECT root FROM ledger_identity").fetchone()[0]
    payload = {"score": 1}
    ledger.put("forecasts", "a", payload)
    recorded = ledger.db.execute("SELECT recorded_at FROM records").fetchone()[0]
    one = expected_v3_link(GENESIS, "forecasts", "a", payload, recorded, root)
    assert ledger.db.execute(CHAIN).fetchall() == [
        (1, "forecasts", "a", digest(payload), GENESIS, one)
    ]
    assert link_hash(GENESIS, "forecasts", "a", digest(payload), recorded, root) == one
    assert ledger.verify()["head"] == one


def test_identical_ledgers_with_different_nonces_have_different_chains():
    heads = []
    for nonce in ("11" * 16, "22" * 16):
        ledger = fresh(nonce)
        ledger.put("forecasts", "a", {"score": 1})
        report = ledger.verify()
        assert report["ok"] and report["identity"] == nonce
        heads.append(report["head"])
    assert heads[0] != heads[1]


def test_edited_recorded_at_is_detected():
    ledger = fresh()
    ledger.put("forecasts", "a", {"score": 1})
    ledger.db.execute("DROP TRIGGER records_no_update")
    ledger.db.execute("UPDATE records SET recorded_at='2020-01-01T00:00:00.000000Z'")
    ledger.db.execute(store._SCHEMA[1])
    assert ledger.verify()["problems"] == [
        "Chain hash does not match its contents at chain seq 1 (forecasts/a)"
    ]


def test_identity_row_is_immutable():
    ledger = fresh()
    for query in (
        "UPDATE ledger_identity SET nonce='x'",
        "DELETE FROM ledger_identity",
        "INSERT INTO ledger_identity VALUES ('n', 'c', 0, 0, 'h', 'l', 'r')",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            ledger.db.execute(query)
    assert ledger.verify()["ok"]


def test_tampered_missing_or_unguarded_identity_is_reported():
    ledger = fresh()
    ledger.put("runs", "r1", {"job": "poll"})
    for name in IDENTITY_TRIGGERS:
        ledger.db.execute(f"DROP TRIGGER {name}")
    report = ledger.verify()
    assert report["problems"] == [
        f"Immutability trigger is missing: {name}" for name in sorted(IDENTITY_TRIGGERS)
    ]
    ledger.db.execute("UPDATE ledger_identity SET nonce=?", ("cd" * 16,))
    report = ledger.verify()
    assert report["identity"] == "cd" * 16
    assert "Ledger identity does not match its root" in report["problems"]
    assert (
        "Chain hash does not match its contents at chain seq 1 (runs/r1)"
        not in (report["problems"])
    )
    ledger.db.execute("UPDATE ledger_identity SET root=?", ("f" * 64,))
    assert (
        "Chain hash does not match its contents at chain seq 1 (runs/r1)"
        in (ledger.verify()["problems"])
    )
    ledger.db.execute("DELETE FROM ledger_identity")
    report = ledger.verify()
    assert report["identity"] is None
    assert "Ledger identity is missing" in report["problems"]
    ledger.db.execute("DROP TABLE ledger_identity")
    assert "Ledger identity is missing" in ledger.verify()["problems"]


def test_duplicate_identity_rows_are_reported():
    ledger = fresh()
    ledger.db.execute("DROP TRIGGER ledger_identity_single")
    ledger.db.execute("INSERT INTO ledger_identity SELECT * FROM ledger_identity")
    assert "Ledger identity must be one row, found 2" in ledger.verify()["problems"]


def test_anchor_may_name_the_identity():
    ledger = fresh()
    ledger.put("runs", "r1", {"job": "poll"})
    anchor = {**ledger.head(), "identity": "ab" * 16}
    assert ledger.verify(anchor=anchor)["ok"]
    report = ledger.verify(anchor={**anchor, "identity": "cd" * 16})
    assert report["problems"] == ["Ledger identity differs from the anchored identity"]
    with pytest.raises(ValueError, match="Anchor"):
        ledger.verify(anchor={**anchor, "identity": 7})


@pytest.mark.parametrize(("golden", "version"), [(GOLDEN_V1, 1), (GOLDEN_V2, 2)])
def test_golden_ledgers_migrate_to_three_and_keep_their_history(tmp_path, golden, version):
    path = load_sql(golden, tmp_path / "golden.sqlite")
    with patch("jevtrader.store.new_nonce", return_value="ef" * 16), Ledger(path, clock=CLOCK):
        pass
    assert raw(path, "PRAGMA user_version") == [(3,)]
    legacy = [(kind, identity, recorded_at) for kind, identity, _, recorded_at in GOLDEN_ROWS]
    created = CLOCK.now().iso()
    head = GOLDEN_HEAD["chain_hash"]
    root = expected_root("ef" * 16, created, version, 4, head, legacy_digest(legacy))
    assert raw(path, IDENTITY) == [
        ("ef" * 16, created, version, 4, head, legacy_digest(legacy), root)
    ]
    with Ledger(path, readonly=True) as ledger:
        report = ledger.verify(anchor=GOLDEN_HEAD)
    assert report == {
        "ok": True,
        "records": 4,
        "chain_length": 4,
        "head": head,
        "identity": "ef" * 16,
        "problems": [],
    }
    with Ledger(path, clock=CLOCK) as ledger:
        ledger.put("runs", "r-002", {"job": "bars"})
        report = ledger.verify(anchor={**GOLDEN_HEAD, "identity": "ef" * 16})
        last = ledger.db.execute(CHAIN).fetchall()[-1]
    assert report["ok"] and report["chain_length"] == 5
    recorded = CLOCK.now().iso()
    assert last[5] == expected_v3_link(head, "runs", "r-002", {"job": "bars"}, recorded, root)


def test_legacy_recorded_at_edit_is_detected_after_migration(tmp_path):
    path = load_sql(GOLDEN_V2, tmp_path / "v2.sqlite")
    Ledger(path).close()
    db = sqlite3.connect(str(path))
    db.execute("DROP TRIGGER records_no_update")
    db.execute("UPDATE records SET recorded_at='2020-01-01T00:00:00Z' WHERE id='r-001'")
    db.commit()
    db.close()
    with Ledger(path, readonly=True) as ledger:
        problems = ledger.verify()["problems"]
    assert "Legacy recorded_at digest does not match the ledger identity" in problems


def test_legacy_head_mismatch_is_detected(tmp_path):
    path = load_sql(GOLDEN_V2, tmp_path / "v2.sqlite")
    Ledger(path).close()
    db = sqlite3.connect(str(path))
    db.execute("DROP TRIGGER chain_no_update")
    db.execute("UPDATE chain SET chain_hash=? WHERE seq=4", ("f" * 64,))
    db.commit()
    db.close()
    with Ledger(path, readonly=True) as ledger:
        problems = ledger.verify()["problems"]
    assert "Ledger identity does not match the chain head at seq 4" in problems


def test_readonly_v2_verifies_without_an_identity(tmp_path):
    path = load_sql(GOLDEN_V2, tmp_path / "v2.sqlite")
    with Ledger(path, readonly=True) as ledger:
        assert ledger.version == 2
        assert ledger.identity() is None
        report = ledger.verify(anchor=GOLDEN_HEAD)
    assert (report["ok"], report["identity"]) == (True, None)
    assert raw(path, "PRAGMA user_version") == [(2,)]


def test_put_refuses_a_ledger_whose_identity_was_removed():
    ledger = fresh()
    ledger.db.execute("DROP TABLE ledger_identity")
    with pytest.raises(ValueError, match="identity is missing"):
        ledger.put("runs", "r1", {"job": "poll"})
    assert ledger.db.execute("SELECT COUNT(*) FROM records").fetchone() == (0,)


def test_a_writer_left_on_pre_upgrade_code_cannot_append_after_the_upgrade(tmp_path):
    # PIT-1: a schema-2 handle opened before another process upgraded the file used to append
    # a schema-2 link after legacy_seq, breaking verify for good.
    path = load_sql(GOLDEN_V2, tmp_path / "v2.sqlite")
    old = sqlite3.connect(str(path), isolation_level=None)  # the old code's handle
    try:
        old.execute("SELECT count(*) FROM chain").fetchone()
        Ledger(path, clock=CLOCK).close()  # new code upgrades the file to schema 3
        payload = {"job": "late"}
        head = old.execute("SELECT chain_hash FROM chain ORDER BY seq DESC LIMIT 1").fetchone()[0]
        old.execute("BEGIN IMMEDIATE")
        old.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
            ("runs", "late", json.dumps(payload), digest(payload), "2026-02-03T00:00:00Z"),
        )
        with pytest.raises(sqlite3.OperationalError, match="jevtrader_link_v3"):
            old.execute(
                "INSERT INTO chain (kind, id, content_hash, prev_hash, chain_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "runs",
                    "late",
                    digest(payload),
                    head,
                    link_hash(head, "runs", "late", digest(payload)),
                ),
            )
        old.execute("ROLLBACK")
    finally:
        old.close()
    with Ledger(path, readonly=True) as ledger:
        assert ledger.verify(anchor=GOLDEN_HEAD)["ok"]


def test_schema_two_links_are_refused_after_the_creation_record():
    ledger = fresh()
    ledger.put("runs", "r1", {"job": "poll"})
    payload = {"job": "raw"}
    ledger.db.execute(
        "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
        ("runs", "r2", json.dumps(payload), digest(payload), CLOCK.now().iso()),
    )
    head = ledger.head()["chain_hash"]
    with pytest.raises(sqlite3.IntegrityError, match="schema-3 formula"):
        ledger.db.execute(
            "INSERT INTO chain (kind, id, content_hash, prev_hash, chain_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            ("runs", "r2", digest(payload), head, link_hash(head, "runs", "r2", digest(payload))),
        )


def test_a_schema_three_ledger_without_the_link_trigger_gets_it_on_a_writable_open(tmp_path):
    path = tmp_path / "v3.sqlite"
    Ledger(path, clock=CLOCK).close()
    db = sqlite3.connect(str(path))
    db.execute("DROP TRIGGER chain_links_v3")
    db.commit()
    db.close()
    with Ledger(path, readonly=True) as ledger:
        problems = ledger.verify()["problems"]
    assert problems == ["Immutability trigger is missing: chain_links_v3"]
    with Ledger(path, clock=CLOCK) as ledger:
        assert ledger.verify()["ok"]


def test_put_refuses_when_another_process_changed_the_schema(tmp_path):
    path = tmp_path / "v3.sqlite"
    with Ledger(path, clock=CLOCK) as ledger:
        other = sqlite3.connect(str(path))
        other.execute("PRAGMA user_version=4")
        other.commit()
        other.close()
        with pytest.raises(ValueError, match="schema changed to 4"):
            ledger.put("runs", "r1", {"job": "poll"})
        assert ledger.db.execute("SELECT count(*) FROM records").fetchone() == (0,)


def test_an_anchor_carries_the_root_so_a_reused_nonce_is_caught():
    # PIT-4: the nonce is public once an anchor is published. A ledger forged with the same
    # (empty) legacy prefix and the same nonce passed a nonce-only anchor.
    original = fresh("ab" * 16)
    anchor = original.anchor()
    assert anchor == {
        "seq": 0,
        "chain_hash": GENESIS,
        "identity": "ab" * 16,
        "root": original.db.execute("SELECT root FROM ledger_identity").fetchone()[0],
    }
    assert original.verify(anchor=anchor)["ok"]
    with patch("jevtrader.store.new_nonce", return_value="ab" * 16):
        forged = Ledger(":memory:", clock=FixedClock(Instant.parse("2026-03-03T00:00:00Z")))
    nonce_only = {key: anchor[key] for key in ("seq", "chain_hash", "identity")}
    assert forged.verify(anchor=nonce_only)["ok"]  # why anchors must carry the root
    assert forged.verify(anchor=anchor)["problems"] == [
        "Ledger root differs from the anchored root"
    ]
    with pytest.raises(ValueError, match="Anchor"):
        original.verify(anchor={**anchor, "root": 7})


def test_anchor_of_a_legacy_ledger_has_no_identity(tmp_path):
    path = load_sql(GOLDEN_V2, tmp_path / "v2.sqlite")
    with Ledger(path, readonly=True) as ledger:
        assert ledger.anchor() == GOLDEN_HEAD
        report = ledger.verify(anchor={**GOLDEN_HEAD, "root": "f" * 64})
    assert report["problems"] == ["Ledger root differs from the anchored root"]
