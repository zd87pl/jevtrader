"""Frozen observations connect extraction, causal market data, and small models."""

from __future__ import annotations

from . import cohorts, registry
from .common import digest, instant, round_trip_bps, timestamp, utc_now, validate_strategy
from .market import outcome, snapshot
from .providers import (
    MAX_TEXT_CHARS,
    PAID_PROVIDERS,
    ProviderInputError,
    ProviderValidationError,
    extract_features,
    prompt_template_hash,
    render_request,
)
from .research import FEATURE_NAMES, VERSION, fit_model, predict, walk_forward
from .security.sanitize import SANITIZER_VERSION, sanitize_text

PREVIOUS_MIN_CHARS = 10_000


class ObservationRejected(ValueError):
    """Rejected by local checks before any provider request; safe to skip and retry later."""


def _excerpt(current: str, previous: str) -> tuple[str, str, dict | None]:
    """Deterministic text budget: current text first, keeping room for the comparison text."""
    if len(current) + len(previous) <= MAX_TEXT_CHARS:
        return current, previous, None
    kept_current = current[: MAX_TEXT_CHARS - min(len(previous), PREVIOUS_MIN_CHARS)]
    kept_previous = previous[: MAX_TEXT_CHARS - len(kept_current)]
    return (
        kept_current,
        kept_previous,
        {
            "current_chars": len(kept_current),
            "current_total": len(current),
            "previous_chars": len(kept_previous),
            "previous_total": len(previous),
        },
    )


def observe(
    ledger,
    event_id: str,
    strategy: dict,
    *,
    provider: str = "rules",
    model: str = "rules-v1",
    as_of: str | None = None,
    calibrator_id: str | None = None,
    transport=None,
    base_url: str | None = None,
    overrides: dict | None = None,
    adhoc: bool = False,
) -> dict:
    """Freeze one decision. ``base_url`` reaches a local engine; ``overrides`` are the
    config's registry declarations, used only to label the new forecast's evidence.
    ``adhoc`` marks a replay of an event picked by hand, which never counts as evidence."""
    validate_strategy(strategy)
    overrides = registry.validate_overrides(overrides)
    event = ledger.get("disclosures", event_id)
    if event is None:
        raise ObservationRejected(f"Unknown disclosure: {event_id}")
    if as_of is None and event["mode"] != "forward":
        # A live clock would pair old text with today's market and label it historical.
        raise ObservationRejected(
            f"{event['mode'].capitalize()} disclosures need --replay or --as-of: {event_id}"
        )
    decision_at = timestamp(as_of or utc_now())
    if instant(event["first_seen_at"]) > instant(decision_at):
        raise ObservationRejected("Disclosure was not observed by decision_at")
    mode = (
        "synthetic"
        if event["mode"] == "synthetic"
        else ("forward" if as_of is None and event["mode"] == "forward" else "historical")
    )
    forecast_model = None
    if calibrator_id:
        forecast_model = ledger.get("models", calibrator_id)
        if forecast_model is None:
            raise ObservationRejected(f"Unknown calibrator: {calibrator_id}")
        if forecast_model.get("version") != VERSION:
            # Earlier fits could scale constant features by rounding noise; never reuse them.
            raise ObservationRejected(
                f"Calibrator was fit by {forecast_model.get('version')}, not {VERSION}; re-fit it"
            )
        if instant(forecast_model["cutoff"]) >= instant(decision_at):
            raise ObservationRejected("Calibrator cutoff must be strictly before this decision")
        if mode != "synthetic" and "synthetic" in forecast_model["training_modes"]:
            raise ObservationRejected("A synthetic model cannot score real disclosures")
        if event_id in forecast_model["training_event_ids"]:
            raise ObservationRejected("Cannot forecast an event used to train the calibrator")
    # Validate the local data before incurring a provider charge (and before the
    # full disclosure scan below, so a queue of unscorable events stays cheap).
    try:
        market = snapshot(ledger, event["symbol"], decision_at, strategy, mode=mode)
    except ObservationRejected:
        raise
    except ValueError as exc:
        raise ObservationRejected(str(exc)) from None
    previous = [
        item
        # as_of keeps only disclosures first seen by decision_at (ADR-0004).
        for item in ledger.as_of("disclosures", decision_at)
        if item["symbol"] == event["symbol"]
        and item["mode"] == event["mode"]
        and instant(item["published_at"]) < instant(event["published_at"])
    ]
    prior = max(previous, key=lambda item: (item["published_at"], item["id"])) if previous else None
    spec = {
        "provider": provider,
        "requested_model": model,
        "questions": strategy["questions"],
        "feature_version": "disclosure-eight-v1",
        "benchmark": strategy["benchmark"],
        "horizon_sessions": strategy["horizon_sessions"],
        "history_sessions": strategy["min_history_sessions"],
        "spread_bps": strategy["spread_bps"],
        # Providers only ever see sanitized text (P0-05); the version is part of the key.
        "sanitizer_version": SANITIZER_VERSION,
        # A template edit is a different extractor, never a silent reuse (P0-07).
        "prompt_template": prompt_template_hash(provider),
    }
    extraction_id = digest(
        {"spec": spec, "current": event["text"], "previous": prior["text"] if prior else ""}
    )
    extraction = ledger.get("extractions", extraction_id)
    if forecast_model:
        # A cached extraction, or a calibrator from another provider, model or question set,
        # is rejected before paying; only a changed resolved model must wait for a response.
        if extraction:
            compatible = extraction["extractor_key"] == forecast_model["extractor_key"]
        else:
            trained = next(
                (
                    f
                    for f in ledger.all("forecasts")
                    if f["extractor_key"] == forecast_model["extractor_key"]
                ),
                None,
            )
            compatible = (
                trained is None
                or ledger.get("extractions", trained["extraction_id"])["spec"] == spec
            )
        if not compatible:
            raise ObservationRejected("Calibrator and extraction schemas/models do not match")
    if extraction is None:
        # Legacy ledger text predates the sanitizer; sanitizing is idempotent for newer text.
        current_text, previous_text, excerpt = _excerpt(
            sanitize_text(event["text"]), sanitize_text(prior["text"]) if prior else ""
        )
        # Only a local engine takes an address; other adapters keep their exact call.
        location = {"base_url": base_url} if provider == "local" and base_url is not None else {}
        try:
            result = extract_features(
                provider,
                model,
                current_text,
                previous_text,
                strategy,
                transport=transport,
                **location,
            )
        except ProviderInputError:
            raise
        except BaseException as exc:
            # Includes Ctrl-C: an interrupted request may still be billed. A local engine
            # bills nothing; only its repeated invalid answers are recorded, so queues stop
            # retrying them, while an engine that was merely down is retried later.
            if provider in PAID_PROVIDERS or (
                provider == "local" and isinstance(exc, ProviderValidationError)
            ):
                _record_failed_attempt(
                    ledger, event_id, provider, model, strategy, extraction_id, exc
                )
            raise
        extraction = {"id": extraction_id, "created_at": utc_now(), "spec": spec, **result}
        extraction["extractor_key"] = digest({**spec, "resolved_model": result["resolved_model"]})
        # What was sent, so spend estimates need not trust a provider's own token count.
        extraction["input_chars"] = (
            len(current_text)
            + len(previous_text)
            + sum(len(question) for question in strategy["questions"].values())
        )
        if excerpt:
            extraction["text_excerpt"] = excerpt
        # The exact request body (first attempt), so every prompt is auditable from the ledger.
        extraction["prompt"] = render_request(
            provider, model, current_text, previous_text, strategy
        )
        ledger.put("extractions", extraction_id, extraction)
    if mode == "forward":
        # A prediction only exists when extraction has finished; never backdate API latency.
        decision_at = utc_now()
    features = [extraction[name] if name in extraction else market[name] for name in FEATURE_NAMES]
    if forecast_model and forecast_model["extractor_key"] != extraction["extractor_key"]:
        # Reached only after a fresh extraction (e.g. a paid one resolving to another model).
        raise ValueError("Calibrator and extraction schemas/models do not match")
    expected = predict(forecast_model, features) if forecast_model else None
    reasons = []
    if market["price"] < strategy["min_price"]:
        reasons.append("price below floor")
    if market["dollar_volume"] < strategy["min_dollar_volume"]:
        reasons.append("dollar volume below floor")
    if extraction["uncertainty"] > strategy["max_uncertainty"]:
        reasons.append("text evidence too uncertain")
    if extraction["novelty"] < 0.5 or extraction["materiality"] < 0.5:
        reasons.append("insufficient novel, material evidence")
    action = "WATCH" if expected is None else "PASS"
    if expected is None:
        reasons.append("no trained calibrator: observation only")
    elif not reasons:
        long_threshold = (round_trip_bps(strategy) + strategy["min_edge_bps"]) / 10000
        short_threshold = (round_trip_bps(strategy, short=True) + strategy["min_edge_bps"]) / 10000
        if expected > long_threshold:
            action = "LONG"
        elif strategy["allow_short"] and expected < -short_threshold:
            action = "SHORT"
        else:
            reasons.append("predicted excess return does not clear cost and edge threshold")
    identity = digest(
        {
            "event": event_id,
            "extraction": extraction_id,
            "decision_at": decision_at,
            "mode": mode,
            "strategy": strategy,
            "calibrator": calibrator_id,
        }
    )
    existing = ledger.get("forecasts", identity)
    if existing:
        return existing
    label, basis = _eligibility(
        provider, extraction["resolved_model"], mode, event["published_at"], overrides, adhoc=adhoc
    )
    # Every historical disclosure was imported or backfilled, so the user chose it; its replay
    # counts only when a cohort listing it was registered before it entered the ledger.
    if (
        mode == "historical"
        and registry.counts_as_evidence(label)
        and cohorts.preregistered(ledger, event_id) is None
    ):
        label = cohorts.LABEL
    record = {
        "id": identity,
        "event_id": event_id,
        "symbol": event["symbol"],
        "decision_at": decision_at,
        "recorded_at": utc_now(),
        "mode": mode,
        "previous_event_id": prior["id"] if prior else None,
        "extraction_id": extraction_id,
        "extractor_key": extraction["extractor_key"],
        "provider": provider,
        "resolved_model": extraction["resolved_model"],
        "eligibility": label,
        # The registry facts behind the label, frozen with it: a config declaration can change.
        "eligibility_basis": basis,
        "features": features,
        "feature_names": list(FEATURE_NAMES),
        "market": market,
        "calibrator_id": calibrator_id,
        "expected_return": expected,
        "target": "future_benchmark_relative_return",
        "action": action,
        "reasons": reasons,
        "strategy": strategy,
    }
    ledger.put("forecasts", identity, record)
    return record


def _eligibility(
    provider: str,
    resolved_model: str,
    mode: str,
    published_at: str,
    overrides: dict,
    *,
    adhoc: bool = False,
) -> tuple[str, dict | None]:
    """The registry's evidence label and the facts behind it; an extractor it cannot identify
    never earns a replay label, and neither does a hand-picked replay."""
    try:
        entry = registry.lookup(provider, resolved_model, overrides)
        label = registry.eligibility(
            provider, resolved_model, mode=mode, published_at=published_at, overrides=overrides
        )
    except ValueError:
        return (mode if mode in ("forward", "synthetic") else "unknown_cutoff"), None
    if adhoc and mode == "historical":
        label = "adhoc_replay"
    basis = {key: entry[key] for key in ("key", "training_cutoff", "origin", "source")}
    return label, basis


def _record_failed_attempt(
    ledger,
    event_id: str,
    provider: str,
    model: str,
    strategy: dict,
    extraction_id: str,
    error: BaseException,
) -> None:
    """A request may have been sent and billed; keep an append-only record so queues skip it."""
    record = {
        "event_id": event_id,
        "provider": provider,
        "requested_model": model,
        # The paid payload depends on the questions, not on other strategy fields.
        "questions_digest": digest(strategy["questions"]),
        "attempted_at": utc_now(),
    }
    identity = digest(record)
    ledger.put(
        "attempts",
        identity,
        {
            "id": identity,
            **record,
            "extraction_id": extraction_id,
            "error_type": type(error).__name__,
            "error": str(error),
        },
    )


def settle(ledger, *, as_of: str | None = None) -> dict:
    cutoff = timestamp(as_of or utc_now())
    added, unresolved = 0, []
    for forecast in ledger.all("forecasts"):
        if ledger.get("outcomes", forecast["id"]):
            continue
        result = outcome(ledger, forecast, cutoff)
        if result is None:
            unresolved.append(forecast["id"])
            continue
        result["recorded_at"] = utc_now()
        added += ledger.put("outcomes", forecast["id"], result)
    return {"added": added, "unresolved_count": len(unresolved), "unresolved_ids": unresolved}


def training_rows(
    ledger,
    extractor_key: str,
    *,
    before: str | None = None,
    event_ids: set[str] | None = None,
    mode: str | None = None,
) -> list[dict]:
    rows, seen = [], set()
    if mode not in {None, "forward", "historical", "synthetic"}:
        raise ValueError("Invalid observation mode")
    forecasts = sorted(
        (
            f
            for f in ledger.all("forecasts")
            if f["extractor_key"] == extractor_key
            and (event_ids is None or f["event_id"] in event_ids)
            and (mode is None or f["mode"] == mode)
        ),
        key=lambda f: (f["decision_at"], f["id"]),
    )
    if len({f["mode"] for f in forecasts}) > 1:
        raise ValueError(
            "Mixed observation modes; select --mode forward, historical, or synthetic explicitly"
        )
    for forecast in forecasts:
        if forecast["event_id"] in seen:
            continue
        # Freeze the earliest observation per event even if its outcome is missing.
        seen.add(forecast["event_id"])
        label = ledger.get("outcomes", forecast["id"])
        if label is None:
            continue
        available = max(instant(label["outcome_at"]), instant(label["label_available_at"]))
        if before is not None and available >= instant(before):
            continue
        rows.append(
            {
                "event_id": forecast["event_id"],
                "symbol": forecast["symbol"],
                "decision_at": forecast["decision_at"],
                "outcome_at": timestamp(available.isoformat()),
                "features": forecast["features"],
                "target": label["target"],
                "extractor_key": extractor_key,
                "mode": forecast["mode"],
            }
        )
    return rows


def train(
    ledger, extractor_key: str, cutoff: str, strategy: dict, *, mode: str | None = None
) -> dict:
    result = fit_model(
        training_rows(ledger, extractor_key, before=cutoff, mode=mode),
        cutoff=timestamp(cutoff),
        alpha=strategy["ridge_alpha"],
        min_samples=strategy["min_train_samples"],
    )
    existing = ledger.get("models", result["model_id"])
    if existing:
        return existing
    result["recorded_at"] = utc_now()
    ledger.put("models", result["model_id"], result)
    return result


def evaluate(
    ledger,
    extractor_key: str,
    strategy: dict,
    *,
    before: str | None = None,
    event_ids: set[str] | None = None,
    mode: str | None = None,
) -> dict:
    rows = training_rows(ledger, extractor_key, before=before, event_ids=event_ids, mode=mode)
    report = walk_forward(
        rows,
        min_train=strategy["min_train_samples"],
        alpha=strategy["ridge_alpha"],
        cost_bps=round_trip_bps(strategy),
        min_edge_bps=strategy["min_edge_bps"],
        allow_short=strategy["allow_short"],
    )
    report["modes"] = sorted({row["mode"] for row in rows})
    report["extractor_provider"] = next(
        f["provider"] for f in ledger.all("forecasts") if f["extractor_key"] == extractor_key
    )
    report["limitations"].append(
        "Research comparison omits forecast evidence/liquidity gates and paper sizing; it is not the executable policy."
    )
    report["limitations"].append(
        "Event evaluation excludes per-order dollar commissions; full sizing costs are included only in paper plans."
    )
    return report
