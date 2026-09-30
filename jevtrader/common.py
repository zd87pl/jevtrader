"""Small validation and serialization helpers shared by the CLI and ledger."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

# US equity sessions and EDGAR dates are defined in New York time.
EASTERN = ZoneInfo("America/New_York")

# The documented minimum for each cost the gate nets (R2, invariant I-2; ADR-0002). A floor
# is a sanity bound, not an estimate: the default strategy charges 4 to 12 times these. A
# strategy below any of them is rejected, and a forecast frozen with one never counts.
# Moving a floor changes what may count as evidence, so it needs an ADR.
COST_FLOOR: dict[str, float] = {
    "spread_bps": 2.0,  # quoted spread, charged once per round trip
    "slippage_bps_per_side": 1.0,  # charged on entry and on exit
    "short_borrow_bps_annual": 25.0,  # general-collateral borrow, shorts only
    "min_edge_bps": 5.0,  # required margin above costs before a call is made
}


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
        result = float(cast(Any, value))
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


def sec_symbol(value: str) -> str:
    """A symbol in SEC's share-class form (BRK-B), which users and brokers write as BRK.B."""
    return symbol(value).replace(".", "-")


def ledger_file(ledger: object) -> Path | None:
    """The resolved file behind a ledger's main database; None for memory or fakes."""
    connection = getattr(ledger, "db", None)
    if connection is None:
        return None
    for _, name, location in connection.execute("PRAGMA database_list").fetchall():
        if name == "main" and location:
            return Path(location).resolve()
    return None


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
    for key, floor in COST_FLOOR.items():
        if data[key] < floor:
            raise ValueError(f"{key} is below the documented cost floor of {floor:g} (ADR-0002)")


def meets_cost_floor(strategy: object) -> bool:
    """True only when every floored cost is a finite number at or above its floor.

    It reads frozen records, so anything it cannot prove (a missing strategy or cost, text,
    a boolean, NaN) fails: a call without provable costs never counts (R2).
    """
    if not isinstance(strategy, dict):
        return False
    for key, floor in COST_FLOOR.items():
        value = strategy.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not math.isfinite(value) or value < floor:
            return False
    return True


def round_trip_bps(strategy: dict, *, short: bool = False) -> float:
    cost = strategy["spread_bps"] + 2 * strategy["slippage_bps_per_side"]
    if short:
        cost += strategy["short_borrow_bps_annual"] * strategy["horizon_sessions"] / 252
    return float(cost)
