import copy
import json
import unittest

from jevtrader.common import load_strategy, validate_strategy
from jevtrader.paper import plan_order


def forecast(action="LONG", price=50.0):
    return {
        "id": "forecast-1",
        "symbol": "TEST",
        "mode": "forward",
        "action": action,
        "features": [1.0, 0.8, 0.9, 0.2, 0.03, 0.1, 0.02, 0.001],
        "market": {"price": price, "dollar_volume": 10_000_000.0},
        "expected_return": -0.03 if action == "SHORT" else 0.03,
    }


class PaperPlanTests(unittest.TestCase):
    def test_default_long_risk_and_two_sided_costs(self):
        signal = forecast()
        original = copy.deepcopy(signal)
        plan = plan_order(signal, {}, equity=3000)
        self.assertEqual(plan["action"], "LONG")
        self.assertEqual(plan["quantity"], 3)
        self.assertAlmostEqual(plan["estimated_entry_price"], 50.05)
        self.assertAlmostEqual(plan["stop_price"], 48.0)
        self.assertAlmostEqual(plan["estimated_round_trip_cost"], 0.3)
        self.assertAlmostEqual(plan["planned_loss_at_stop"], 6.3)
        self.assertLessEqual(plan["planned_loss_at_stop"], 7.5)
        self.assertTrue(plan["simulation_only"])
        self.assertEqual(signal, original)
        json.dumps(plan, allow_nan=False)

    def test_question_rules_match_what_providers_accept(self):
        padded = load_strategy()
        padded["questions"]["novelty"] = "   New info?     "  # Providers accept it.
        validate_strategy(padded)
        self.assertEqual(plan_order(forecast(), padded, equity=3000)["action"], "LONG")
        padded["questions"]["novelty"] = " " * 12  # Never accepted by any provider.
        with self.assertRaisesRegex(ValueError, "Questions"):
            plan_order(forecast(), padded, equity=3000)

    def test_commissions_reserved_in_risk_and_cash(self):
        plan = plan_order(forecast(), {"commission_per_order": 1.0}, equity=3000)
        self.assertEqual(plan["quantity"], 2)
        self.assertAlmostEqual(plan["estimated_round_trip_cost"], 2.2)
        self.assertAlmostEqual(plan["planned_loss_at_stop"], 6.2)
        self.assertAlmostEqual(plan["cash_reserved"], 102.2)
        blocked = plan_order(forecast(), {"commission_per_order": 4.0}, equity=3000)
        self.assertEqual(blocked["quantity"], 0)

    def test_minimum_stop_and_position_cap(self):
        signal = forecast(price=10)
        signal["features"][6] = 0.0
        plan = plan_order(signal, {"risk_per_trade": 0.1}, equity=3000)
        self.assertAlmostEqual(plan["stop_fraction"], 0.03)
        self.assertLessEqual(plan["quantity"] * plan["estimated_entry_price"], 450)
        self.assertEqual(plan["quantity"], 44)

    def test_cash_limit_and_omitted_cash_conservative(self):
        plan = plan_order(forecast(), {}, equity=3000, cash=51)
        self.assertEqual(plan["quantity"], 1)
        self.assertLessEqual(plan["cash_reserved"], 51)
        blocked = plan_order(forecast(), {}, equity=3000, cash=50)
        self.assertEqual(blocked["quantity"], 0)
        positions = [{"symbol": "OTHER", "side": "LONG", "quantity": 59, "price": 50}]
        inferred = plan_order(forecast(), {}, equity=3000, positions=positions)
        self.assertEqual(inferred["quantity"], 0)

    def test_gross_short_caps_and_availability(self):
        signal = forecast("SHORT")
        self.assertEqual(plan_order(signal, {}, equity=3000, shortable=True)["quantity"], 0)
        self.assertEqual(plan_order(signal, {"allow_short": True}, equity=3000)["quantity"], 0)
        plan = plan_order(signal, {"allow_short": True}, equity=3000, shortable=True)
        self.assertEqual(plan["action"], "SHORT")
        self.assertLess(plan["estimated_entry_price"], plan["reference_price"])
        self.assertGreater(plan["stop_price"], plan["reference_price"])
        self.assertGreater(plan["estimated_round_trip_cost"], 0.3)
        holdings = [{"symbol": "OTHER", "side": "SHORT", "quantity": 15, "price": 50}]
        blocked = plan_order(
            signal,
            {"allow_short": True},
            equity=3000,
            cash=3000,
            positions=holdings,
            shortable=True,
        )
        self.assertEqual(blocked["quantity"], 0)
        gross = [{"symbol": "OTHER", "side": "LONG", "quantity": 60, "price": 50}]
        self.assertEqual(
            plan_order(forecast(), {}, equity=3000, cash=3000, positions=gross)["quantity"], 0
        )

    def test_pause_exactly_at_drawdown_boundary(self):
        plan = plan_order(forecast(), {}, equity=2760, peak_equity=3000)
        self.assertEqual(plan["quantity"], 0)
        self.assertTrue(any("drawdown" in item for item in plan["reasons"]))
        self.assertGreater(plan_order(forecast(), {}, equity=2761, peak_equity=3000)["quantity"], 0)

    def test_duplicate_and_max_positions(self):
        holdings = [{"symbol": "TEST", "side": "LONG", "quantity": 1, "price": 50}]
        self.assertEqual(plan_order(forecast(), {}, equity=3000, positions=holdings)["quantity"], 0)
        holdings = [
            {"symbol": name, "side": "LONG", "quantity": 1, "price": 50}
            for name in ("AAA", "BBB", "CCC", "DDD")
        ]
        self.assertEqual(plan_order(forecast(), {}, equity=3000, positions=holdings)["quantity"], 0)
        with self.assertRaises(ValueError):
            plan_order(forecast(), {}, equity=3000, positions=[holdings[0], holdings[0]])

    def test_market_eligibility_and_observed_spread(self):
        signal = forecast()
        signal["features"][7] = 0.01
        plan = plan_order(signal, {}, equity=3000)
        self.assertAlmostEqual(plan["assumed_spread_fraction"], 0.01)
        self.assertAlmostEqual(plan["estimated_entry_price"], 50.275)
        self.assertEqual(plan_order(forecast(price=4), {}, equity=3000)["quantity"], 0)
        signal["market"]["dollar_volume"] = 10
        self.assertEqual(plan_order(signal, {}, equity=3000)["quantity"], 0)

    def test_missing_prediction_or_insufficient_net_edge(self):
        signal = forecast()
        signal["expected_return"] = None
        self.assertEqual(plan_order(signal, {}, equity=3000)["quantity"], 0)
        signal["expected_return"] = 0.004
        self.assertEqual(plan_order(signal, {}, equity=3000)["quantity"], 0)
        signal["expected_return"] = 0.01
        self.assertEqual(
            plan_order(signal, {"commission_per_order": 1.0}, equity=3000)["quantity"], 0
        )

    def test_invalid_numerics_fail_closed(self):
        for invalid in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                plan_order(forecast(price=invalid), {}, equity=3000)
            with self.assertRaises(ValueError):
                plan_order(forecast(), {}, equity=invalid)
        for invalid in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                plan_order(forecast(), {}, equity=3000, cash=invalid)
        signal = forecast()
        signal["features"][6] = float("nan")
        with self.assertRaises(ValueError):
            plan_order(signal, {}, equity=3000)
        with self.assertRaises(ValueError):
            plan_order(forecast("SHORT"), {"allow_short": True}, equity=3000, shortable=1)

    def test_pass_watch_and_synthetic_are_never_fills(self):
        for action in ("PASS", "WATCH"):
            plan = plan_order(forecast(action), {}, equity=3000)
            self.assertEqual(plan["action"], action)
            self.assertEqual(plan["quantity"], 0)
            self.assertIsNone(plan["stop_price"])
        signal = forecast()
        signal["mode"] = "synthetic"
        plan = plan_order(signal, {}, equity=3000)
        self.assertTrue(any("Synthetic" in note for note in plan["notes"]))
        self.assertTrue(any("not a guaranteed" in note for note in plan["notes"]))


class PaperRefusalTests(unittest.TestCase):
    """One case per refusal branch of ``plan_order``, each pinned by its own reason (P0-20)."""

    def refused(self, signal, strategy=None, **kwargs):
        kwargs.setdefault("equity", 3000)
        plan = plan_order(signal, strategy or {}, **kwargs)
        self.assertEqual(plan["action"], "PASS")
        self.assertEqual(plan["quantity"], 0)
        self.assertIsNone(plan["stop_price"])
        return plan["reasons"]

    def planned(self, signal, strategy=None, **kwargs):
        kwargs.setdefault("equity", 3000)
        plan = plan_order(signal, strategy or {}, **kwargs)
        self.assertGreater(plan["quantity"], 0, plan["reasons"])
        return plan

    def test_drawdown_pause(self):
        reasons = self.refused(forecast(), equity=2760, peak_equity=3000)
        self.assertEqual(reasons, ["Portfolio drawdown reached the pause threshold."])

    def test_existing_position_in_the_symbol(self):
        held = [{"symbol": "TEST", "side": "LONG", "quantity": 1, "price": 50}]
        reasons = self.refused(forecast(), positions=held)
        self.assertEqual(reasons, ["A position in this symbol already exists."])

    def test_maximum_position_count(self):
        held = [
            {"symbol": name, "side": "LONG", "quantity": 1, "price": 50}
            for name in ("AAA", "BBB", "CCC", "DDD")
        ]
        self.assertEqual(
            self.refused(forecast(), positions=held), ["Maximum position count reached."]
        )
        self.planned(forecast(), positions=held[:3])

    def test_existing_short_exposure_above_the_maximum(self):
        # max_short_fraction 0.25 of 3000 equity is 750; the rule refuses only above it.
        over = [{"symbol": "OTHER", "side": "SHORT", "quantity": 16, "price": 50}]
        reasons = self.refused(forecast(), positions=over)
        self.assertEqual(reasons, ["Existing short exposure exceeds the configured maximum."])
        at = [{"symbol": "OTHER", "side": "SHORT", "quantity": 15, "price": 50}]
        self.planned(forecast(), positions=at, cash=3000)

    def test_price_below_minimum(self):
        self.assertEqual(
            self.refused(forecast(price=4.99)), ["Price is below the configured minimum."]
        )
        self.planned(forecast(price=5.0))

    def test_dollar_volume_below_minimum(self):
        signal = forecast()
        signal["market"]["dollar_volume"] = 4_999_999.0
        reasons = self.refused(signal)
        self.assertEqual(reasons, ["Dollar volume is below the configured liquidity minimum."])
        signal["market"]["dollar_volume"] = 5_000_000.0
        self.planned(signal)

    def test_semantic_uncertainty_above_maximum(self):
        signal = forecast()
        signal["features"][3] = 0.61
        reasons = self.refused(signal)
        self.assertEqual(reasons, ["Semantic uncertainty exceeds the configured maximum."])
        signal["features"][3] = 0.6
        self.planned(signal)

    def test_short_disabled_by_the_strategy(self):
        reasons = self.refused(forecast("SHORT"), shortable=True)
        self.assertEqual(reasons, ["Shorts are disabled by the strategy."])

    def test_short_availability_not_confirmed(self):
        reasons = self.refused(forecast("SHORT"), {"allow_short": True})
        self.assertEqual(reasons, ["Short availability was not explicitly confirmed."])

    def test_missing_expected_return(self):
        signal = forecast()
        signal["expected_return"] = None
        self.assertEqual(self.refused(signal), ["No numerical expected return is available."])

    def test_expected_return_against_the_requested_direction(self):
        message = ["Expected return does not support the requested direction."]
        short = {"allow_short": True}
        for action, value, strategy in (
            ("LONG", 0.0, {}),
            ("LONG", -0.03, {}),
            ("SHORT", 0.0, short),
            ("SHORT", 0.03, short),
        ):
            with self.subTest(action=action, value=value):
                signal = forecast(action)
                signal["expected_return"] = value
                self.assertEqual(self.refused(signal, strategy, shortable=True), message)

    def test_spread_and_slippage_that_consume_the_price_raise(self):
        # A configured spread of 200% (no upper bound in the strategy) leaves no entry price.
        with self.assertRaisesRegex(ValueError, "invalid entry price"):
            plan_order(forecast(), {"spread_bps": 20_000.0}, equity=3000)

    def test_stop_distance_too_large(self):
        signal = forecast()
        signal["features"][6] = 0.5  # stop_volatility_multiple 2.0 x 0.5 = the whole price.
        self.assertEqual(
            self.refused(signal), ["Stop distance is too large for this paper policy."]
        )
        signal["features"][6] = 0.4999
        reasons = plan_order(signal, {}, equity=3000)["reasons"]
        self.assertNotIn("Stop distance is too large for this paper policy.", reasons)

    def test_insufficient_whole_share_capacity_names_the_binding_limit(self):
        reasons = self.refused(forecast(), cash=50)
        self.assertEqual(reasons, ["Insufficient whole-share capacity under cash reserve."])

    def test_expected_edge_below_costs_plus_minimum_edge(self):
        signal = forecast()
        signal["expected_return"] = 0.003
        reasons = self.refused(signal)
        self.assertEqual(
            reasons, ["Expected edge does not cover full costs plus the minimum edge requirement."]
        )


if __name__ == "__main__":
    unittest.main()
