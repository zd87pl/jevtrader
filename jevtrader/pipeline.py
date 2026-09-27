"""The pending-observation queue: bounded scans, no live decisions on old text, no silent rebilling."""

from __future__ import annotations

from collections.abc import Callable

from . import engine
from .common import digest, instant
from .engine import ObservationRejected
from .market import bars_for
from .providers import (
    PAID_PROVIDERS,
    MissingCredentials,
    ProviderError,
    ProviderInputError,
    ProviderValidationError,
    Transport,
)
from .research import VERSION

MAX_LIMIT = 200
FORECAST_FIELDS = (
    "id",
    "event_id",
    "mode",
    "action",
    "reasons",
    "expected_return",
    "extractor_key",
)


def check_options(
    *,
    limit: int,
    max_scan: int,
    event: str | None = None,
    as_of: str | None = None,
    replay: bool = False,
) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}")
    if max_scan < 1:
        raise ValueError("--max-scan must be positive")
    if as_of and not event:
        raise ValueError("--as-of requires one --event")
    if as_of and replay:
        raise ValueError("--replay and --as-of are mutually exclusive")


def observe_queue(
    ledger,
    strategy: dict,
    *,
    provider: str,
    model: str,
    replay: bool = False,
    event: str | None = None,
    as_of: str | None = None,
    calibrator: str | None = None,
    limit: int = 10,
    retry_failed: bool = False,
    transport: Transport | None = None,
    max_scan: int = 200,
    observer: Callable[..., dict] | None = None,
    base_url: str | None = None,
    overrides: dict | None = None,
) -> dict:
    """Observe one named event or the pending queue; ``observer`` defaults to engine.observe.

    ``base_url`` (a local engine's address) and ``overrides`` (config registry declarations)
    are passed to the observer only when given. A replay of one named event is ``adhoc``: the
    user picked it, possibly knowing its outcome, so it is never evidence.
    """
    check_options(limit=limit, max_scan=max_scan, event=event, as_of=as_of, replay=replay)
    if not model:
        raise ValueError("Choose a provider model")
    scorer = _calibrator(ledger, calibrator)
    events = sorted(ledger.all("disclosures"), key=lambda e: (e["first_seen_at"], e["id"]))
    skipped = {
        "requires_replay": 0,
        "failed_before": 0,
        "calibrator_ineligible": 0,
        "no_market_data": 0,
        "scan_truncated": False,
        "missing_credentials": False,
    }
    extra: dict = {
        key: value
        for key, value in (("base_url", base_url), ("overrides", overrides))
        if value is not None
    }
    if event:
        events = [e for e in events if e["id"] == event]
        if not events:
            raise ValueError("Disclosure not found")
        if replay or as_of:
            extra["adhoc"] = True
    else:
        events = _pending(ledger, strategy, events, skipped, provider, model, replay, calibrator)
        if not retry_failed:
            events = _without_failed(ledger, strategy, events, skipped, provider, model)
        if scorer:
            events = _scorable_by(scorer, events, skipped, replay)
        events = _with_market_data(ledger, strategy, events, skipped)
    return _run(
        ledger,
        strategy,
        events,
        skipped,
        observer=observer or engine.observe,
        provider=provider,
        model=model,
        replay=replay,
        as_of=as_of,
        calibrator=calibrator,
        limit=limit,
        max_scan=max_scan,
        transport=transport,
        extra=extra,
    )


def _calibrator(ledger, model_id: str | None) -> dict | None:
    if not model_id:
        return None
    model = ledger.get("models", model_id)
    if model is None:
        raise ValueError(f"Unknown calibrator: {model_id}")
    if model.get("version") != VERSION:
        raise ValueError(f"Calibrator was fit by {model.get('version')}, not {VERSION}; re-fit it")
    return model


def _pending(
    ledger,
    strategy: dict,
    events: list[dict],
    skipped: dict,
    provider: str,
    model: str,
    replay: bool,
    calibrator: str | None,
) -> list[dict]:
    done = {
        (f["event_id"], f["mode"])
        for f in ledger.all("forecasts")
        if f["provider"] == provider
        and f["strategy"] == strategy
        and f["calibrator_id"] == calibrator
        and ledger.get("extractions", f["extraction_id"])["spec"]["requested_model"] == model
    }
    events = [
        e
        for e in events
        if (
            e["id"],
            "synthetic"
            if e["mode"] == "synthetic"
            else "historical"
            if replay or e["mode"] == "historical"
            else "forward",
        )
        not in done
    ]
    if not replay:
        # A live clock never decides historical or synthetic records; replay them.
        skipped["requires_replay"] = sum(e["mode"] != "forward" for e in events)
        events = [e for e in events if e["mode"] == "forward"]
    return events


def _without_failed(
    ledger, strategy: dict, events: list[dict], skipped: dict, provider: str, model: str
) -> list[dict]:
    # A failed paid request may have been billed; never repeat it unasked. The request
    # depends on the questions, not other strategy fields; a later successful retry
    # caches the extraction, so scoring it again is free.
    questions = digest(strategy["questions"])
    failed = {
        a["event_id"]
        for a in ledger.all("attempts")
        if a["provider"] == provider
        and a["requested_model"] == model
        and a["questions_digest"] == questions
        and ledger.get("extractions", a["extraction_id"]) is None
    }
    skipped["failed_before"] = sum(e["id"] in failed for e in events)
    return [e for e in events if e["id"] not in failed]


def _scorable_by(scorer: dict, events: list[dict], skipped: dict, replay: bool) -> list[dict]:
    # Keep events the model can never score from consuming the limit. A replay decides
    # at first sight, so it also needs first_seen_at after the cutoff.
    eligible = [
        e
        for e in events
        if e["id"] not in scorer["training_event_ids"]
        and (e["mode"] == "synthetic" or "synthetic" not in scorer["training_modes"])
        and (not replay or instant(e["first_seen_at"]) > instant(scorer["cutoff"]))
    ]
    skipped["calibrator_ineligible"] = len(events) - len(eligible)
    return eligible


def _with_market_data(ledger, strategy: dict, events: list[dict], skipped: dict) -> list[dict]:
    # One bar lookup per symbol: events that can never get a market snapshot must not
    # fill the bounded scan and hide scorable events behind them.
    needed = strategy["min_history_sessions"]
    enough = {
        name: len(bars_for(ledger, name)) >= needed
        for name in {e["symbol"] for e in events} | {strategy["benchmark"]}
    }
    scorable = [e for e in events if enough[e["symbol"]] and enough[strategy["benchmark"]]]
    skipped["no_market_data"] = len(events) - len(scorable)
    return scorable


def _run(
    ledger,
    strategy: dict,
    events: list[dict],
    skipped: dict,
    *,
    observer: Callable[..., dict],
    provider: str,
    model: str,
    replay: bool,
    as_of: str | None,
    calibrator: str | None,
    limit: int,
    max_scan: int,
    transport: Transport | None,
    extra: dict,
) -> dict:
    records, errors, attempted, rejected = [], [], 0, 0
    for event in events:
        if attempted >= limit:
            break
        try:
            result = observer(
                ledger,
                event["id"],
                strategy,
                provider=provider,
                model=model,
                as_of=event["first_seen_at"] if replay else as_of,
                calibrator_id=calibrator,
                transport=transport,
                **extra,
            )
            records.append({key: result[key] for key in FORECAST_FIELDS})
        except MissingCredentials as exc:
            # Every uncached event would fail the same way; cached ones never need a key.
            errors.append({"event_id": event["id"], "error": str(exc)})
            skipped["missing_credentials"] = True
            break
        except (ObservationRejected, ProviderInputError) as exc:
            # Nothing was sent, so report it without letting it block later events, but
            # bound the scan: rejections do not count toward the limit.
            errors.append({"event_id": event["id"], "error": str(exc)})
            rejected += 1
            if rejected >= max_scan:
                skipped["scan_truncated"] = True
                break
            continue
        except (ValueError, RuntimeError) as exc:
            attempted += 1
            errors.append({"event_id": event["id"], "error": str(exc)})
            if provider in PAID_PROVIDERS:
                break  # Bound surprise costs after a provider or validation failure.
            if (
                provider == "local"
                and isinstance(exc, ProviderError)
                and not isinstance(exc, ProviderValidationError)
            ):
                break  # The engine is down or refusing; every later event would fail too.
        else:
            attempted += 1
    return {"forecasts": records, "errors": errors, "skipped": skipped}
