"""Evidence scoreboard: every number is computed here from records visible at ``as_of``.

A forecast counts only when its registry label is evidence (records older than the label:
forward mode only); synthetic records never count. Each event contributes one decision. An
event decided forward uses only its forward forecasts (the first LONG/SHORT call recorded,
else the first recorded forecast): a replay, recorded when the outcome may already be known,
never replaces a forward observation. An event with replays only uses its first recorded
replay, so re-scoring cannot swap in a better call. A call is scored only once its label was
available by ``as_of``. The gate is fixed and fingerprinted so a reader can tell if
thresholds moved.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable
from datetime import datetime
from statistics import fmean, stdev

from .common import EASTERN, digest, instant, round_trip_bps, timestamp
from .registry import counts_as_evidence

GATE = {"version": 1, "min_matured_calls": 100, "confidence": 0.90, "futility_upper_bps": 10.0}
ACTIONS = ("WATCH", "PASS", "LONG", "SHORT")
CALL_SIGNS = {"LONG": 1, "SHORT": -1}
STATUSES = ("collecting", "inconclusive", "supported", "no_edge")
METHOD = (
    "Student t interval on per-decision-date mean net returns (decision dates in New York "
    "time), so calls made on the same day count once; net = sign x target - round-trip cost"
)
ELIGIBILITY_RULE = (
    "registry evidence labels (forward, post_cutoff, no_model_knowledge); unlabelled "
    "forecasts count only in forward mode; synthetic never counts"
)


def is_evidence(forecast: dict) -> bool:
    if forecast.get("mode") == "synthetic":
        return False
    if "eligibility" in forecast:
        return counts_as_evidence(forecast["eligibility"])
    return forecast.get("mode") == "forward"


def evidence_label(forecast: dict) -> str:
    """Display label; unlabelled replays say so instead of borrowing a registry label."""
    label = forecast.get("eligibility")
    if isinstance(label, str) and label:
        return label
    mode = forecast.get("mode")
    return "forward" if mode == "forward" else f"{mode}_unlabelled"


def label_available_at(outcome: dict) -> datetime:
    return max(instant(outcome["outcome_at"]), instant(outcome["label_available_at"]))


def net_return(forecast: dict, outcome: dict) -> float | None:
    """Benchmark-relative return after round-trip cost for a call; None for WATCH/PASS."""
    action = forecast.get("action")
    sign = CALL_SIGNS.get(action) if isinstance(action, str) else None
    return None if sign is None else _net(forecast, outcome, sign)


def _net(forecast: dict, outcome: dict, sign: int) -> float:
    cost = round_trip_bps(forecast["strategy"], short=sign < 0) / 10_000
    return sign * float(outcome["target"]) - cost


def t_quantile(p: float, df: int) -> float:
    """Student t quantile by bisection on the exact CDF; numpy has none and scipy is not allowed."""
    if type(df) is not int or df < 1:
        raise ValueError("t quantile needs a positive integer degrees of freedom")
    if not 0 < p < 1:
        raise ValueError("t quantile probability must be in (0, 1)")
    if p < 0.5:
        return -t_quantile(1 - p, df)
    if p == 0.5:
        return 0.0
    low, high = 0.0, 1.0
    while _t_cdf(high, df) < p:
        high *= 2
    for _ in range(200):
        middle = (low + high) / 2
        if _t_cdf(middle, df) < p:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _t_cdf(value: float, df: int) -> float:
    tail = 0.5 * _incomplete_beta(df / 2, 0.5, df / (df + value * value))
    return 1 - tail if value >= 0 else tail


def _incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1) / (a + b + 2):
        return front * _beta_fraction(a, b, x) / a
    return 1 - front * _beta_fraction(b, a, 1 - x) / b


def _beta_fraction(a: float, b: float, x: float) -> float:
    """Lentz continued fraction for the regularized incomplete beta function."""
    tiny = 1e-300

    def guard(value: float) -> float:
        return tiny if abs(value) < tiny else value

    c, d = 1.0, 1 / guard(1 - (a + b) * x / (a + 1))
    result = d
    for m in range(1, 500):
        even = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        d = 1 / guard(1 + even * d)
        c = guard(1 + even / c)
        result *= d * c
        odd = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1 / guard(1 + odd * d)
        c = guard(1 + odd / c)
        result *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return result


def _order(forecast: dict) -> tuple:
    recorded = forecast.get("recorded_at") or forecast["decision_at"]
    return instant(recorded), instant(forecast["decision_at"]), forecast["id"]


def decisions(forecasts: list[dict]) -> list[dict]:
    """One decision per event: among its forward forecasts the first recorded call, else the
    first recorded one; without a forward forecast, its first recorded replay."""
    by_event: dict[str, list[dict]] = {}
    for forecast in forecasts:
        by_event.setdefault(forecast["event_id"], []).append(forecast)
    chosen = []
    for group in by_event.values():
        forward = [f for f in group if f.get("mode") == "forward"]
        if forward:
            # Each forward forecast was decided live, so a later forward call used no hindsight.
            calls = [f for f in forward if f.get("action") in CALL_SIGNS]
            chosen.append(min(calls or forward, key=_order))
        else:
            chosen.append(min(group, key=_order))
    return sorted(chosen, key=lambda f: (instant(f["decision_at"]), f["id"]))


def _interval(by_date: dict[str, list[float]], confidence: float) -> dict | None:
    means = [fmean(values) for _, values in sorted(by_date.items())]
    if len(means) < 2:
        return None
    center = fmean(means)
    spread = stdev(means) / math.sqrt(len(means))
    width = t_quantile(0.5 + confidence / 2, len(means) - 1) * spread
    return {
        "low": center - width,
        "high": center + width,
        "confidence": confidence,
        "dates": len(means),
        "method": METHOD,
    }


def _status(calls: int, interval: dict | None, gate: dict) -> str:
    if calls == 0 or calls < gate["min_matured_calls"]:
        return "collecting"
    if interval is None:
        return "inconclusive"
    if interval["low"] > 0:
        return "supported"
    if interval["high"] < gate["futility_upper_bps"] / 10_000:
        return "no_edge"
    return "inconclusive"


def label(board: dict) -> str:
    """One calm sentence built from the board's own numbers."""
    status, calls, gate = board["status"], board["calls"], board["gate"]
    interval = board["interval"]
    level = f"{gate['confidence']:.0%}"
    if status == "collecting":
        return f"Collecting evidence: {calls} of {gate['min_matured_calls']} matured calls"
    if interval is None:
        return f"Inconclusive: {calls} matured calls, but an interval needs 2+ decision dates"
    span = f"{level} interval {interval['low']:+.2%} to {interval['high']:+.2%}"
    if status == "supported":
        return f"Supported: {calls} matured calls, {span} net of costs"
    if status == "no_edge":
        floor = gate["futility_upper_bps"] / 10_000
        return f"No edge: {calls} matured calls, {span} stays below {floor:+.2%}"
    return f"Inconclusive: {calls} matured calls, {span} net of costs"


def scoreboard(ledger, *, as_of: str, eligible: Callable[[dict], bool] | None = None) -> dict:
    """``eligible`` can only narrow the evidence set, never admit a non-evidence forecast."""
    boundary = instant(as_of)
    if eligible is not None and not callable(eligible):
        raise ValueError("eligible must be a callable taking a forecast")
    gate = copy.deepcopy(GATE)
    visible = [f for f in ledger.all("forecasts") if instant(f["decision_at"]) <= boundary]
    evidence = [f for f in visible if is_evidence(f) and (eligible is None or eligible(f))]
    counts = {action: 0 for action in ACTIONS}
    counts["other"] = 0
    by_date: dict[str, list[float]] = {}
    nets: list[float] = []
    targets: list[float] = []
    pending = 0
    chosen = decisions(evidence)
    for forecast in chosen:
        action = forecast.get("action")
        counts[action if action in ACTIONS else "other"] += 1
        outcome = ledger.get("outcomes", forecast["id"])
        matured = outcome is not None and label_available_at(outcome) <= boundary
        if matured:
            targets.append(float(outcome["target"]))
        if action not in CALL_SIGNS:
            continue
        if not matured:
            pending += 1
            continue
        value = _net(forecast, outcome, CALL_SIGNS[action])
        day = instant(forecast["decision_at"]).astimezone(EASTERN).date().isoformat()
        by_date.setdefault(day, []).append(value)
        nets.append(value)
    interval = _interval(by_date, gate["confidence"])
    board: dict = {
        "as_of": timestamp(as_of),
        "gate": gate,
        "gate_sha256": digest(gate),
        "eligibility_rule": ELIGIBILITY_RULE,
        "calls": len(nets),
        "call_dates": len(by_date),
        "positive_calls": sum(value > 0 for value in nets),
        "pending_calls": pending,
        "mean_net_return": fmean(fmean(v) for v in by_date.values()) if by_date else None,
        "mean_net_return_per_call": fmean(nets) if nets else None,
        "interval": interval,
        "counts": {"scored_events": len(chosen), **counts},
        "baseline": {"events": len(targets), "mean_target": fmean(targets) if targets else None},
        "excluded_forecasts": len(visible) - len(evidence),
    }
    board["status"] = _status(len(nets), interval, gate)
    board["label"] = label(board)
    return board
