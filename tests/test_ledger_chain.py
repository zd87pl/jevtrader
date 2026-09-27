"""Hash-chain, schema-migration, read-only and single-transaction tests for the ledger."""

import hashlib
import itertools
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jevtrader import store
from jevtrader.common import canonical, digest
from jevtrader.store import GENESIS, MAX_PROBLEMS, SCHEMA_VERSION, TRIGGERS, Ledger, link_hash

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


def expected_link(prev: str, kind: str, identity: str, payload: dict) -> str:
    """Independent restatement of the documented chain formula."""
    preimage = f"{prev}|{kind}|{identity}|{digest(payload)}"
    return hashlib.sha256(preimage.encode()).hexdigest()


def make_v1(path: Path, rows=(), *, version: int = 1) -> None:
    """A ledger written by the schema-1 code: records only, no chain."""
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(V1_SCHEMA)
    for kind, identity, payload, recorded_at in rows:
        db.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
            (kind, identity, canonical(payload), digest(payload), recorded_at),
        )
    db.execute(f"PRAGMA user_version={version}")
    db.commit()
    db.close()


def raw(path: Path, query: str, params=()):
    db = sqlite3.connect(str(path))
    try:
        return db.execute(query, params).fetchall()
    finally:
        db.close()


def chain_rows(ledger: Ledger) -> list[tuple]:
    return ledger.db.execute(
        "SELECT seq, kind, id, content_hash, prev_hash, chain_hash FROM chain ORDER BY seq"
    ).fetchall()


def disclosure(**changes) -> dict:
    event = {
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


class FileCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.path = self.dir / "ledger.sqlite"

    def ledger(self, path=None, **options) -> Ledger:
        result = Ledger(path or self.path, **options)
        self.addCleanup(result.db.close)
        return result


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)

    def test_new_ledger_is_schema_two_with_an_empty_chain(self):
        self.assertEqual(self.ledger.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(GENESIS, "0" * 64)
        self.assertEqual(self.ledger.head(), {"seq": 0, "chain_hash": GENESIS})
        self.assertEqual(
            self.ledger.verify(),
            {"ok": True, "records": 0, "chain_length": 0, "head": GENESIS, "problems": []},
        )
        triggers = {
            name
            for (name,) in self.ledger.db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        self.assertEqual(triggers, set(TRIGGERS))

    def test_put_links_each_record_to_the_previous_head(self):
        first, second = {"score": 0.25}, {"nested": {"reason": "new fact"}, "score": -1}
        self.assertTrue(self.ledger.put("forecasts", "a", first))
        self.assertTrue(self.ledger.put("bars", "ABC:2026-01-05", second))
        rows = chain_rows(self.ledger)
        one = expected_link(GENESIS, "forecasts", "a", first)
        two = expected_link(one, "bars", "ABC:2026-01-05", second)
        self.assertEqual(
            rows,
            [
                (1, "forecasts", "a", digest(first), GENESIS, one),
                (2, "bars", "ABC:2026-01-05", digest(second), one, two),
            ],
        )
        self.assertEqual(self.ledger.head(), {"seq": 2, "chain_hash": two})
        self.assertEqual(link_hash(one, "bars", "ABC:2026-01-05", digest(second)), two)
        result = self.ledger.verify()
        self.assertEqual((result["ok"], result["chain_length"], result["head"]), (True, 2, two))

    def test_chain_commits_to_the_same_hash_as_the_record(self):
        payload = {"text": "café — \U0001f600", "value": 1.5}
        self.ledger.put("forecasts", "unicode|id", payload)
        stored = self.ledger.db.execute("SELECT content_hash FROM records").fetchone()[0]
        self.assertEqual(stored, digest(payload))
        self.assertEqual(chain_rows(self.ledger)[0][3], stored)
        self.assertTrue(self.ledger.verify()["ok"])

    def test_repeat_and_conflicting_puts_do_not_extend_the_chain(self):
        payload = {"score": 1}
        self.ledger.put("forecasts", "a", payload)
        head = self.ledger.head()
        self.assertFalse(self.ledger.put("forecasts", "a", {"score": 1}))
        with self.assertRaisesRegex(ValueError, "Immutable record conflict: forecasts/a"):
            self.ledger.put("forecasts", "a", {"score": 2})
        self.assertEqual(self.ledger.head(), head)
        self.assertEqual(len(chain_rows(self.ledger)), 1)
        self.assertFalse(self.ledger.db.in_transaction)

    def test_invalid_puts_write_nothing(self):
        for kind, identity, payload in (
            ("unknown", "a", {}),
            ("forecasts", "", {}),
            ("forecasts", 7, {}),
            ("forecasts", "a", {"score": float("nan")}),
            ("forecasts", "a", {"when": object()}),
        ):
            with (
                self.subTest(kind=kind, identity=identity),
                self.assertRaises((ValueError, TypeError)),
            ):
                self.ledger.put(kind, identity, payload)
        self.assertEqual(self.ledger.counts(), {})
        self.assertEqual(chain_rows(self.ledger), [])
        self.assertFalse(self.ledger.db.in_transaction)

    def test_failure_inside_the_transaction_rolls_back_record_and_chain(self):
        with (
            patch("jevtrader.store.link_hash", side_effect=RuntimeError("disk full")),
            self.assertRaisesRegex(RuntimeError, "disk full"),
        ):
            self.ledger.put("forecasts", "a", {"score": 1})
        self.assertIsNone(self.ledger.get("forecasts", "a"))
        self.assertEqual(chain_rows(self.ledger), [])
        self.assertFalse(self.ledger.db.in_transaction)
        self.assertTrue(self.ledger.put("forecasts", "a", {"score": 1}))
        self.assertTrue(self.ledger.verify()["ok"])

    def test_disclosures_are_chained_and_repeat_observations_are_not(self):
        original = disclosure(mode="forward")
        with patch("jevtrader.store.utc_now", return_value="2026-01-07T22:00:00Z"):
            self.assertTrue(self.ledger.disclosure(original, imported=False))
            self.assertFalse(
                self.ledger.disclosure(
                    {**original, "first_seen_at": "2026-01-07T21:00:00Z"}, imported=False
                )
            )
            with self.assertRaisesRegex(ValueError, "content changed"):
                self.ledger.disclosure({**original, "text": "Rewritten"}, imported=False)
        self.assertEqual(len(chain_rows(self.ledger)), 1)
        self.assertEqual(chain_rows(self.ledger)[0][1:3], ("disclosures", original["id"]))
        self.assertTrue(self.ledger.verify()["ok"])

    def test_imported_disclosure_correction_is_still_a_conflict(self):
        original = disclosure()
        self.assertTrue(self.ledger.disclosure(original))
        self.assertFalse(self.ledger.disclosure(dict(original)))
        with self.assertRaisesRegex(ValueError, "content changed"):
            self.ledger.disclosure({**original, "first_seen_at": "2026-01-06T14:31:00Z"})
        self.assertEqual(len(chain_rows(self.ledger)), 1)

    def test_chain_rows_cannot_be_changed_with_sql(self):
        self.ledger.put("forecasts", "a", {"score": 1})
        self.ledger.put("forecasts", "b", {"score": 2})
        # An unchained record lets each REPLACE below pass every check except immutability.
        extra = {"score": 3}
        self.ledger.db.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
            ("forecasts", "c", canonical(extra), digest(extra), "2026-01-01T00:00:00Z"),
        )
        head = self.ledger.head()["chain_hash"]
        for query, params in (
            ("UPDATE chain SET chain_hash=? WHERE seq=1", (GENESIS,)),
            ("DELETE FROM chain WHERE seq=2", ()),
            (
                "INSERT OR REPLACE INTO chain (kind, id, content_hash, prev_hash, chain_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                ("forecasts", "a", digest({"score": 1}), head, "x"),
            ),
            (
                "INSERT OR REPLACE INTO chain VALUES (?, ?, ?, ?, ?, ?)",
                (1, "forecasts", "c", digest(extra), head, "x"),
            ),
        ):
            with (
                self.subTest(query=query),
                self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"),
            ):
                self.ledger.db.execute(query, params)
        self.assertEqual(len(chain_rows(self.ledger)), 2)
        self.assertEqual(
            self.ledger.verify()["problems"], ["Record is missing from the chain: forecasts/c"]
        )

    def test_raw_chain_inserts_must_extend_the_head_and_match_a_record(self):
        payload = {"score": 3}
        self.ledger.put("forecasts", "a", {"score": 1})
        head = self.ledger.head()["chain_hash"]
        self.ledger.db.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
            ("forecasts", "c", canonical(payload), digest(payload), "2026-01-01T00:00:00Z"),
        )
        insert = (
            "INSERT INTO chain (kind, id, content_hash, prev_hash, chain_hash) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        for params, message in (
            (("forecasts", "c", digest(payload), GENESIS, "x"), "extend its head"),
            (("forecasts", "c", digest({"score": 4}), head, "x"), "match a stored record"),
            (("forecasts", "missing", digest(payload), head, "x"), "match a stored record"),
        ):
            with (
                self.subTest(params=params),
                self.assertRaisesRegex(sqlite3.IntegrityError, message),
            ):
                self.ledger.db.execute(insert, params)

    def test_link_hash_separators_keep_ids_distinct(self):
        content = digest({"a": 1})
        self.assertNotEqual(
            link_hash(GENESIS, "bars", "a|b", content), link_hash(GENESIS, "bars", "a", content)
        )
        self.assertNotEqual(
            link_hash(GENESIS, "bars", "x", content), link_hash(GENESIS, "runs", "x", content)
        )


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)
        for index, identity in enumerate("abc"):
            self.ledger.put("forecasts", identity, {"score": index, "text": "SECRET-TOKEN-123"})

    def drop(self, *triggers):
        for name in triggers:
            self.ledger.db.execute(f"DROP TRIGGER {name}")

    def restore_triggers(self):
        for statement in store._SCHEMA:
            self.ledger.db.execute(statement)

    def assertProblems(self, expected: list[str]):
        result = self.ledger.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["problems"], expected)
        for problem in result["problems"]:
            self.assertNotIn("SECRET", problem)
        return result

    def test_clean_ledger_verifies_and_reports_the_head(self):
        before = self.ledger.db.total_changes
        result = self.ledger.verify()
        self.assertEqual(
            result,
            {
                "ok": True,
                "records": 3,
                "chain_length": 3,
                "head": self.ledger.head()["chain_hash"],
                "problems": [],
            },
        )
        self.assertEqual(self.ledger.db.total_changes, before)

    def test_edited_payload_is_reported_without_leaking_content(self):
        self.drop("records_no_update")
        self.ledger.db.execute(
            "UPDATE records SET payload=? WHERE id='b'",
            (canonical({"score": 99, "text": "SECRET-TOKEN-123"}),),
        )
        self.restore_triggers()
        self.assertProblems(["Record content does not match its hash: forecasts/b"])

    def test_non_canonical_or_unreadable_payload_is_reported(self):
        self.drop("records_no_update")
        for payload in ('{"score": 1, "text": "SECRET-TOKEN-123"}', "{not json", "NaN"):
            with self.subTest(payload=payload):
                self.ledger.db.execute("UPDATE records SET payload=? WHERE id='b'", (payload,))
                result = self.ledger.verify()
                self.assertIn(
                    "Record content does not match its hash: forecasts/b", result["problems"]
                )

    def test_rehashed_record_no_longer_matches_its_chain_entry(self):
        changed = {"score": 99, "text": "SECRET-TOKEN-123"}
        self.drop("records_no_update")
        self.ledger.db.execute(
            "UPDATE records SET payload=?, content_hash=? WHERE id='b'",
            (canonical(changed), digest(changed)),
        )
        self.restore_triggers()
        self.assertProblems(["Record hash differs from its chain entry: forecasts/b"])

    def test_deleted_middle_entry_breaks_the_link(self):
        self.drop("chain_no_delete")
        self.ledger.db.execute("DELETE FROM chain WHERE seq=2")
        self.restore_triggers()
        self.assertProblems(
            [
                "Chain link is broken at chain seq 3 (forecasts/c)",
                "Record is missing from the chain: forecasts/b",
            ]
        )

    def test_deleted_tail_entry_is_reported_as_truncation(self):
        self.drop("chain_no_delete")
        self.ledger.db.execute("DELETE FROM chain WHERE seq=3")
        self.restore_triggers()
        result = self.assertProblems(
            [
                "Record is missing from the chain: forecasts/c",
                "Chain was truncated: entries reached seq 3, head is 2",
            ]
        )
        self.assertEqual(result["chain_length"], 2)

    def test_deleted_record_leaves_an_orphan_entry(self):
        self.drop("records_no_delete")
        self.ledger.db.execute("DELETE FROM records WHERE id='a'")
        self.restore_triggers()
        result = self.assertProblems(["Chain entry has no record: chain seq 1 (forecasts/a)"])
        self.assertEqual((result["records"], result["chain_length"]), (2, 3))

    def test_edited_chain_hash_is_reported_at_that_entry_and_the_next(self):
        self.drop("chain_no_update")
        self.ledger.db.execute("UPDATE chain SET chain_hash=? WHERE seq=2", ("f" * 64,))
        self.restore_triggers()
        self.assertProblems(
            [
                "Chain hash does not match its contents at chain seq 2 (forecasts/b)",
                "Chain link is broken at chain seq 3 (forecasts/c)",
            ]
        )

    def test_record_inserted_around_the_chain_is_reported(self):
        payload = {"score": 5}
        self.ledger.db.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
            ("runs", "sneaky", canonical(payload), digest(payload), "2026-01-01T00:00:00Z"),
        )
        self.assertProblems(["Record is missing from the chain: runs/sneaky"])

    def test_dropped_triggers_are_reported(self):
        self.drop("records_no_update", "chain_extends_head")
        self.assertProblems(
            [
                "Immutability trigger is missing: chain_extends_head",
                "Immutability trigger is missing: records_no_update",
            ]
        )

    def test_missing_chain_table_is_reported_not_raised(self):
        self.ledger.db.execute("DROP TABLE chain")
        result = self.assertProblems(["Chain table is missing"])
        self.assertEqual((result["records"], result["chain_length"]), (3, 0))

    def test_rewritten_head_needs_an_anchor_to_be_detected(self):
        anchor = self.ledger.head()
        early = {"seq": 1, "chain_hash": chain_rows(self.ledger)[0][5]}
        changed = {"score": 99}
        self.drop("records_no_update", "chain_no_update")
        prev = chain_rows(self.ledger)[1][5]
        self.ledger.db.execute(
            "UPDATE records SET payload=?, content_hash=? WHERE id='c'",
            (canonical(changed), digest(changed)),
        )
        self.ledger.db.execute(
            "UPDATE chain SET content_hash=?, chain_hash=? WHERE seq=3",
            (digest(changed), expected_link(prev, "forecasts", "c", changed)),
        )
        self.restore_triggers()
        self.assertTrue(self.ledger.verify()["ok"])
        self.assertTrue(self.ledger.verify(anchor=early)["ok"])
        result = self.ledger.verify(anchor=anchor)
        self.assertEqual(result["problems"], ["Chain differs from the anchored head at seq 3"])

    def test_anchor_checks(self):
        head = self.ledger.head()
        self.assertTrue(self.ledger.verify(anchor=head)["ok"])
        self.ledger.put("forecasts", "d", {"score": 4})
        self.assertTrue(self.ledger.verify(anchor=head)["ok"])
        self.assertTrue(self.ledger.verify(anchor={"seq": 0, "chain_hash": GENESIS})["ok"])
        for anchor, problem in (
            ({"seq": 9, "chain_hash": GENESIS}, "Anchored head seq 9 is not in the chain"),
            ({"seq": 0, "chain_hash": "f" * 64}, "Chain differs from the anchored head at seq 0"),
            ({"seq": 2, "chain_hash": head["chain_hash"]}, "anchored head at seq 2"),
        ):
            with self.subTest(anchor=anchor):
                result = self.ledger.verify(anchor=anchor)
                self.assertFalse(result["ok"])
                self.assertIn(problem, result["problems"][0])
        for anchor in (
            "head",
            {"seq": "3", "chain_hash": GENESIS},
            {"seq": True, "chain_hash": GENESIS},
            {"seq": -1, "chain_hash": GENESIS},
            {"seq": 3},
        ):
            with self.subTest(anchor=anchor), self.assertRaisesRegex(ValueError, "Anchor"):
                self.ledger.verify(anchor=anchor)

    def test_problem_list_is_bounded(self):
        for index in range(MAX_PROBLEMS + 20):
            self.ledger.put("bars", f"X:{index:04d}", {"close": index})
        self.drop("records_no_update")
        self.ledger.db.execute("UPDATE records SET payload='{}'")
        result = self.ledger.verify()
        self.assertEqual(len(result["problems"]), MAX_PROBLEMS + 1)
        self.assertRegex(result["problems"][-1], r"^\.\.\. and \d+ more problems$")
        self.assertEqual(result["records"], MAX_PROBLEMS + 23)


class OpenModeTests(FileCase):
    def seed(self) -> dict:
        with Ledger(self.path) as ledger:
            ledger.put("forecasts", "a", {"score": 1})
            ledger.put("bars", "ABC:2026-01-05", {"close": 10})
            return ledger.head()

    def test_default_open_still_creates_parent_directories(self):
        path = self.dir / "nested" / "deeper" / "ledger.sqlite"
        self.ledger(path).put("runs", "r1", {"job": "poll"})
        self.assertTrue(path.is_file())

    def test_create_false_refuses_a_missing_file_without_creating_anything(self):
        path = self.dir / "absent" / "ledger.sqlite"
        with self.assertRaises(ValueError) as caught:
            Ledger(path, create=False)
        self.assertEqual(str(caught.exception), f"No ledger at {path}; run init first")
        self.assertFalse(path.parent.exists())
        with self.assertRaisesRegex(ValueError, "No ledger at .*; run init first"):
            Ledger(self.path, create=False)
        self.assertFalse(self.path.exists())

    def test_create_false_refuses_files_that_are_not_ledgers(self):
        self.path.touch()
        with self.assertRaisesRegex(ValueError, "run init first"):
            Ledger(self.path, create=False)
        self.assertEqual(self.path.stat().st_size, 0)
        other = self.dir / "other.sqlite"
        raw(other, "CREATE TABLE notes (x)")
        with self.assertRaisesRegex(ValueError, "run init first"):
            Ledger(other, create=False)
        self.assertEqual(
            raw(other, "SELECT name FROM sqlite_master WHERE type='table'"), [("notes",)]
        )
        self.assertEqual(raw(other, "PRAGMA user_version"), [(0,)])

    def test_create_false_opens_existing_ledgers_for_writing(self):
        head = self.seed()
        ledger = self.ledger(create=False)
        self.assertEqual(ledger.head(), head)
        self.assertTrue(ledger.put("runs", "r1", {"job": "poll"}))
        self.assertTrue(ledger.verify()["ok"])

    def test_memory_ledgers_cannot_be_readonly_or_must_exist(self):
        for options in ({"readonly": True}, {"create": False}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "in-memory"):
                Ledger(":memory:", **options)

    def test_readonly_reads_and_verifies_but_cannot_write(self):
        head = self.seed()
        ledger = self.ledger(readonly=True)
        self.assertTrue(ledger.readonly)
        self.assertEqual(ledger.get("forecasts", "a"), {"score": 1})
        self.assertEqual(ledger.prefix("bars", "ABC:"), [{"close": 10}])
        self.assertEqual(ledger.counts(), {"bars": 1, "forecasts": 1})
        self.assertEqual(ledger.head(), head)
        self.assertTrue(ledger.verify(anchor=head)["ok"])
        with self.assertRaisesRegex(ValueError, "read-only"):
            ledger.put("runs", "r1", {"job": "poll"})
        with self.assertRaisesRegex(ValueError, "read-only"):
            ledger.disclosure(disclosure())
        with self.assertRaisesRegex(ValueError, "read-only"):
            ledger.put("forecasts", "a", {"score": 1})
        with self.assertRaises(sqlite3.OperationalError):
            ledger.db.execute("CREATE TABLE extra (x)")
        self.assertFalse(ledger.db.in_transaction)
        self.assertEqual(ledger.counts(), {"bars": 1, "forecasts": 1})

    def test_readonly_open_never_writes_the_file(self):
        self.seed()
        raw(self.path, "PRAGMA journal_mode=DELETE")
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with Ledger(self.path, readonly=True) as ledger:
            ledger.all("forecasts")
            ledger.verify()
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual(raw(self.path, "PRAGMA journal_mode"), [("delete",)])

    def test_readonly_missing_path_is_refused_without_mkdir(self):
        path = self.dir / "absent" / "ledger.sqlite"
        with self.assertRaisesRegex(ValueError, "No ledger at .*; run init first"):
            Ledger(path, readonly=True)
        self.assertFalse(path.parent.exists())
        self.path.touch()
        with self.assertRaisesRegex(ValueError, "run init first"):
            Ledger(self.path, readonly=True)
        self.assertEqual(self.path.stat().st_size, 0)

    def test_readonly_uri_escapes_unusual_paths(self):
        path = self.dir / "odd dir #1 ?x=y %20" / "led ger?mode=rw.sqlite"
        with Ledger(path) as ledger:
            ledger.put("runs", "r1", {"job": "poll"})
        ledger = self.ledger(path, readonly=True)
        self.assertEqual(ledger.get("runs", "r1"), {"job": "poll"})
        with self.assertRaisesRegex(ValueError, "read-only"):
            ledger.put("runs", "r2", {"job": "bars"})
        with self.assertRaises(sqlite3.OperationalError):
            ledger.db.execute("DELETE FROM chain")

    def test_readonly_reader_sees_a_concurrent_writers_commits(self):
        writer = self.ledger()
        writer.put("runs", "r1", {"job": "poll"})
        reader = self.ledger(readonly=True)
        writer.db.execute("BEGIN IMMEDIATE")
        self.assertEqual(reader.counts(), {"runs": 1})
        writer.db.execute("ROLLBACK")
        writer.put("runs", "r2", {"job": "bars"})
        self.assertEqual(reader.get("runs", "r2"), {"job": "bars"})
        self.assertEqual(reader.head(), writer.head())

    def test_unsupported_versions_fail_closed_in_every_mode(self):
        for version in (3, 99, -1):
            path = self.dir / f"v{version}.sqlite"
            make_v1(
                path, [("runs", "r1", {"job": "poll"}, "2026-01-01T00:00:00Z")], version=version
            )
            for options in ({}, {"create": False}, {"readonly": True}):
                with (
                    self.subTest(version=version, options=options),
                    self.assertRaisesRegex(ValueError, f"Unsupported ledger schema {version}"),
                ):
                    Ledger(path, **options)
            self.assertEqual(raw(path, "PRAGMA user_version"), [(version,)])
            self.assertEqual(raw(path, "SELECT name FROM sqlite_master WHERE name='chain'"), [])


class MigrationTests(FileCase):
    ROWS = [
        ("forecasts", "b", {"score": 2}, "2026-01-02T00:00:00.000000Z"),
        ("bars", "z", {"close": 1}, "2026-01-01T00:00:00.000000Z"),
        ("bars", "a", {"close": 3}, "2026-01-02T00:00:00.000000Z"),
        ("attempts", "q", {"status": "failed"}, "2026-01-02T00:00:00.000000Z"),
    ]

    def test_v1_is_backfilled_in_recorded_at_kind_id_order(self):
        make_v1(self.path, self.ROWS)
        before = raw(self.path, "SELECT * FROM records ORDER BY kind, id")
        ledger = self.ledger()
        self.assertEqual(ledger.version, 2)
        self.assertEqual(raw(self.path, "PRAGMA user_version"), [(2,)])
        order = [("bars", "z"), ("attempts", "q"), ("bars", "a"), ("forecasts", "b")]
        payloads = {(kind, identity): payload for kind, identity, payload, _ in self.ROWS}
        expected, prev = [], GENESIS
        for seq, (kind, identity) in enumerate(order, 1):
            link = expected_link(prev, kind, identity, payloads[(kind, identity)])
            expected.append((seq, kind, identity, digest(payloads[(kind, identity)]), prev, link))
            prev = link
        self.assertEqual(chain_rows(ledger), expected)
        self.assertEqual(ledger.head(), {"seq": 4, "chain_hash": prev})
        self.assertEqual(raw(self.path, "SELECT * FROM records ORDER BY kind, id"), before)
        self.assertTrue(ledger.verify()["ok"])
        ledger.put("runs", "r1", {"job": "poll"})
        self.assertEqual(chain_rows(ledger)[-1][4], prev)
        self.assertTrue(ledger.verify()["ok"])

    def test_reopening_a_migrated_ledger_is_a_no_op(self):
        make_v1(self.path, self.ROWS)
        with Ledger(self.path) as ledger:
            rows = chain_rows(ledger)
        for options in ({}, {"create": False}, {"readonly": True}):
            with self.subTest(options=options), Ledger(self.path, **options) as ledger:
                self.assertEqual(chain_rows(ledger), rows)

    def test_create_false_still_upgrades_an_existing_v1_ledger(self):
        make_v1(self.path, self.ROWS)
        ledger = self.ledger(create=False)
        self.assertEqual(ledger.verify()["chain_length"], 4)

    def test_migrated_ledger_keeps_the_old_record_triggers(self):
        make_v1(self.path, self.ROWS)
        ledger = self.ledger()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            ledger.db.execute("DELETE FROM records WHERE id='a'")
        with self.assertRaisesRegex(ValueError, "Immutable record conflict"):
            ledger.put("bars", "a", {"close": 4})
        self.assertTrue(ledger.verify()["ok"])

    def test_partially_initialized_version_zero_file_is_migrated(self):
        make_v1(self.path, self.ROWS[:1], version=0)
        ledger = self.ledger(create=False)
        self.assertEqual(raw(self.path, "PRAGMA user_version"), [(2,)])
        self.assertEqual(ledger.verify()["chain_length"], 1)

    def test_failed_migration_leaves_the_v1_ledger_untouched(self):
        make_v1(self.path, self.ROWS)
        calls = itertools.count()

        def flaky(*args):
            if next(calls) == 2:
                raise RuntimeError("disk full")
            return link_hash(*args)

        with (
            patch("jevtrader.store.link_hash", side_effect=flaky),
            self.assertRaisesRegex(RuntimeError, "disk full"),
        ):
            Ledger(self.path)
        self.assertEqual(raw(self.path, "PRAGMA user_version"), [(1,)])
        self.assertEqual(raw(self.path, "SELECT name FROM sqlite_master WHERE name='chain'"), [])
        self.assertTrue(self.ledger().verify()["ok"])

    def test_corrupted_v1_record_is_chained_as_stored_and_still_reported(self):
        make_v1(self.path, self.ROWS)
        db = sqlite3.connect(str(self.path))
        db.execute("DROP TRIGGER records_no_update")
        db.execute("UPDATE records SET payload='{\"close\":999}' WHERE id='z'")
        db.executescript(V1_SCHEMA)
        db.close()
        result = self.ledger().verify()
        self.assertEqual(result["problems"], ["Record content does not match its hash: bars/z"])
        self.assertEqual(result["chain_length"], 4)

    def test_readonly_v1_ledger_is_readable_but_reported_unchained(self):
        make_v1(self.path, self.ROWS)
        ledger = self.ledger(readonly=True)
        self.assertEqual(ledger.version, 1)
        self.assertEqual(ledger.get("bars", "a"), {"close": 3})
        result = ledger.verify()
        self.assertFalse(result["ok"])
        self.assertEqual((result["records"], result["chain_length"]), (4, 0))
        self.assertIn("has no hash chain", result["problems"][0])
        with self.assertRaisesRegex(ValueError, "has no hash chain"):
            ledger.head()
        self.assertEqual(raw(self.path, "PRAGMA user_version"), [(1,)])
        self.assertEqual(raw(self.path, "SELECT name FROM sqlite_master WHERE name='chain'"), [])

    def test_concurrent_opens_migrate_exactly_once(self):
        rows = [
            ("bars", f"X:{index:03d}", {"close": index}, f"2026-01-01T00:00:{index % 60:02d}Z")
            for index in range(60)
        ]
        make_v1(self.path, rows)
        barrier, errors = threading.Barrier(4), []

        def open_ledger():
            barrier.wait()
            try:
                Ledger(self.path).close()
            except Exception as exc:  # collected for the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=open_ledger) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        result = self.ledger(readonly=True).verify()
        self.assertEqual((result["ok"], result["chain_length"]), (True, 60))


class ConcurrencyTests(FileCase):
    def setUp(self):
        super().setUp()
        Ledger(self.path).close()

    def race(self, contender_payload: dict) -> dict:
        """Hold writer A inside its transaction while writer B tries the same id."""
        entered, release, results = threading.Event(), threading.Event(), {}
        real_now = store.utc_now
        holder: list[threading.Thread] = []

        def paused_now():
            if threading.current_thread() is holder[0] and not entered.is_set():
                entered.set()
                release.wait(10)
            return real_now()

        def write(name: str, payload: dict):
            try:
                with Ledger(self.path) as ledger:
                    results[name] = ledger.put("forecasts", "same", payload)
            except Exception as exc:  # collected for the assertion
                results[name] = exc

        with patch("jevtrader.store.utc_now", side_effect=paused_now):
            first = threading.Thread(target=write, args=("a", {"score": 1}))
            holder.append(first)
            first.start()
            self.assertTrue(entered.wait(10))
            second = threading.Thread(target=write, args=("b", contender_payload))
            second.start()
            time.sleep(0.3)
            self.assertTrue(second.is_alive(), "contender should wait for the write lock")
            release.set()
            first.join(10)
            second.join(15)
        return results

    def test_verify_reads_one_snapshot_while_the_service_commits(self):
        reader, writer = self.ledger(readonly=True), self.ledger()
        writer.put("runs", "before", {"n": 1})
        scan = reader._verify_records

        def racing(problems):
            recorded = scan(problems)
            writer.put("runs", "during", {"n": 2})  # committed between the two reads
            return recorded

        with patch.object(reader, "_verify_records", racing):
            result = reader.verify()
        self.assertEqual((result["ok"], result["chain_length"]), (True, 1), result["problems"])
        self.assertEqual(reader.verify()["chain_length"], 2)

    def test_identical_racing_insert_returns_false(self):
        results = self.race({"score": 1})
        self.assertEqual(results, {"a": True, "b": False})
        result = self.ledger(readonly=True).verify()
        self.assertEqual((result["ok"], result["chain_length"]), (True, 1))

    def test_conflicting_racing_insert_is_a_clear_conflict(self):
        results = self.race({"score": 2})
        self.assertIs(results["a"], True)
        self.assertIsInstance(results["b"], ValueError)
        self.assertNotIsInstance(results["b"], sqlite3.Error)
        self.assertIn("Immutable record conflict: forecasts/same", str(results["b"]))
        self.assertEqual(self.ledger(readonly=True).get("forecasts", "same"), {"score": 1})

    def test_parallel_writers_build_one_linear_chain(self):
        workers, each = 6, 15
        barrier, results, errors = threading.Barrier(workers), [], []

        def work(worker: int):
            try:
                with Ledger(self.path) as ledger:
                    barrier.wait()
                    for index in range(each):
                        ledger.put("bars", f"W{worker}:{index:02d}", {"close": index})
                    results.append(ledger.put("runs", "shared", {"job": "poll"}))
            except Exception as exc:  # collected for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False] * (workers - 1) + [True])
        ledger = self.ledger(readonly=True)
        result = ledger.verify()
        self.assertEqual((result["ok"], result["chain_length"]), (True, workers * each + 1))
        self.assertEqual([row[0] for row in chain_rows(ledger)], list(range(1, workers * each + 2)))
