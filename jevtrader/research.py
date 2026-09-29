"""Small, auditable event-return research; no portfolio or execution simulation.

Rows describe *completed* event outcomes. ``fit_model`` rejects future labels
rather than silently filtering them. ``walk_forward`` selects only labels whose
outcome time is strictly earlier than each test block's first decision time.
Overlapping test events are retained as event observations, never compounded
into an equity curve or treated as independent evidence of portfolio alpha.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from numbers import Real
from statistics import mean, median
from typing import Any

import numpy as np


FEATURE_NAMES = (
    "direction",
    "materiality",
    "novelty",
    "uncertainty",
    "reaction",
    "momentum",
    "volatility",
    "spread",
)
VERSION = "ridge-event-v2"
TARGET = "future_benchmark_relative_return"


def _time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a UTC ISO timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a UTC ISO timestamp") from exc
    offset = result.utcoffset()  # None exactly when the value is naive
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{name} must have a UTC offset")
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _features(values: Any) -> list[float]:
    if not isinstance(values, (list, tuple)) or len(values) != len(FEATURE_NAMES):
        raise ValueError(f"features must contain {len(FEATURE_NAMES)} values")
    return [_number(value, "feature") for value in values]


def _rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("rows must be a nonempty list")
    clean, seen, extractor_keys = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each row must be a dictionary")
        for key in ("event_id", "symbol", "extractor_key", "mode"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"{key} must be a nonempty string")
        if row["mode"] not in {"forward", "historical", "synthetic"}:
            raise ValueError("mode must be forward, historical, or synthetic")
        if row["event_id"] in seen:
            raise ValueError(f"duplicate event_id: {row['event_id']}")
        seen.add(row["event_id"])
        extractor_keys.add(row["extractor_key"])
        decision = _time(row.get("decision_at"), "decision_at")
        outcome = _time(row.get("outcome_at"), "outcome_at")
        if outcome <= decision:
            raise ValueError("outcome_at must be later than decision_at")
        clean.append(
            {
                "event_id": row["event_id"],
                "symbol": row["symbol"],
                "decision_at": _iso(decision),
                "outcome_at": _iso(outcome),
                "features": _features(row.get("features")),
                "target": _number(row.get("target"), "target"),
                "extractor_key": row["extractor_key"],
                "mode": row["mode"],
            }
        )
    if len(extractor_keys) != 1:
        raise ValueError("mixed extractor_key values are not comparable")
    return sorted(
        clean, key=lambda row: (_time(row["decision_at"], "decision_at"), row["event_id"])
    )


def _fit(
    rows: list[dict[str, Any]],
    *,
    cutoff: str,
    alpha: float,
    min_samples: int,
    feature_indices: tuple[int, ...],
) -> dict[str, Any]:
    boundary = _time(cutoff, "cutoff")
    strength = _number(alpha, "alpha")
    if strength <= 0:
        raise ValueError("alpha must be positive")
    _positive_int(min_samples, "min_samples")
    if len(rows) < min_samples:
        raise ValueError(f"need at least {min_samples} training samples")
    if any(_time(row["outcome_at"], "outcome_at") >= boundary for row in rows):
        raise ValueError("every training outcome_at must be strictly before cutoff")

    x = np.asarray([row["features"] for row in rows], dtype=float)
    y = np.asarray([row["target"] for row in rows], dtype=float)
    average = x.mean(axis=0)
    scale = x.std(axis=0)
    # A constant column's mean is often inexact (30 x 0.8 gives std ~2e-16); scaling by
    # that rounding noise would multiply any later deviation by ~1e15.
    scale[scale <= 1e-9 * np.maximum(1.0, np.abs(average))] = 1.0
    z = ((x - average) / scale)[:, feature_indices]
    intercept = float(y.mean())
    partial = np.linalg.solve(
        z.T @ z + strength * np.eye(len(feature_indices)),
        z.T @ (y - intercept),
    )
    coefficients = np.zeros(len(FEATURE_NAMES))
    coefficients[list(feature_indices)] = partial
    if not all(
        np.isfinite(value).all() for value in (average, scale, coefficients)
    ) or not math.isfinite(intercept):
        raise ValueError("training values exceed stable numerical range")
    config = {
        "version": VERSION,
        "feature_names": list(FEATURE_NAMES),
        "feature_indices": list(feature_indices),
        "target": TARGET,
        "cutoff": _iso(boundary),
        "alpha": strength,
        "min_samples": min_samples,
    }
    identity = json.dumps(
        {"config": config, "rows": rows}, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return {
        **config,
        "coefficients": coefficients.tolist(),
        "intercept": intercept,
        "mean": average.tolist(),
        "scale": scale.tolist(),
        "training_count": len(rows),
        "training_start": _iso(min(_time(row["decision_at"], "decision_at") for row in rows)),
        "training_end": _iso(max(_time(row["outcome_at"], "outcome_at") for row in rows)),
        "extractor_key": rows[0]["extractor_key"],
        "training_event_ids": [row["event_id"] for row in rows],
        "training_modes": sorted({row["mode"] for row in rows}),
        "model_id": hashlib.sha256(identity.encode()).hexdigest(),
    }


def fit_model(
    rows: list[dict[str, Any]],
    *,
    cutoff: str,
    alpha: float = 10.0,
    min_samples: int = 30,
) -> dict[str, Any]:
    """Fit ridge on standardized features, with an unpenalized intercept.

    All supplied rows must have outcomes strictly before ``cutoff``. Duplicate
    IDs, incompatible extractors, nonfinite numbers and immature labels fail
    closed. Identity includes exact normalized training rows and configuration.
    """
    return _fit(
        _rows(rows),
        cutoff=cutoff,
        alpha=alpha,
        min_samples=min_samples,
        feature_indices=tuple(range(len(FEATURE_NAMES))),
    )


def predict(model: dict[str, Any], features: list[float]) -> float:
    """Predict benchmark-relative event return in decimal units."""
    if not isinstance(model, dict) or model.get("feature_names") != list(FEATURE_NAMES):
        raise ValueError("model feature_names do not match the research schema")
    x = np.asarray(_features(features))
    coefficients = np.asarray(_features(model.get("coefficients")))
    average = np.asarray(_features(model.get("mean")))
    scale = np.asarray(_features(model.get("scale")))
    if np.any(scale <= 0):
        raise ValueError("model scale must be positive")
    result = float(
        np.dot((x - average) / scale, coefficients) + _number(model.get("intercept"), "intercept")
    )
    if not math.isfinite(result):
        raise ValueError("prediction exceeds stable numerical range")
    return result


def _action(prediction: float, threshold: float, allow_short: bool) -> int:
    if prediction > threshold:
        return 1
    if allow_short and prediction < -threshold:
        return -1
    return 0


def _observation(
    row: dict[str, Any], action: int, cost: float, prediction: float | None
) -> dict[str, Any]:
    return {
        "prediction": prediction,
        "action": action,
        "gross_return": action * row["target"] if action else 0.0,
        "cost": cost if action else 0.0,
        "net_return": action * row["target"] - cost if action else 0.0,
    }


def _summary(predictions: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    observations = [item[strategy] for item in predictions]
    trades = [item["net_return"] for item in observations if item["action"]]
    all_returns = [item["net_return"] for item in observations]
    return {
        "sample_count": len(observations),
        "trade_count": len(trades),
        "long_count": sum(item["action"] == 1 for item in observations),
        "short_count": sum(item["action"] == -1 for item in observations),
        "mean_net_return": mean(trades) if trades else None,
        "median_net_return": median(trades) if trades else None,
        "win_rate": sum(value > 0 for value in trades) / len(trades) if trades else None,
        "mean_net_return_per_opportunity": mean(all_returns) if all_returns else None,
    }


def walk_forward(
    rows: list[dict[str, Any]],
    *,
    min_train: int = 30,
    test_size: int = 10,
    alpha: float = 10.0,
    cost_bps: float = 20,
    min_edge_bps: float = 10,
    allow_short: bool = False,
) -> dict[str, Any]:
    """Expanding, purged chronological event evaluation with matched baselines.

    Models are frozen for each block. Equal decision timestamps are never split
    across blocks; ``test_size`` is a minimum except for the final block. Rows
    with outcomes at/after the block cutoff are excluded from training, even
    when their decisions precede the cutoff. Later blocks can train on matured
    earlier test outcomes. ``direction`` is a simple semantic-sign baseline.
    Fixed costs are round-trip bps per traded event. Short results omit borrow
    and locate availability and are explicitly conditional.
    """
    clean = _rows(rows)
    _positive_int(min_train, "min_train")
    _positive_int(test_size, "test_size")
    strength = _number(alpha, "alpha")
    if strength <= 0:
        raise ValueError("alpha must be positive")
    cost = _number(cost_bps, "cost_bps") / 10000
    edge = _number(min_edge_bps, "min_edge_bps") / 10000
    if cost < 0 or edge < 0:
        raise ValueError("cost_bps and min_edge_bps must be nonnegative")
    if not isinstance(allow_short, bool):
        raise ValueError("allow_short must be boolean")
    predictions, blocks, skipped = [], [], []
    position = 0
    while position < len(clean):
        cutoff = clean[position]["decision_at"]
        cutoff_time = _time(cutoff, "decision_at")
        prior = clean[:position]
        training = [row for row in prior if _time(row["outcome_at"], "outcome_at") < cutoff_time]
        if len(training) < min_train:
            while (
                position < len(clean)
                and _time(clean[position]["decision_at"], "decision_at") == cutoff_time
            ):
                skipped.append(clean[position]["event_id"])
                position += 1
            continue
        end = min(position + test_size, len(clean))
        while end < len(clean) and clean[end]["decision_at"] == clean[end - 1]["decision_at"]:
            end += 1
        model = _fit(
            training,
            cutoff=cutoff,
            alpha=strength,
            min_samples=min_train,
            feature_indices=tuple(range(len(FEATURE_NAMES))),
        )
        numerical = _fit(
            training,
            cutoff=cutoff,
            alpha=strength,
            min_samples=min_train,
            feature_indices=(4, 5, 6, 7),
        )
        test = clean[position:end]
        blocks.append(
            {
                "cutoff": cutoff,
                "model_id": model["model_id"],
                "numerical_model_id": numerical["model_id"],
                "training_count": len(training),
                "purged_count": len(prior) - len(training),
                "training_event_ids": model["training_event_ids"],
                "training_end": model["training_end"],
                "test_event_ids": [row["event_id"] for row in test],
                "test_start": test[0]["decision_at"],
                "test_end": test[-1]["decision_at"],
            }
        )
        for row in test:
            forecast = predict(model, row["features"])
            numerical_forecast = predict(numerical, row["features"])
            direction_action = (
                1
                if row["features"][0] > 0
                else (-1 if allow_short and row["features"][0] < 0 else 0)
            )
            predictions.append(
                {
                    "event_id": row["event_id"],
                    "symbol": row["symbol"],
                    "decision_at": row["decision_at"],
                    "outcome_at": row["outcome_at"],
                    "target": row["target"],
                    "model_id": model["model_id"],
                    "semantic": _observation(
                        row, _action(forecast, cost + edge, allow_short), cost, forecast
                    ),
                    "numerical": _observation(
                        row,
                        _action(numerical_forecast, cost + edge, allow_short),
                        cost,
                        numerical_forecast,
                    ),
                    "direction": _observation(row, direction_action, cost, None),
                    "all_long": _observation(row, 1, cost, None),
                }
            )
        position = end
    coverage = {
        "first_decision_at": predictions[0]["decision_at"] if predictions else None,
        "last_decision_at": predictions[-1]["decision_at"] if predictions else None,
        "last_outcome_at": _iso(max(_time(row["outcome_at"], "outcome_at") for row in predictions))
        if predictions
        else None,
    }
    return {
        "version": VERSION,
        "target": TARGET,
        "config": {
            "min_train": min_train,
            "test_size": test_size,
            "alpha": strength,
            "cost_bps": cost_bps,
            "min_edge_bps": min_edge_bps,
            "allow_short": allow_short,
        },
        "input_count": len(clean),
        "evaluated_count": len(predictions),
        "skipped_event_ids": skipped,
        "extractor_key": clean[0]["extractor_key"],
        "coverage": coverage,
        "blocks": blocks,
        "predictions": predictions,
        "strategies": {
            name: _summary(predictions, name)
            for name in ("semantic", "numerical", "direction", "all_long")
        },
        "limitations": [
            "Event-level benchmark-relative returns; not portfolio returns, Sharpe, or an equity curve.",
            "Overlapping outcomes and shared market exposures make observations dependent.",
            "Fixed round-trip costs omit execution capacity, variable spreads, financing, taxes, and missed fills.",
            "Historical LLM features may contain pretrained future knowledge; forward frozen collection is required.",
        ]
        + (
            [
                "Short results are conditional on borrow/locate availability and omit borrow fees and recalls."
            ]
            if allow_short
            else []
        ),
    }
