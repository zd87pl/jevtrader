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


if __name__ == "__main__":
    unittest.main()
