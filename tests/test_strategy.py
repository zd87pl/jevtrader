"""Strategy validation: the documented cost floor (R2, ADR-0002) and every refusal branch."""

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from jevtrader.common import (
    COST_FLOOR,
    load_strategy,
    meets_cost_floor,
    round_trip_bps,
    validate_strategy,
)

DEFAULT = load_strategy()


def changed(**fields):
    strategy = copy.deepcopy(DEFAULT)
    strategy.update(fields)
    return strategy


def without(key):
    strategy = copy.deepcopy(DEFAULT)
    del strategy[key]
    return strategy


class CostFloorTests(unittest.TestCase):
    def test_floor_values_are_pinned(self):
        # Moving a floor changes which strategies may count as evidence: invariant I-2.
        self.assertEqual(
            COST_FLOOR,
            {
                "spread_bps": 2.0,
                "slippage_bps_per_side": 1.0,
                "short_borrow_bps_annual": 25.0,
                "min_edge_bps": 5.0,
            },
        )

    def test_default_strategy_clears_the_floor_with_unchanged_costs(self):
        self.assertTrue(meets_cost_floor(DEFAULT))
        for key, floor in COST_FLOOR.items():
            with self.subTest(key=key):
                self.assertGreater(DEFAULT[key], floor)
        self.assertEqual(round_trip_bps(DEFAULT), 20.0)
        self.assertAlmostEqual(round_trip_bps(DEFAULT, short=True), 20 + 300 * 10 / 252)
        self.assertEqual(DEFAULT["min_edge_bps"], 20.0)

    def test_zero_cost_strategy_is_rejected(self):
        free = changed(
            spread_bps=0.0, slippage_bps_per_side=0.0, short_borrow_bps_annual=0.0, min_edge_bps=0.0
        )
        self.assertFalse(meets_cost_floor(free))
        with self.assertRaisesRegex(ValueError, "cost floor"):
            validate_strategy(free)

    def test_zero_cost_strategy_file_does_not_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "free.json"
            path.write_text(json.dumps(changed(spread_bps=0, slippage_bps_per_side=0)))
            with self.assertRaisesRegex(ValueError, "spread_bps .*cost floor"):
                load_strategy(path)

    def test_each_cost_is_accepted_at_its_floor_and_rejected_just_below(self):
        for key, floor in COST_FLOOR.items():
            with self.subTest(key=key):
                at_floor = changed(**{key: floor})
                validate_strategy(at_floor)
                self.assertTrue(meets_cost_floor(at_floor))
                below = changed(**{key: math.nextafter(floor, 0)})
                self.assertFalse(meets_cost_floor(below))
                with self.assertRaisesRegex(ValueError, f"{key} .*cost floor"):
                    validate_strategy(below)
                with self.assertRaisesRegex(ValueError, key):
                    validate_strategy(changed(**{key: 0}))

    def test_whole_number_costs_at_the_floor_are_accepted(self):
        validate_strategy(changed(spread_bps=2, slippage_bps_per_side=1, min_edge_bps=5))

    def test_commission_is_not_floored(self):
        # Dollars per order, outside the gate's bps round trip; zero-commission brokers exist.
        self.assertNotIn("commission_per_order", COST_FLOOR)
        validate_strategy(changed(commission_per_order=0))

    def test_unprovable_costs_never_meet_the_floor(self):
        cases = {
            "not a dict": None,
            "list": [],
            "missing spread": without("spread_bps"),
            "missing borrow": without("short_borrow_bps_annual"),
            "string cost": changed(spread_bps="10"),
            "boolean cost": changed(min_edge_bps=True),
            "nan cost": changed(slippage_bps_per_side=float("nan")),
            "infinite cost": changed(short_borrow_bps_annual=float("inf")),
            "negative cost": changed(min_edge_bps=-20.0),
        }
        for name, strategy in cases.items():
            with self.subTest(name=name):
                self.assertFalse(meets_cost_floor(strategy))


class StrategyRefusalTests(unittest.TestCase):
    """One case per rejection branch of ``validate_strategy`` (P0-20)."""

    def assert_rejected(self, strategy, message):
        with self.assertRaisesRegex(ValueError, message):
            validate_strategy(strategy)

    def test_default_strategy_is_valid(self):
        validate_strategy(copy.deepcopy(DEFAULT))

    def test_fields_must_match_the_documented_set(self):
        message = "exactly the documented configuration fields"
        self.assert_rejected([], message)
        self.assert_rejected(without("spread_bps"), message)
        self.assert_rejected(changed(leverage=2.0), message)

    def test_version_and_name(self):
        message = "version must be 1 and name"
        for fields in (
            dict(version=2),
            dict(version=True),
            dict(version=1.0),
            dict(name=""),
            dict(name="   "),
            dict(name=7),
            dict(name="x" * 201),
        ):
            with self.subTest(fields=fields):
                self.assert_rejected(changed(**fields), message)
        validate_strategy(changed(name="x" * 200))

    def test_questions_need_the_three_names(self):
        message = "requires direction, materiality, novelty"
        self.assert_rejected(changed(questions="direction"), message)
        questions = dict(DEFAULT["questions"])
        del questions["novelty"]
        self.assert_rejected(changed(questions=questions), message)
        self.assert_rejected(
            changed(questions={**DEFAULT["questions"], "sentiment": "Is the tone upbeat here?"}),
            message,
        )

    def test_question_text_length_and_content(self):
        message = "Questions must contain 10–4000 characters"
        for text in ("too short", " " * 12, "x" * 4001, 12345678901):
            with self.subTest(text=text):
                questions = {**DEFAULT["questions"], "direction": text}
                self.assert_rejected(changed(questions=questions), message)
        for text in ("x" * 10, "x" * 4000):
            validate_strategy(changed(questions={**DEFAULT["questions"], "direction": text}))

    def test_benchmark_must_be_a_symbol(self):
        self.assert_rejected(changed(benchmark="S P Y"), "Invalid symbol")
        self.assert_rejected(changed(benchmark=""), "Invalid symbol")

    def test_allow_short_must_be_boolean(self):
        for value in (1, 0, "false", None):
            with self.subTest(value=value):
                self.assert_rejected(changed(allow_short=value), "allow_short must be boolean")

    def test_numeric_fields_must_be_numbers(self):
        for value in ("10", None, True, [10]):
            with self.subTest(value=value):
                self.assert_rejected(changed(min_price=value), "min_price must be a number")

    def test_numeric_fields_must_be_finite_and_nonnegative(self):
        for value in (-1.0, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assert_rejected(changed(min_dollar_volume=value), "Invalid min_dollar_volume")

    def test_integer_fields_must_be_positive_integers(self):
        for key in ("horizon_sessions", "max_positions", "max_market_age_hours"):
            for value in (0, 3.0):
                with self.subTest(key=key, value=value):
                    self.assert_rejected(
                        changed(**{key: value}), f"{key} must be a positive integer"
                    )

    def test_fractions_must_be_in_the_unit_interval(self):
        for key in (
            "risk_per_trade",
            "max_position_fraction",
            "max_gross_fraction",
            "max_short_fraction",
            "pause_drawdown",
            "min_stop_fraction",
            "max_uncertainty",
        ):
            with self.subTest(key=key):
                self.assert_rejected(changed(**{key: 0.0}), rf"{key} must be in \(0, 1\]")
                self.assert_rejected(changed(**{key: 1.01}), rf"{key} must be in \(0, 1\]")
                validate_strategy(changed(**{key: 1.0}))

    def test_minimum_history_and_training_sizes(self):
        message = "at least 21 history sessions and 10 training events"
        self.assert_rejected(changed(min_history_sessions=20), message)
        self.assert_rejected(changed(min_train_samples=9), message)
        validate_strategy(changed(min_history_sessions=21, min_train_samples=10))

    def test_ridge_alpha_must_be_positive(self):
        self.assert_rejected(changed(ridge_alpha=0.0), "ridge_alpha and max_market_age_hours")
        validate_strategy(changed(ridge_alpha=0.001))


if __name__ == "__main__":
    unittest.main()
