"""Offline tests for the evening reconcile: a fake daily index and a fake SEC transport."""

from datetime import date, datetime, timezone
from unittest.mock import patch

from test_feeds import (
    ABC,
    TICKER_JSON,
    TICKERS,
    UA,
    UNMAPPED,
    XYZ,
    FeedsCase,
    Filing,
    accession,
    form_index,
    http_error,
    index_url,
    submissions,
    submissions_url,
)

from jevtrader import feeds, paths, sec
from jevtrader.store import Ledger

DAY = date(2026, 9, 25)  # Friday
EVENING = datetime(2026, 9, 26, 2, 45, tzinfo=timezone.utc)  # 22:45 ET


def row(cik, number, form="8-K"):
    return (form, "Company", cik, "2026-09-25", number)


class ReconcileTests(FeedsCase):
    now = EVENING

    def setUp(self):
        super().setUp()
        self.memory = feeds.PollMemory(clock=lambda: 0.0)
        self.net.responses[TICKERS] = TICKER_JSON

    def reconcile(self, symbols=None, **kwargs):
        kwargs.setdefault("memory", self.memory)
        return feeds.reconcile(
            self.ledger, UA, DAY, symbols=symbols, transport=self.net.transport, **kwargs
        )

    def test_classifies_every_8k_and_recovers_missed_qualifying_ones_late(self):
        kept = Filing(ABC, accession(1))
        missed = Filing(ABC, accession(2))
        other = Filing(ABC, accession(3), items="2.02,9.01")
        self.net.add(kept, missed, other)
        self.ledger.disclosure(
            {**_forward(kept), "first_seen_at": "2026-09-25T20:45:00Z"}, imported=False
        )
        self.net.responses[index_url(DAY)] = form_index(
            row(ABC, kept.accession),
            row(ABC, missed.accession),
            row(ABC, other.accession, "8-K/A"),
            row(UNMAPPED, "0000999999-26-000001"),
            row(XYZ, "0000654321-26-000001"),
        )
        result = self.reconcile({"abc"})
        self.assertEqual(result["indexed"], 5)
        self.assertEqual(result["collected"], 1)
        self.assertEqual(result["recovered"], [missed.id])
        self.assertEqual(
            {key: result[key] for key in ("not_qualifying", "unmapped", "not_watched")},
            {"not_qualifying": 1, "unmapped": 1, "not_watched": 1},
        )
        self.assertEqual(result["gaps"], [])
        record = self.ledger.get("disclosures", missed.id)
        self.assertEqual(record["mode"], "forward")
        # Honest: the instant this reconcile received it, hours after acceptance.
        self.assertEqual(record["first_seen_at"], "2026-09-26T02:45:00.000000Z")
        self.assertEqual(record["first_seen_basis"], "reconcile_late")
        self.assertGreater(record["first_seen_at"], record["accepted_at"])
        # The already collected record keeps its original observation.
        self.assertEqual(
            self.ledger.get("disclosures", kept.id)["first_seen_at"], "2026-09-25T20:45:00.000000Z"
        )
        self.assertNotIn(submissions_url(XYZ), self.net.urls())

    def test_filings_the_poller_gave_up_on_are_still_recovered(self):
        missed = Filing(ABC, accession(2))
        self.net.add(missed)
        self.net.responses[index_url(DAY)] = form_index(row(ABC, missed.accession))
        for _ in range(feeds.MAX_FILING_ATTEMPTS):
            self.memory.fail(missed.accession)
        self.assertTrue(self.memory.exhausted(missed.accession))
        result = self.reconcile()
        self.assertEqual(result["recovered"], [missed.id])
        self.assertFalse(self.memory.exhausted(missed.accession))

    def test_known_non_qualifying_filings_cost_no_request(self):
        self.memory.reject(accession(3))
        self.net.responses[index_url(DAY)] = form_index(row(ABC, accession(3)))
        result = self.reconcile()
        self.assertEqual(result["not_qualifying"], 1)
        self.assertEqual(self.net.urls(), [index_url(DAY)])

    def test_unrecoverable_filings_are_recorded_as_coverage_gaps(self):
        self.net.responses[submissions_url(ABC)] = submissions(ABC, Filing(ABC, accession(9)))
        bad = Filing(XYZ, "0000654321-26-000001")
        self.net.add(bad)
        self.net.responses[base_doc(bad)] = http_error(base_doc(bad), 404)
        self.net.responses[index_url(DAY)] = form_index(
            row(ABC, accession(1)), row(XYZ, bad.accession)
        )
        result = self.reconcile()
        self.assertEqual(result["recovered"], [])
        gaps = {gap["accession"]: gap for gap in result["gaps"]}
        self.assertEqual(gaps[accession(1)]["reason"], "not_in_submissions")
        self.assertEqual(gaps[accession(1)]["cik"], ABC)
        self.assertEqual(gaps[accession(1)]["form"], "8-K")
        self.assertTrue(gaps[bad.accession]["reason"].startswith("error: "))

    def test_throttling_stops_and_leaves_the_rest_as_gaps(self):
        first, second = Filing(ABC, accession(1)), Filing(XYZ, "0000654321-26-000001")
        self.net.add(first, second)
        self.net.responses[submissions_url(ABC)] = http_error(submissions_url(ABC), 429)
        self.net.responses[index_url(DAY)] = form_index(
            row(ABC, first.accession), row(XYZ, second.accession)
        )
        result = self.reconcile()
        self.assertIn("429", result["stopped"])
        self.assertEqual([gap["reason"][:6] for gap in result["gaps"]], ["error:", "stoppe"])
        self.assertNotIn(submissions_url(XYZ), self.net.urls())

    def test_the_filing_budget_defers_the_rest_as_gaps(self):
        filings = [Filing(ABC, accession(n)) for n in (1, 2)]
        self.net.add(*filings)
        self.net.responses[index_url(DAY)] = form_index(*[row(ABC, f.accession) for f in filings])
        result = self.reconcile(max_filings=1)
        self.assertEqual(result["recovered"], [filings[0].id])
        self.assertEqual(result["gaps"], [_gap(filings[1], "deferred")])

    def test_missing_index_is_itself_a_gap_and_weekends_are_empty(self):
        self.net.responses[index_url(DAY)] = http_error(index_url(DAY), 404)
        result = self.reconcile()
        self.assertEqual((result["indexed"], result["index_missing"]), (0, True))
        saturday = feeds.reconcile(
            self.ledger, UA, date(2026, 9, 26), symbols=None, transport=self.net.transport
        )
        self.assertEqual((saturday["indexed"], saturday["index_missing"]), (0, False))

    def test_a_record_the_ledger_refuses_is_a_gap(self):
        missed = Filing(ABC, accession(2))
        self.net.add(missed)
        self.net.responses[index_url(DAY)] = form_index(row(ABC, missed.accession))
        future = datetime(2099, 1, 2, 3, 0, tzinfo=timezone.utc)  # a receipt the ledger refuses
        with patch.object(sec, "utc_now", lambda: future):
            result = self.reconcile()
        self.assertEqual(result["recovered"], [])
        self.assertEqual(result["gaps"], [_gap(missed, "error: not stored")])
        self.assertIn("future", result["errors"][0]["error"])

    def test_refuses_the_research_ledger(self):
        with Ledger(paths.research_ledger_path()) as research:
            with self.assertRaises(ValueError):
                feeds.reconcile(research, UA, DAY, symbols=None, transport=self.net.transport)
        self.assertEqual(self.net.calls, [])


def base_doc(filing):
    return next(url for url in filing.documents if url.endswith("ex991.htm"))


def _gap(filing, reason):
    return {"accession": filing.accession, "cik": filing.cik, "form": "8-K", "reason": reason}


def _forward(filing):
    return {
        "id": filing.id,
        "symbol": "ABC",
        "published_at": "2026-09-25T20:30:00Z",
        "first_seen_at": "2026-09-25T20:45:00Z",
        "text": "Operating update.",
        "source_url": base_doc(filing),
        "mode": "forward",
    }
