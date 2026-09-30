"""Deterministic, deliberately learnable synthetic data. Never evidence of alpha."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from .common import load_strategy, timestamp
from .engine import evaluate, observe, settle, train
from .market import normalize_bar
from .paper import plan_order


def run_demo(ledger) -> dict:
    if ledger.counts():
        raise ValueError("Demo requires a new, empty ledger; existing records were preserved")
    strategy = load_strategy()
    rng = random.Random(17)
    sessions: list[datetime] = []
    day = datetime(2022, 1, 3, tzinfo=timezone.utc)
    while len(sessions) < 360:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)
    symbols = ["SIMAA", "SIMBB", "SIMCC"]
    events = []
    effects = {name: [0.0] * len(sessions) for name in symbols}
    for j, name in enumerate(symbols):
        for k, i in enumerate(range(30, 335, 12)):
            sign = 1 if (k + j) % 2 == 0 else -1
            words = (
                "Strong demand raised guidance revenue increased improved margins new contract. "
                "Customers accelerated purchases delivery volumes expanded factory utilization."
                if sign > 0
                else "Weak demand lowered guidance revenue declined declining margins contract terminated. "
                "Cancellations disrupted production layoffs closed facilities inventories obsolete."
            )
            at = timestamp((sessions[i] + timedelta(hours=21, minutes=5)).isoformat())
            event = {
                "id": f"synthetic-{name}-{i}",
                "symbol": name,
                "published_at": at,
                "first_seen_at": at,
                "mode": "synthetic",
                "source_type": "synthetic_fixture",
                "source_url": "synthetic://demo",
                "text": words + f" Simulated operating update number {k}.",
            }
            ledger.disclosure(event)
            events.append(event)
            for future in range(i + 1, min(i + 11, len(sessions))):
                effects[name][future] += sign * 0.003
    market_returns = [rng.gauss(0.0001, 0.003) for _ in sessions]
    for name in [strategy["benchmark"], *symbols]:
        previous = 100.0 if name == strategy["benchmark"] else 40.0
        for i, session in enumerate(sessions):
            opening = previous * math.exp(rng.gauss(0, 0.0005))
            change = market_returns[i] + (
                effects[name][i] + rng.gauss(0, 0.001) if name in effects else 0
            )
            close = opening * math.exp(change)
            bar = normalize_bar(
                {
                    "symbol": name,
                    "session": session.date().isoformat(),
                    "open_at": (session + timedelta(hours=14, minutes=30)).isoformat(),
                    "close_at": (session + timedelta(hours=21)).isoformat(),
                    "open": opening,
                    "high": max(opening, close) * 1.003,
                    "low": min(opening, close) * 0.997,
                    "close": close,
                    "volume": 1_000_000,
                },
                mode="synthetic",
            )
            ledger.put("bars", bar["id"], bar)
            previous = close
    observations = [
        observe(ledger, e["id"], strategy, as_of=e["first_seen_at"])
        for e in sorted(events, key=lambda e: (e["first_seen_at"], e["id"]))
    ]
    final = timestamp((sessions[-1] + timedelta(days=1)).isoformat())
    settlement = settle(ledger, as_of=final)
    key = observations[0]["extractor_key"]
    cutoff = timestamp((sessions[280] + timedelta(hours=22)).isoformat())
    fitted = train(ledger, key, cutoff, strategy)
    forecasts = [
        observe(
            ledger, e["id"], strategy, as_of=e["first_seen_at"], calibrator_id=fitted["model_id"]
        )
        for e in events
        if e["first_seen_at"] > cutoff
    ]
    settle(ledger, as_of=final)
    plans = [plan_order(f, strategy, equity=3000) for f in forecasts]
    candidate = next((p for p in plans if p["quantity"] > 0), plans[0] if plans else None)
    report = evaluate(ledger, key, strategy)
    return {
        "mode": "synthetic",
        "warning": "Artificial signal was injected. Results do not establish alpha.",
        "counts": ledger.counts(),
        "extractor_key": key,
        "calibrator_id": fitted["model_id"],
        "settled_observations": settlement["added"],
        "training_events": fitted["training_count"],
        "evaluation": {
            key: report[key] for key in ("evaluated_count", "strategies", "limitations")
        },
        "paper_plan_example": candidate,
    }
