"""Direct coverage of the pending-observation queue shared by the CLI and the daemon."""

import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from jevtrader import cohorts
from jevtrader.common import digest, load_strategy, timestamp
from jevtrader.engine import ObservationRejected
from jevtrader.market import normalize_bar
from jevtrader.pipeline import FORECAST_FIELDS, check_options, observe_queue
from jevtrader.providers import MissingCredentials, ProviderInputError
from jevtrader.research import FEATURE_NAMES, VERSION
from jevtrader.store import KINDS, Ledger


def seed_market(ledger, count=40, symbols=("ABC", "SPY")):
    """Aligned historical fixture sessions; not an exchange calendar."""
    days, day = [], date(2026, 1, 5)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    for index, session in enumerate(days):
        for ticker in symbols:
            price = 100 + index
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


def event(identity, session, *, symbol="ABC", mode="historical"):
    return {
        "id": identity,
        "symbol": symbol,
        "text": f"Raised guidance and strong demand. Record revenue. Update {identity}.",
        "source_url": f"https://example.test/{identity}",
        "mode": mode,
        "published_at": f"{session}T21:15:00Z",
        "first_seen_at": f"{session}T21:30:00Z",
    }


def forecast(event_id, **changes):
    return {
        "id": f"forecast-{event_id}",
        "event_id": event_id,
        "mode": "historical",
        "action": "WATCH",
        "reasons": [],
        "expected_return": None,
        "extractor_key": "key",
        "extra": "not printed",
        **changes,
    }


def fake_observer(failures=None):
    """Returns a forecast per event unless ``failures`` maps its id to an exception."""
    failures = failures or {}

    def observe(ledger, event_id, strategy, **kwargs):
        if event_id in failures:
            raise failures[event_id]
        return forecast(event_id)

    return MagicMock(side_effect=observe)


def calibrator(cutoff, **changes):
    return {
        "model_id": "calibrator",
        "version": VERSION,
        "extractor_key": "key",
        "cutoff": timestamp(cutoff),
        "training_modes": ["historical"],
        "training_event_ids": [],
        "feature_names": list(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "mean": [0.0] * len(FEATURE_NAMES),
        "scale": [1.0] * len(FEATURE_NAMES),
        "intercept": 0.0,
        **changes,
    }


class ObserveQueueTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)
        self.strategy = load_strategy()
        self.days = seed_market(self.ledger)

    def queue(self, **options):
        options.setdefault("provider", "rules")
        options.setdefault("model", "rules-v1")
        return observe_queue(self.ledger, self.strategy, **options)

    def test_runs_is_a_ledger_kind(self):
        self.assertIn("runs", KINDS)
        self.assertTrue(self.ledger.put("runs", "run-1", {"job": "poll", "status": "ok"}))

    def test_out_of_time_starts_no_further_event_after_the_first(self):
        for index in (25, 26, 27):
            self.ledger.disclosure(event(f"valid-{index}", self.days[index]))
        observer, asked = fake_observer(), []

        def out_of_time():
            asked.append(observer.call_count)
            return True

        result = self.queue(replay=True, observer=observer, out_of_time=out_of_time)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["valid-25"])
        self.assertTrue(result["skipped"]["out_of_time"])
        self.assertEqual(asked, [1])  # never before the first event: every batch makes progress
        result = self.queue(replay=True, observer=fake_observer(), out_of_time=lambda: False)
        self.assertEqual(len(result["forecasts"]), 3)
        self.assertFalse(result["skipped"]["out_of_time"])

    def test_rejections_limit_and_market_data_skips_with_the_real_engine(self):
        self.ledger.disclosure(event("too-early", self.days[5]))  # Too little history.
        self.ledger.disclosure(event("no-bars", self.days[24], symbol="XYZ"))
        for index in (25, 26, 27):
            self.ledger.disclosure(event(f"valid-{index}", self.days[index]))
        result = self.queue(replay=True, limit=2)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["valid-25", "valid-26"])
        self.assertEqual([e["event_id"] for e in result["errors"]], ["too-early"])
        self.assertEqual(result["skipped"]["no_market_data"], 1)
        self.assertFalse(result["skipped"]["scan_truncated"])
        self.assertEqual(set(result["forecasts"][0]), set(FORECAST_FIELDS))
        stored = self.ledger.get("forecasts", result["forecasts"][0]["id"])
        self.assertEqual(
            stored["decision_at"], timestamp(event("x", self.days[25])["first_seen_at"])
        )
        # Decided events leave the queue; the local rejection is reported again.
        again = self.queue(replay=True, limit=2)
        self.assertEqual([f["event_id"] for f in again["forecasts"]], ["valid-27"])
        self.assertEqual([e["event_id"] for e in again["errors"]], ["too-early"])

    def test_live_queue_never_decides_historical_or_synthetic_records(self):
        self.ledger.disclosure(event("old", self.days[25]))
        self.ledger.disclosure(event("demo", self.days[26], mode="synthetic"))
        observer = fake_observer()
        result = self.queue(observer=observer)
        self.assertEqual((result["forecasts"], result["errors"]), ([], []))
        self.assertEqual(result["skipped"]["requires_replay"], 2)
        observer.assert_not_called()
        replayed = self.queue(replay=True, observer=observer)
        self.assertEqual([c.args[1] for c in observer.call_args_list], ["old", "demo"])
        self.assertEqual(
            [c.kwargs["as_of"] for c in observer.call_args_list],
            [timestamp(event("x", self.days[i])["first_seen_at"]) for i in (25, 26)],
        )
        self.assertEqual(len(replayed["forecasts"]), 2)

    def test_queue_replay_shows_the_label_mix_and_needs_a_preregistered_cohort(self):
        # #35: imported cohorts only count when registered before the import.
        cohorts.register(
            self.ledger, "c1", event_ids=["listed"], rule="fixture", now="2026-01-01T00:00:00Z"
        )
        self.ledger.disclosure(event("listed", self.days[25]))
        self.ledger.disclosure(event("unlisted", self.days[26]))
        result = self.queue(replay=True)
        stored = {
            f["event_id"]: self.ledger.get("forecasts", f["id"])["eligibility"]
            for f in result["forecasts"]
        }
        self.assertEqual(stored, {"listed": "no_model_knowledge", "unlisted": cohorts.LABEL})
        self.assertEqual(result["labels"], {"no_model_knowledge": 1, cohorts.LABEL: 1})

    def test_limit_counts_attempts_but_not_local_rejections(self):
        for index in range(21, 27):
            self.ledger.disclosure(event(f"event-{index}", self.days[index]))
        failures = {
            "event-21": ObservationRejected("Market data is stale"),
            "event-22": ProviderInputError("Disclosure too long"),
        }
        observer = fake_observer(failures)
        result = self.queue(replay=True, limit=2, observer=observer)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["event-23", "event-24"])
        self.assertEqual([e["event_id"] for e in result["errors"]], ["event-21", "event-22"])
        self.assertEqual(observer.call_count, 4)
        self.assertNotIn("extra", result["forecasts"][0])

    def test_scan_bound_stops_a_queue_of_rejections(self):
        for index in range(21, 30):
            self.ledger.disclosure(event(f"event-{index}", self.days[index]))
        stale = {f"event-{index}": ObservationRejected("stale") for index in range(21, 30)}
        for max_scan, calls, truncated in ((3, 3, True), (9, 9, True), (10, 9, False)):
            with self.subTest(max_scan=max_scan):
                observer = fake_observer(stale)
                result = self.queue(replay=True, limit=1, max_scan=max_scan, observer=observer)
                self.assertEqual(observer.call_count, calls)
                self.assertEqual(len(result["errors"]), calls)
                self.assertIs(result["skipped"]["scan_truncated"], truncated)

    def test_provider_failure_stops_paid_runs_but_not_the_rules_baseline(self):
        for index in (21, 22, 23):
            self.ledger.disclosure(event(f"event-{index}", self.days[index]))
        failures = {"event-21": RuntimeError("Provider transport failed")}
        paid = fake_observer(failures)
        result = self.queue(replay=True, provider="jev", model="jev-1.13.0", observer=paid)
        self.assertEqual(result["forecasts"], [])
        self.assertEqual([e["event_id"] for e in result["errors"]], ["event-21"])
        self.assertEqual(paid.call_count, 1)
        free = fake_observer(failures)
        result = self.queue(replay=True, limit=2, observer=free)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["event-22"])
        self.assertEqual(free.call_count, 2)  # The failure counted toward the limit.

    def test_missing_credentials_stop_the_run_and_keep_earlier_forecasts(self):
        for index in (21, 22, 23):
            self.ledger.disclosure(event(f"event-{index}", self.days[index]))
        missing = MissingCredentials("Set TYPESAFE_API_KEY to use JEV")
        observer = fake_observer({"event-22": missing})
        result = self.queue(replay=True, provider="jev", model="jev-1.13.0", observer=observer)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["event-21"])
        self.assertEqual(result["errors"], [{"event_id": "event-22", "error": str(missing)}])
        self.assertTrue(result["skipped"]["missing_credentials"])
        self.assertEqual(observer.call_count, 2)

    def test_failed_paid_request_is_skipped_unless_retried_or_named(self):
        self.ledger.disclosure(event("failing", self.days[24]))
        self.ledger.disclosure(event("valid", self.days[25]))
        attempt = {
            "event_id": "failing",
            "provider": "jev",
            "requested_model": "jev-1.13.0",
            "questions_digest": digest(self.strategy["questions"]),
            "attempted_at": "2026-02-10T00:00:00.000000Z",
            "extraction_id": "never-stored",
        }
        self.ledger.put("attempts", "attempt", attempt)
        paid = {"replay": True, "provider": "jev", "model": "jev-1.13.0"}
        cases = [
            ({}, ["valid"], 1),
            ({"retry_failed": True}, ["failing", "valid"], 0),
            ({"event": "failing"}, ["failing"], 0),
            ({"model": "jev-1.14.0"}, ["failing", "valid"], 0),
            ({"provider": "openai", "model": "jev-1.13.0"}, ["failing", "valid"], 0),
        ]
        for changes, expected, failed_before in cases:
            with self.subTest(changes):
                observer = fake_observer()
                result = self.queue(**{**paid, **changes, "observer": observer})
                self.assertEqual([c.args[1] for c in observer.call_args_list], expected)
                self.assertEqual(result["skipped"]["failed_before"], failed_before)

    def test_calibrator_prefilter_and_refusals(self):
        for index in range(21, 26):
            self.ledger.disclosure(event(f"event-{index}", self.days[index]))
        model = calibrator(f"{self.days[23]}T00:00:00Z", training_event_ids=["event-25"])
        self.ledger.put("models", "calibrator", model)
        synthetic = {**model, "model_id": "synthetic", "training_modes": ["synthetic"]}
        self.ledger.put("models", "synthetic", synthetic)
        self.ledger.put("models", "stale", {**model, "model_id": "stale", "version": "v0"})
        observer = fake_observer()
        result = self.queue(replay=True, calibrator="calibrator", observer=observer)
        # First seen at or before the cutoff (21, 22) or used in training (25).
        self.assertEqual([c.args[1] for c in observer.call_args_list], ["event-23", "event-24"])
        self.assertEqual(
            {c.kwargs["calibrator_id"] for c in observer.call_args_list}, {"calibrator"}
        )
        self.assertEqual(result["skipped"]["calibrator_ineligible"], 3)
        result = self.queue(replay=True, calibrator="synthetic", observer=fake_observer())
        self.assertEqual(result["skipped"]["calibrator_ineligible"], 5)
        for name, message in (("stale", "re-fit"), ("missing", "Unknown calibrator")):
            with self.subTest(name), self.assertRaisesRegex(ValueError, message):
                self.queue(replay=True, calibrator=name, observer=observer)

    def test_invalid_options_fail_before_any_observation(self):
        self.ledger.disclosure(event("valid", self.days[25]))
        observer = fake_observer()
        cases = [
            ({"limit": 0}, "--limit"),
            ({"limit": 201}, "--limit"),
            ({"max_scan": 0}, "--max-scan"),
            ({"as_of": "2026-02-10T00:00:00Z"}, "--as-of requires"),
            ({"as_of": "2026-02-10T00:00:00Z", "event": "valid", "replay": True}, "exclusive"),
            ({"model": ""}, "model"),
            ({"event": "missing"}, "Disclosure not found"),
        ]
        for options, message in cases:
            with self.subTest(options), self.assertRaisesRegex(ValueError, message):
                self.queue(observer=observer, **options)
        observer.assert_not_called()
        check_options(limit=200, max_scan=1, event="valid", as_of="2026-02-10T00:00:00Z")

    def test_named_event_bypasses_queue_filters_and_uses_explicit_time(self):
        self.ledger.disclosure(event("old", self.days[25]))
        observer = fake_observer()
        as_of = f"{self.days[30]}T15:00:00Z"
        self.queue(event="old", as_of=as_of, observer=observer)
        self.assertEqual(observer.call_args.args[1], "old")
        self.assertEqual(observer.call_args.kwargs["as_of"], as_of)
        # Picked by hand, possibly knowing the outcome: the replay is never evidence.
        self.assertIs(observer.call_args.kwargs["adhoc"], True)
        self.queue(replay=True, observer=observer)
        self.assertNotIn("adhoc", observer.call_args.kwargs)  # the queue picks by rule
        # Without a time a named historical event reaches the engine, which rejects it.
        result = self.queue(event="old")
        self.assertEqual(result["forecasts"], [])
        self.assertIn("--replay", result["errors"][0]["error"])

    def test_engine_address_and_declared_cutoffs_reach_the_observer(self):
        self.ledger.disclosure(event("valid", self.days[25]))
        declared = {"local:qwen3:14b": {"training_cutoff": "2024-12-01"}}
        for options in (
            {"event": "valid", "as_of": f"{self.days[30]}T15:00:00Z"},
            {"replay": True},
        ):
            with self.subTest(named="event" in options):
                observer = fake_observer()
                self.queue(
                    observer=observer,
                    base_url="http://127.0.0.1:1234/v1",
                    overrides=declared,
                    **options,
                )
                self.assertEqual(observer.call_args.kwargs["overrides"], declared)
                self.assertEqual(observer.call_args.kwargs["base_url"], "http://127.0.0.1:1234/v1")
        # Not given: the engine keeps its own defaults.
        observer = fake_observer()
        self.queue(event="valid", replay=True, observer=observer)
        self.assertNotIn("overrides", observer.call_args.kwargs)
        self.assertNotIn("base_url", observer.call_args.kwargs)

    def test_transport_reaches_the_default_engine_observer(self):
        self.ledger.disclosure(event("valid", self.days[25]))
        with patch("jevtrader.engine.observe", return_value=forecast("valid")) as observe:
            transport = object()
            self.queue(replay=True, transport=transport)
        self.assertIs(observe.call_args.kwargs["transport"], transport)
        self.assertEqual(observe.call_args.kwargs["model"], "rules-v1")

    def test_paid_extraction_uses_injected_transport_offline(self):
        self.ledger.disclosure(event("valid", self.days[25]))
        response = {
            "model": "jev-1.13.0",
            "answers": {
                "direction": {
                    "type": "choice",
                    "choice": "improving",
                    "confidence": 0.75,
                    "probabilities": {
                        "improving": 0.8,
                        "unchanged": 0.05,
                        "deteriorating": 0.1,
                        "unclear": 0.05,
                    },
                },
                "materiality": {"type": "noul", "noul": 0.9},
                "novelty": {"type": "noul", "noul": 0.8},
            },
            "usage": {"input_tokens": 122, "output_tokens": 10},
        }
        transport = MagicMock(return_value=response)
        with (
            patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-secret"}),
            patch("jevtrader.providers.post_json") as network,
        ):
            result = self.queue(
                replay=True, provider="jev", model="jev-1.13.0", transport=transport
            )
        network.assert_not_called()
        transport.assert_called_once()
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["valid"])
        self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()
