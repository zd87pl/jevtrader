"""Point-in-time read path (P0-17, #21): one Instant type, an injectable clock and an indexed
``Ledger.as_of`` that never returns a row known after the requested instant."""

import random
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jevtrader.pit import SYSTEM_CLOCK, FixedClock, Instant, knowledge_time
from jevtrader.store import Ledger

BASE = datetime(2026, 3, 6, 14, 30, tzinfo=timezone.utc)


def iso(offset_minutes: float) -> str:
    return Instant(BASE + timedelta(minutes=offset_minutes)).iso()


def disclosure(identity: str, seen: float, *, symbol: str = "ABC") -> dict:
    return {
        "id": identity,
        "symbol": symbol,
        "text": "Item 2.02 results",
        "source_url": "https://www.sec.gov/Archives/x.htm",
        "mode": "historical",
        "published_at": iso(seen - 1),
        "first_seen_at": iso(seen),
    }


class InstantTests(unittest.TestCase):
    def test_parse_normalizes_to_fixed_width_utc(self):
        value = Instant.parse("2026-03-08T01:30:00-05:00")
        self.assertEqual(value.iso(), "2026-03-08T06:30:00.000000Z")
        self.assertEqual(str(value), value.iso())
        self.assertEqual(Instant.parse("2026-03-08T06:30:00Z"), value)

    def test_naive_and_malformed_values_are_rejected(self):
        for bad in ("2026-03-08T06:30:00", "not a time", 5):
            with self.assertRaises(ValueError):
                Instant.coerce(bad)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Instant(datetime(2026, 3, 8))

    def test_coerce_accepts_instant_datetime_and_string(self):
        value = Instant(BASE)
        self.assertIs(Instant.coerce(value), value)
        self.assertEqual(Instant.coerce(BASE), value)
        self.assertEqual(Instant.coerce(BASE.isoformat()), value)

    def test_string_order_equals_time_order_across_dst(self):
        rng = random.Random(17)
        # The 2026 US spring-forward (Mar 8) and fall-back (Nov 1) are inside this span.
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        values = [
            Instant(start + timedelta(seconds=rng.randrange(0, 366 * 86400))) for _ in range(500)
        ]
        self.assertEqual(sorted(values), sorted(values, key=lambda v: v.iso()))

    def test_clocks(self):
        at = Instant(BASE)
        self.assertEqual(FixedClock(at).now(), at)
        before = datetime.now(timezone.utc)
        self.assertGreaterEqual(SYSTEM_CLOCK.now().moment, before)


class KnowledgeTimeTests(unittest.TestCase):
    RECORDED = "2026-09-01T00:00:00.000000Z"

    def test_each_kind_uses_its_availability_field(self):
        cases = [
            ("disclosures", {"first_seen_at": iso(0), "published_at": iso(-5)}, iso(0)),
            ("bars", {"available_at": iso(3), "close_at": iso(1)}, iso(3)),
            ("forecasts", {"decision_at": iso(4)}, iso(4)),
            ("extractions", {"created_at": iso(5)}, iso(5)),
            ("outcomes", {"outcome_at": iso(9), "label_available_at": iso(7)}, iso(9)),
            ("outcomes", {"outcome_at": iso(7), "label_available_at": iso(9)}, iso(9)),
        ]
        for kind, payload, expected in cases:
            with self.subTest(kind=kind, payload=payload):
                self.assertEqual(knowledge_time(kind, payload, self.RECORDED).iso(), expected)

    def test_other_kinds_and_missing_fields_fall_back_to_recorded_at(self):
        for kind, payload in (
            ("models", {"created_at": iso(0)}),
            ("runs", {}),
            ("bars", {"close_at": iso(0)}),
            ("outcomes", {"outcome_at": iso(0)}),
            ("disclosures", {"first_seen_at": "garbage"}),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(knowledge_time(kind, payload, self.RECORDED).iso(), self.RECORDED)


class LedgerClockTests(unittest.TestCase):
    def test_recorded_at_comes_from_the_injected_clock(self):
        clock = FixedClock(Instant(BASE))
        with Ledger(":memory:", clock=clock) as ledger:
            ledger.put("runs", "r1", {"id": "r1"})
            recorded = ledger.db.execute("SELECT recorded_at FROM records").fetchone()[0]
            self.assertEqual(recorded, Instant(BASE).iso())
            self.assertEqual(ledger.as_of("runs", Instant(BASE)), [{"id": "r1"}])
            self.assertEqual(ledger.as_of("runs", iso(-0.001)), [])

    def test_forward_future_check_uses_the_injected_clock(self):
        event = disclosure("d1", 10) | {"mode": "forward"}
        with Ledger(":memory:", clock=FixedClock(Instant.parse(iso(5)))) as ledger:
            with self.assertRaisesRegex(ValueError, "future"):
                ledger.disclosure(event, imported=False)
        with Ledger(":memory:", clock=FixedClock(Instant.parse(iso(10)))) as ledger:
            self.assertTrue(ledger.disclosure(event, imported=False))


class AsOfTests(unittest.TestCase):
    def test_boundary_is_inclusive_and_prefix_narrows(self):
        with Ledger(":memory:") as ledger:
            ledger.disclosure(disclosure("a", 0))
            ledger.disclosure(disclosure("b", 1, symbol="XYZ"))
            ids = [row["id"] for row in ledger.as_of("disclosures", iso(0))]
            self.assertEqual(ids, ["a"])
            self.assertEqual(len(ledger.as_of("disclosures", iso(1))), 2)
            self.assertEqual(
                ledger.as_of("disclosures", iso(1), prefix="b"), [ledger.get("disclosures", "b")]
            )
            self.assertEqual(ledger.as_of("disclosures", iso(-1)), [])

    def test_rejects_unknown_kind_and_naive_time(self):
        with Ledger(":memory:") as ledger:
            with self.assertRaises(ValueError):
                ledger.as_of("nope", iso(0))
            with self.assertRaises(ValueError):
                ledger.as_of("disclosures", "2026-03-06T14:30:00")

    def test_query_uses_the_knowledge_index(self):
        with Ledger(":memory:") as ledger:
            plan = " ".join(
                str(row)
                for row in ledger.db.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM knowledge WHERE kind=? AND knowledge_time<=?",
                    ("bars", iso(0)),
                )
            )
            self.assertIn("knowledge_by_time", plan)

    def test_property_no_row_is_known_after_t(self):
        kinds = ["disclosures", "bars", "forecasts", "outcomes", "runs"]
        for seed in range(40):
            rng = random.Random(seed)
            times = iter(sorted(rng.uniform(-2000, 2000) for _ in range(200)))
            clock_now = {"value": Instant(BASE)}

            class Clock:
                def now(self) -> Instant:
                    return clock_now["value"]

            with Ledger(":memory:", clock=Clock()) as ledger:
                truth: dict[tuple[str, str], Instant] = {}
                for index in range(rng.randrange(5, 60)):
                    kind = rng.choice(kinds)
                    identity = f"{kind}:{index}"
                    clock_now["value"] = Instant.parse(iso(next(times)))
                    a, b = iso(rng.uniform(-3000, 3000)), iso(rng.uniform(-3000, 3000))
                    payload: dict = {"id": identity}
                    if kind == "disclosures":
                        payload = disclosure(identity, rng.uniform(-3000, 3000))
                        ledger.disclosure(payload)
                        payload = ledger.get(kind, identity)
                    else:
                        field = {"bars": "available_at", "forecasts": "decision_at"}.get(kind)
                        if field and rng.random() < 0.8:
                            payload[field] = a
                        if kind == "outcomes" and rng.random() < 0.8:
                            payload |= {"outcome_at": a, "label_available_at": b}
                        ledger.put(kind, identity, payload)
                    truth[(kind, identity)] = knowledge_time(
                        kind, payload, clock_now["value"].iso()
                    )
                for _ in range(25):
                    t = Instant.parse(iso(rng.uniform(-3500, 3500)))
                    kind = rng.choice(kinds)
                    rows = ledger.as_of(kind, t)
                    got = {row["id"] for row in rows}
                    expected = {i for (k, i), known in truth.items() if k == kind and known <= t}
                    self.assertEqual(got, expected, f"seed {seed}")
                    for row in rows:
                        self.assertLessEqual(truth[(kind, row["id"])], t)
                self.assertTrue(ledger.verify()["ok"])


class IndexMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "l.sqlite"

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_index_rows_are_backfilled_on_open(self):
        with Ledger(self.path) as ledger:
            ledger.disclosure(disclosure("a", 0))
        raw = sqlite3.connect(self.path)
        raw.execute("DROP TABLE knowledge")
        raw.commit()
        raw.close()
        with Ledger(self.path, readonly=True) as ledger:
            # Without the index a read-only ledger still answers, by scanning.
            self.assertEqual([r["id"] for r in ledger.as_of("disclosures", iso(0))], ["a"])
            self.assertEqual(ledger.as_of("disclosures", iso(-1)), [])
        with Ledger(self.path) as ledger:
            self.assertEqual([r["id"] for r in ledger.as_of("disclosures", iso(0))], ["a"])
            count = ledger.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
            self.assertEqual(count, 1)
            self.assertTrue(ledger.verify()["ok"])

    def test_tampered_index_cannot_leak_a_future_row(self):
        with Ledger(self.path) as ledger:
            ledger.disclosure(disclosure("a", 10))
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE knowledge SET knowledge_time=?", (iso(-100),))
        raw.commit()
        raw.close()
        with Ledger(self.path) as ledger:
            with self.assertRaisesRegex(ValueError, "Knowledge index"):
                ledger.as_of("disclosures", iso(0))

    def test_corrupted_payload_is_refused(self):
        with Ledger(self.path, clock=FixedClock(Instant(BASE))) as ledger:
            ledger.put("runs", "r", {"id": "r"})
            ledger.db.execute("DROP TRIGGER records_no_update")
            ledger.db.execute('UPDATE records SET payload=\'{"id":"x"}\'')
            with self.assertRaisesRegex(ValueError, "Corrupted"):
                ledger.as_of("runs", iso(0))


if __name__ == "__main__":
    unittest.main()
