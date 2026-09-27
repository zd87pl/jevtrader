"""Explicit session bars, causal features, and next-open event labels.

Prices are raw, not adjusted. split_ratio is new shares per prior share at the
session open; cash_dividend is per post-split share on its ex-date. Labels use
total returns, crediting dividends only when held before the ex-date open.
"""

from __future__ import annotations

import csv
import math
from datetime import date
from statistics import pstdev
from zoneinfo import ZoneInfo

from .common import instant, number, symbol, timestamp, utc_now


def normalize_bar(row: dict, *, mode: str = "historical") -> dict:
    if mode not in {"forward", "historical", "synthetic"}:
        raise ValueError("Invalid bar mode")
    result = {"symbol": symbol(row.get("symbol", "")), "mode": mode}
    result["session"] = date.fromisoformat(row["session"]).isoformat()
    for key in ("open_at", "close_at"):
        result[key] = timestamp(row[key])
    if instant(result["open_at"]) >= instant(result["close_at"]):
        raise ValueError("Bar open_at must precede close_at")
    if instant(result["close_at"]).date().isoformat() != result["session"]:
        raise ValueError("US stock session must match close_at UTC date")
    if any(
        instant(result[key]).astimezone(ZoneInfo("America/New_York")).date().isoformat()
        != result["session"]
        for key in ("open_at", "close_at")
    ):
        raise ValueError("Open and close must belong to the same US stock session")
    for key in ("open", "high", "low", "close", "volume"):
        result[key] = number(row[key], key, minimum=0)
        if key != "volume" and result[key] <= 0:
            raise ValueError("OHLC prices must be positive")
    if not (
        result["low"]
        <= min(result["open"], result["close"])
        <= max(result["open"], result["close"])
        <= result["high"]
    ):
        raise ValueError("Inconsistent OHLC range")
    for key, default in (("split_ratio", 1), ("cash_dividend", 0)):
        value = row.get(key)
        result[key] = number(default if value is None or value == "" else value, key, minimum=0)
    if result["split_ratio"] <= 0:
        raise ValueError("split_ratio must be positive")
    # Actual receipt time is mandatory for forward use. Replayed bars stay labeled historical.
    result["available_at"] = (
        utc_now() if mode == "forward" else timestamp(row.get("available_at") or result["close_at"])
    )
    if instant(result["available_at"]) < instant(result["close_at"]):
        raise ValueError("A completed bar cannot be available before close")
    result["id"] = f"{result['symbol']}:{result['session']}"
    return result


def import_bars(ledger, path: str, *, mode: str = "historical") -> int:
    count = 0
    with open(path, newline="", encoding="utf-8") as source:
        rows = [normalize_bar(row, mode=mode) for row in csv.DictReader(source)]
    for row in rows:
        existing = ledger.get("bars", row["id"])
        if existing and mode == "forward":
            row["available_at"] = existing["available_at"]
        count += ledger.put("bars", row["id"], row)
    return count


def bars_for(ledger, ticker: str) -> list[dict]:
    return sorted(ledger.prefix("bars", f"{ticker}:"), key=lambda b: b["open_at"])


def _compatible(bar: dict, mode: str) -> bool:
    return bar["mode"] == mode if mode in {"forward", "synthetic"} else bar["mode"] != "synthetic"


def _daily(previous: dict, current: dict) -> float:
    return (current["close"] + current["cash_dividend"]) * current["split_ratio"] / previous[
        "close"
    ] - 1


def snapshot(ledger, ticker: str, decision_at: str, strategy: dict, *, mode: str) -> dict:
    boundary = instant(decision_at)

    def visible(name):
        return [
            b
            for b in bars_for(ledger, name)
            if instant(b["available_at"]) <= boundary
            and instant(b["close_at"]) <= boundary
            and _compatible(b, mode)
        ]

    stock, benchmark = visible(ticker), visible(strategy["benchmark"])
    needed = strategy["min_history_sessions"]
    if len(stock) < needed or len(benchmark) < needed:
        raise ValueError(f"Need {needed} visible stock and benchmark bars (matching mode)")
    stock, benchmark = stock[-needed:], benchmark[-needed:]
    if [b["session"] for b in stock] != [b["session"] for b in benchmark]:
        raise ValueError("Stock and benchmark history sessions do not align; missing data")
    age = (boundary - instant(stock[-1]["close_at"])).total_seconds() / 3600
    if age > strategy["max_market_age_hours"]:
        raise ValueError("Market data is stale")
    returns = [_daily(a, b) for a, b in zip(stock, stock[1:])]
    momentum = math.prod(1 + value for value in returns[-20:]) - 1
    return {
        "price": stock[-1]["close"],
        "session": stock[-1]["session"],
        "close_at": stock[-1]["close_at"],
        "dollar_volume": sum(b["close"] * b["volume"] for b in stock[-20:]) / 20,
        "reaction": returns[-1] - _daily(benchmark[-2], benchmark[-1]),
        "momentum": momentum,
        "volatility": pstdev(returns[-20:]),
        "spread": strategy["spread_bps"] / 10000,
        "bar_ids": [b["id"] for b in stock + benchmark],
        "spread_basis": "configured assumption, not a quote",
    }


def _holding_return(bars: list[dict]) -> float:
    shares, dividends = 1.0, 0.0
    for bar in bars[1:]:
        shares *= bar["split_ratio"]
        dividends += shares * bar["cash_dividend"]
    return (bars[-1]["close"] * shares + dividends) / bars[0]["open"] - 1


def outcome(ledger, forecast: dict, as_of: str) -> dict | None:
    boundary, decision = instant(as_of), instant(forecast["decision_at"])
    strategy = forecast["strategy"]
    mode = forecast.get("mode", "historical")
    stock_all = [
        b
        for b in bars_for(ledger, forecast["symbol"])
        if _compatible(b, mode) and instant(b["open_at"]) > decision
    ]
    benchmark = [
        b
        for b in bars_for(ledger, strategy["benchmark"])
        if instant(b["open_at"]) > decision and _compatible(b, mode)
    ]
    horizon = strategy["horizon_sessions"]
    if len(benchmark) < horizon or len(stock_all) < horizon:
        return None
    benchmark = benchmark[:horizon]
    if [b["session"] for b in stock_all[:horizon]] != [b["session"] for b in benchmark]:
        return None
    by_session = {b["session"]: b for b in stock_all}
    if any(b["session"] not in by_session for b in benchmark):
        return None  # Report unresolved; never silently shorten the holding period.
    stock = [by_session[b["session"]] for b in benchmark]
    if any(
        instant(b["available_at"]) > boundary or instant(b["close_at"]) > boundary
        for b in stock + benchmark
    ):
        return None
    if any(instant(b["open_at"]) <= decision for b in stock):
        raise ValueError("Stock entry does not follow the frozen decision")
    gross, bench = _holding_return(stock), _holding_return(benchmark)
    return {
        "forecast_id": forecast["id"],
        "event_id": forecast["event_id"],
        "entry_at": stock[0]["open_at"],
        "outcome_at": stock[-1]["close_at"],
        "label_available_at": max(b["available_at"] for b in stock + benchmark),
        "entry_price": stock[0]["open"],
        "exit_price": stock[-1]["close"],
        "gross_return": gross,
        "benchmark_return": bench,
        "target": gross - bench,
        "horizon_sessions": horizon,
        "bar_ids": [b["id"] for b in stock + benchmark],
        "label_basis": "next open to horizon close, split/dividend-aware raw prices",
    }
