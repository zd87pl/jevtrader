"""Registry labels are pure: exact model names, fixed boundaries, conservative defaults."""

import copy
import unittest
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jevtrader import config, providers, registry

NEW_YORK = ZoneInfo("America/New_York")
DATED = {key: entry for key, entry in registry.MODELS.items() if entry["training_cutoff"]}
OPENAI_SNAPSHOT = "gpt-example-2025-01-31"


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def edgar_day(day: date, clock: time) -> str:
    return iso(datetime.combine(day, clock, tzinfo=NEW_YORK))


def historical(provider, model, published_at, overrides=None):
    return registry.eligibility(
        provider, model, mode="historical", published_at=published_at, overrides=overrides
    )


class CatalogTests(unittest.TestCase):
    def test_contract_constants(self):
        self.assertEqual(registry.BUFFER_DAYS, 92)
        self.assertEqual(
            registry.EVIDENCE_LABELS, frozenset({"forward", "post_cutoff", "no_model_knowledge"})
        )
        self.assertLessEqual(registry.EVIDENCE_LABELS, set(registry.LABELS))
        self.assertEqual(len(set(registry.LABELS)), 7)
        self.assertIn("adhoc_replay", registry.LABELS)  # hand-picked replays: never evidence
        self.assertIs(registry.PROVIDERS, providers.PROVIDERS)
        self.assertIs(config.PROVIDERS, providers.PROVIDERS)
        self.assertEqual(
            registry.PRICES, ("usd_per_million_input_tokens", "usd_per_million_output_tokens")
        )

    def test_contract_models(self):
        expected = {
            "rules:rules-v1": (None, "none", False),
            "jev:jev-1.13.0": (None, "pretrained", False),
            "local:gpt-oss:120b": ("2024-06-01", "pretrained", True),
            "local:gpt-oss-120b": ("2024-06-01", "pretrained", True),
            "local:llama3.3:70b": ("2023-12-01", "pretrained", True),
            "local:gemma3:27b": ("2024-08-01", "pretrained", True),
        }
        for key, (cutoff, knowledge, open_weights) in expected.items():
            with self.subTest(key=key):
                entry = registry.MODELS[key]
                self.assertEqual(entry["training_cutoff"], cutoff)
                self.assertEqual(entry["knowledge"], knowledge)
                self.assertIs(entry["open_weights"], open_weights)
        self.assertFalse([key for key in registry.MODELS if key.startswith("openai:")])

    def test_every_entry_is_well_formed(self):
        fields = {
            "training_cutoff",
            "source",
            "open_weights",
            "knowledge",
            "usd_per_million_input_tokens",
            "usd_per_million_output_tokens",
        }
        for key, entry in registry.MODELS.items():
            with self.subTest(key=key):
                provider, _, model = key.partition(":")
                self.assertIn(provider, registry.PROVIDERS)
                self.assertTrue(model)
                self.assertEqual(set(entry), fields)
                self.assertTrue(entry["source"].strip())
                self.assertIn(entry["knowledge"], ("none", "pretrained"))
                self.assertIs(type(entry["open_weights"]), bool)
                if entry["training_cutoff"] is not None:
                    parsed = date.fromisoformat(entry["training_cutoff"])
                    self.assertEqual(parsed.isoformat(), entry["training_cutoff"])
                if provider in ("rules", "local"):
                    self.assertEqual(entry["usd_per_million_input_tokens"], 0.0)
                    self.assertEqual(entry["usd_per_million_output_tokens"], 0.0)
        for provider, entry in registry.UNREGISTERED.items():
            with self.subTest(provider=provider):
                self.assertEqual(set(entry), fields)
                self.assertIsNone(entry["training_cutoff"])
                self.assertEqual(entry["knowledge"], "pretrained")
        self.assertNotIn("rules", registry.UNREGISTERED)

    def test_only_the_rules_baseline_lacks_learned_knowledge(self):
        clean = [key for key, entry in registry.MODELS.items() if entry["knowledge"] == "none"]
        self.assertEqual(clean, ["rules:rules-v1"])

    def test_configured_default_models_are_registered(self):
        for provider, model in config.DEFAULT_MODELS.items():
            with self.subTest(provider=provider):
                self.assertEqual(registry.lookup(provider, model)["origin"], "builtin")


class LookupTests(unittest.TestCase):
    def test_builtin_entry_carries_identity(self):
        entry = registry.lookup("local", "gpt-oss:120b")
        self.assertEqual(entry["key"], "local:gpt-oss:120b")
        self.assertEqual(entry["provider"], "local")
        self.assertEqual(entry["model"], "gpt-oss:120b")
        self.assertEqual(entry["training_cutoff"], "2024-06-01")
        self.assertEqual(entry["origin"], "builtin")
        self.assertIn("model card", entry["source"])

    def test_result_is_an_independent_copy(self):
        before = copy.deepcopy(registry.MODELS)
        entry = registry.lookup("local", "gemma3:27b")
        entry["training_cutoff"] = "1990-01-01"
        entry["knowledge"] = "none"
        registry.lookup("openai", OPENAI_SNAPSHOT)["source"] = "changed"
        self.assertEqual(registry.MODELS, before)
        self.assertEqual(registry.lookup("local", "gemma3:27b")["training_cutoff"], "2024-08-01")
        self.assertNotEqual(registry.lookup("openai", OPENAI_SNAPSHOT)["source"], "changed")

    def test_unregistered_models_get_conservative_entries(self):
        for provider, open_weights, price in (
            ("openai", False, None),
            ("local", True, 0.0),
            ("jev", False, None),
        ):
            with self.subTest(provider=provider):
                entry = registry.lookup(provider, "some-future-model")
                self.assertIsNone(entry["training_cutoff"])
                self.assertEqual(entry["knowledge"], "pretrained")
                self.assertIs(entry["open_weights"], open_weights)
                self.assertEqual(entry["usd_per_million_input_tokens"], price)
                self.assertEqual(entry["usd_per_million_output_tokens"], price)
                self.assertEqual(entry["origin"], "unregistered")

    def test_names_match_exactly(self):
        # A fine-tune or re-tagged build may have learned from later data than its base.
        for model in (
            "GPT-OSS:120B",
            " gpt-oss:120b",
            "gpt-oss:120b ",
            "gpt-oss:120b-news-2025",
            "gpt-oss",
            "llama3.3",
            "gemma3:27b-it-qat",
        ):
            with self.subTest(model=model):
                entry = registry.lookup("local", model)
                self.assertIsNone(entry["training_cutoff"])
                self.assertEqual(entry["origin"], "unregistered")
        self.assertEqual(registry.lookup("openai", "gpt-oss:120b")["origin"], "unregistered")

    def test_unknown_rules_version_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Unknown rules model"):
            registry.lookup("rules", "rules-v2")

    def test_invalid_inputs_are_rejected(self):
        for provider in ("", "Rules", "anthropic", None, 1, ["rules"]):
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "provider"):
                registry.lookup(provider, "rules-v1")
        for model in ("", "   ", None, 7, "m" * 201):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "model"):
                registry.lookup("openai", model)
        self.assertEqual(registry.lookup("openai", "m" * 200)["origin"], "unregistered")


class EligibilityTests(unittest.TestCase):
    def test_forward_mode_is_always_forward(self):
        for provider, model in (
            ("rules", "rules-v1"),
            ("jev", "jev-1.13.0"),
            ("jev", "jev-9"),
            ("openai", OPENAI_SNAPSHOT),
            ("local", "gpt-oss:120b"),
            ("local", "unregistered-model"),
        ):
            for published in ("2000-01-03T14:30:00Z", "2024-06-01T00:00:00Z"):
                with self.subTest(provider=provider, model=model, published=published):
                    label = registry.eligibility(
                        provider, model, mode="forward", published_at=published
                    )
                    self.assertEqual(label, "forward")

    def test_synthetic_mode_is_never_evidence(self):
        for provider, model in (
            ("rules", "rules-v1"),
            ("local", "gpt-oss:120b"),
            ("openai", OPENAI_SNAPSHOT),
        ):
            with self.subTest(provider=provider):
                label = registry.eligibility(
                    provider, model, mode="synthetic", published_at="2030-01-02T15:00:00Z"
                )
                self.assertEqual(label, "synthetic")
                self.assertFalse(registry.counts_as_evidence(label))

    def test_rules_replays_have_no_model_knowledge_at_any_date(self):
        for published in ("1995-05-01T12:00:00Z", "2024-06-01T00:00:00Z", "2030-01-01T00:00:00Z"):
            with self.subTest(published=published):
                self.assertEqual(historical("rules", "rules-v1", published), "no_model_knowledge")

    def test_undisclosed_cutoffs_are_unknown_for_replays(self):
        for provider, model in (
            ("jev", "jev-1.13.0"),
            ("jev", "jev-2.0.0"),
            ("openai", OPENAI_SNAPSHOT),
            ("local", "qwen3:32b"),
        ):
            for published in ("2000-01-03T14:30:00Z", "2035-01-02T14:30:00Z"):
                with self.subTest(provider=provider, model=model, published=published):
                    self.assertEqual(historical(provider, model, published), "unknown_cutoff")

    def test_gpt_oss_boundary(self):
        # 2024-06-01 + 92 days = 2024-09-01; that whole New York day stays in the buffer.
        for model in ("gpt-oss:120b", "gpt-oss-120b"):
            with self.subTest(model=model):
                cases = {
                    "2023-01-03T15:00:00Z": "contaminated",
                    "2024-06-01T00:00:00Z": "contaminated",
                    "2024-09-01T00:00:00Z": "contaminated",
                    "2024-09-01T12:00:00-04:00": "contaminated",
                    "2024-09-02T00:00:00Z": "contaminated",
                    "2024-09-01T23:59:59.999999-04:00": "contaminated",
                    "2024-09-02T03:59:59.999999Z": "contaminated",
                    "2024-09-02T00:00:00-04:00": "post_cutoff",
                    "2024-09-02T04:00:00Z": "post_cutoff",
                    "2025-01-02T14:30:00Z": "post_cutoff",
                }
                for published, label in cases.items():
                    with self.subTest(published=published):
                        self.assertEqual(historical("local", model, published), label)

    def test_llama_boundary_crosses_leap_day_in_standard_time(self):
        # 2023-12-01 + 92 days = 2024-03-02 (2024 is a leap year; New York is on EST, UTC-5).
        self.assertEqual(
            historical("local", "llama3.3:70b", "2024-03-03T04:59:59.999999Z"), "contaminated"
        )
        self.assertEqual(historical("local", "llama3.3:70b", "2024-03-03T05:00:00Z"), "post_cutoff")

    def test_gemma_boundary(self):
        # 2024-08-01 + 92 days = 2024-11-01, still daylight time (UTC-4).
        self.assertEqual(
            historical("local", "gemma3:27b", "2024-11-02T03:59:59.999999Z"), "contaminated"
        )
        self.assertEqual(historical("local", "gemma3:27b", "2024-11-02T04:00:00Z"), "post_cutoff")

    def test_every_dated_model_follows_the_same_boundary(self):
        for key, entry in DATED.items():
            provider, _, model = key.partition(":")
            boundary = date.fromisoformat(entry["training_cutoff"]) + timedelta(days=92)
            cases = {
                edgar_day(date.fromisoformat(entry["training_cutoff"]), time(0)): "contaminated",
                edgar_day(boundary, time(0)): "contaminated",
                edgar_day(boundary, time(23, 59, 59, 999999)): "contaminated",
                iso(datetime.combine(boundary, time(0), tzinfo=timezone.utc)): "contaminated",
                edgar_day(boundary + timedelta(days=1), time(0)): "post_cutoff",
                edgar_day(boundary + timedelta(days=400), time(9, 30)): "post_cutoff",
            }
            for published, label in cases.items():
                with self.subTest(key=key, published=published):
                    self.assertEqual(historical(provider, model, published), label)

    def test_labels_are_always_known(self):
        for key in registry.MODELS:
            provider, _, model = key.partition(":")
            for mode in registry.MODES:
                for published in ("2020-01-02T15:00:00Z", "2026-01-02T15:00:00Z"):
                    label = registry.eligibility(provider, model, mode=mode, published_at=published)
                    self.assertIn(label, registry.LABELS)

    def test_inputs_are_validated_in_every_mode(self):
        for mode in ("live", "Forward", "", None, ["forward"]):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "mode"):
                registry.eligibility(
                    "rules", "rules-v1", mode=mode, published_at="2024-01-02T15:00:00Z"
                )
        for published in ("2024-01-02T15:00:00", "2024-01-02", "yesterday", "", None, 1704207600):
            for mode in registry.MODES:
                with self.subTest(published=published, mode=mode):
                    with self.assertRaises(ValueError):
                        registry.eligibility("rules", "rules-v1", mode=mode, published_at=published)
        for published in ("0001-01-01T00:00:00Z", "9999-12-31T23:59:59-12:00"):
            with self.subTest(published=published), self.assertRaises(ValueError):
                historical("local", "gpt-oss:120b", published)
        for mode in registry.MODES:
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "provider"):
                registry.eligibility(
                    "anthropic", "x", mode=mode, published_at="2024-01-02T15:00:00Z"
                )
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "rules model"):
                registry.eligibility(
                    "rules", "rules-v2", mode=mode, published_at="2024-01-02T15:00:00Z"
                )

    def test_mode_and_timestamp_are_keyword_only(self):
        with self.assertRaises(TypeError):
            registry.eligibility("rules", "rules-v1", "historical", "2024-01-02T15:00:00Z")

    def test_no_clock_is_consulted(self):
        with patch("jevtrader.common.utc_now", side_effect=AssertionError("clock read")):
            self.assertEqual(
                historical("local", "gpt-oss:120b", "2025-01-02T14:30:00Z"), "post_cutoff"
            )


class EvidenceTests(unittest.TestCase):
    def test_counts_as_evidence(self):
        for label in registry.LABELS:
            with self.subTest(label=label):
                self.assertIs(registry.counts_as_evidence(label), label in registry.EVIDENCE_LABELS)
        for label in ("forward", "post_cutoff", "no_model_knowledge"):
            self.assertTrue(registry.counts_as_evidence(label))
        for label in ("contaminated", "unknown_cutoff", "synthetic"):
            self.assertFalse(registry.counts_as_evidence(label))

    def test_unrecognized_values_are_not_evidence(self):
        for label in ("Forward", " forward", "historical", "", None, 1, True, {"forward": 1}, []):
            with self.subTest(label=label):
                self.assertIs(registry.counts_as_evidence(label), False)


class OverrideTests(unittest.TestCase):
    def test_declared_local_cutoff(self):
        overrides = {"local:qwen3:32b": {"training_cutoff": "2024-10-01", "source": " Qwen card "}}
        entry = registry.lookup("local", "qwen3:32b", overrides)
        self.assertEqual(entry["training_cutoff"], "2024-10-01")
        self.assertEqual(entry["origin"], "declared")
        self.assertEqual(entry["source"], "Qwen card")
        self.assertIs(entry["open_weights"], True)
        self.assertEqual(entry["knowledge"], "pretrained")
        # 2024-10-01 + 92 days = 2025-01-01.
        self.assertEqual(
            historical("local", "qwen3:32b", "2025-01-01T20:00:00Z", overrides), "contaminated"
        )
        self.assertEqual(
            historical("local", "qwen3:32b", "2025-01-02T05:00:00Z", overrides), "post_cutoff"
        )
        self.assertEqual(historical("local", "qwen3:32b", "2025-01-02T05:00:00Z"), "unknown_cutoff")

    def test_declared_openai_snapshot(self):
        overrides = {
            f"openai:{OPENAI_SNAPSHOT}": {
                "training_cutoff": "2024-05-31",
                "usd_per_million_input_tokens": 2,
            }
        }
        entry = registry.lookup("openai", OPENAI_SNAPSHOT, overrides)
        self.assertEqual(entry["source"], "declared in config")
        self.assertEqual(entry["usd_per_million_input_tokens"], 2.0)
        self.assertIs(entry["open_weights"], False)
        self.assertEqual(
            historical("openai", OPENAI_SNAPSHOT, "2024-08-31T23:00:00Z", overrides),
            "contaminated",
        )
        self.assertEqual(
            historical("openai", OPENAI_SNAPSHOT, "2024-09-01T12:00:00Z", overrides),
            "post_cutoff",
        )
        # A declaration names one resolved snapshot, never its alias or siblings.
        self.assertEqual(
            historical("openai", "gpt-example", "2025-06-01T12:00:00Z", overrides),
            "unknown_cutoff",
        )

    def test_builtin_cutoff_may_only_become_more_conservative(self):
        later = {"local:gpt-oss:120b": {"training_cutoff": "2025-01-01"}}
        entry = registry.lookup("local", "gpt-oss:120b", later)
        self.assertEqual(entry["training_cutoff"], "2025-01-01")
        self.assertEqual(entry["origin"], "declared")
        self.assertIn("model card", entry["source"])
        self.assertIn("declared in config", entry["source"])
        self.assertEqual(
            historical("local", "gpt-oss:120b", "2025-01-02T14:30:00Z", later), "contaminated"
        )
        same = {"local:gpt-oss:120b": {"training_cutoff": "2024-06-01"}}
        self.assertEqual(
            registry.lookup("local", "gpt-oss:120b", same)["training_cutoff"], "2024-06-01"
        )
        unknown = {"local:gpt-oss:120b": {"training_cutoff": None}}
        self.assertEqual(
            historical("local", "gpt-oss:120b", "2025-01-02T14:30:00Z", unknown), "unknown_cutoff"
        )
        earlier = {"local:gpt-oss:120b": {"training_cutoff": "2024-05-31"}}
        with self.assertRaisesRegex(ValueError, "cannot precede the published 2024-06-01"):
            historical("local", "gpt-oss:120b", "2024-09-02T14:30:00Z", earlier)

    def test_jev_and_rules_cannot_gain_a_cutoff(self):
        with self.assertRaisesRegex(ValueError, "JEV does not disclose"):
            registry.lookup(
                "jev", "jev-1.13.0", {"jev:jev-1.13.0": {"training_cutoff": "2020-01-01"}}
            )
        with self.assertRaisesRegex(ValueError, "JEV does not disclose"):
            registry.lookup("jev", "jev-1.13.0", {"jev:jev-2": {"training_cutoff": "2020-01-01"}})
        with self.assertRaisesRegex(ValueError, "rules baseline"):
            registry.lookup("rules", "rules-v1", {"rules:rules-v1": {"training_cutoff": None}})
        priced = {"jev:jev-1.13.0": {"usd_per_million_input_tokens": 1.5, "training_cutoff": None}}
        entry = registry.lookup("jev", "jev-1.13.0", priced)
        self.assertIsNone(entry["training_cutoff"])
        self.assertEqual(entry["usd_per_million_input_tokens"], 1.5)
        self.assertEqual(
            historical("jev", "jev-1.13.0", "2035-01-02T14:30:00Z", priced), "unknown_cutoff"
        )

    def test_a_declaration_cannot_remove_learned_knowledge(self):
        for field in ("knowledge", "open_weights"):
            overrides = {"local:qwen3:32b": {"training_cutoff": "2024-01-01", field: "none"}}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unknown override"):
                registry.lookup("local", "qwen3:32b", overrides)

    def test_invalid_keys(self):
        for key in (
            "qwen3:32b",
            "gpt-oss",
            "local:",
            ":model",
            "Local:qwen3",
            " local:qwen3",
            "local: qwen3",
            "local:qwen3 ",
            "local:qwen\n3",
            "local:" + "m" * 201,
            "local:*",
            "openai:gpt-*",
            7,
            None,
            ("local", "qwen3"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                registry.validate_overrides({key: {"training_cutoff": "2024-01-01"}})

    def test_invalid_declarations(self):
        key = "local:qwen3:32b"
        for declared in (
            None,
            "2024-01-01",
            ["training_cutoff"],
            {},
            {"source": "only a source"},
            {"training_cutoff": "2024-01-01", "cutoff": "2024-01-01"},
            {"training_cutoff": "2024-01-01", "usd_per_million_input_tokens": 0},
            {"training_cutoff": "2024-01-01", "usd_per_million_output_tokens": 0},
            {"training_cutoff": "2024-01-01", "usd_per_million_reasoning_tokens": 1},
            {"training_cutoff": "2024-01-01", "source": ""},
            {"training_cutoff": "2024-01-01", "source": "   "},
            {"training_cutoff": "2024-01-01", "source": "x" * 201},
            {"training_cutoff": "2024-01-01", "source": "bad\x07source"},
            {"training_cutoff": "2024-01-01", "source": 5},
        ):
            with self.subTest(declared=declared), self.assertRaises(ValueError):
                registry.validate_overrides({key: declared})

    def test_invalid_cutoffs(self):
        for cutoff in (
            "2024-6-1",
            "20240601",
            "2024-02-30",
            "0000-01-01",
            "2024-06-01T00:00:00Z",
            " 2024-06-01",
            "June 2024",
            20240601,
            True,
            date(2024, 6, 1),
        ):
            with self.subTest(cutoff=cutoff), self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                registry.validate_overrides({"local:qwen3:32b": {"training_cutoff": cutoff}})

    def test_invalid_prices(self):
        key = f"openai:{OPENAI_SNAPSHOT}"
        for field in registry.PRICES:
            for price in (
                -0.01,
                1000.01,
                float("nan"),
                float("inf"),
                -float("inf"),
                True,
                "1",
                10**400,
            ):
                with (
                    self.subTest(field=field, price=price),
                    self.assertRaisesRegex(ValueError, f"{field} must be null or 0–1000"),
                ):
                    registry.validate_overrides({key: {field: price}})
            for price in (0, 0.0, 1000, None):
                with self.subTest(field=field, price=price):
                    result = registry.validate_overrides({key: {field: price}})
                    self.assertEqual(result[key], {field: price})

    def test_output_price_is_declared_and_looked_up_separately(self):
        key = f"openai:{OPENAI_SNAPSHOT}"
        both = {key: {"usd_per_million_input_tokens": 1.25, "usd_per_million_output_tokens": 10}}
        entry = registry.lookup("openai", OPENAI_SNAPSHOT, both)
        self.assertEqual(
            (entry["usd_per_million_input_tokens"], entry["usd_per_million_output_tokens"]),
            (1.25, 10.0),
        )
        self.assertEqual(entry["origin"], "declared")
        only_input = {key: {"usd_per_million_input_tokens": 1.25}}
        self.assertIsNone(
            registry.lookup("openai", OPENAI_SNAPSHOT, only_input)["usd_per_million_output_tokens"]
        )
        jev = {"jev:jev-1.13.0": {"usd_per_million_output_tokens": 8}}
        entry = registry.lookup("jev", "jev-1.13.0", jev)
        self.assertEqual(entry["usd_per_million_output_tokens"], 8.0)
        self.assertIsNone(entry["usd_per_million_input_tokens"])
        with self.assertRaisesRegex(ValueError, "no token price"):
            registry.validate_overrides({"local:qwen3:32b": {"usd_per_million_output_tokens": 1}})

    def test_container_shape_and_size(self):
        self.assertEqual(registry.validate_overrides(None), {})
        self.assertEqual(registry.validate_overrides({}), {})
        for overrides in ([], "local:x", [("local:x", {"training_cutoff": None})]):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, "object"):
                registry.validate_overrides(overrides)
        limit = {f"local:m{n}": {"training_cutoff": None} for n in range(registry.MAX_OVERRIDES)}
        self.assertEqual(len(registry.validate_overrides(limit)), registry.MAX_OVERRIDES)
        limit["local:one-more"] = {"training_cutoff": None}
        with self.assertRaisesRegex(ValueError, "At most"):
            registry.validate_overrides(limit)

    def test_validation_normalizes_without_mutating_input(self):
        overrides = {
            "local:qwen3:32b": {"training_cutoff": "2024-10-01", "source": "  card  "},
            f"openai:{OPENAI_SNAPSHOT}": {"usd_per_million_input_tokens": 3},
        }
        original = copy.deepcopy(overrides)
        result = registry.validate_overrides(overrides)
        self.assertEqual(overrides, original)
        self.assertEqual(
            result,
            {
                "local:qwen3:32b": {"training_cutoff": "2024-10-01", "source": "card"},
                f"openai:{OPENAI_SNAPSHOT}": {"usd_per_million_input_tokens": 3.0},
            },
        )
        result["local:qwen3:32b"]["training_cutoff"] = "1990-01-01"
        self.assertEqual(overrides, original)

    def test_any_bad_declaration_fails_every_lookup(self):
        overrides = {
            "local:qwen3:32b": {"training_cutoff": "2024-10-01"},
            "local:broken": {"training_cutoff": "someday"},
        }
        with self.assertRaisesRegex(ValueError, "local:broken"):
            registry.lookup("rules", "rules-v1", overrides)
        with self.assertRaisesRegex(ValueError, "local:broken"):
            registry.eligibility(
                "rules",
                "rules-v1",
                mode="forward",
                published_at="2024-01-02T15:00:00Z",
                overrides=overrides,
            )

    def test_unrelated_declarations_do_not_change_other_models(self):
        overrides = {"local:qwen3:32b": {"training_cutoff": "2020-01-01"}}
        self.assertEqual(
            registry.lookup("local", "gpt-oss:120b", overrides),
            registry.lookup("local", "gpt-oss:120b"),
        )


if __name__ == "__main__":
    unittest.main()
