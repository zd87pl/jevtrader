"""Point-in-time security master (P0-16, #20): CIK-ticker history, delistings and corporate
actions, read through ``Ledger.as_of`` so a fact recorded late never reaches an earlier read.

Every event here is synthetic; no licensed or captured data is used."""

import unittest
from datetime import date

from jevtrader import feeds
from jevtrader.pit import FixedClock, Instant, knowledge_time
from jevtrader.pit.securities import (
    SecurityMaster,
    event_id,
    parse_event,
    record_events,
    ticker_snapshot,
)
from jevtrader.store import Ledger
from tests.test_feeds import ABC, UA, XYZ, FeedsCase, Filing, accession, form_index, index_url

OLD = "0000111111"


def ticker(cik, tickers, effective, known_at, source="owner_file"):
    return {
        "type": "ticker",
        "cik": cik,
        "tickers": list(tickers),
        "effective": effective,
        "known_at": known_at,
        "source": source,
    }


def delisting(cik, effective, known_at, delisting_return=None):
    return {
        "type": "delisting",
        "cik": cik,
        "effective": effective,
        "known_at": known_at,
        "source": "owner_file",
        "delisting_return": delisting_return,
    }


def split(cik, effective, known_at, ratio):
    return {
        "type": "split",
        "cik": cik,
        "effective": effective,
        "known_at": known_at,
        "source": "owner_file",
        "ratio": ratio,
    }


class ParseTests(unittest.TestCase):
    def test_normalizes_and_rejects_bad_events(self):
        event = parse_event(ticker("123456", ["abc", "ABC-B"], "2020-01-02", "2020-01-02T12:00Z"))
        self.assertEqual(event["cik"], "0000123456")
        self.assertEqual(event["tickers"], ["ABC", "ABC-B"])
        self.assertEqual(event["known_at"], "2020-01-02T12:00:00.000000Z")
        bad = [
            {**ticker(ABC, ["ABC"], "2020-01-02", "2020-01-02T12:00Z"), "type": "merger"},
            ticker("0", ["ABC"], "2020-01-02", "2020-01-02T12:00Z"),
            ticker(ABC, [], "2020-01-02", "2020-01-02T12:00Z"),
            ticker(ABC, ["1BAD"], "2020-01-02", "2020-01-02T12:00Z"),
            ticker(ABC, ["ABC"], "2020-01-02T00:00Z", "2020-01-02T12:00Z"),
            ticker(ABC, ["ABC"], "2020-01-02", "2020-01-02T12:00"),
            ticker(ABC, ["ABC"], "2020-01-02", "2020-01-02T12:00Z", source=""),
            split(ABC, "2020-01-02", "2020-01-02T12:00Z", 0),
            split(ABC, "2020-01-02", "2020-01-02T12:00Z", True),
            split(ABC, "2020-01-02", "2020-01-02T12:00Z", float("inf")),
            delisting(ABC, "2020-01-02", "2020-01-02T12:00Z", delisting_return=-1.5),
            {**ticker(ABC, ["ABC"], "2020-01-02", "2020-01-02T12:00Z"), "extra": 1},
            "not a mapping",
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_event(value)

    def test_id_is_content_derived_and_knowledge_time_is_known_at(self):
        event = parse_event(ticker(ABC, ["ABC"], "2020-01-02", "2020-01-03T12:00Z"))
        self.assertEqual(event_id(event), event_id(dict(event)))
        self.assertTrue(event_id(event).startswith(f"{ABC}:2020-01-02:ticker:"))
        later = parse_event(ticker(ABC, ["ABC"], "2020-01-02", "2020-01-04T12:00Z"))
        self.assertNotEqual(event_id(event), event_id(later))
        self.assertEqual(
            knowledge_time("securities", event, "2030-01-01T00:00:00Z"),
            Instant.parse("2020-01-03T12:00:00Z"),
        )


class MasterTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:", clock=FixedClock(Instant.parse("2026-09-01T00:00:00Z")))
        self.addCleanup(self.ledger.db.close)
        events = [
            ticker(OLD, ["OLDC"], "2015-01-02", "2015-01-02T00:00Z"),
            delisting(OLD, "2021-06-01", "2021-06-02T00:00Z", delisting_return=-0.3),
            ticker(ABC, ["ABCO"], "2015-01-02", "2015-01-02T00:00Z"),
            ticker(ABC, ["ABC"], "2019-03-01", "2019-03-01T00:00Z"),
            # Recorded late: a split effective in 2020 that the master learned in 2022.
            split(ABC, "2020-08-31", "2022-01-10T00:00Z", 4.0),
            split(ABC, "2018-05-01", "2018-05-01T00:00Z", 0.5),
        ]
        self.assertEqual(record_events(self.ledger, events), 6)
        self.assertEqual(record_events(self.ledger, events), 0)  # idempotent

    def master(self, known_at):
        return SecurityMaster.from_ledger(self.ledger, known_at)

    def test_ticker_history_is_valid_time(self):
        master = self.master("2026-01-01T00:00Z")
        self.assertEqual(master.tickers(ABC, date(2015, 1, 1)), ())
        self.assertEqual(master.tickers(ABC, date(2015, 1, 2)), ("ABCO",))
        self.assertEqual(master.tickers(ABC, date(2019, 2, 28)), ("ABCO",))
        self.assertEqual(master.tickers(ABC, date(2019, 3, 1)), ("ABC",))
        self.assertEqual(master.cik_for("abco", date(2016, 1, 1)), ABC)
        self.assertIsNone(master.cik_for("ABCO", date(2020, 1, 1)))
        self.assertEqual(master.tickers(XYZ, date(2020, 1, 1)), ())

    def test_delisted_companies_stay_in_history(self):
        master = self.master("2026-01-01T00:00Z")
        self.assertEqual(master.tickers(OLD, date(2021, 5, 31)), ("OLDC",))
        self.assertEqual(master.tickers(OLD, date(2021, 6, 1)), ())
        self.assertEqual(master.table(date(2020, 1, 1)), {OLD: ["OLDC"], ABC: ["ABC"]})
        self.assertEqual(master.table(date(2021, 6, 1)), {ABC: ["ABC"]})
        event = master.delisting(OLD)
        assert event is not None
        self.assertEqual((event["effective"], event["delisting_return"]), ("2021-06-01", -0.3))
        self.assertIsNone(master.delisting(ABC))

    def test_a_late_action_is_invisible_before_it_was_known(self):
        before = self.master("2022-01-09T23:59:59Z")
        after = self.master("2022-01-10T00:00:00Z")  # known_at equal to the read instant
        self.assertEqual(before.split_factor(ABC, date(2020, 1, 1), date(2021, 1, 1)), 1.0)
        self.assertEqual(after.split_factor(ABC, date(2020, 1, 1), date(2021, 1, 1)), 4.0)
        # (after, through]: the ex-date itself counts, the start date does not.
        self.assertEqual(after.split_factor(ABC, date(2020, 8, 30), date(2020, 8, 31)), 4.0)
        self.assertEqual(after.split_factor(ABC, date(2020, 8, 31), date(2021, 1, 1)), 1.0)
        self.assertEqual(after.split_factor(ABC, date(2017, 1, 1), date(2021, 1, 1)), 2.0)
        with self.assertRaises(ValueError):
            after.split_factor(ABC, date(2021, 1, 1), date(2020, 1, 1))
        self.assertEqual(self.master("2021-06-01T23:59Z").delisting(OLD), None)

    def test_a_later_correction_wins_for_the_same_effective_date(self):
        record_events(self.ledger, [ticker(ABC, ["ABCD"], "2019-03-01", "2023-01-01T00:00Z")])
        self.assertEqual(self.master("2022-12-31T00:00Z").tickers(ABC, date(2020, 1, 1)), ("ABC",))
        self.assertEqual(self.master("2023-01-01T00:00Z").tickers(ABC, date(2020, 1, 1)), ("ABCD",))

    def test_ticker_snapshot_is_never_backdated(self):
        at = Instant.parse("2026-09-25T21:00:00Z")
        events = ticker_snapshot({ABC: ["ABC"], XYZ: ["XYZ", "XYZ-B"]}, at)
        self.assertEqual({e["effective"] for e in events}, {"2026-09-25"})
        self.assertEqual({e["known_at"] for e in events}, {at.iso()})
        self.assertEqual({e["source"] for e in events}, {"sec_company_tickers"})
        master = SecurityMaster(events)
        self.assertEqual(master.tickers(XYZ, date(2026, 9, 24)), ())
        self.assertEqual(master.tickers(XYZ, date(2026, 9, 25)), ("XYZ", "XYZ-B"))

    def test_empty_master(self):
        master = SecurityMaster([])
        self.assertFalse(master)
        self.assertTrue(self.master("2026-01-01T00:00Z"))


class BackfillUsesMasterTests(FeedsCase):
    FRIDAY = date(2026, 9, 18)

    def setUp(self):
        super().setUp()
        self.old = Filing(OLD, "0000111111-26-000001", acceptance="2026-09-18T10:00:00-04:00")
        self.abc = Filing(ABC, accession(1), acceptance="2026-09-18T11:00:00-04:00")
        self.net.add(self.old, self.abc)
        rows = [(f.form, "COMPANY", f.cik, "20260918", f.accession) for f in (self.old, self.abc)]
        self.net.responses[index_url(self.FRIDAY)] = form_index(*rows)

    def test_backfill_maps_ciks_through_the_master_as_of_the_filing_day(self):
        # OLD was delisted after the filing and is absent from today's SEC ticker map; the
        # master still names it, and ABC carries the ticker it had that day.
        events = [
            ticker(OLD, ["OLDC"], "2015-01-02", "2015-01-02T00:00Z"),
            delisting(OLD, "2026-09-21", "2026-09-22T00:00Z"),
            ticker(ABC, ["ABCO"], "2015-01-02", "2015-01-02T00:00Z"),
            ticker(ABC, ["ABC"], "2026-09-21", "2026-09-21T00:00Z"),
        ]
        record_events(self.ledger, events)
        result = feeds.backfill(
            self.ledger, UA, self.FRIDAY, self.FRIDAY, transport=self.net.transport
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["added"], [self.old.id, self.abc.id])
        self.assertNotIn("https://www.sec.gov/files/company_tickers.json", self.net.urls())
        old = self.ledger.get("disclosures", self.old.id)
        abc = self.ledger.get("disclosures", self.abc.id)
        self.assertEqual((old["symbol"], abc["symbol"]), ("OLDC", "ABCO"))
        self.assertEqual(old["symbol_basis"], "security_master")

    def test_backfill_ignores_facts_learned_after_the_run(self):
        # The only mapping is known after the backfill's own clock, so nothing maps.
        record_events(self.ledger, [ticker(ABC, ["ABCO"], "2015-01-02", "2027-01-01T00:00Z")])
        self.net.responses["https://www.sec.gov/files/company_tickers.json"] = {
            "0": {"cik_str": 123456, "ticker": "ABC", "title": "ABC Corp"}
        }
        result = feeds.backfill(
            self.ledger, UA, self.FRIDAY, self.FRIDAY, transport=self.net.transport
        )
        abc = self.ledger.get("disclosures", self.abc.id)
        self.assertEqual(abc["symbol_basis"], "sec_ticker_map_at_backfill")
        self.assertEqual(result["skipped"]["unmapped"], 1)

    def test_an_explicit_master_takes_precedence(self):
        master = SecurityMaster(
            [parse_event(ticker(OLD, ["OLDC"], "2015-01-02", "2015-01-02T00:00Z"))]
        )
        result = feeds.backfill(
            self.ledger,
            UA,
            self.FRIDAY,
            self.FRIDAY,
            transport=self.net.transport,
            master=master,
        )
        self.assertEqual(result["added"], [self.old.id])
        self.assertEqual(result["skipped"]["unmapped"], 1)


if __name__ == "__main__":
    unittest.main()
