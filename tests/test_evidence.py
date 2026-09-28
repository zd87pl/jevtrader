"""Scoreboard numbers: eligibility, point-in-time maturity, clustering, the gate and status."""

import hashlib
import math
import unittest
from datetime import datetime, timedelta, timezone
from statistics import fmean, stdev

from jevtrader import evidence
from jevtrader.common import canonical, load_strategy, round_trip_bps
from jevtrader.store import Ledger

STRATEGY = load_strategy()
LONG_COST = round_trip_bps(STRATEGY) / 10_000
SHORT_COST = round_trip_bps(STRATEGY, short=True) / 10_000
START = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)
AS_OF = "2026-12-31T00:00:00Z"
# Pinned so any edit to the gate thresholds shows up as a failing test and a new fingerprint.
GATE_SHA256 = hashlib.sha256(
    b'{"confidence":0.9,"futility_upper_bps":10.0,"min_matured_calls":100,"version":1}'
).hexdigest()


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class FakeLedger:
    def __init__(self, forecasts=(), outcomes=()):
        self.records = {
            "forecasts": {f["id"]: f for f in forecasts},
            "outcomes": {o["forecast_id"]: o for o in outcomes},
        }

    def all(self, kind):
        return [value for _, value in sorted(self.records.get(kind, {}).items())]

    def get(self, kind, identity):
        return self.records.get(kind, {}).get(identity)


def forecast(identity, *, action="LONG", decided=None, recorded=None, mode="forward", **extra):
    decided = decided or iso(START)
    return {
        "id": identity,
        "event_id": f"event-{identity}",
        "action": action,
        "decision_at": decided,
        "recorded_at": recorded or decided,
        "mode": mode,
        "strategy": STRATEGY,
        **extra,
    }


def outcome(identity, target, *, available="2026-03-20T21:00:00Z", outcome_at=None):
    return {
        "forecast_id": identity,
        "target": target,
        "outcome_at": outcome_at or "2026-03-16T20:00:00Z",
        "label_available_at": available,
    }


def calls(targets_by_day, *, action="LONG"):
    """One matured call per target; day index -> list of benchmark-relative targets."""
    forecasts, outcomes = [], []
    for day, targets in enumerate(targets_by_day):
        for index, target in enumerate(targets):
            identity = f"c{day:03d}-{index:03d}"
            decided = iso(START + timedelta(days=day, minutes=index))
            forecasts.append(forecast(identity, action=action, decided=decided))
            outcomes.append(outcome(identity, target))
    return FakeLedger(forecasts, outcomes)


class TQuantileTests(unittest.TestCase):
    def test_matches_published_student_t_table(self):
        table = {
            (0.95, 1): 6.313752,
            (0.95, 2): 2.919986,
            (0.95, 5): 2.015048,
            (0.95, 10): 1.812461,
            (0.95, 30): 1.697261,
            (0.95, 99): 1.660391,
            (0.975, 3): 3.182446,
            (0.995, 20): 2.845340,
        }
        for (p, df), expected in table.items():
            with self.subTest(p=p, df=df):
                self.assertAlmostEqual(evidence.t_quantile(p, df), expected, places=5)
        self.assertAlmostEqual(evidence.t_quantile(0.05, 4), -2.131847, places=5)
        self.assertEqual(evidence.t_quantile(0.5, 7), 0.0)
        self.assertAlmostEqual(evidence.t_quantile(0.95, 100_000), 1.644868, places=4)

    def test_rejects_invalid_arguments(self):
        for p, df in ((0, 3), (1, 3), (1.5, 3), (0.9, 0), (0.9, 2.0), (0.9, True)):
            with self.subTest(p=p, df=df), self.assertRaises(ValueError):
                evidence.t_quantile(p, df)


class ScoreboardTests(unittest.TestCase):
    def test_empty_ledger_is_collecting_with_a_fingerprinted_gate(self):
        board = evidence.scoreboard(FakeLedger(), as_of=AS_OF)
        self.assertEqual(board["status"], "collecting")
        self.assertEqual((board["calls"], board["call_dates"], board["pending_calls"]), (0, 0, 0))
        self.assertIsNone(board["interval"])
        self.assertIsNone(board["mean_net_return"])
        self.assertEqual(board["gate"], evidence.GATE)
        self.assertEqual(board["gate_sha256"], GATE_SHA256)
        self.assertEqual(
            board["gate_sha256"], hashlib.sha256(canonical(board["gate"]).encode()).hexdigest()
        )
        self.assertEqual(board["label"], "Collecting evidence: 0 of 100 matured calls")
        self.assertEqual(board["as_of"], "2026-12-31T00:00:00.000000Z")
        board["gate"]["min_matured_calls"] = 1
        self.assertEqual(evidence.GATE["min_matured_calls"], 100)

    def test_rejects_invalid_times_and_filters(self):
        with self.assertRaises(ValueError):
            evidence.scoreboard(FakeLedger(), as_of="2026-12-31T00:00:00")
        with self.assertRaises(ValueError):
            evidence.scoreboard(FakeLedger(), as_of="yesterday")
        with self.assertRaises(ValueError):
            evidence.scoreboard(FakeLedger(), as_of=AS_OF, eligible="forward")

    def test_registry_labels_decide_evidence_and_unlabelled_needs_forward_mode(self):
        cases = {
            "forward": (dict(eligibility="forward"), True),
            "post": (dict(eligibility="post_cutoff", mode="historical"), True),
            "rules": (dict(eligibility="no_model_knowledge", mode="historical"), True),
            "contaminated": (dict(eligibility="contaminated", mode="historical"), False),
            "unknown": (dict(eligibility="unknown_cutoff", mode="historical"), False),
            "synthetic": (dict(eligibility="synthetic", mode="synthetic"), False),
            "synthetic-mislabelled": (
                dict(eligibility="no_model_knowledge", mode="synthetic"),
                False,
            ),
            "odd-label": (dict(eligibility="FORWARD"), False),
            "non-string-label": (dict(eligibility=1), False),
            "legacy-forward": (dict(), True),
            "legacy-historical": (dict(mode="historical"), False),
            "legacy-synthetic": (dict(mode="synthetic"), False),
        }
        forecasts = [forecast(name, **fields) for name, (fields, _) in cases.items()]
        outcomes = [outcome(name, 0.01) for name in cases]
        board = evidence.scoreboard(FakeLedger(forecasts, outcomes), as_of=AS_OF)
        expected = sum(counted for _, counted in cases.values())
        self.assertEqual(board["calls"], expected)
        self.assertEqual(board["counts"]["scored_events"], expected)
        self.assertEqual(board["excluded_forecasts"], len(cases) - expected)
        for name, (fields, counted) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(evidence.is_evidence(forecast(name, **fields)), counted)

    def test_evidence_labels_for_display(self):
        self.assertEqual(
            evidence.evidence_label(forecast("a", eligibility="post_cutoff")), "post_cutoff"
        )
        self.assertEqual(evidence.evidence_label(forecast("b")), "forward")
        self.assertEqual(
            evidence.evidence_label(forecast("c", mode="historical")), "historical_unlabelled"
        )

    def test_eligible_filter_only_narrows_the_evidence_set(self):
        forecasts = [
            forecast("keep"),
            forecast("drop"),
            forecast("bad", eligibility="contaminated", mode="historical"),
        ]
        ledger = FakeLedger(forecasts, [outcome(f["id"], 0.02) for f in forecasts])
        wide = evidence.scoreboard(ledger, as_of=AS_OF, eligible=lambda f: True)
        self.assertEqual(wide["calls"], 2)
        narrow = evidence.scoreboard(ledger, as_of=AS_OF, eligible=lambda f: f["id"] != "drop")
        self.assertEqual(narrow["calls"], 1)
        self.assertEqual(narrow["excluded_forecasts"], 2)

    def test_label_must_be_available_by_as_of(self):
        available = "2026-03-20T21:00:00.000000Z"
        ledger = FakeLedger([forecast("x")], [outcome("x", 0.01, available=available)])
        exact = evidence.scoreboard(ledger, as_of=available)
        self.assertEqual((exact["calls"], exact["pending_calls"]), (1, 0))
        early = evidence.scoreboard(ledger, as_of="2026-03-20T20:59:59.999999Z")
        self.assertEqual((early["calls"], early["pending_calls"]), (0, 1))
        self.assertEqual(early["baseline"]["events"], 0)
        # A label is not known before its outcome time even if the bars arrived earlier.
        late_close = FakeLedger(
            [forecast("y")],
            [
                outcome(
                    "y", 0.01, available="2026-03-10T21:00:00Z", outcome_at="2026-03-12T21:00:00Z"
                )
            ],
        )
        self.assertEqual(evidence.scoreboard(late_close, as_of="2026-03-11T00:00:00Z")["calls"], 0)
        self.assertEqual(evidence.scoreboard(late_close, as_of="2026-03-12T21:00:00Z")["calls"], 1)

    def test_forecasts_decided_after_as_of_are_invisible(self):
        ledger = FakeLedger([forecast("x", decided="2026-03-02T15:00:00Z")], [outcome("x", 0.01)])
        board = evidence.scoreboard(ledger, as_of="2026-03-02T14:59:59Z")
        self.assertEqual(board["counts"]["scored_events"], 0)
        self.assertEqual(board["excluded_forecasts"], 0)

    def test_net_return_charges_round_trip_cost_and_short_borrow(self):
        long_call = forecast("long", action="LONG")
        short_call = forecast("short", action="SHORT")
        watch = forecast("watch", action="WATCH")
        ledger = FakeLedger(
            [long_call, short_call, watch],
            [outcome("long", 0.01), outcome("short", -0.01), outcome("watch", 0.5)],
        )
        self.assertGreater(SHORT_COST, LONG_COST)
        self.assertAlmostEqual(
            evidence.net_return(long_call, outcome("long", 0.01)), 0.01 - LONG_COST
        )
        self.assertAlmostEqual(
            evidence.net_return(short_call, outcome("short", -0.01)), 0.01 - SHORT_COST
        )
        self.assertIsNone(evidence.net_return(watch, outcome("watch", 0.5)))
        board = evidence.scoreboard(ledger, as_of=AS_OF)
        self.assertEqual(board["calls"], 2)
        self.assertEqual(board["positive_calls"], 2)
        self.assertAlmostEqual(
            board["mean_net_return_per_call"], fmean([0.01 - LONG_COST, 0.01 - SHORT_COST])
        )
        self.assertEqual(board["counts"]["WATCH"], 1)

    def test_interval_is_a_t_interval_on_new_york_decision_date_means(self):
        # 2026-03-10T03:30Z is 23:30 on 9 March in New York (daylight time began 8 March).
        decided = {
            "a": ("2026-03-09T20:00:00Z", 0.012),
            "b": ("2026-03-10T03:30:00Z", 0.032),
            "c": ("2026-03-10T15:00:00Z", -0.008),
            "d": ("2026-03-11T15:00:00Z", 0.004),
        }
        ledger = FakeLedger(
            [forecast(k, decided=v[0]) for k, v in decided.items()],
            [outcome(k, v[1]) for k, v in decided.items()],
        )
        board = evidence.scoreboard(ledger, as_of=AS_OF)
        means = [
            fmean([0.012 - LONG_COST, 0.032 - LONG_COST]),
            -0.008 - LONG_COST,
            0.004 - LONG_COST,
        ]
        center = fmean(means)
        width = evidence.t_quantile(0.95, 2) * stdev(means) / math.sqrt(3)
        self.assertEqual((board["calls"], board["call_dates"]), (4, 3))
        self.assertAlmostEqual(board["mean_net_return"], center)
        self.assertAlmostEqual(board["interval"]["low"], center - width)
        self.assertAlmostEqual(board["interval"]["high"], center + width)
        self.assertEqual(board["interval"]["dates"], 3)
        self.assertEqual(board["interval"]["confidence"], 0.9)

    def test_status_follows_the_gate(self):
        spread = [0.001 * ((i % 5) - 2) for i in range(5)]
        cases = {
            "supported": [[0.03 + s for s in spread]] * 20,
            "no_edge": [[-0.01 + s for s in spread]] * 20,
            "inconclusive": [
                [0.08 + s for s in spread] if d % 2 else [-0.08 + s for s in spread]
                for d in range(20)
            ],
        }
        for status, days in cases.items():
            with self.subTest(status=status):
                board = evidence.scoreboard(calls(days), as_of=AS_OF)
                self.assertEqual(board["calls"], 100)
                self.assertEqual(board["status"], status)
                self.assertTrue(board["label"].startswith(status.replace("_", " ").capitalize()))
        supported = evidence.scoreboard(calls(cases["supported"]), as_of=AS_OF)
        self.assertGreater(supported["interval"]["low"], 0)
        self.assertIn("net of costs", supported["label"])
        no_edge = evidence.scoreboard(calls(cases["no_edge"]), as_of=AS_OF)
        self.assertLess(no_edge["interval"]["high"], evidence.GATE["futility_upper_bps"] / 10_000)
        self.assertIn("stays below +0.10%", no_edge["label"])

    def test_fewer_than_minimum_calls_is_collecting_even_with_a_clear_edge(self):
        days = [[0.05] * 3] * 33  # 99 calls
        board = evidence.scoreboard(calls(days), as_of=AS_OF)
        self.assertEqual(board["calls"], 99)
        self.assertEqual(board["status"], "collecting")
        self.assertIsNotNone(board["interval"])
        self.assertEqual(board["label"], "Collecting evidence: 99 of 100 matured calls")

    def test_one_decision_date_cannot_support_a_claim(self):
        board = evidence.scoreboard(calls([[0.05] * 120]), as_of=AS_OF)
        self.assertEqual((board["calls"], board["call_dates"]), (120, 1))
        self.assertIsNone(board["interval"])
        self.assertEqual(board["status"], "inconclusive")
        self.assertIn("2+ decision dates", board["label"])

    def test_zero_spread_across_dates_gives_a_point_interval(self):
        board = evidence.scoreboard(calls([[0.01]] * 100), as_of=AS_OF)
        self.assertAlmostEqual(board["interval"]["low"], 0.01 - LONG_COST)
        self.assertAlmostEqual(board["interval"]["high"], 0.01 - LONG_COST)
        self.assertEqual(board["status"], "supported")

    def test_each_event_counts_once_by_its_first_recorded_call(self):
        records = [
            forecast("watch", action="WATCH", recorded="2026-03-02T15:00:00Z"),
            forecast(
                "first",
                action="LONG",
                decided="2026-03-03T15:00:00Z",
                recorded="2026-03-03T15:00:00Z",
            ),
            forecast(
                "later",
                action="SHORT",
                decided="2026-03-04T15:00:00Z",
                recorded="2026-03-04T15:00:00Z",
            ),
            # A replay recorded afterwards cannot displace the first recorded call.
            forecast(
                "replay",
                action="LONG",
                decided="2026-03-01T15:00:00Z",
                recorded="2026-04-01T00:00:00Z",
            ),
        ]
        for record in records:
            record["event_id"] = "same-event"
        ledger = FakeLedger(
            records,
            [
                outcome("watch", 0.0),
                outcome("first", 0.02),
                outcome("later", 0.5),
                outcome("replay", 0.9),
            ],
        )
        board = evidence.scoreboard(ledger, as_of=AS_OF)
        self.assertEqual(board["counts"]["scored_events"], 1)
        self.assertEqual(board["counts"]["LONG"], 1)
        self.assertEqual(board["calls"], 1)
        self.assertAlmostEqual(board["mean_net_return"], 0.02 - LONG_COST)
        chosen = evidence.decisions(records)
        self.assertEqual([f["id"] for f in chosen], ["first"])

    def test_a_replay_never_replaces_a_forward_decision(self):
        # Decided forward as WATCH; a LONG replay recorded once the outcome was known must not
        # take its place, however its registry label reads.
        records = [
            forecast("forward-watch", action="WATCH", decided="2026-03-02T15:00:00Z"),
            forecast(
                "late-replay",
                action="LONG",
                decided="2026-03-02T14:00:00Z",
                recorded="2026-09-01T00:00:00Z",
                mode="historical",
                eligibility="no_model_knowledge",
            ),
        ]
        for record in records:
            record["event_id"] = "same-event"
        ledger = FakeLedger(records, [outcome("forward-watch", 0.0), outcome("late-replay", 0.9)])
        board = evidence.scoreboard(ledger, as_of=AS_OF)
        self.assertEqual((board["calls"], board["counts"]["WATCH"]), (0, 1))
        self.assertEqual([f["id"] for f in evidence.decisions(records)], ["forward-watch"])

    def test_replays_only_use_the_first_recorded_one_not_the_first_call(self):
        records = [
            forecast(
                "first-pass",
                action="PASS",
                mode="historical",
                recorded="2026-04-01T00:00:00Z",
            ),
            forecast(
                "later-long",
                action="LONG",
                mode="historical",
                recorded="2026-05-01T00:00:00Z",
            ),
        ]
        for record in records:
            record["event_id"] = "replayed"
        self.assertEqual([f["id"] for f in evidence.decisions(records)], ["first-pass"])

    def test_events_without_calls_use_their_first_forecast(self):
        records = [
            forecast("pass", action="PASS", recorded="2026-03-02T16:00:00Z"),
            forecast("watch", action="WATCH", recorded="2026-03-02T15:00:00Z"),
        ]
        for record in records:
            record["event_id"] = "quiet"
        self.assertEqual([f["id"] for f in evidence.decisions(records)], ["watch"])

    def test_baseline_mean_target_covers_every_matured_scored_event(self):
        records = [forecast("w", action="WATCH"), forecast("p", action="PASS"), forecast("l")]
        records.append(forecast("pending", action="WATCH"))
        ledger = FakeLedger(records, [outcome("w", 0.02), outcome("p", -0.01), outcome("l", 0.03)])
        board = evidence.scoreboard(ledger, as_of=AS_OF)
        self.assertEqual(board["baseline"]["events"], 3)
        self.assertAlmostEqual(board["baseline"]["mean_target"], fmean([0.02, -0.01, 0.03]))
        self.assertEqual(
            board["counts"],
            {"scored_events": 4, "WATCH": 2, "PASS": 1, "LONG": 1, "SHORT": 0, "other": 0},
        )

    def test_unknown_actions_are_never_calls(self):
        ledger = FakeLedger([forecast("odd", action="BUY")], [outcome("odd", 0.5)])
        board = evidence.scoreboard(ledger, as_of=AS_OF)
        self.assertEqual((board["calls"], board["counts"]["other"]), (0, 1))

    def test_reads_real_ledger_records(self):
        ledger = Ledger(":memory:")
        self.addCleanup(ledger.db.close)
        ledger.put("forecasts", "f1", forecast("f1", eligibility="forward"))
        ledger.put("outcomes", "f1", outcome("f1", 0.015))
        board = evidence.scoreboard(ledger, as_of=AS_OF)
        self.assertEqual(board["calls"], 1)
        self.assertAlmostEqual(board["mean_net_return"], 0.015 - LONG_COST)


if __name__ == "__main__":
    unittest.main()
