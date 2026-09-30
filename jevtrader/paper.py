"""Deterministic, whole-share paper-order plans. Nothing here submits an order."""

from __future__ import annotations

import math
from typing import Any

from .common import load_strategy, number, symbol, validate_strategy


_NOTE = (
    "Paper estimate only: a stop is not a guaranteed fill or maximum loss; gaps, "
    "halts, liquidity, borrow recalls and changing borrow fees can increase losses."
)


def _positive(value: Any, name: str) -> float:
    result = number(value, name, minimum=0)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def plan_order(
    forecast: dict,
    strategy: dict,
    *,
    equity: float,
    cash: float | None = None,
    peak_equity: float | None = None,
    positions: list[dict] | None = None,
    shortable: bool = False,
) -> dict:
    """Produce an unfilled, simulation-only order plan using supplied marks.

    ``volatility`` and ``spread`` are decimal fractions in feature slots 6/7.
    The larger of observed and configured spread is charged. Spread half plus
    slippage applies on both sides; round-trip costs include two commissions
    and, for shorts, configured annual borrow over the strategy's horizon.
    Sizing budgets the stop distance plus full costs. With no cash supplied,
    existing gross exposure is conservatively deducted from equity. Shorts
    reserve full notional plus costs and never create spendable cash here.
    """
    if not isinstance(forecast, dict) or not isinstance(strategy, dict):
        raise ValueError("forecast and strategy must be dictionaries")
    settings = load_strategy()
    settings.update(strategy)
    validate_strategy(settings)
    account_equity = _positive(equity, "equity")
    peak = _positive(peak_equity, "peak_equity") if peak_equity is not None else account_equity
    peak = max(peak, account_equity)
    if not isinstance(shortable, bool):
        raise ValueError("shortable must be an explicit boolean")
    action = forecast.get("action")
    if action not in {"LONG", "SHORT", "PASS", "WATCH"}:
        raise ValueError("forecast action must be LONG, SHORT, PASS, or WATCH")
    ticker = symbol(forecast.get("symbol", ""))
    if not isinstance(forecast.get("id"), str) or not forecast["id"].strip():
        raise ValueError("forecast id must be nonempty")
    if forecast.get("mode") not in {"forward", "historical", "synthetic"}:
        raise ValueError("forecast mode must be forward, historical, or synthetic")
    features = forecast.get("features")
    if not isinstance(features, (list, tuple)) or len(features) != 8:
        raise ValueError("forecast features must contain eight finite numbers")
    features = [number(value, "feature") for value in features]
    volatility = number(features[6], "volatility", minimum=0)
    observed_spread = number(features[7], "spread", minimum=0)
    if observed_spread >= 1:
        raise ValueError("spread must be a decimal fraction below 1")
    market = forecast.get("market")
    if not isinstance(market, dict):
        raise ValueError("forecast market must contain price and dollar_volume")
    price = _positive(market.get("price"), "price")
    dollar_volume = number(market.get("dollar_volume"), "dollar_volume", minimum=0)
    expected_return = forecast.get("expected_return")
    if expected_return is not None:
        expected_return = number(expected_return, "expected_return")

    holdings = [] if positions is None else positions
    if not isinstance(holdings, list):
        raise ValueError("positions must be a list")
    held_symbols: set[str] = set()
    gross_exposure = short_exposure = 0.0
    for holding in holdings:
        if not isinstance(holding, dict):
            raise ValueError("each position must be a dictionary")
        held_symbol = symbol(holding.get("symbol", ""))
        if held_symbol in held_symbols:
            raise ValueError("existing positions must not contain duplicate symbols")
        held_symbols.add(held_symbol)
        if holding.get("side") not in {"LONG", "SHORT"}:
            raise ValueError("position side must be LONG or SHORT")
        notional = _positive(holding.get("quantity"), "position quantity") * _positive(
            holding.get("price"), "position price"
        )
        number(notional, "position notional", minimum=0)
        gross_exposure += notional
        if holding["side"] == "SHORT":
            short_exposure += notional
    number(gross_exposure, "gross exposure", minimum=0)
    available_cash = (
        max(account_equity - gross_exposure, 0.0)
        if cash is None
        else number(cash, "cash", minimum=0)
    )

    result = {
        "forecast_id": forecast["id"],
        "symbol": ticker,
        "mode": forecast["mode"],
        "action": "WATCH" if action == "WATCH" else "PASS",
        "quantity": 0,
        "reference_price": price,
        "estimated_entry_price": None,
        "stop_price": None,
        "estimated_round_trip_cost": 0.0,
        "planned_loss_at_stop": 0.0,
        "reasons": [],
        "simulation_only": True,
        "notes": [_NOTE],
    }
    if forecast["mode"] == "synthetic":
        result["notes"].append(
            "Synthetic demonstration; this plan is not economic performance evidence."
        )
    if action in {"PASS", "WATCH"}:
        result["reasons"].append(f"Forecast action is {action}; no paper order proposed.")
        return result

    reasons = result["reasons"]
    if forecast.get("quarantined") is True:
        # ADR-0001 D3 (#13): adversarial-looking source text never reaches a plan.
        reasons.append("Forecast is quarantined: its source text looked adversarial.")
        return result
    drawdown = (peak - account_equity) / peak
    if drawdown >= settings["pause_drawdown"] - 1e-12:
        reasons.append("Portfolio drawdown reached the pause threshold.")
    if ticker in held_symbols:
        reasons.append("A position in this symbol already exists.")
    if len(holdings) >= settings["max_positions"]:
        reasons.append("Maximum position count reached.")
    if short_exposure > account_equity * settings["max_short_fraction"]:
        reasons.append("Existing short exposure exceeds the configured maximum.")
    if price < settings["min_price"]:
        reasons.append("Price is below the configured minimum.")
    if dollar_volume < settings["min_dollar_volume"]:
        reasons.append("Dollar volume is below the configured liquidity minimum.")
    if features[3] > settings["max_uncertainty"]:
        reasons.append("Semantic uncertainty exceeds the configured maximum.")
    if action == "SHORT" and not settings["allow_short"]:
        reasons.append("Shorts are disabled by the strategy.")
    if action == "SHORT" and not shortable:
        reasons.append("Short availability was not explicitly confirmed.")
    if expected_return is None:
        reasons.append("No numerical expected return is available.")
    elif (action == "LONG" and expected_return <= 0) or (
        action == "SHORT" and expected_return >= 0
    ):
        reasons.append("Expected return does not support the requested direction.")
    if reasons:
        return result

    side = 1 if action == "LONG" else -1
    spread = max(observed_spread, settings["spread_bps"] / 10000)
    impact = spread / 2 + settings["slippage_bps_per_side"] / 10000
    if impact >= 1:
        raise ValueError("spread and slippage produce an invalid entry price")
    stop_fraction = max(
        settings["min_stop_fraction"], settings["stop_volatility_multiple"] * volatility
    )
    if stop_fraction >= 1:
        reasons.append("Stop distance is too large for this paper policy.")
        return result
    entry_price = _positive(price * (1 + side * impact), "estimated entry price")
    stop_price = _positive(price * (1 - side * stop_fraction), "stop price")
    borrow_fraction = (
        settings["short_borrow_bps_annual"] / 10000 * settings["horizon_sessions"] / 252
        if side == -1
        else 0.0
    )
    # Charge spread/slippage on both legs and borrow on reference notional.
    flat_cost_per_share = price * (2 * impact + borrow_fraction)
    cost_at_stop_per_share = (price + stop_price) * impact + price * borrow_fraction
    risk_per_share = price * stop_fraction + max(flat_cost_per_share, cost_at_stop_per_share)
    commission = settings["commission_per_order"] * 2
    risk_budget = account_equity * settings["risk_per_trade"]
    gross_room = max(account_equity * settings["max_gross_fraction"] - gross_exposure, 0.0)
    short_room = max(account_equity * settings["max_short_fraction"] - short_exposure, 0.0)
    # Use the greater of mark and estimated entry for exposure limits.
    exposure_per_share = max(price, entry_price)
    capacities = {
        "risk budget": max(risk_budget - commission, 0.0) / risk_per_share,
        "position limit": account_equity * settings["max_position_fraction"] / exposure_per_share,
        "gross exposure limit": gross_room / exposure_per_share,
        "cash reserve": max(available_cash - commission, 0.0) / (price + flat_cost_per_share),
    }
    if side == -1:
        capacities["short exposure limit"] = short_room / exposure_per_share
    quantity = math.floor(min(capacities.values()))
    if quantity < 1:
        binding = [name for name, capacity in capacities.items() if capacity < 1]
        reasons.append("Insufficient whole-share capacity under " + ", ".join(binding) + ".")
        return result
    # Narrowing only: a None expected return already returned through `reasons`.
    assert expected_return is not None
    total_cost = quantity * flat_cost_per_share + commission
    expected_pnl = quantity * price * abs(expected_return)
    required_edge = quantity * price * settings["min_edge_bps"] / 10000
    if expected_pnl - total_cost <= required_edge:
        reasons.append("Expected edge does not cover full costs plus the minimum edge requirement.")
        return result
    planned_loss = quantity * risk_per_share + commission
    result.update(
        {
            "action": action,
            "quantity": quantity,
            "estimated_entry_price": entry_price,
            "stop_price": stop_price,
            "estimated_round_trip_cost": total_cost,
            "planned_loss_at_stop": planned_loss,
            "risk_budget": risk_budget,
            "stop_fraction": stop_fraction,
            "assumed_spread_fraction": spread,
            "cash_reserved": quantity * price + total_cost,
            "expected_net_benchmark_relative_dollars": expected_pnl - total_cost,
        }
    )
    reasons.append(
        "Whole-share paper plan satisfies the supplied risk, exposure, cash, and cost limits."
    )
    return result
