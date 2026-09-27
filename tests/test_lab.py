"""Protocol tests use synthetic ledger records and mocked extraction/evaluation.

No provider calls, market feeds, or real trades are needed to test research
budgeting and development-universe boundaries.
"""

import copy
import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from jevtrader.common import digest, load_strategy
from jevtrader.lab import (
    MAX_TRIALS,
    ProposalRejected,
    autoresearch,
    development_feedback,
    experiment,
    generate_proposal,
)
from jevtrader.research import VERSION
from jevtrader.store import Ledger


CUTOFF = "2024-05-01T00:00:00Z"


class LabTests(unittest.TestCase):
    def setUp(self):
        # Paid calls are mocked, but experiments check credentials before reserving budget.
        env = patch.dict(
            "os.environ", {"OPENAI_API_KEY": "test-secret", "TYPESAFE_API_KEY": "test-secret"}
        )
        env.start()
        self.addCleanup(env.stop)
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)
        self.baseline = load_strategy()
        self.baseline["min_train_samples"] = 10
        self.candidate = copy.deepcopy(self.baseline)
        self.candidate["name"] = "candidate-one"
        self.candidate["questions"]["novelty"] = (
            "Identify substantive new demand evidence, using the prior disclosure as a comparison."
        )
        self.ids = [self.seed(index) for index in range(25)]
        self.report = {
            "evaluated_count": 10,
            "predictions": [{"event_id": event_id} for event_id in self.ids[-10:]],
            "blocks": [{"training_event_ids": self.ids[:15], "test_event_ids": self.ids[-10:]}],
            "strategies": {"semantic": {"mean_net_return_per_opportunity": 0.0005}},
        }

    def seed(
        self,
        index,
        *,
        strategy=None,
        decision_at=None,
        label_at=None,
        outcome_at=None,
        event_id=None,
        mode=None,
    ):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        event_id = event_id or f"event-{index:03d}"
        identity = f"forecast-{event_id}"
        decision = decision_at or (start + timedelta(days=index)).isoformat()
        label = label_at or (start + timedelta(days=index + 1)).isoformat()
        self.ledger.put(
            "forecasts",
            identity,
            {
                "id": identity,
                "event_id": event_id,
                "decision_at": decision,
                "strategy": strategy or self.baseline,
                "provider": "openai",
                "resolved_model": "frozen-test-model",
                "mode": mode or "synthetic",
                "extractor_key": "baseline-extractor",
            },
        )
        self.ledger.put(
            "outcomes",
            identity,
            {
                "id": identity,
                "label_available_at": label,
                "outcome_at": outcome_at or label,
                "target": 0.0,
            },
        )
        return event_id

    def mocked_run(self, candidate=None, baseline=None, cutoff=CUTOFF, report=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        observe = stack.enter_context(
            patch("jevtrader.lab.observe", return_value={"extractor_key": "trial-extractor"})
        )
        settle = stack.enter_context(patch("jevtrader.lab.settle", return_value={"added": 0}))
        evaluate = stack.enter_context(
            patch(
                "jevtrader.lab.evaluate",
                return_value=copy.deepcopy(self.report if report is None else report),
            )
        )
        result = experiment(
            self.ledger,
            candidate or self.candidate,
            baseline or self.baseline,
            provider="openai",
            model="frozen-test-model",
            development_until=cutoff,
        )
        return result, observe, settle, evaluate

    def test_question_only_edit_runs_on_synthetic_records_without_api(self):
        result, observe, settle, evaluate = self.mocked_run()
        self.assertEqual(observe.call_count, 25)
        self.assertFalse(result["promoted"])
        self.assertIn("development", result["score_basis"])
        self.assertIn("not an unbiased", result["warning"])
        self.assertEqual(result["score"], 0.0005)
        settle.assert_called_once()
        evaluate.assert_called_once()
        self.assertEqual({call.args[1] for call in observe.call_args_list}, set(self.ids))
        self.assertTrue(all(call.kwargs["as_of"] for call in observe.call_args_list))

    def test_rule_changes_and_rules_provider_are_rejected_before_calls(self):
        candidate = copy.deepcopy(self.candidate)
        candidate["horizon_sessions"] += 1
        with patch("jevtrader.lab.observe") as observe:
            with self.assertRaisesRegex(ValueError, "only name/questions"):
                experiment(
                    self.ledger,
                    candidate,
                    self.baseline,
                    provider="openai",
                    model="frozen-test-model",
                    development_until=CUTOFF,
                )
            with self.assertRaisesRegex(ValueError, "Rules ignore questions"):
                experiment(
                    self.ledger,
                    self.candidate,
                    self.baseline,
                    provider="rules",
                    model="rules-v1",
                    development_until=CUTOFF,
                )
            observe.assert_not_called()
        self.assertIsNone(self.ledger.get("experiments", "protocol-v1"))

    def test_protocol_cutoff_baseline_and_budget_are_immutable(self):
        self.mocked_run()
        for change in ("cutoff", "baseline", "model", "provider", "budget"):
            with self.subTest(change=change), patch("jevtrader.lab.observe") as observe:
                baseline = copy.deepcopy(self.baseline)
                if change == "baseline":
                    baseline["name"] = "changed-baseline"
                budget = MAX_TRIALS + 1 if change == "budget" else MAX_TRIALS
                with (
                    patch("jevtrader.lab.MAX_TRIALS", budget),
                    self.assertRaisesRegex(ValueError, "already locked"),
                ):
                    experiment(
                        self.ledger,
                        self.candidate,
                        baseline,
                        provider="jev" if change == "provider" else "openai",
                        model="different-model" if change == "model" else "frozen-test-model",
                        development_until="2024-06-01T00:00:00Z" if change == "cutoff" else CUTOFF,
                    )
                observe.assert_not_called()

    def test_successful_repeat_is_idempotent_and_sixth_candidate_rejected(self):
        first, _, _, _ = self.mocked_run()
        with patch("jevtrader.lab.observe") as observe, patch("jevtrader.lab.evaluate") as evaluate:
            same = experiment(
                self.ledger,
                self.candidate,
                self.baseline,
                provider="openai",
                model="frozen-test-model",
                development_until=CUTOFF,
            )
            self.assertEqual(first, same)
            observe.assert_not_called()
            evaluate.assert_not_called()
        for index in range(1, MAX_TRIALS):
            candidate = copy.deepcopy(self.candidate)
            candidate["name"] = f"candidate-{index + 1}"
            self.mocked_run(candidate)
        candidate = copy.deepcopy(self.candidate)
        candidate["name"] = "one-too-many"
        with (
            patch("jevtrader.lab.observe") as observe,
            self.assertRaisesRegex(ValueError, "budget exhausted"),
        ):
            experiment(
                self.ledger,
                candidate,
                self.baseline,
                provider="openai",
                model="frozen-test-model",
                development_until=CUTOFF,
            )
        observe.assert_not_called()

    def test_failed_candidates_consume_trial_slots(self):
        for index in range(MAX_TRIALS):
            candidate = copy.deepcopy(self.candidate)
            candidate["name"] = f"failed-{index}"
            with patch(
                "jevtrader.lab.observe", side_effect=RuntimeError("synthetic provider failure")
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic provider failure"):
                    experiment(
                        self.ledger,
                        candidate,
                        self.baseline,
                        provider="openai",
                        model="frozen-test-model",
                        development_until=CUTOFF,
                    )
        with (
            patch("jevtrader.lab.observe") as observe,
            self.assertRaisesRegex(ValueError, "budget exhausted"),
        ):
            experiment(
                self.ledger,
                self.candidate,
                self.baseline,
                provider="openai",
                model="frozen-test-model",
                development_until=CUTOFF,
            )
        observe.assert_not_called()

    def test_cutoff_labels_and_later_events_are_never_sent_to_provider(self):
        excluded = {
            self.seed(
                100, event_id="future-label", decision_at="2024-02-01T00:00:00Z", label_at=CUTOFF
            ),
            self.seed(
                101,
                event_id="held-out",
                decision_at="2024-05-02T00:00:00Z",
                label_at="2024-05-03T00:00:00Z",
            ),
        }
        _, observe, _, _ = self.mocked_run()
        observed = {call.args[1] for call in observe.call_args_list}
        self.assertFalse(observed & excluded)
        self.assertEqual(len(observed), 25)

    def test_event_universe_frozen_after_protocol_creation(self):
        self.mocked_run()
        late_event = self.seed(50, event_id="late-import")
        candidate = copy.deepcopy(self.candidate)
        candidate["name"] = "second-candidate"
        _, observe, _, _ = self.mocked_run(candidate)
        self.assertNotIn(late_event, {call.args[1] for call in observe.call_args_list})

    def test_outside_test_events_and_too_small_reports_are_rejected(self):
        report = copy.deepcopy(self.report)
        report["predictions"][0]["event_id"] = "outside-universe"
        with self.assertRaisesRegex(ValueError, "outside"):
            self.mocked_run(report=report)
        report = copy.deepcopy(self.report)
        report["evaluated_count"] = 9
        candidate = copy.deepcopy(self.candidate)
        candidate["name"] = "separate-small-evaluation"
        with self.assertRaisesRegex(ValueError, "Too few"):
            self.mocked_run(candidate=candidate, report=report)

    def test_mixed_resolved_extractors_rejected_without_scoring(self):
        records = [{"extractor_key": "one"}] * 24 + [{"extractor_key": "two"}]
        with (
            patch("jevtrader.lab.observe", side_effect=records),
            patch("jevtrader.lab.evaluate") as evaluate,
        ):
            with self.assertRaisesRegex(ValueError, "different models"):
                experiment(
                    self.ledger,
                    self.candidate,
                    self.baseline,
                    provider="openai",
                    model="frozen-test-model",
                    development_until=CUTOFF,
                )
            evaluate.assert_not_called()

    def test_protocol_uses_baseline_observations_only(self):
        unrelated = copy.deepcopy(self.baseline)
        unrelated["name"] = "other-strategy"
        unrelated["questions"]["direction"] = (
            "A different research question that must not choose the development universe."
        )
        extra = self.seed(40, event_id="unrelated-event", strategy=unrelated)
        _, observe, _, _ = self.mocked_run()
        self.assertNotIn(extra, {call.args[1] for call in observe.call_args_list})
        self.assertEqual(observe.call_count, len(self.ids))

    def test_extra_universe_training_events_are_rejected(self):
        report = copy.deepcopy(self.report)
        report["blocks"][0]["training_event_ids"].append("extra-universe-training-event")
        with self.assertRaisesRegex(ValueError, "outside|universe|training"):
            self.mocked_run(report=report)

    def test_failed_trial_cannot_retry_paid_calls(self):
        with patch("jevtrader.lab.observe", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                experiment(
                    self.ledger,
                    self.candidate,
                    self.baseline,
                    provider="openai",
                    model="frozen-test-model",
                    development_until=CUTOFF,
                )
        with (
            patch("jevtrader.lab.observe") as observe,
            self.assertRaisesRegex(ValueError, "no automatic retry"),
        ):
            experiment(
                self.ledger,
                self.candidate,
                self.baseline,
                provider="openai",
                model="frozen-test-model",
                development_until=CUTOFF,
            )
        observe.assert_not_called()
        starts = [
            row for row in self.ledger.all("experiments") if row.get("type") == "trial_started"
        ]
        self.assertEqual(len(starts), 1)

    def test_resolved_model_changes_between_trials_rejected(self):
        self.mocked_run()
        candidate = copy.deepcopy(self.candidate)
        candidate["name"] = "different-resolved-model"
        response = {"extractor_key": "new-extractor", "resolved_model": "changed-provider-version"}
        with (
            patch("jevtrader.lab.observe", return_value=response),
            patch("jevtrader.lab.evaluate") as evaluate,
        ):
            with self.assertRaisesRegex(ValueError, "resolved-model|[Mm]odel|conflict"):
                experiment(
                    self.ledger,
                    candidate,
                    self.baseline,
                    provider="openai",
                    model="frozen-test-model",
                    development_until=CUTOFF,
                )
            evaluate.assert_not_called()
        completed = [
            row for row in self.ledger.all("experiments") if row.get("type") == "trial_completed"
        ]
        self.assertEqual(len(completed), 1)

    def run_openai_trial(self, candidate):
        return experiment(
            self.ledger,
            candidate,
            self.baseline,
            provider="openai",
            model="frozen-test-model",
            development_until=CUTOFF,
        )

    def test_missing_credentials_fail_before_any_budget_reservation(self):
        trial, _, _, _ = self.mocked_run(candidate=self.baseline)
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("jevtrader.lab.observe") as observe,
            patch("jevtrader.lab.propose_strategy") as propose,
        ):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                self.run_openai_trial(self.candidate)
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                generate_proposal(self.ledger, trial, "proposal-model")
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                autoresearch(
                    self.ledger,
                    self.baseline,
                    provider="openai",
                    model="frozen-test-model",
                    proposal_model="proposal-model",
                    development_until=CUTOFF,
                    rounds=2,
                )
        observe.assert_not_called()
        propose.assert_not_called()
        kinds = [row.get("type") for row in self.ledger.all("experiments")]
        self.assertEqual(kinds.count("trial_slot"), 1)
        self.assertEqual(kinds.count("trial_started"), 1)
        self.assertEqual(kinds.count("proposal_started"), 0)

    def test_missing_keys_cannot_lock_a_protocol(self):
        for provider, variable in (("openai", "OPENAI_API_KEY"), ("jev", "TYPESAFE_API_KEY")):
            with (
                self.subTest(provider),
                patch.dict("os.environ", {}, clear=True),
                patch("jevtrader.lab.observe") as observe,
                self.assertRaisesRegex(RuntimeError, variable),
            ):
                experiment(
                    self.ledger,
                    self.candidate,
                    self.baseline,
                    provider=provider,
                    model="frozen-test-model",
                    development_until=CUTOFF,
                )
            observe.assert_not_called()
        self.assertEqual(self.ledger.all("experiments"), [])

    def test_autoresearch_checks_proposal_key_before_the_baseline_trial_spends(self):
        with (
            patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-secret"}, clear=True),
            patch("jevtrader.lab.observe") as observe,
            self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"),
        ):
            autoresearch(
                self.ledger,
                self.baseline,
                provider="jev",
                model="jev-1.13.0",
                proposal_model="proposal-model",
                development_until=CUTOFF,
                rounds=2,
            )
        observe.assert_not_called()
        self.assertEqual(self.ledger.all("experiments"), [])

    def test_autoresearch_checks_extraction_key_before_paying_for_a_proposal(self):
        with (
            patch("jevtrader.lab.observe", return_value={"extractor_key": "baseline"}),
            patch("jevtrader.lab.settle", return_value={}),
            patch("jevtrader.lab.evaluate", return_value=copy.deepcopy(self.report)),
        ):
            experiment(
                self.ledger,
                self.baseline,
                self.baseline,
                provider="jev",
                model="jev-1.13.0",
                development_until=CUTOFF,
            )
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret"}, clear=True),
            patch("jevtrader.lab.propose_strategy") as propose,
            patch("jevtrader.lab.observe") as observe,
            self.assertRaisesRegex(RuntimeError, "TYPESAFE_API_KEY"),
        ):
            autoresearch(
                self.ledger,
                self.baseline,
                provider="jev",
                model="jev-1.13.0",
                proposal_model="proposal-model",
                development_until=CUTOFF,
                rounds=2,
            )
        propose.assert_not_called()
        observe.assert_not_called()
        types = [row.get("type") for row in self.ledger.all("experiments")]
        self.assertNotIn("proposal_started", types)

    def test_completed_trials_are_returned_without_keys(self):
        trial, _, _, _ = self.mocked_run(candidate=self.baseline)
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("jevtrader.lab.observe") as observe,
        ):
            result = autoresearch(
                self.ledger,
                self.baseline,
                provider="openai",
                model="frozen-test-model",
                proposal_model="unused",
                development_until=CUTOFF,
                rounds=1,
            )
        self.assertEqual(result["best_trial_id"], trial["id"])
        observe.assert_not_called()

    def test_trials_from_another_evaluator_version_are_not_reused(self):
        first, _, _, _ = self.mocked_run()
        protocol = self.ledger.get("experiments", "protocol-v1")
        self.assertEqual(
            first["id"],
            digest({"protocol": protocol, "candidate": self.candidate, "evaluator": VERSION}),
        )
        with patch("jevtrader.lab.VERSION", "ridge-event-v3"):
            rescored, observe, _, _ = self.mocked_run()
        self.assertNotEqual(rescored["id"], first["id"])
        self.assertEqual(observe.call_count, 25)

    def legacy_trial(self, candidate, *, version=None):
        """Records as written before trial ids named the evaluator (HEAD scheme)."""
        protocol = self.ledger.get("experiments", "protocol-v1")
        identity = digest({"protocol": protocol, "candidate": candidate})
        start = {
            "id": f"start:{identity}",
            "type": "trial_started",
            "candidate": candidate,
            "created_at": CUTOFF,
            "max_provider_calls": 25,
        }
        self.ledger.put("experiments", start["id"], start)
        if version is None:
            return None
        completed = {
            "id": identity,
            "type": "trial_completed",
            "candidate": candidate,
            "score": 0.0005,
            "report": {**copy.deepcopy(self.report), "version": version},
        }
        self.ledger.put("experiments", identity, completed)
        return completed

    def test_completed_trials_from_before_evaluator_ids_are_reused_if_current(self):
        self.mocked_run(candidate=self.baseline)
        legacy = self.legacy_trial(self.candidate, version=VERSION)
        with patch.dict("os.environ", {}, clear=True), patch("jevtrader.lab.observe") as observe:
            self.assertEqual(self.run_openai_trial(self.candidate), legacy)
        observe.assert_not_called()
        stale = copy.deepcopy(self.candidate)
        stale["name"] = "scored-by-an-earlier-evaluator"
        self.legacy_trial(stale, version="ridge-event-v1")
        rescored, observe, _, _ = self.mocked_run(candidate=stale)
        self.assertEqual(observe.call_count, 25)
        self.assertEqual(rescored["type"], "trial_completed")

    def test_started_trials_are_never_rerun_under_another_evaluator_or_id(self):
        with (
            patch("jevtrader.lab.VERSION", "ridge-event-v1"),
            patch("jevtrader.lab.observe", side_effect=RuntimeError("synthetic failure")),
            self.assertRaises(RuntimeError),
        ):
            self.run_openai_trial(self.candidate)
        legacy = copy.deepcopy(self.candidate)
        legacy["name"] = "started-before-evaluator-ids"
        self.legacy_trial(legacy)
        for candidate in (self.candidate, legacy):
            with (
                self.subTest(candidate["name"]),
                patch("jevtrader.lab.observe") as observe,
                self.assertRaisesRegex(ValueError, "no automatic retry"),
            ):
                self.run_openai_trial(candidate)
            observe.assert_not_called()
        slots = [r for r in self.ledger.all("experiments") if r.get("type") == "trial_slot"]
        self.assertEqual(len(slots), 1)

    def fresh_ledger(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)

    def test_universe_that_cannot_survive_purging_is_rejected_before_calls(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # Outcomes are one day later, but labels only become available `lag` days later.
        for lag, message in ((40, "after purging"), (6, "only 9 evaluated")):
            self.fresh_ledger()
            for index in range(25):
                self.seed(
                    index,
                    outcome_at=(start + timedelta(days=index + 1)).isoformat(),
                    label_at=(start + timedelta(days=index + lag)).isoformat(),
                )
            with (
                self.subTest(lag=lag),
                patch("jevtrader.lab.observe") as observe,
                self.assertRaisesRegex(ValueError, message),
            ):
                self.run_openai_trial(self.candidate)
            observe.assert_not_called()
            self.assertEqual(self.ledger.all("experiments"), [])
        self.fresh_ledger()
        for index in range(25):
            self.seed(
                index,
                outcome_at=(start + timedelta(days=index + 1)).isoformat(),
                label_at=(start + timedelta(days=index + 5)).isoformat(),
            )
        _, observe, _, _ = self.mocked_run()  # Exactly ten evaluable events is enough.
        self.assertEqual(observe.call_count, 25)

    def test_universe_mixing_forward_and_historical_observations_is_accepted(self):
        self.fresh_ledger()
        for index in range(25):
            self.seed(index, mode="forward" if index % 2 else "historical")
        _, observe, _, _ = self.mocked_run()
        self.assertEqual(observe.call_count, 25)

    def test_universe_mixing_real_and_synthetic_is_rejected_before_calls(self):
        for index in range(25, 30):
            self.seed(index, mode="historical")
        with (
            patch("jevtrader.lab.observe") as observe,
            self.assertRaisesRegex(ValueError, "mixes real and synthetic"),
        ):
            self.run_openai_trial(self.candidate)
        observe.assert_not_called()
        self.assertEqual(self.ledger.all("experiments"), [])

    def test_resolved_model_change_stops_after_first_paid_call(self):
        self.mocked_run()
        candidate = copy.deepcopy(self.candidate)
        candidate["name"] = "drifted-resolved-model"
        response = {"extractor_key": "new-extractor", "resolved_model": "changed-provider-version"}
        with (
            patch("jevtrader.lab.observe", return_value=response) as observe,
            patch("jevtrader.lab.evaluate") as evaluate,
            self.assertRaisesRegex(ValueError, "[Rr]esolved"),
        ):
            self.run_openai_trial(candidate)
        self.assertEqual(observe.call_count, 1)
        evaluate.assert_not_called()

    def test_candidates_providers_would_reject_fail_before_slot_reservation(self):
        long_name = copy.deepcopy(self.candidate)
        long_name["name"] = "x" * 201
        blank_question = copy.deepcopy(self.candidate)
        blank_question["questions"]["novelty"] = " " * 12
        for label, candidate in (("long-name", long_name), ("blank-question", blank_question)):
            with (
                self.subTest(label),
                patch("jevtrader.lab.observe") as observe,
                self.assertRaises(ValueError),
            ):
                self.run_openai_trial(candidate)
            observe.assert_not_called()
        slots = [r for r in self.ledger.all("experiments") if r.get("type") == "trial_slot"]
        self.assertEqual(slots, [])

    def test_autoresearch_end_to_end_bounded_rounds_development_feedback_only(self):
        losing = copy.deepcopy(self.baseline)
        losing["name"] = "losing-proposal"
        losing["questions"]["novelty"] = (
            "Test the first document novelty hypothesis using supplied evidence only."
        )
        winning = copy.deepcopy(self.baseline)
        winning["name"] = "winning-proposal"
        winning["questions"]["novelty"] = (
            "Test another document novelty hypothesis using supplied evidence only."
        )
        reports = []
        for score in (0.0005, -0.0002, 0.0008):
            report = copy.deepcopy(self.report)
            report["strategies"]["semantic"]["mean_net_return_per_opportunity"] = score
            report["heldout_secret"] = "THIS_MUST_NEVER_REACH_PROPOSAL_FEEDBACK"
            reports.append(report)
        with (
            patch(
                "jevtrader.lab.observe",
                return_value={
                    "extractor_key": "same-extractor",
                    "resolved_model": "frozen-test-model",
                },
            ) as observe,
            patch("jevtrader.lab.settle", return_value={}),
            patch("jevtrader.lab.evaluate", side_effect=reports) as evaluate,
            patch("jevtrader.lab.propose_strategy", side_effect=[losing, winning]) as propose,
        ):
            result = autoresearch(
                self.ledger,
                self.baseline,
                provider="openai",
                model="frozen-test-model",
                proposal_model="proposal-model",
                development_until=CUTOFF,
                rounds=3,
            )
        self.assertEqual(len(result["trials"]), 3)
        self.assertEqual(propose.call_count, 2)
        self.assertEqual(evaluate.call_count, 3)
        self.assertEqual(observe.call_count, 75)
        self.assertFalse(result["promoted"])
        self.assertEqual(result["candidate"], winning)
        self.assertEqual(result["development_score"], 0.0008)
        self.assertEqual(self.ledger.counts().get("models", 0), 0)
        for call in propose.call_args_list:
            # The losing candidate never becomes the parent of a later proposal.
            self.assertEqual(call.args[0], self.baseline)
            feedback = call.args[1]
            self.assertEqual(
                set(feedback),
                {"development_until", "score", "metrics", "sample_count", "instruction"},
            )
            self.assertNotIn("THIS_MUST_NEVER", str(feedback))
            self.assertNotIn("predictions", feedback)
            self.assertIn("adaptive development", feedback["instruction"])
        for call in evaluate.call_args_list:
            self.assertEqual(call.kwargs["event_ids"], set(self.ids))
        self.assertEqual(
            len([r for r in self.ledger.all("experiments") if r.get("type") == "trial_started"]), 3
        )

    def test_autoresearch_invalid_rounds_and_failed_feedback_rejected(self):
        for rounds in (0, MAX_TRIALS + 1, True, 1.5):
            with (
                patch("jevtrader.lab.propose_strategy") as propose,
                self.assertRaisesRegex(ValueError, "rounds"),
            ):
                autoresearch(
                    self.ledger,
                    self.baseline,
                    provider="openai",
                    model="frozen-test-model",
                    proposal_model="proposal-model",
                    development_until=CUTOFF,
                    rounds=rounds,
                )
            propose.assert_not_called()
        with self.assertRaisesRegex(ValueError, "completed development"):
            development_feedback({"type": "trial_started"})

    def test_autoresearch_exhausted_budget_does_not_call_proposer(self):
        self.mocked_run(candidate=self.baseline)
        for index in range(MAX_TRIALS - 1):
            candidate = copy.deepcopy(self.candidate)
            candidate["name"] = f"budget-fill-{index}"
            self.mocked_run(candidate=candidate)
        with (
            patch("jevtrader.lab.propose_strategy") as propose,
            patch("jevtrader.lab.observe") as observe,
        ):
            result = autoresearch(
                self.ledger,
                self.baseline,
                provider="openai",
                model="frozen-test-model",
                proposal_model="proposal-model",
                development_until=CUTOFF,
                rounds=MAX_TRIALS,
            )
        self.assertEqual(len(result["trials"]), 1)
        self.assertFalse(result["promoted"])
        propose.assert_not_called()
        observe.assert_not_called()

    def test_duplicate_proposals_consume_persistent_proposal_budget(self):
        trial, _, _, _ = self.mocked_run(candidate=self.baseline)
        with patch(
            "jevtrader.lab.propose_strategy", return_value=copy.deepcopy(self.baseline)
        ) as propose:
            proposals = [
                generate_proposal(self.ledger, trial, "proposal-model")
                for _ in range(MAX_TRIALS - 1)
            ]
            with self.assertRaisesRegex(ValueError, "Proposal budget exhausted"):
                generate_proposal(self.ledger, trial, "proposal-model")
        self.assertEqual(propose.call_count, MAX_TRIALS - 1)
        self.assertEqual(len({item["reservation_id"] for item in proposals}), MAX_TRIALS - 1)
        records = self.ledger.all("experiments")
        self.assertEqual(
            sum(row.get("type") == "proposal_started" for row in records), MAX_TRIALS - 1
        )
        self.assertEqual(sum(row.get("type") == "trial_started" for row in records), 1)
        # A separate autoresearch invocation uses the same exhausted budget,
        # even though duplicate proposals did not consume extra trial slots.
        with (
            patch("jevtrader.lab.propose_strategy") as propose,
            self.assertRaisesRegex(ValueError, "Proposal budget exhausted"),
        ):
            autoresearch(
                self.ledger,
                self.baseline,
                provider="openai",
                model="frozen-test-model",
                proposal_model="proposal-model",
                development_until=CUTOFF,
                rounds=2,
            )
        propose.assert_not_called()

    def test_unusable_paid_proposal_is_recorded_as_rejected(self):
        trial, _, _, _ = self.mocked_run(candidate=self.baseline)
        short = copy.deepcopy(self.baseline)
        short["questions"]["novelty"] = "Short?"  # Providers accept it; strategies do not.
        with (
            patch("jevtrader.lab.propose_strategy", return_value=short),
            self.assertRaisesRegex(ProposalRejected, "rejected.*proposal-slot:0"),
        ):
            generate_proposal(self.ledger, trial, "proposal-model")
        types = [row.get("type") for row in self.ledger.all("experiments")]
        self.assertEqual(types.count("proposal_started"), 1)
        self.assertNotIn("proposal", types)
        [row] = [r for r in self.ledger.all("experiments") if r.get("type") == "proposal_rejected"]
        self.assertEqual(row["candidate"], short)
        self.assertEqual(row["reservation_id"], "proposal-slot:0")
        self.assertIn("Questions", row["error"])

    def test_blank_paid_proposal_is_recorded_as_rejected(self):
        trial, _, _, _ = self.mocked_run(candidate=self.baseline)
        proposed = {"name": "blank", "questions": {**self.baseline["questions"], "novelty": ""}}
        response = {
            "model": "proposal-model-resolved",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": json.dumps(proposed)}],
                }
            ],
            "usage": {"input_tokens": 10},
        }
        with (
            patch("jevtrader.providers._post_json", return_value=response) as post,
            self.assertRaisesRegex(ProposalRejected, "novelty.*proposal-slot:0"),
        ):
            generate_proposal(self.ledger, trial, "proposal-model")
        post.assert_called_once()
        [row] = [r for r in self.ledger.all("experiments") if r.get("type") == "proposal_rejected"]
        self.assertIsNone(row["candidate"])
        self.assertEqual(row["metadata"]["raw"], response)
        self.assertEqual(row["reservation_id"], "proposal-slot:0")

    def test_autoresearch_reports_a_rejected_proposal_and_continues(self):
        short = copy.deepcopy(self.baseline)
        short["questions"]["novelty"] = "Short?"
        with (
            patch(
                "jevtrader.lab.observe",
                return_value={"extractor_key": "same", "resolved_model": "frozen-test-model"},
            ) as observe,
            patch("jevtrader.lab.settle", return_value={}),
            patch("jevtrader.lab.evaluate", return_value=copy.deepcopy(self.report)),
            patch("jevtrader.lab.propose_strategy", side_effect=[short, self.candidate]),
        ):
            result = autoresearch(
                self.ledger,
                self.baseline,
                provider="openai",
                model="frozen-test-model",
                proposal_model="proposal-model",
                development_until=CUTOFF,
                rounds=3,
            )
        self.assertEqual(len(result["trials"]), 2)
        [message] = result["rejected_proposals"]
        self.assertIn("proposal-slot:0", message)
        self.assertEqual(observe.call_count, 50)

    def test_failed_proposal_reserves_slot_before_call_and_cannot_evade_cap(self):
        trial, _, _, _ = self.mocked_run(candidate=self.baseline)
        calls = []

        def failure(*args, **kwargs):
            starts = [
                row
                for row in self.ledger.all("experiments")
                if row.get("type") == "proposal_started"
            ]
            self.assertEqual(len(starts), len(calls) + 1)
            calls.append(starts[-1]["id"])
            raise RuntimeError("synthetic proposal failure")

        with patch("jevtrader.lab.propose_strategy", side_effect=failure) as propose:
            for _ in range(MAX_TRIALS - 1):
                with self.assertRaisesRegex(RuntimeError, "synthetic proposal failure"):
                    generate_proposal(self.ledger, trial, "proposal-model")
            with self.assertRaisesRegex(ValueError, "Proposal budget exhausted"):
                generate_proposal(self.ledger, trial, "proposal-model")
        self.assertEqual(propose.call_count, MAX_TRIALS - 1)
        self.assertEqual(
            sum(row.get("type") == "proposal" for row in self.ledger.all("experiments")), 0
        )


if __name__ == "__main__":
    unittest.main()
