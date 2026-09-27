"""Small validation and serialization helpers shared by the CLI and ledger."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an ISO 8601 string with an explicit timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp needs an explicit timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def timestamp(value: str) -> str:
    return instant(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def number(value: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"Invalid {name}: {value!r}")
    return result


def symbol(value: str) -> str:
    result = str(value).upper().strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", result):
        raise ValueError(f"Invalid symbol: {value!r}")
    return result


def load_strategy(path: str | Path | None = None) -> dict:
    data = json.loads(
        Path(path).read_text()
        if path
        else files("jevtrader").joinpath("default_strategy.json").read_text()
    )
    validate_strategy(data)
    return data


def validate_strategy(data: dict) -> None:
    defaults = json.loads(files("jevtrader").joinpath("default_strategy.json").read_text())
    if not isinstance(data, dict) or set(data) != set(defaults):
        raise ValueError("Strategy must contain exactly the documented configuration fields")
    # At least as strict as the provider request checks (nonblank name and questions), so
    # a strategy that validates here cannot be rejected only after budget is reserved.
    if (
        type(data["version"]) is not int
        or data["version"] != 1
        or not isinstance(data["name"], str)
        or not data["name"].strip()
        or len(data["name"]) > 200
    ):
        raise ValueError("Strategy version must be 1 and name must have 1–200 characters")
    if not isinstance(data["questions"], dict) or set(data["questions"]) != {
        "direction",
        "materiality",
        "novelty",
    }:
        raise ValueError("Strategy requires direction, materiality, novelty questions")
    for question in data["questions"].values():
        if not isinstance(question, str) or not 10 <= len(question) <= 4000 or not question.strip():
            raise ValueError("Questions must contain 10–4000 characters of text")
    symbol(data["benchmark"])
    if type(data["allow_short"]) is not bool:
        raise ValueError("allow_short must be boolean")
    for key, default in defaults.items():
        if key == "version" or isinstance(default, bool):
            continue
        if isinstance(default, (float, int)):
            if type(data[key]) not in (int, float):
                raise ValueError(f"{key} must be a number")
            val = number(data[key], key, minimum=0)
            if isinstance(default, int) and (type(data[key]) is not int or val < 1):
                raise ValueError(f"{key} must be a positive integer")
    for key in (
        "risk_per_trade",
        "max_position_fraction",
        "max_gross_fraction",
        "max_short_fraction",
        "pause_drawdown",
        "min_stop_fraction",
        "max_uncertainty",
    ):
        if not 0 < data[key] <= 1:
            raise ValueError(f"{key} must be in (0, 1]")
    if data["min_history_sessions"] < 21 or data["min_train_samples"] < 10:
        raise ValueError("Require at least 21 history sessions and 10 training events")
    if data["ridge_alpha"] <= 0 or data["max_market_age_hours"] <= 0:
        raise ValueError("ridge_alpha and max_market_age_hours must be positive")


def round_trip_bps(strategy: dict, *, short: bool = False) -> float:
    cost = strategy["spread_bps"] + 2 * strategy["slippage_bps_per_side"]
    if short:
        cost += strategy["short_borrow_bps_annual"] * strategy["horizon_sessions"] / 252
    return float(cost)
