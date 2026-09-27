"""Offline integration tests for immutable, causally timed research records."""

import unittest
from datetime import date, timedelta
from unittest.mock import patch

from jevtrader import engine
from jevtrader.common import load_strategy, timestamp
from jevtrader.market import normalize_bar, outcome
from jevtrader.providers import extract_features
from jevtrader.research import FEATURE_NAMES
from jevtrader.store import Ledger


def sessions(count=50):
    """Explicit fixture sessions; this is not a production exchange calendar."""
    result, day = [], date(2026, 1, 5)
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day.isoformat())
        day += timedelta(days=1)
    return result


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)
        self.strategy = load_strategy()
        self.strategy["horizon_sessions"] = 3
        self.strategy["min_train_samples"] = 10
        self.days = sessions()

    def at(self, index, clock="22:00:00"):
        return self.days[index] + "T" + clock + "Z"

    def populate(self, *, mode="historical", receipts=None):
        receipts = receipts or {}
        for index, day in enumerate(self.days):
            for ticker, growth in (("ABC", 0.7), ("SPY", 0.2)):
                opening = 100 + growth * index
                closing = opening + 0.1 + (index % 3) * 0.03
                row = {
                    "symbol": ticker,
                    "session": day,
                    "open_at": self.at(index, "14:30:00"),
                    "close_at": self.at(index, "21:00:00"),
                    "open": opening,
                    "high": closing + 1,
                    "low": opening - 1,
                    "close": closing,
                    "volume": 1_000_000,
                    "available_at": receipts.get((ticker, index), self.at(index, "21:00:00")),
                }
                # Forward fixtures simulate receipt at each completed session,
                # without creating historical records disguised as live input.
                with patch("jevtrader.market.utc_now", return_value=row["available_at"]):
                    bar = normalize_bar(row, mode=mode)
                self.ledger.put("bars", bar["id"], bar)

    def event(
        self,
        identity="current",
        index=20,
        *,
        mode="historical",
        published=None,
        first_seen=None,
        text=None,
        imported=True,
    ):
        event = {
            "id": identity,
            "symbol": "ABC",
            "mode": mode,
            "published_at": published or self.at(index, "21:15:00"),
            "first_seen_at": first_seen or self.at(index, "21:30:00"),
            "source_url": f"https://example.test/{identity}",
            "source_type": "fixture",
            "text": text
            or f"Raised guidance and strong demand for recurring subscription contracts. Record revenue. Disclosure {identity}.",
        }
        self.ledger.disclosure(event, imported=imported)
        return event

    def observe(self, event_id="current", index=20, **kwargs):
        return engine.observe(self.ledger, event_id, self.strategy, as_of=self.at(index), **kwargs)

    def calibrator(self, key, *, identity="calibrator", **changes):
        model = {
            "model_id": identity,
            "extractor_key": key,
            "cutoff": self.at(19),
            "training_modes": ["historical"],
            "training_event_ids": ["unrelated"],
            "feature_names": list(FEATURE_NAMES),
            "coefficients": [0.0] * len(FEATURE_NAMES),
            "mean": [0.0] * len(FEATURE_NAMES),
            "scale": [1.0] * len(FEATURE_NAMES),
            "intercept": 0.03,
        }
        model.update(changes)
        self.ledger.put("models", identity, model)
        return identity

    def test_observation_requires_actual_first_seen_not_publication(self):
        self.populate()
        self.event(first_seen=self.at(20, "22:15:00"))
        with patch.object(engine, "extract_features", wraps=extract_features) as extract:
            with self.assertRaisesRegex(ValueError, "not observed"):
                self.observe()
            extract.assert_not_called()
        self.assertEqual(self.ledger.counts().get("forecasts", 0), 0)

    def test_previous_document_excludes_future_publication_and_late_receipt(self):
        self.populate()
        self.event("known", index=17, text="The previous operating report was stable.")
        self.event(
            "late", index=19, first_seen=self.at(22), text="Older publication received later."
        )
        self.event("future", index=23, text="Future published disclosure.")
        self.event()
        with patch.object(engine, "extract_features", wraps=extract_features) as extract:
            result = self.observe()
        self.assertEqual(result["previous_event_id"], "known")
        self.assertEqual(extract.call_args.args[3], "The previous operating report was stable.")

    def test_previous_document_does_not_cross_synthetic_provenance(self):
        self.populate()
        self.event("fixture-prior", index=19, mode="synthetic")
        self.event()
        result = self.observe()
        self.assertIsNone(result["previous_event_id"])
        self.assertEqual(result["features"][FEATURE_NAMES.index("novelty")], 0)

    def test_future_market_bars_cannot_change_decision_snapshot(self):
        self.populate()
        self.event()
        result = self.observe()
        self.assertEqual(result["market"]["session"], self.days[20])
        self.assertAlmostEqual(result["market"]["price"], 100 + 0.7 * 20 + 0.16)
        self.assertEqual(len(result["market"]["bar_ids"]), 42)
        self.assertNotIn(f"ABC:{self.days[21]}", result["market"]["bar_ids"])

    def test_extraction_cache_and_historical_forecast_are_idempotent(self):
        self.populate()
        self.event("previous", index=17, text="Business was unchanged.")
        self.event()

        def no_network(*args, **kwargs):
            self.fail("Rules extraction must never call a network transport")

        with patch.object(engine, "extract_features", wraps=extract_features) as extract:
            first = self.observe(transport=no_network)
            repeated = self.observe(transport=no_network)
            later = engine.observe(
                self.ledger,
                "current",
                self.strategy,
                as_of=self.at(20, "22:01:00"),
                transport=no_network,
            )
        self.assertEqual(first, repeated)
        self.assertNotEqual(first["id"], later["id"])
        self.assertEqual(first["extraction_id"], later["extraction_id"])
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(self.ledger.counts()["extractions"], 1)
        self.assertEqual(self.ledger.counts()["forecasts"], 2)

    def test_uncalibrated_rules_observation_is_watch(self):
        self.populate()
        self.event("previous", index=17, text="Business was unchanged.")
        self.event()
        result = self.observe()
        self.assertEqual(result["action"], "WATCH")
        self.assertIsNone(result["expected_return"])
        self.assertEqual(result["resolved_model"], "rules-v1")
        self.assertIn("no trained calibrator: observation only", result["reasons"])

    def test_forward_decision_time_includes_extraction_latency(self):
        self.populate(mode="forward")
        self.event(mode="forward", imported=False)
        clock_values = [
            self.at(20),
            self.at(20, "22:00:01"),
            self.at(20, "22:00:02"),
            self.at(20, "22:00:03"),
        ]
        with patch.object(engine, "utc_now", side_effect=clock_values):
            result = engine.observe(self.ledger, "current", self.strategy)
        self.assertEqual(result["mode"], "forward")
        self.assertEqual(timestamp(result["decision_at"]), timestamp(self.at(20, "22:00:02")))
        self.assertEqual(result["market"]["session"], self.days[20])

    def test_imported_disclosures_cannot_claim_forward_provenance(self):
        with self.assertRaisesRegex(ValueError, "live collector"):
            self.event(mode="forward")
        self.assertEqual(self.ledger.counts(), {})

    def test_settlement_is_maturity_gated_and_outcomes_are_immutable(self):
        self.populate()
        self.event()
        forecast = self.observe()
        immature = engine.settle(self.ledger, as_of=self.at(22))
        self.assertEqual(immature["added"], 0)
        self.assertEqual(immature["unresolved_ids"], [forecast["id"]])
        mature = engine.settle(self.ledger, as_of=self.at(23))
        frozen = self.ledger.get("outcomes", forecast["id"])
        self.assertEqual(mature["added"], 1)
        self.assertEqual(frozen["entry_at"], timestamp(self.at(21, "14:30:00")))
        self.assertEqual(frozen["outcome_at"], timestamp(self.at(23, "21:00:00")))
        self.assertEqual(engine.settle(self.ledger, as_of=self.at(30))["added"], 0)
        self.assertEqual(frozen, self.ledger.get("outcomes", forecast["id"]))
        modified = {**frozen, "target": frozen["target"] + 1}
        with self.assertRaisesRegex(ValueError, "Immutable record conflict"):
            self.ledger.put("outcomes", forecast["id"], modified)

    def test_delayed_label_receipt_controls_settlement_and_training_cutoff(self):
        receipt = self.at(27)
        self.populate(receipts={("ABC", 23): receipt})
        self.event()
        forecast = self.observe()
        self.assertEqual(engine.settle(self.ledger, as_of=self.at(24))["added"], 0)
        self.assertEqual(engine.settle(self.ledger, as_of=self.at(28))["added"], 1)
        key = forecast["extractor_key"]
        self.assertEqual(engine.training_rows(self.ledger, key, before=self.at(25)), [])
        self.assertEqual(engine.training_rows(self.ledger, key, before=receipt), [])
        rows = engine.training_rows(self.ledger, key, before=self.at(28))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome_at"], timestamp(receipt))

    def test_training_freezes_earliest_event_observation_even_if_unresolved(self):
        self.populate()
        self.event()
        first = self.observe()
        later = self.observe(index=21)
        later_outcome = outcome(self.ledger, later, self.at(30))
        self.ledger.put("outcomes", later["id"], later_outcome)
        self.assertEqual(engine.training_rows(self.ledger, first["extractor_key"]), [])
        first_outcome = outcome(self.ledger, first, self.at(30))
        self.ledger.put("outcomes", first["id"], first_outcome)
        rows = engine.training_rows(self.ledger, first["extractor_key"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision_at"], first["decision_at"])

    def test_replay_cannot_silently_replace_forward_training_observation(self):
        self.populate(mode="forward")
        self.event(mode="forward", imported=False)
        with patch.object(engine, "utc_now", return_value=self.at(20)):
            forward = engine.observe(self.ledger, "current", self.strategy)
        replay = engine.observe(
            self.ledger, "current", self.strategy, as_of=self.at(20, "21:30:00")
        )
        self.assertEqual(forward["extractor_key"], replay["extractor_key"])
        self.assertLess(replay["decision_at"], forward["decision_at"])
        self.assertEqual(engine.settle(self.ledger, as_of=self.at(30))["added"], 2)
        key = forward["extractor_key"]
        with self.assertRaises(ValueError):
            engine.training_rows(self.ledger, key)
        with self.assertRaises(ValueError):
            engine.train(self.ledger, key, self.at(30), self.strategy)
        with self.assertRaises(ValueError):
            engine.evaluate(self.ledger, key, self.strategy, before=self.at(30))
        for mode, expected in (("forward", forward), ("historical", replay)):
            with self.subTest(mode=mode):
                rows = engine.training_rows(self.ledger, key, mode=mode)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["mode"], mode)
                self.assertEqual(rows[0]["decision_at"], expected["decision_at"])
        self.assertEqual(engine.training_rows(self.ledger, key, mode="synthetic"), [])

    def test_invalid_calibrator_is_rejected_before_paid_extraction(self):
        self.populate()
        self.event()
        cases = [
            ("missing", None, "Unknown calibrator"),
            ("equal-cutoff", {"cutoff": self.at(20)}, "strictly before"),
            ("future-cutoff", {"cutoff": self.at(21)}, "strictly before"),
            ("same-event", {"training_event_ids": ["current"]}, "used to train"),
            ("synthetic-model", {"training_modes": ["synthetic"]}, "synthetic model"),
        ]
        for identity, changes, message in cases:
            with self.subTest(identity=identity):
                if changes is not None:
                    self.calibrator("unresolved-extractor", identity=identity, **changes)
                with patch.object(
                    engine,
                    "extract_features",
                    side_effect=AssertionError("Paid extraction ran before calibrator rejection"),
                ) as extract:
                    with self.assertRaisesRegex(ValueError, message):
                        self.observe(provider="jev", model="jev-1.13.0", calibrator_id=identity)
                    extract.assert_not_called()
        self.assertEqual(self.ledger.counts().get("extractions", 0), 0)
        self.assertEqual(self.ledger.counts().get("forecasts", 0), 0)

    def test_historical_observation_requires_explicit_decision_time(self):
        self.populate()
        for mode in ("historical", "synthetic"):
            with self.subTest(mode=mode):
                self.event(f"{mode}-event", mode=mode)
                with (
                    patch.object(engine, "utc_now", return_value=self.at(22)),
                    patch.object(engine, "extract_features") as extract,
                    self.assertRaisesRegex(ValueError, "--replay or --as-of"),
                ):
                    engine.observe(self.ledger, f"{mode}-event", self.strategy)
                extract.assert_not_called()
        self.assertEqual(self.ledger.counts().get("forecasts", 0), 0)

    def test_long_documents_use_a_recorded_deterministic_excerpt(self):
        self.populate()
        long_text = "Raised guidance with strong demand. " * 1_400  # 50,400 characters
        self.event("long-prior", index=21, text=long_text)
        self.event("long-current", index=23, text=long_text + "New contract signed.")
        first = self.observe("long-prior", index=21)
        second = self.observe("long-current", index=23)
        repeated = self.observe("long-current", index=23)
        self.assertEqual(second, repeated)
        self.assertEqual(second["previous_event_id"], "long-prior")
        for forecast, previous_total in ((first, 0), (second, len(long_text))):
            excerpt = self.ledger.get("extractions", forecast["extraction_id"])["text_excerpt"]
            self.assertLessEqual(excerpt["current_chars"] + excerpt["previous_chars"], 40_000)
            self.assertEqual(excerpt["previous_total"], previous_total)
        self.assertEqual(
            self.ledger.get("extractions", second["extraction_id"])["text_excerpt"],
            {
                "current_chars": 30_000,
                "current_total": len(long_text) + 20,
                "previous_chars": 10_000,
                "previous_total": len(long_text),
            },
        )

    def test_calibrator_schema_mismatch_is_rejected_before_paid_extraction(self):
        self.populate()
        self.event()
        rules = self.observe()
        model_id = self.calibrator(rules["extractor_key"])
        self.event("fresh", index=21)
        with (
            patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-secret"}),
            patch.object(
                engine,
                "extract_features",
                side_effect=AssertionError("Paid extraction ran before schema check"),
            ) as extract,
            self.assertRaisesRegex(ValueError, "schemas/models"),
        ):
            self.observe(
                "fresh", index=21, provider="jev", model="jev-1.13.0", calibrator_id=model_id
            )
        extract.assert_not_called()

    def test_calibrator_rejects_schema_cutoff_and_training_event_mismatches(self):
        self.populate()
        self.event()
        forecast = self.observe()
        cases = [
            ("schema", {"extractor_key": "wrong"}, "schemas/models"),
            ("equal-cutoff", {"cutoff": self.at(20)}, "strictly before"),
            ("future-cutoff", {"cutoff": self.at(21)}, "strictly before"),
            ("same-event", {"training_event_ids": ["current"]}, "used to train"),
            ("feature-schema", {"feature_names": ["wrong"]}, "feature_names"),
        ]
        for identity, updates, message in cases:
            with self.subTest(identity=identity):
                model_id = self.calibrator(forecast["extractor_key"], identity=identity, **updates)
                with self.assertRaisesRegex(ValueError, message):
                    self.observe(calibrator_id=model_id)
        self.assertEqual(self.ledger.counts()["forecasts"], 1)

    def test_synthetic_calibrator_cannot_score_real_disclosures(self):
        self.populate()
        self.event()
        forecast = self.observe()
        model_id = self.calibrator(forecast["extractor_key"], training_modes=["synthetic"])
        with self.assertRaisesRegex(ValueError, "synthetic model"):
            self.observe(calibrator_id=model_id)

    def test_synthetic_observations_and_labels_keep_their_provenance(self):
        self.populate(mode="synthetic")
        self.event(mode="synthetic")
        forecast = self.observe()
        self.assertEqual(forecast["mode"], "synthetic")
        self.assertEqual(engine.settle(self.ledger, as_of=self.at(24))["added"], 1)
        rows = engine.training_rows(self.ledger, forecast["extractor_key"])
        self.assertEqual(rows[0]["mode"], "synthetic")

    def test_train_builds_and_reuses_real_ridge_model_from_matured_events(self):
        self.populate()
        for index in range(20, 32):
            self.event(f"event-{index}", index=index)
            forecast = self.observe(f"event-{index}", index=index)
        self.assertEqual(engine.settle(self.ledger, as_of=self.at(36))["added"], 12)
        key = forecast["extractor_key"]
        first = engine.train(self.ledger, key, self.at(36), self.strategy)
        repeated = engine.train(self.ledger, key, self.at(36), self.strategy)
        self.assertEqual(first, repeated)
        self.assertEqual(first["training_count"], 12)
        self.assertEqual(first["training_modes"], ["historical"])
        self.assertEqual(self.ledger.counts()["models"], 1)
        self.event("fresh", index=37)
        scored = self.observe("fresh", index=37, calibrator_id=first["model_id"])
        self.assertIsInstance(scored["expected_return"], float)
        self.assertNotEqual(scored["action"], "WATCH")


if __name__ == "__main__":
    unittest.main()
