"""Provider contract tests use only local responses, never live API calls."""

import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from jevtrader import providers


STRATEGY = {
    "version": 1,
    "name": "test-policy",
    "questions": {
        "direction": "Are business prospects improving or deteriorating?",
        "materiality": "Is there a material change to business prospects?",
        "novelty": "Does the current text add substantive new information relative to the previous text?",
    },
    "risk": {"max_gross": 0.5},
}


def jev_response():
    return {
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


def openai_response(features=None):
    if features is None:
        features = {"direction": -0.7, "materiality": 0.9, "novelty": 0.6, "uncertainty": 0.15}
    return {
        "model": "test-model-resolved",
        "status": "completed",
        "error": None,
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(features)}],
            },
        ],
        "usage": {"input_tokens": 234, "output_tokens": 40},
    }


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            "os.environ", {"TYPESAFE_API_KEY": "test-secret", "OPENAI_API_KEY": "test-secret"}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def extract(
        self,
        provider,
        raw=None,
        *,
        current="Raised guidance with strong demand.",
        previous="Old report.",
    ):
        transport = Mock(return_value=raw)
        result = providers.extract_features(
            provider, "requested-model", current, previous, STRATEGY, transport=transport
        )
        return result, transport

    def test_rules_never_calls_transport_and_is_labeled_heuristic(self):
        result, transport = self.extract("rules")
        transport.assert_not_called()
        self.assertEqual(result["resolved_model"], "rules-v1")
        self.assertEqual(result["input_tokens"], 0)
        self.assertEqual(result["direction"], 1.0)
        self.assertIn("heuristic", result["raw"]["method"])
        self.assertIn("ignores strategy questions", result["raw"]["method"])

    def test_rules_negative_mixed_and_unknown_evidence(self):
        negative, _ = self.extract("rules", current="Weak demand and lowered guidance.")
        mixed, _ = self.extract("rules", current="Strong demand but lowered guidance.")
        unknown, _ = self.extract("rules", current="The meeting will be on Tuesday.")
        self.assertEqual(negative["direction"], -1.0)
        self.assertEqual(mixed["direction"], 0.0)
        self.assertGreater(mixed["uncertainty"], negative["uncertainty"])
        self.assertEqual(unknown["materiality"], 0.0)

    def test_rules_identical_documents_have_no_novelty(self):
        result, _ = self.extract("rules", current="Raised guidance.", previous="Raised guidance.")
        self.assertEqual(result["novelty"], 0.0)

    def test_jev_uses_probability_difference_and_actual_model(self):
        raw = jev_response()
        result, transport = self.extract("jev", raw)
        self.assertAlmostEqual(result["direction"], 0.7)
        self.assertEqual(result["uncertainty"], 0.25)
        self.assertEqual(result["materiality"], 0.9)
        self.assertEqual(result["novelty"], 0.8)
        self.assertEqual(result["resolved_model"], "jev-1.13.0")
        self.assertEqual(result["input_tokens"], 122)
        self.assertEqual(result["raw"], raw)
        url, payload, key, timeout = transport.call_args.args
        self.assertEqual(url, providers.JEV_ENDPOINT)
        self.assertEqual(payload["model"], "requested-model")
        self.assertEqual(payload["questions"]["direction"]["type"], "choice")
        self.assertEqual(set(payload["questions"]["direction"]["criteria"]), providers._DIRECTIONS)
        self.assertEqual(key, "test-secret")
        self.assertLessEqual(timeout, 60)
        transport.assert_called_once()

    def test_jev_unclear_probability_raises_uncertainty(self):
        raw = jev_response()
        raw["answers"]["direction"].update(
            choice="unclear",
            confidence=0.9,
            probabilities={
                "improving": 0.1,
                "unchanged": 0.1,
                "deteriorating": 0.1,
                "unclear": 0.7,
            },
        )
        result, _ = self.extract("jev", raw)
        self.assertEqual(result["uncertainty"], 0.7)
        self.assertEqual(result["direction"], 0.0)

    def test_missing_previous_forces_conservative_novelty_for_all_providers(self):
        for provider, raw in (
            ("rules", None),
            ("jev", jev_response()),
            ("openai", openai_response()),
        ):
            with self.subTest(provider=provider):
                result, _ = self.extract(provider, raw, previous="  ")
                self.assertEqual(result["novelty"], 0.0)
                self.assertGreaterEqual(result["uncertainty"], 0.5)

    def test_bad_jev_answers_are_rejected(self):
        mutations = [
            lambda raw: raw["answers"].pop("novelty"),
            lambda raw: raw["answers"]["novelty"].update(type="choice"),
            lambda raw: raw["answers"]["direction"].update(choice="invented"),
            lambda raw: raw["answers"]["direction"].update(choice=[]),
            lambda raw: raw["answers"]["direction"].update(choice="deteriorating"),
            lambda raw: raw["answers"]["direction"]["probabilities"].pop("unclear"),
            lambda raw: raw["answers"]["direction"]["probabilities"].update(improving=0.99),
            lambda raw: raw["answers"]["direction"].pop("confidence"),
            lambda raw: raw["answers"]["materiality"].update(noul="0.9"),
        ]
        for mutate in mutations:
            raw = jev_response()
            mutate(raw)
            with self.subTest(raw=raw), self.assertRaises(providers.ProviderValidationError):
                self.extract("jev", raw)

    def test_nonfinite_boolean_out_of_range_and_missing_values_are_rejected(self):
        for value in (float("nan"), float("inf"), -0.01, 1.01, True, None, "0.5"):
            raw = jev_response()
            raw["answers"]["materiality"]["noul"] = value
            with self.subTest(value=value), self.assertRaises(providers.ProviderValidationError):
                self.extract("jev", raw)

    def test_actual_model_and_usage_are_required(self):
        for provider, base in (("jev", jev_response()), ("openai", openai_response())):
            mutations = [
                lambda raw: raw.pop("model"),
                lambda raw: raw.update(model=""),
                lambda raw: raw.pop("usage"),
                lambda raw: raw["usage"].update(input_tokens=-1),
                lambda raw: raw["usage"].update(input_tokens=True),
                lambda raw: raw["usage"].update(input_tokens=1.5),
            ]
            for mutate in mutations:
                raw = copy.deepcopy(base)
                mutate(raw)
                with (
                    self.subTest(provider=provider, raw=raw),
                    self.assertRaises(providers.ProviderValidationError),
                ):
                    self.extract(provider, raw)

    def test_openai_uses_responses_structured_output_without_tools(self):
        result, transport = self.extract("openai", openai_response())
        self.assertEqual(result["direction"], -0.7)
        self.assertEqual(result["resolved_model"], "test-model-resolved")
        self.assertEqual(result["input_tokens"], 234)
        url, payload, _, _ = transport.call_args.args
        self.assertEqual(url, providers.OPENAI_ENDPOINT)
        self.assertEqual(payload["model"], "requested-model")
        self.assertFalse(payload["store"])
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(
            set(payload["text"]["format"]["schema"]["required"]), providers._FEATURE_KEYS
        )
        self.assertEqual(json.loads(payload["input"])["questions"], STRATEGY["questions"])

    def test_openai_refusal_and_incomplete_are_rejected(self):
        refusal = openai_response()
        refusal["output"][1]["content"] = [{"type": "refusal", "refusal": "No."}]
        incomplete = openai_response()
        incomplete.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        errored = openai_response()
        errored["error"] = {"message": "sensitive diagnostic"}
        for raw in (refusal, incomplete, errored):
            with self.subTest(raw=raw), self.assertRaises(providers.ProviderError) as caught:
                self.extract("openai", raw)
            self.assertNotIn("sensitive diagnostic", str(caught.exception))

    def test_openai_missing_invalid_or_extra_fields_are_rejected(self):
        for features in (
            {"direction": 0, "materiality": 0.5, "novelty": 0.5},
            {"direction": 2, "materiality": 0.5, "novelty": 0.5, "uncertainty": 0.5},
            {
                "direction": 0,
                "materiality": 0.5,
                "novelty": 0.5,
                "uncertainty": 0.5,
                "trade": "BUY",
            },
            {"direction": True, "materiality": 0.5, "novelty": 0.5, "uncertainty": 0.5},
        ):
            with (
                self.subTest(features=features),
                self.assertRaises(providers.ProviderValidationError),
            ):
                self.extract("openai", openai_response(features))

    def test_openai_malformed_content_is_rejected(self):
        for content in (
            [],
            [{"type": "output_text", "text": "not json"}],
            [{"type": "output_text", "text": "[]"}],
        ):
            raw = openai_response()
            raw["output"][1]["content"] = content
            with (
                self.subTest(content=content),
                self.assertRaises(providers.ProviderValidationError),
            ):
                self.extract("openai", raw)

    def test_document_limit_rejects_before_any_request(self):
        transport = Mock()
        with self.assertRaisesRegex(providers.ProviderValidationError, "exceeds"):
            providers.extract_features(
                "jev", "model", "a" * 30_000, "b" * 10_001, STRATEGY, transport=transport
            )
        transport.assert_not_called()

    def test_invalid_input_or_strategy_rejects_before_any_request(self):
        transport = Mock()
        for current, previous, strategy in (
            ("", "x", STRATEGY),
            (None, "x", STRATEGY),
            ("x", None, STRATEGY),
            ("x", "y", {}),
        ):
            with (
                self.subTest(current=current, previous=previous),
                self.assertRaises(providers.ProviderValidationError),
            ):
                providers.extract_features(
                    "jev", "model", current, previous, strategy, transport=transport
                )
        transport.assert_not_called()

    def test_missing_api_key_is_safe_configuration_error(self):
        with patch.dict("os.environ", {}, clear=True):
            for provider, variable in (("jev", "TYPESAFE_API_KEY"), ("openai", "OPENAI_API_KEY")):
                with (
                    self.subTest(provider=provider),
                    self.assertRaisesRegex(providers.ProviderError, variable),
                ):
                    self.extract(provider)

    def test_transport_failure_is_not_retried_or_leaked(self):
        transport = Mock(side_effect=RuntimeError("Authorization: Bearer test-secret"))
        with self.assertRaises(providers.ProviderError) as caught:
            providers.extract_features(
                "jev", "model", "Current text", "Previous text", STRATEGY, transport=transport
            )
        transport.assert_called_once()
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertIn("not retried", str(caught.exception))

    def test_http_failure_discards_sensitive_body_and_headers(self):
        error = urllib.error.HTTPError(
            providers.JEV_ENDPOINT, 429, "test-secret", {}, io.BytesIO(b"test-secret")
        )
        opener = Mock()
        opener.open.side_effect = error
        with (
            patch("urllib.request.build_opener", return_value=opener),
            self.assertRaises(providers.ProviderError) as caught,
        ):
            providers.post_json(providers.JEV_ENDPOINT, {"test": True}, "test-secret", 30)
        opener.open.assert_called_once()
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertIn("429", str(caught.exception))

    def test_http_transport_sets_timeout_and_rejects_invalid_or_oversized_json(self):
        for body in (b"not json", b"[]", b"x" * (providers.MAX_RESPONSE_BYTES + 1)):
            response = Mock()
            response.read.return_value = body
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=response)
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with (
                patch("urllib.request.build_opener", return_value=opener),
                self.assertRaises(providers.ProviderValidationError),
            ):
                providers.post_json(providers.JEV_ENDPOINT, {}, "test-secret", 30)
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)

    def test_proposer_preserves_policy_and_reports_metadata(self):
        proposal = {
            "name": "candidate-two",
            "questions": {
                key: f"Revised {question}" for key, question in STRATEGY["questions"].items()
            },
        }
        metadata = {}
        transport = Mock(return_value=openai_response(proposal))
        original = copy.deepcopy(STRATEGY)
        result = providers.propose_strategy(
            STRATEGY, {"development_metric": 0.1}, "model", transport=transport, metadata=metadata
        )
        self.assertEqual(result["risk"], STRATEGY["risk"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["name"], "candidate-two")
        self.assertEqual(STRATEGY, original)
        result["risk"]["max_gross"] = 999
        self.assertEqual(STRATEGY["risk"]["max_gross"], 0.5)
        self.assertEqual(metadata["input_tokens"], 234)
        self.assertEqual(metadata["resolved_model"], "test-model-resolved")
        payload = transport.call_args.args[1]
        self.assertEqual(
            set(payload["text"]["format"]["schema"]["properties"]), {"name", "questions"}
        )
        self.assertNotIn("risk", json.loads(payload["input"])["current"])

    def test_proposer_cannot_modify_risk(self):
        proposal = {"name": "bad", "questions": STRATEGY["questions"], "risk": {"max_gross": 9}}
        with self.assertRaises(providers.ProviderValidationError):
            providers.propose_strategy(
                STRATEGY, {}, "model", transport=Mock(return_value=openai_response(proposal))
            )

    def test_proposer_invalid_questions_do_not_mutate_original(self):
        original = copy.deepcopy(STRATEGY)
        proposal = {"name": "bad", "questions": {"direction": ""}}
        with self.assertRaises(providers.ProviderValidationError):
            providers.propose_strategy(
                STRATEGY, {}, "model", transport=Mock(return_value=openai_response(proposal))
            )
        self.assertEqual(STRATEGY, original)


if __name__ == "__main__":
    unittest.main()
