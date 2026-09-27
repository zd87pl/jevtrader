"""The background service: one writer, an Eastern-time schedule, every run recorded, spend capped.

Each job run is appended to the ledger as a ``runs`` record, including skips, failures and
coverage gaps, so the brief can say honestly what was and was not watched. Paid extraction
stops before a conservative estimate of this month's spend could exceed the configured cap.
"""

from __future__ import annotations

import fcntl
import importlib
import math
import os
import re
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import config as settings
from . import engine, paths, pipeline
from .common import canonical, digest, instant, timestamp, utc_now, validate_strategy
from .providers import MAX_OUTPUT_TOKENS, MAX_TEXT_CHARS, PAID_PROVIDERS
from .secrets import KNOWN as SECRET_NAMES

JOBS = ("poll", "bars", "observe", "settle", "brief", "reconcile")
GAP_JOB = "coverage_gap"
ET = ZoneInfo("America/New_York")
STATUSES = ("ok", "partial", "skipped", "failed", "gap")
DONE = frozenset({"ok", "partial", "skipped"})  # a failed run is retried; these are not

POLL_ACTIVE_SECONDS = 60
POLL_IDLE_SECONDS = 15 * 60
ACTIVE_HOURS = (time(6, 0), time(22, 0))  # weekdays, [start, end)
BARS_AT = time(16, 30)
RECONCILE_AT = time(22, 45)
OBSERVE_INTERVAL_SECONDS = 60
RETRY_SECONDS = 15 * 60
GAP_SECONDS = 5 * 60
TICK_SECONDS = 10
OBSERVE_LIMIT = 10
BARS_LOOKBACK_DAYS = 60  # covers every forward event still inside its label horizon
MAX_BAR_SYMBOLS = 1000
MAX_ERROR_CHARS = 300
# Worst case for one paid request: the full text budget plus three 4,000-character questions
# at a pessimistic two characters per token, plus instructions; the answer (reasoning included)
# bills as output up to MAX_OUTPUT_TOKENS.
WORST_CASE_INPUT_TOKENS = (MAX_TEXT_CHARS + 3 * 4_000) // 2 + 4_000

Prices = tuple[float | None, float | None]  # USD per million input tokens, output tokens
Rates = tuple[float, float]  # the same, both known and positive


class AlreadyRunning(RuntimeError):
    """Another process holds the single-writer lock."""


class _Skip(Exception):
    """A precondition is unmet; the run is recorded as skipped with this reason."""


def _lazy(module: str, name: str) -> Callable[..., Any]:
    """Resolve a sibling module's function at call time so tests and stage wiring stay cheap."""

    def call(*args: Any, **kwargs: Any) -> Any:
        return getattr(importlib.import_module(f"{__package__}.{module}"), name)(*args, **kwargs)

    call.__name__ = f"{module}.{name}"
    return call


def registry_price(provider: str, model: str) -> Prices | None:
    """USD per million input and output tokens from the model registry; None for no entry."""
    try:
        registry = importlib.import_module(f"{__package__}.registry")
        entry = registry.lookup(provider, model)
    except (ImportError, ValueError, KeyError):
        return None
    if not isinstance(entry, dict):
        return None
    return entry.get("usd_per_million_input_tokens"), entry.get("usd_per_million_output_tokens")


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


@dataclass
class Context:
    """Everything a job touches. Adapters are injectable; defaults are the real modules."""

    ledger: Any
    config: dict
    strategy: dict
    clock: Callable[[], str] = utc_now
    poll: Callable[..., dict] = field(default_factory=lambda: _lazy("feeds", "poll"))
    bars: Callable[..., dict] = field(default_factory=lambda: _lazy("bars", "fetch_forward"))
    observe: Callable[..., dict] = pipeline.observe_queue
    settle: Callable[..., dict] = engine.settle
    brief: Callable[..., dict] = field(default_factory=lambda: _lazy("brief", "compose"))
    render: Callable[[dict], tuple[str, str]] = field(
        default_factory=lambda: _lazy("brief", "render_text")
    )
    notify: Callable[..., bool] = field(default_factory=lambda: _lazy("notify", "macos"))
    reconcile: Callable[..., list] = field(default_factory=lambda: _lazy("feeds", "daily_index"))
    price: Callable[[str, str], Prices | None] = registry_price
    log: Callable[[str], None] = _stderr

    def __post_init__(self) -> None:
        self.config = settings.validate(self.config)
        validate_strategy(self.strategy)


# ---------------------------------------------------------------- schedule (pure)


def due_jobs(now_et: datetime, state: dict, config: dict) -> list[str]:
    """Jobs due at ``now_et`` (any aware datetime), in run order.

    ``state`` holds ISO timestamps: ``attempted[job]`` (last start), ``completed[job]``
    (last start of a run that ended ok, partial or skipped), plus ``pending["observe"]``
    and ``backoff["poll"]`` (the last poll failed or SEC stopped the batch).
    """
    now = _eastern(now_et)
    attempted = state.get("attempted", {})
    completed = state.get("completed", {})
    poll_every = poll_interval(now)
    if state.get("backoff", {}).get("poll"):
        poll_every = max(poll_every, RETRY_SECONDS)
    bars_enabled = config.get("bars_source", "none") != "none"
    bars_slot = _latest_weekday_slot(now, BARS_AT)
    brief_at = _clock_time(config.get("brief_time", settings.DEFAULTS["brief_time"]))
    brief_slot = datetime.combine(now.date(), brief_at, ET)
    checks = {
        "poll": _interval_due(now, attempted.get("poll"), poll_every),
        "bars": bars_enabled
        and _slot_due(now, bars_slot, attempted.get("bars"), completed.get("bars")),
        "observe": bool(state.get("pending", {}).get("observe"))
        and _interval_due(now, attempted.get("observe"), OBSERVE_INTERVAL_SECONDS),
        # Settle only once the slot's bars are in; without a bar source it keeps the same slot.
        "settle": (not bars_enabled or _done_since(completed.get("bars"), bars_slot))
        and _slot_due(now, bars_slot, attempted.get("settle"), completed.get("settle")),
        # The brief never catches up across days: tomorrow's brief covers everything since.
        "brief": now.weekday() < 5
        and now >= brief_slot
        and _slot_due(now, brief_slot, attempted.get("brief"), completed.get("brief")),
        "reconcile": _slot_due(
            now,
            _latest_weekday_slot(now, RECONCILE_AT),
            attempted.get("reconcile"),
            completed.get("reconcile"),
        ),
    }
    return [job for job in JOBS if checks[job]]


def poll_interval(now_et: datetime) -> int:
    now = _eastern(now_et)
    start, end = ACTIVE_HOURS
    active = now.weekday() < 5 and start <= now.time() < end
    return POLL_ACTIVE_SECONDS if active else POLL_IDLE_SECONDS


def reconcile_day(now_et: datetime) -> date:
    """The weekday whose EDGAR daily index the latest 22:45 ET slot reconciles."""
    return _latest_weekday_slot(_eastern(now_et), RECONCILE_AT).date()


def _eastern(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Scheduling needs a timezone-aware datetime")
    return value.astimezone(ET)


def _clock_time(value: str) -> time:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value) if isinstance(value, str) else None
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError("brief_time must be HH:MM (24-hour, America/New_York)")
    return time(int(match[1]), int(match[2]))


def _latest_weekday_slot(now: datetime, at: time) -> datetime:
    for back in range(8):
        day = now.date() - timedelta(days=back)
        slot = datetime.combine(day, at, ET)
        if day.weekday() < 5 and slot <= now:
            return slot
    raise AssertionError("unreachable: a weekday occurs within any 8 days")


def _done_since(value: str | None, slot: datetime) -> bool:
    return value is not None and instant(value) >= slot


def _interval_due(now: datetime, last: str | None, seconds: int) -> bool:
    if last is None:
        return True
    elapsed = (now - instant(last)).total_seconds()
    # A clock that moved backwards must not stall the schedule.
    return elapsed < 0 or elapsed >= seconds


def _slot_due(now: datetime, slot: datetime, attempted: str | None, completed: str | None) -> bool:
    if _done_since(completed, slot):
        return False
    if _done_since(attempted, slot):
        # A failed attempt for this slot is retried, but not on every tick.
        return _interval_due(now, attempted, RETRY_SECONDS)
    return True


# ---------------------------------------------------------------- jobs


def run_job(name: str, ctx: Context) -> dict:
    """Run one job and append its ``runs`` record; job failures are recorded, not raised."""
    if name not in JOBS:
        raise ValueError(f"Unknown job {name!r}; expected one of: {', '.join(JOBS)}")
    started_at = timestamp(ctx.clock())
    status, error = "ok", None
    counts: dict = {}
    try:
        counts, problems = _HANDLERS[name](ctx, started_at)
        if problems:
            status, error = "partial", _problem_summary(problems)
    except _Skip as reason:
        status, error = "skipped", _safe_text(str(reason))
    except Exception as exc:
        status, error = "failed", _safe_text(f"{type(exc).__name__}: {exc}")
    return _record(ctx, name, started_at, timestamp(ctx.clock()), status, counts, error)


def _poll(ctx: Context, now: str) -> tuple[dict, list]:
    agent = _user_agent(ctx)
    symbols = _scope(ctx.config)
    result = ctx.poll(ctx.ledger, agent, symbols=symbols)
    errors = list(result.get("errors") or [])
    counts = {
        "seen": _count(result.get("seen")),
        "new": _count(result.get("new")),
        "added": _count(result.get("added")),
        "skipped": _count(result.get("skipped")),
        "errors": len(errors),
        "requests": _count(result.get("requests")),
        "stopped": int(bool(result.get("stopped"))),
    }
    return counts, errors


def _bars(ctx: Context, now: str) -> tuple[dict, list]:
    if ctx.config["bars_source"] == "none":
        raise _Skip("bars_source is none; import bars manually or choose alpaca in setup")
    symbols = _bar_symbols(ctx, now)
    result = ctx.bars(ctx.ledger, symbols, now=now, feed=ctx.config["alpaca_feed"])
    errors = list(result.get("errors") or [])
    counts = {
        "symbols": _count(result.get("symbols", len(symbols))),
        "added": _count(result.get("added")),
        "skipped": _count(result.get("skipped")),
        "errors": len(errors),
    }
    return counts, errors


def _observe(ctx: Context, now: str) -> tuple[dict, list]:
    provider = ctx.config["provider"]
    model = settings.resolved_model(ctx.config)
    limit = OBSERVE_LIMIT
    spend: dict = {}
    if provider in PAID_PROVIDERS:
        limit, spend = _paid_limit(ctx, provider, model, now)
    result = ctx.observe(ctx.ledger, ctx.strategy, provider=provider, model=model, limit=limit)
    errors = list(result.get("errors") or [])
    counts = {
        "forecasts": len(result.get("forecasts") or []),
        "errors": len(errors),
        "limit": limit,
        **{key: _count(value) for key, value in sorted((result.get("skipped") or {}).items())},
        **spend,
    }
    return counts, errors


def _paid_limit(ctx: Context, provider: str, model: str, now: str) -> tuple[int, dict]:
    cap = ctx.config["spend_cap_usd_month"]
    if cap <= 0:
        raise _Skip("spend_cap_usd_month is 0; paid extraction is disabled")
    rates = _rates(ctx.price, provider, model)
    if rates is None:
        raise _Skip(
            f"No input- and output-token prices are known for {provider}:{model}; "
            "paid extraction stays off until both are declared"
        )
    spent = month_spend(ctx.ledger, now=now, price=ctx.price, default_rates=rates)
    per_request = _usd(rates, WORST_CASE_INPUT_TOKENS, MAX_OUTPUT_TOKENS)
    affordable = math.floor((cap - spent) / per_request) if spent < cap else 0
    spend = {"spend_usd_month": round(spent, 6), "cap_usd_month": cap}
    if affordable < 1:
        raise _Skip(
            f"Monthly spend cap ${cap:.2f} reached (estimated ${spent:.2f} this UTC month); "
            "paid extraction stopped until next month or a higher cap"
        )
    return min(OBSERVE_LIMIT, affordable), spend


def month_spend(
    ledger,
    *,
    now: str,
    price: Callable[[str, str], Prices | None],
    default_rates: Rates,
) -> float:
    """Estimated paid-extraction USD this UTC month; failed attempts count at the worst case."""
    month = instant(now).strftime("%Y-%m")
    total = 0.0
    for extraction in ledger.all("extractions"):
        spec = extraction.get("spec") or {}
        provider = spec.get("provider")
        if provider not in PAID_PROVIDERS or _month(extraction.get("created_at")) != month:
            continue
        rates = _rates(price, provider, spec.get("requested_model")) or _rates(
            price, provider, extraction.get("resolved_model")
        )
        total += _usd(rates or default_rates, _input_tokens(extraction), _output_tokens(extraction))
    # A failed request may still have been billed; it recorded no token count.
    for attempt in ledger.all("attempts"):
        provider = attempt.get("provider")
        if provider in PAID_PROVIDERS and _month(attempt.get("attempted_at")) == month:
            rates = _rates(price, provider, attempt.get("requested_model"))
            total += _usd(rates or default_rates, WORST_CASE_INPUT_TOKENS, MAX_OUTPUT_TOKENS)
    return total


def _usd(rates: Rates, input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1e6


def _input_tokens(extraction: dict) -> int:
    tokens = extraction.get("input_tokens")
    if type(tokens) is not int or tokens < 0:
        return WORST_CASE_INPUT_TOKENS
    # What was sent bounds a provider's own count from below, at two characters per token.
    chars = extraction.get("input_chars")
    return max(tokens, -(-chars // 2)) if type(chars) is int and chars > 0 else tokens


def _output_tokens(extraction: dict) -> int:
    raw = extraction.get("raw")
    usage = raw.get("usage") if isinstance(raw, dict) else None
    tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
    # Reasoning bills as output too; without a usable count assume the requested maximum.
    return tokens if type(tokens) is int and tokens >= 0 else MAX_OUTPUT_TOKENS


def _settle(ctx: Context, now: str) -> tuple[dict, list]:
    result = ctx.settle(ctx.ledger, as_of=now)
    counts = {
        "added": _count(result.get("added")),
        "unresolved": _count(result.get("unresolved_count")),
    }
    return counts, []


def _brief(ctx: Context, now: str) -> tuple[dict, list]:
    previous = last_run(ctx.ledger, "brief", statuses=DONE)
    report = ctx.brief(
        ctx.ledger,
        now=now,
        since=previous["started_at"] if previous else None,
        watchlist=list(ctx.config["watchlist"]),
    )
    filings = report.get("total", report.get("filings"))  # total counts past the display cap
    counts, problems = {"filings": _count(filings), "notified": 0}, []
    if ctx.config["notify"]:
        try:
            title, body = ctx.render(report)
            counts["notified"] = int(bool(ctx.notify(title, body)))
        except Exception as exc:
            # The brief itself was composed; a missed banner must not trigger a resend.
            problems.append(f"notification failed: {type(exc).__name__}")
    return counts, problems


def _reconcile(ctx: Context, now: str) -> tuple[dict, list]:
    agent = _user_agent(ctx)
    rows = ctx.reconcile(agent, reconcile_day(instant(now)))
    accessions = {
        row["accession"] for row in rows if isinstance(row, dict) and row.get("accession")
    }
    collected = sum(bool(ctx.ledger.prefix("disclosures", f"sec:{a}:")) for a in accessions)
    counts = {
        "indexed": len(accessions),
        "collected": collected,
        "not_collected": len(accessions) - collected,
        "unparsed": len(rows) - sum(isinstance(r, dict) and bool(r.get("accession")) for r in rows),
    }
    return counts, []


_HANDLERS: dict[str, Callable[[Context, str], tuple[dict, list]]] = {
    "poll": _poll,
    "bars": _bars,
    "observe": _observe,
    "settle": _settle,
    "brief": _brief,
    "reconcile": _reconcile,
}


def _user_agent(ctx: Context) -> str:
    agent = ctx.config["sec_user_agent"]
    if not agent:
        raise _Skip("sec_user_agent is not set; run setup with your name and email")
    return agent


def _scope(config: dict) -> set[str] | None:
    if config["universe"] == "all":
        return None
    if not config["watchlist"]:
        raise _Skip("watchlist is empty; add symbols or set universe to all")
    return set(config["watchlist"])


def _bar_symbols(ctx: Context, now: str) -> list[str]:
    since = instant(now) - timedelta(days=BARS_LOOKBACK_DAYS)
    recent = sorted(
        (
            (event["first_seen_at"], event["symbol"])
            for event in ctx.ledger.all("disclosures")
            if event.get("mode") == "forward" and instant(event["first_seen_at"]) >= since
        ),
        reverse=True,
    )
    symbols = [*ctx.config["watchlist"], *(name for _, name in recent)]
    return list(dict.fromkeys(symbols))[:MAX_BAR_SYMBOLS]


def _rates(
    price: Callable[[str, str], Prices | None], provider: str, model: object
) -> Rates | None:
    if not isinstance(model, str) or not model:
        return None
    try:
        value = price(provider, model)
    except (ValueError, LookupError):
        return None
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    input_rate, output_rate = (_positive(rate) for rate in value)
    if input_rate is None or output_rate is None:
        return None
    return input_rate, output_rate


def _positive(value: object) -> float | None:
    # Zero, negative or non-numeric prices would disable the cap; treat them as unknown.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _month(value: object) -> str | None:
    try:
        return instant(value).strftime("%Y-%m") if isinstance(value, str) else None
    except ValueError:
        return None


def _count(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return sum(_count(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value)
    return 0


def _problem_summary(problems: list) -> str:
    first = problems[0]
    text = first.get("error", first) if isinstance(first, dict) else first
    return _safe_text(f"{len(problems)} error(s); first: {text}")


def _safe_text(message: str) -> str:
    """Errors reach the ledger and logs: drop key material, control characters and bulk."""
    for name in SECRET_NAMES:
        value = os.environ.get(name, "")
        if len(value) >= 4:
            message = message.replace(value, "[redacted]")
    message = re.sub(r"(?i)(bearer|apca-api-[a-z-]+:?)\s+\S+", r"\1 [redacted]", message)
    message = "".join(ch if ch.isprintable() else " " for ch in message)
    return message[:MAX_ERROR_CHARS]


def _record(
    ctx: Context,
    job: str,
    started_at: str,
    finished_at: str,
    status: str,
    counts: dict,
    error: str | None,
) -> dict:
    record = {
        "job": job,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "counts": counts,
        "error": error,
    }
    # Ids sort by start time within a job, so a prefix scan finds the latest run.
    record = {"id": f"{job}:{started_at}:{digest(record)[:12]}", **record}
    ctx.ledger.put("runs", record["id"], record)
    ctx.log(f"{finished_at} {job} {status} {canonical(counts)}" + (f" {error}" if error else ""))
    return record


# ---------------------------------------------------------------- state and loop


def last_run(ledger, job: str, *, statuses: frozenset[str] | None = None) -> dict | None:
    for record in reversed(ledger.prefix("runs", f"{job}:")):
        if statuses is None or record.get("status") in statuses:
            return record
    return None


def last_runs(ledger) -> dict[str, dict]:
    """Latest run per job (and the latest coverage gap), for health displays."""
    result = {}
    for job in (*JOBS, GAP_JOB):
        record = last_run(ledger, job)
        if record is not None:
            result[job] = record
    return result


def load_state(ledger) -> dict:
    """Rebuild the schedule from recorded runs so a restart never repeats a finished slot."""
    state: dict = {"attempted": {}, "completed": {}, "pending": {"observe": True}, "backoff": {}}
    for job in JOBS:
        runs = ledger.prefix("runs", f"{job}:")
        if runs:
            state["attempted"][job] = runs[-1]["started_at"]
        done = next((r for r in reversed(runs) if r.get("status") in DONE), None)
        if done:
            state["completed"][job] = done["started_at"]
        if runs and job == "poll":
            state["backoff"]["poll"] = _poll_backoff(runs[-1])
    return state


def _poll_backoff(record: dict) -> bool:
    # SEC throttling or a broken poller must not be hit again every minute.
    return record["status"] == "failed" or bool((record.get("counts") or {}).get("stopped"))


def remember(state: dict, record: dict, *, at: str | None = None) -> None:
    """Fold one run into the schedule state; ``at`` is the tick time the job was due."""
    job, counts = record["job"], record.get("counts") or {}
    state.setdefault("attempted", {})[job] = at or record["started_at"]
    if record["status"] in DONE:
        state.setdefault("completed", {})[job] = at or record["started_at"]
    if job == "poll":
        state.setdefault("backoff", {})["poll"] = _poll_backoff(record)
    pending = state.setdefault("pending", {})
    if job in ("poll", "bars") and counts.get("added", 0) > 0:
        pending["observe"] = True
    elif job == "observe":
        # A full batch may leave more queued work; errors wait for new data instead of
        # retrying a paid provider every minute.
        pending["observe"] = record["status"] == "ok" and counts.get("forecasts", 0) >= counts.get(
            "limit", OBSERVE_LIMIT
        )


def tick(ctx: Context, state: dict, now: str, *, stop: threading.Event | None = None) -> list:
    """Run every job due at ``now`` in order, re-checking after each so observe follows poll."""
    moment = instant(now)
    records = []
    for job in JOBS:
        if stop is not None and stop.is_set():
            break
        if job in due_jobs(moment, state, ctx.config):
            record = run_job(job, ctx)
            remember(state, record, at=timestamp(now))
            records.append(record)
    return records


@contextmanager
def single_writer(path: Path | None = None) -> Iterator[Path]:
    """Exclusive, non-blocking lock; closing the descriptor (even on a crash) releases it."""
    target = Path(path) if path is not None else paths.lock_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(
                f"Another {paths.APP_NAME} daemon is already running (lock: {target}); "
                "stop it first"
            ) from None
        os.ftruncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        yield target
    finally:
        os.close(handle)


def run_forever(
    ctx: Context,
    *,
    clock: Callable[[], str] | None = None,
    sleep: Callable[[float], Any] | None = None,
    stop: threading.Event | None = None,
    lock_path: Path | None = None,
    tick_seconds: float = TICK_SECONDS,
) -> None:
    """Run the schedule until ``stop`` is set; raises AlreadyRunning if another writer exists."""
    if clock is not None:
        ctx = replace(ctx, clock=clock)
    stop = stop or threading.Event()
    sleep = sleep or stop.wait  # an interruptible sleep lets a signal handler stop promptly
    with single_writer(lock_path):
        state = load_state(ctx.ledger)
        now = timestamp(ctx.clock())
        last_poll = state["attempted"].get("poll")
        if last_poll is not None:
            idle = (instant(now) - instant(last_poll)).total_seconds()
            if idle > poll_interval(instant(last_poll)) + GAP_SECONDS:
                _gap(ctx, state, last_poll, now, "the service was not running")
        while not stop.is_set():
            tick(ctx, state, now, stop=stop)
            if stop.is_set():
                break
            before = timestamp(ctx.clock())
            sleep(tick_seconds)
            now = timestamp(ctx.clock())
            overshoot = (instant(now) - instant(before)).total_seconds() - tick_seconds
            if overshoot > GAP_SECONDS:
                _gap(ctx, state, before, now, "the computer slept or the clock jumped")


def _gap(ctx: Context, state: dict, start: str, end: str, reason: str) -> None:
    seconds = int((instant(end) - instant(start)).total_seconds())
    _record(ctx, GAP_JOB, start, end, "gap", {"seconds": seconds}, f"Not watching: {reason}")
    # Catch up now: poll immediately; slot jobs (bars, reconcile) catch up on their own.
    state.setdefault("attempted", {}).pop("poll", None)
    state.setdefault("pending", {})["observe"] = True
