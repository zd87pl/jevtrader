"""Offline integration tests for immutable, causally timed research records."""

import copy
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from jevtrader import cohorts, engine, registry
from jevtrader.common import digest, load_strategy, round_trip_bps, timestamp
from jevtrader.market import normalize_bar, outcome
from jevtrader.providers import (
    MissingCredentials,
    ProviderError,
    ProviderValidationError,
    extract_features,
)
from jevtrader.security.sanitize import SANITIZER_VERSION
from jevtrader.research import FEATURE_NAMES, VERSION
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
            "version": VERSION,
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

    def test_legacy_text_is_sanitized_before_the_provider_and_version_is_in_the_spec(self):
        self.populate()
        self.event("known", index=17, text="Prior re\u200bport was \u202estable.")
        self.event(text="Raised guid\u200bance and \uff33trong demand for subscription contracts.")
        with patch.object(engine, "extract_features", wraps=extract_features) as extract:
            result = self.observe()
        self.assertEqual(
            extract.call_args.args[2],
            "Raised guidance and Strong demand for subscription contracts.",
        )
        self.assertEqual(extract.call_args.args[3], "Prior report was stable.")
        spec = self.ledger.get("extractions", result["extraction_id"])["spec"]
        self.assertEqual(spec["sanitizer_version"], SANITIZER_VERSION)

    def test_decision_path_reads_disclosures_and_bars_through_as_of(self):
        # Issue #21: the prior disclosure and the market snapshot come from Ledger.as_of at
        # decision_at, so a record the ledger learned after the decision is invisible.
        self.populate(receipts={("ABC", 21): self.at(23), ("SPY", 21): self.at(23)})
        self.event("known", index=17, text="The previous operating report was stable.")
        self.event("late", index=19, first_seen=self.at(23), text="Received after decision.")
        self.event(index=21)

        def guarded(read):
            def call(kind, *args):
                if kind in {"disclosures", "bars"}:
                    raise AssertionError(f"decision path bypassed as_of: {kind}")
                return read(kind, *args)

            return call

        with (
            patch.object(self.ledger, "all", side_effect=guarded(self.ledger.all)),
            patch.object(self.ledger, "prefix", side_effect=guarded(self.ledger.prefix)),
            patch.object(self.ledger, "as_of", wraps=self.ledger.as_of) as as_of,
        ):
            result = self.observe(index=21)
        self.assertEqual(result["previous_event_id"], "known")
        self.assertEqual(result["market"]["session"], self.days[20])
        decision = timestamp(self.at(21))
        self.assertIn(("disclosures", decision), [c.args for c in as_of.call_args_list])
        self.assertIn(("bars", decision), [c.args for c in as_of.call_args_list])

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

    def test_replay_freezes_its_evidence_basis_and_hand_picked_replays_never_count(self):
        self.populate()
        # #35: only a pre-registered cohort keeps its registry label.
        cohorts.register(self.ledger, "c", event_ids=["current"], rule="fixture", now=self.at(0))
        self.event("previous", index=17, text="Business was unchanged.")
        current = self.event()
        result = self.observe()
        self.assertEqual(result["eligibility"], "no_model_knowledge")
        self.assertEqual(
            result["eligibility_basis"],
            {
                "key": "rules:rules-v1",
                "training_cutoff": None,
                "origin": "builtin",
                "source": registry.MODELS["rules:rules-v1"]["source"],
            },
        )
        # What was sent, so the spend cap need not trust a provider's own token count.
        questions = sum(len(text) for text in self.strategy["questions"].values())
        extraction = self.ledger.get("extractions", result["extraction_id"])
        self.assertEqual(
            extraction["input_chars"],
            len(current["text"]) + len("Business was unchanged.") + questions,
        )
        adhoc = engine.observe(
            self.ledger, "current", self.strategy, as_of=self.at(20, "22:01:00"), adhoc=True
        )
        self.assertEqual(adhoc["eligibility"], "adhoc_replay")
        self.assertFalse(registry.counts_as_evidence(adhoc["eligibility"]))

    def test_replay_of_an_unregistered_import_is_not_evidence(self):
        # #35: a user could import only filings that were followed by moves.
        self.populate()
        self.event("previous", index=17, text="Business was unchanged.")
        self.event()
        result = self.observe()
        self.assertEqual(result["eligibility"], cohorts.LABEL)
        self.assertFalse(registry.counts_as_evidence(result["eligibility"]))

    def test_replay_of_a_preregistered_cohort_keeps_its_registry_label(self):
        self.populate()
        cohorts.register(
            self.ledger,
            "feb",
            event_ids=["current", "previous"],
            rule="Every fixture filing",
            now=self.at(0),
        )
        self.event("previous", index=17, text="Business was unchanged.")
        self.event()
        result = self.observe()
        self.assertEqual(result["eligibility"], "no_model_knowledge")
        self.assertEqual(cohorts.preregistered(self.ledger, "current"), "feb")
        adhoc = self.observe(index=21, adhoc=True)
        self.assertEqual(adhoc["eligibility"], "adhoc_replay")

    def test_cohort_rule_leaves_forward_and_non_evidence_labels_alone(self):
        self.populate()
        self.event()
        with patch.object(registry, "eligibility", return_value="contaminated"):
            result = self.observe()
        self.assertEqual(result["eligibility"], "contaminated")

    def test_replay_the_registry_cannot_label_is_never_evidence(self):
        self.populate()
        self.event("previous", index=17, text="Business was unchanged.")
        self.event()
        with patch.object(registry, "eligibility", side_effect=ValueError("unidentified")):
            result = self.observe()
        self.assertEqual(result["eligibility"], "unknown_cutoff")
        self.assertIsNone(result["eligibility_basis"])
        self.assertFalse(registry.counts_as_evidence(result["eligibility"]))

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

    def test_outcome_reads_bars_through_as_of_at_the_settlement_time(self):
        # Issue #21: a label bar the ledger learns after as_of is invisible to outcome().
        self.populate(receipts={("ABC", 23): self.at(27)})
        self.event()
        forecast = self.observe()

        def guarded(kind, *args):
            raise AssertionError(f"outcome bypassed as_of: {kind}")

        with (
            patch.object(self.ledger, "prefix", side_effect=guarded),
            patch.object(self.ledger, "as_of", wraps=self.ledger.as_of) as as_of,
        ):
            self.assertIsNone(outcome(self.ledger, forecast, self.at(24)))
            settled = outcome(self.ledger, forecast, self.at(28))
        self.assertEqual(settled["label_available_at"], timestamp(self.at(27)))
        self.assertIn(("bars", self.at(24)), [c.args for c in as_of.call_args_list])

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
        # Position-coded text, so any other slice of either document is detectable.
        prior = "".join(f"p{i:06d} " for i in range(6_300))  # 50,400 characters
        current = "".join(f"c{i:06d} " for i in range(6_300))
        self.event("long-prior", index=21, text=prior)
        self.event("long-current", index=23, text=current)
        with patch.object(engine, "extract_features", wraps=extract_features) as extract:
            first = self.observe("long-prior", index=21)
            second = self.observe("long-current", index=23)
            repeated = self.observe("long-current", index=23)
        self.assertEqual(second, repeated)
        self.assertEqual(second["previous_event_id"], "long-prior")
        self.assertEqual(extract.call_count, 2)
        # Without a comparison document, nothing is reserved for one.
        self.assertEqual(extract.call_args_list[0].args[2:4], (prior[:40_000], ""))
        self.assertEqual(extract.call_args_list[1].args[2:4], (current[:30_000], prior[:10_000]))
        # Budget a short current document leaves unused goes to the previous one.
        short = "".join(f"s{i:06d} " for i in range(550))  # 4,400 characters
        self.event("short-current", index=25, text=short)
        with patch.object(engine, "extract_features", wraps=extract_features) as extract:
            self.observe("short-current", index=25)
        self.assertEqual(extract.call_args.args[2:4], (short, current[: 40_000 - len(short)]))
        excerpts = {
            forecast["id"]: self.ledger.get("extractions", forecast["extraction_id"])
            for forecast in (first, second)
        }
        self.assertEqual(
            excerpts[first["id"]]["text_excerpt"],
            {
                "current_chars": 40_000,
                "current_total": len(prior),
                "previous_chars": 0,
                "previous_total": 0,
            },
        )
        self.assertEqual(
            excerpts[second["id"]]["text_excerpt"],
            {
                "current_chars": 30_000,
                "current_total": len(current),
                "previous_chars": 10_000,
                "previous_total": len(prior),
            },
        )
        # The cache key covers the full stored documents, not just the excerpt sent.
        spec = excerpts[second["id"]]["spec"]
        self.assertEqual(
            second["extraction_id"], digest({"spec": spec, "current": current, "previous": prior})
        )

    def test_calibrator_schema_mismatch_is_rejected_before_paid_extraction(self):
        self.populate()
        self.event()
        rules = self.observe()
        model_id = self.calibrator(rules["extractor_key"])
        self.event("fresh", index=21)
        questions = copy.deepcopy(self.strategy)
        questions["questions"]["novelty"] = "A different novelty question for this calibrator."
        horizon = {**self.strategy, "horizon_sessions": self.strategy["horizon_sessions"] + 1}
        spread = {**self.strategy, "spread_bps": self.strategy["spread_bps"] + 1}
        history = {
            **self.strategy,
            "min_history_sessions": self.strategy["min_history_sessions"] + 1,
        }
        cases = [
            ("provider", self.strategy, {"provider": "jev", "model": "jev-1.13.0"}),
            ("requested-model", self.strategy, {"model": "rules-v2"}),
            ("questions", questions, {}),
            ("horizon", horizon, {}),
            ("spread", spread, {}),
            ("history", history, {}),
        ]
        for label, strategy, options in cases:
            with (
                self.subTest(label),
                patch.object(
                    engine,
                    "extract_features",
                    side_effect=AssertionError("Paid extraction ran before schema check"),
                ) as extract,
                self.assertRaisesRegex(engine.ObservationRejected, "schemas/models"),
            ):
                engine.observe(
                    self.ledger,
                    "fresh",
                    strategy,
                    as_of=self.at(21),
                    calibrator_id=model_id,
                    **options,
                )
            extract.assert_not_called()

    def test_cached_extraction_for_another_calibrator_is_a_local_rejection(self):
        self.populate()
        self.event()
        self.observe()
        model_id = self.calibrator("resolved-by-another-model")
        with (
            patch.object(
                engine, "extract_features", side_effect=AssertionError("Extraction is cached")
            ) as extract,
            self.assertRaisesRegex(engine.ObservationRejected, "schemas/models"),
        ):
            self.observe(calibrator_id=model_id)
        extract.assert_not_called()

    def test_calibrator_from_another_evaluator_version_is_rejected(self):
        self.populate()
        self.event()
        forecast = self.observe()
        self.event("fresh", index=21)
        for version in ("ridge-event-v1", None):
            model_id = self.calibrator(
                forecast["extractor_key"], identity=f"calibrator-{version}", version=version
            )
            with (
                self.subTest(version=version),
                patch.object(
                    engine, "extract_features", side_effect=AssertionError("Must not extract")
                ) as extract,
                self.assertRaisesRegex(engine.ObservationRejected, "re-fit"),
            ):
                self.observe("fresh", index=21, calibrator_id=model_id)
            extract.assert_not_called()

    def test_failed_paid_request_leaves_an_attempt_record(self):
        self.populate()
        self.event()
        paid = {"provider": "jev", "model": "jev-1.13.0"}
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("jevtrader.providers.post_json") as post,
            self.assertRaises(MissingCredentials),
        ):
            self.observe(**paid)
        post.assert_not_called()
        self.assertEqual(self.ledger.all("attempts"), [])
        with (
            patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-secret"}),
            patch("jevtrader.providers.post_json", side_effect=ProviderError("HTTP error 500")),
            self.assertRaises(ProviderError),
        ):
            self.observe(**paid)
        [attempt] = self.ledger.all("attempts")
        self.assertEqual(
            {key: attempt[key] for key in ("event_id", "provider", "requested_model", "error")},
            {
                "event_id": "current",
                "provider": "jev",
                "requested_model": "jev-1.13.0",
                "error": "HTTP error 500",
            },
        )
        self.assertEqual(attempt["questions_digest"], digest(self.strategy["questions"]))
        self.assertEqual(self.ledger.counts().get("extractions", 0), 0)

    def test_billed_malformed_or_incomplete_responses_leave_attempt_records(self):
        self.populate()
        malformed = {"model": "jev-1.13.0", "answers": {}, "usage": {"input_tokens": 1}}
        cases = [
            ("jev", "jev-1.13.0", malformed, ProviderValidationError),
            ("openai", "gpt-test", {"status": "incomplete"}, ProviderError),
        ]
        for index, (provider, model, response, error) in enumerate(cases):
            with (
                self.subTest(provider),
                patch.dict("os.environ", {"TYPESAFE_API_KEY": "test", "OPENAI_API_KEY": "test"}),
                patch("jevtrader.providers.post_json", return_value=response),
            ):
                self.event(provider, index=20 + index)
                with self.assertRaises(error):
                    self.observe(provider, index=20 + index, provider=provider, model=model)
                attempts = [a for a in self.ledger.all("attempts") if a["event_id"] == provider]
                self.assertEqual(len(attempts), 1)

    def test_unscorable_event_is_rejected_before_the_disclosure_scan(self):
        self.event()  # No market data.
        with (
            patch.object(self.ledger, "all", wraps=self.ledger.all) as scan,
            self.assertRaises(engine.ObservationRejected),
        ):
            self.observe()
        self.assertNotIn("disclosures", [call.args[0] for call in scan.call_args_list])

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

    def test_replaying_a_stored_strategy_uses_the_rules_it_was_recorded_under(self):
        self.populate()
        self.event()
        padded = copy.deepcopy(self.strategy)
        padded["questions"]["novelty"] = "   New info?     "  # Accepted by providers too.
        result = engine.observe(self.ledger, "current", padded, as_of=self.at(20))
        self.assertEqual(result["strategy"], padded)

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


BP = 0.0001
# Default costs (default_strategy.json): 10 bps spread + 2 x 5 bps slippage + 20 bps edge.
LONG_THRESHOLD = 0.0040
# Shorts add 300 bps a year of borrow over the 10-session horizon: 40 + 3000/252 = 51.9 bps.
SHORT_THRESHOLD = (40 + 300 * 10 / 252) / 10_000
BELOW_THRESHOLD = "predicted excess return does not clear cost and edge threshold"


class DecisionThresholdTests(unittest.TestCase):
    """Pins the LONG/SHORT rule (engine.observe) for the unmodified default strategy (P0-20)."""

    # Reuse the fixture helpers without inheriting (and re-running) every EngineTests test.
    at = EngineTests.at
    populate = EngineTests.populate
    event = EngineTests.event
    observe = EngineTests.observe
    calibrator = EngineTests.calibrator

    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)
        self.strategy = load_strategy()  # Default horizon (10 sessions) and costs.
        self.days = sessions()

    def decide(self, *expected_returns):
        """Forecasts for one event scored by constant calibrators predicting each value."""
        self.populate()
        self.event("previous", index=17, text="Business was unchanged.")
        self.event()
        key = self.observe()["extractor_key"]
        results = []
        for index, value in enumerate(expected_returns):
            model_id = self.calibrator(key, identity=f"constant-{index}", intercept=value)
            forecast = self.observe(calibrator_id=model_id)
            self.assertEqual(forecast["expected_return"], value)
            results.append(forecast)
        return results

    def test_thresholds_are_pinned_to_the_default_costs(self):
        self.assertEqual(self.strategy["horizon_sessions"], 10)
        self.assertFalse(self.strategy["allow_short"])
        long_bps = round_trip_bps(self.strategy) + self.strategy["min_edge_bps"]
        short_bps = round_trip_bps(self.strategy, short=True) + self.strategy["min_edge_bps"]
        self.assertEqual(long_bps / 10_000, LONG_THRESHOLD)
        self.assertAlmostEqual(short_bps / 10_000, SHORT_THRESHOLD, places=15)
        self.assertAlmostEqual(SHORT_THRESHOLD * 10_000, 51.9, places=1)

    def test_fixture_event_clears_every_other_gate(self):
        (forecast,) = self.decide(0.0)
        self.assertEqual(forecast["action"], "PASS")
        self.assertEqual(forecast["reasons"], [BELOW_THRESHOLD])

    def test_long_needs_more_than_40_bps(self):
        above, at, below = self.decide(LONG_THRESHOLD + BP, LONG_THRESHOLD, LONG_THRESHOLD - BP)
        self.assertEqual(above["action"], "LONG")
        self.assertEqual(above["reasons"], [])
        for forecast in (at, below):
            self.assertEqual(forecast["action"], "PASS")
            self.assertEqual(forecast["reasons"], [BELOW_THRESHOLD])

    def test_short_needs_more_than_51_9_bps_below_zero(self):
        self.strategy["allow_short"] = True
        above, below = self.decide(-(SHORT_THRESHOLD + BP), -(SHORT_THRESHOLD - BP))
        self.assertEqual(above["action"], "SHORT")
        self.assertEqual(above["reasons"], [])
        self.assertEqual(below["action"], "PASS")
        self.assertEqual(below["reasons"], [BELOW_THRESHOLD])

    def test_short_threshold_is_not_the_long_threshold(self):
        # Between -51.9 and -40 bps a short does not clear its borrow-inclusive threshold.
        self.strategy["allow_short"] = True
        (forecast,) = self.decide(-(LONG_THRESHOLD + BP))
        self.assertEqual(forecast["action"], "PASS")
        self.assertEqual(forecast["reasons"], [BELOW_THRESHOLD])

    def test_short_is_refused_when_the_strategy_disallows_it(self):
        (forecast,) = self.decide(-(SHORT_THRESHOLD + 10 * BP))
        self.assertEqual(forecast["action"], "PASS")
        self.assertEqual(forecast["reasons"], [BELOW_THRESHOLD])


if __name__ == "__main__":
    unittest.main()
