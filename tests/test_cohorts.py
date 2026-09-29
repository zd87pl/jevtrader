"""Cohort pre-registration, the stop-gap for replays of imported or backfilled filings (#35)."""

import unittest

from jevtrader import cohorts, registry
from jevtrader.store import Ledger

EVENT = {
    "id": "sec:0001:1",
    "symbol": "ABC",
    "mode": "historical",
    "published_at": "2026-02-02T21:15:00Z",
    "first_seen_at": "2026-02-02T21:30:00Z",
    "source_url": "https://example.test/1",
    "source_type": "fixture",
    "text": "Raised guidance.",
}


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)

    def register(self, identity="c1", event_ids=("sec:0001:1",), rule="All 8-K 2.02 in Feb"):
        return cohorts.register(
            self.ledger,
            identity,
            event_ids=list(event_ids),
            rule=rule,
            now="2026-03-01T00:00:00Z",
        )

    def test_register_freezes_a_sorted_unique_cohort(self):
        record = self.register(event_ids=["b", "a", "b"])
        self.assertEqual(
            record,
            {
                "id": "c1",
                "rule": "All 8-K 2.02 in Feb",
                "event_ids": ["a", "b"],
                "registered_at": "2026-03-01T00:00:00.000000Z",
            },
        )
        self.assertEqual(self.ledger.get("cohorts", "c1"), record)
        self.assertEqual(self.register(event_ids=["a", "b"]), record)
        with self.assertRaisesRegex(ValueError, "Immutable"):
            self.register(event_ids=["a"])

    def test_register_rejects_bad_input(self):
        for kwargs, message in (
            ({"identity": ""}, "cohort id"),
            ({"identity": "x" * 201}, "cohort id"),
            ({"event_ids": []}, "event id"),
            ({"event_ids": [""]}, "event id"),
            ({"event_ids": [3]}, "event id"),
            ({"rule": " "}, "rule"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                self.register(**kwargs)
        with self.assertRaisesRegex(ValueError, "event id"):
            self.register(event_ids=[f"e{i}" for i in range(cohorts.MAX_EVENTS + 1)])
        with self.assertRaisesRegex(ValueError, "naive|timezone|Z"):
            cohorts.register(self.ledger, "c", event_ids=["a"], rule="r", now="2026-03-01")

    def test_only_a_cohort_appended_before_the_filing_preregisters_it(self):
        self.register()
        self.ledger.disclosure(EVENT)
        self.assertEqual(cohorts.preregistered(self.ledger, EVENT["id"]), "c1")
        self.assertIsNone(cohorts.preregistered(self.ledger, "missing"))

    def test_a_cohort_registered_after_the_import_does_not_count(self):
        self.ledger.disclosure(EVENT)
        self.register()
        self.assertIsNone(cohorts.preregistered(self.ledger, EVENT["id"]))

    def test_a_cohort_that_does_not_list_the_filing_does_not_count(self):
        self.register(event_ids=["other"])
        self.ledger.disclosure(EVENT)
        self.assertIsNone(cohorts.preregistered(self.ledger, EVENT["id"]))

    def test_the_label_is_the_existing_never_evidence_hand_picked_label(self):
        self.assertEqual(cohorts.LABEL, "adhoc_replay")
        self.assertIn(cohorts.LABEL, registry.LABELS)
        self.assertFalse(registry.counts_as_evidence(cohorts.LABEL))

    def test_label_mix_counts_each_label(self):
        self.assertEqual(
            cohorts.label_mix(
                [
                    {"eligibility": "post_cutoff"},
                    {"eligibility": cohorts.LABEL},
                    {"eligibility": cohorts.LABEL},
                    {},
                ]
            ),
            {cohorts.LABEL: 2, "post_cutoff": 1, "unlabelled": 1},
        )


if __name__ == "__main__":
    unittest.main()
