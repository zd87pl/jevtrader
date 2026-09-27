"""The local app's wiring: config-driven ledgers, the daemon's real adapters, read-only MCP
handlers, guided setup and a doctor.

Nothing here places an order. Key values are only ever passed to the Keychain; results
report key names, never values. Read-only views open the ledger read-only per request.
"""

from __future__ import annotations

import functools
import getpass
import os
import signal
import sqlite3
import sys
import threading
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import (
    bars,
    brief,
    daemon,
    evidence,
    feeds,
    launchd,
    local,
    notify,
    paths,
    pipeline,
    registry,
    secrets,
    web,
)
from . import config as settings
from .common import instant, timestamp, utc_now
from .providers import PAID_PROVIDERS
from .store import Ledger

HISTORY_PADDING_DAYS = 45  # >= 21 sessions of history before the first backfilled filing
LABEL_PADDING_DAYS = 30  # >= 10 sessions of outcome after the last one
PROVIDER_KEYS = {"jev": "TYPESAFE_API_KEY", "openai": "OPENAI_API_KEY"}
ALPACA_KEYS = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
MAX_SETUP_TRIES = 5
SEC_REASON = (
    "SEC's fair-access policy asks automated tools to identify themselves with a name and a "
    "contact email. It is sent only to sec.gov, in the User-Agent header, and kept in "
    "config.json on this Mac."
)
PRESETS = (
    ("rules", "fixed word lists; offline and free; no learned knowledge"),
    ("local", "a model on this Mac through Ollama or LM Studio; loopback only, free"),
    ("jev", "TypeSafe JEV; paid, sends selected filing text to TypeSafe"),
    ("openai", "OpenAI; paid, sends selected filing text to OpenAI"),
)


# ---------------------------------------------------------------- shared wiring


def model_price(overrides: dict) -> Callable[[str, str], daemon.Prices]:
    """USD per million input and output tokens from the registry plus config declarations."""

    def lookup(provider: str, model: str) -> daemon.Prices:
        entry = registry.lookup(provider, model, overrides)
        return entry["usd_per_million_input_tokens"], entry["usd_per_million_output_tokens"]

    return lookup


def observe_options(config: dict) -> dict:
    """What the observation queue needs from config beyond provider and model."""
    return {
        "base_url": config["local_base_url"] if config["provider"] == "local" else None,
        "overrides": config["model_overrides"],
    }


def daemon_context(ledger, config: dict, strategy: dict, **adapters: Any) -> daemon.Context:
    """The daemon's Context with config-aware observation and pricing; adapters override."""
    config = settings.validate(config)
    wiring: dict[str, Any] = {
        "observe": functools.partial(pipeline.observe_queue, **observe_options(config)),
        "price": model_price(config["model_overrides"]),
    }
    return daemon.Context(ledger=ledger, config=config, strategy=strategy, **{**wiring, **adapters})


def open_ledger(path: str | Path, *, readonly: bool = False) -> Ledger:
    """An existing ledger: app commands never create one; setup does."""
    if not Path(path).is_file():
        raise ValueError(
            f"No ledger at {path}; run `{paths.APP_NAME} setup` first "
            f"(or `{paths.APP_NAME} --db {path} init`)"
        )
    return Ledger(path, readonly=readonly, create=False)


def lookback(now: str) -> str:
    """The web page's window: long enough that Monday still shows Friday's filings."""
    return timestamp((instant(now) - web.LOOKBACK).isoformat())


def run_once(job: str, ledger_path: str, config: dict, strategy: dict, **adapters: Any) -> dict:
    """One daemon job from the command line, recorded like the daemon's own runs."""
    try:
        with daemon.single_writer():
            with open_ledger(ledger_path) as ledger:
                return daemon.run_job(job, daemon_context(ledger, config, strategy, **adapters))
    except daemon.AlreadyRunning:
        raise ValueError(
            f"The background service is running and does the {job} job itself; "
            f"see `{paths.APP_NAME} doctor`"
        ) from None


def run_daemon(ledger_path: str, config: dict, strategy: dict, **adapters: Any) -> dict:
    """Run until SIGTERM/SIGINT; another running daemon is a clean exit, not a crash."""
    stop = threading.Event()
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        for number in previous:
            signal.signal(number, lambda *_: stop.set())
        with open_ledger(ledger_path) as ledger:
            ctx = daemon_context(ledger, config, strategy, **adapters)
            try:
                daemon.run_forever(ctx, stop=stop)
            except daemon.AlreadyRunning as exc:
                # launchd restarts only failed exits; a second copy must not loop.
                print(f"{paths.APP_NAME}: {exc}", file=sys.stderr)
                return {"stopped": True, "already_running": True}
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    return {"stopped": True, "already_running": False}


def compose_brief(
    ledger_path: str,
    config: dict,
    *,
    now: str,
    since: str | None = None,
    send: bool = False,
    notifier: Callable[[str, str], bool] | None = None,
) -> dict:
    """The brief over (since, now] (default: the web page's window); optionally notify."""
    with open_ledger(ledger_path, readonly=True) as ledger:
        report = brief.compose(
            ledger, now=now, since=since or lookback(now), watchlist=list(config["watchlist"])
        )
    if send:
        title, body = brief.render_text(report)
        report["notified"] = bool((notifier or notify.macos)(title, body))
    return report


def mcp_handlers(
    ledger_path: str, config: dict, *, clock: Callable[[], str] = utc_now
) -> dict[str, Callable[[dict], dict]]:
    """Real MCP tools over a read-only ledger opened per call; cards carry no raw text."""
    watchlist = list(config["watchlist"])

    def reading(tool: Callable[[Any, str, dict], dict]) -> Callable[[dict], dict]:
        def handler(arguments: dict) -> dict:
            now = timestamp(clock())
            with web.open_readonly(ledger_path) as ledger:
                return tool(ledger, now, arguments)

        return handler

    def today(ledger, now: str, _: dict) -> dict:
        return brief.compose(ledger, now=now, since=lookback(now), watchlist=watchlist)

    def explain(ledger, now: str, arguments: dict) -> dict:
        return brief.filing_card(ledger, arguments["event_id"], now=now)

    def report(ledger, now: str, _: dict) -> dict:
        return evidence.scoreboard(ledger, as_of=now)

    def health(ledger, now: str, _: dict) -> dict:
        return {**brief.health(ledger, now=now), "ledger": _integrity(ledger)}

    def search(ledger, now: str, arguments: dict) -> dict:
        return brief.search(
            ledger,
            now=now,
            ticker=arguments.get("symbol"),
            since=arguments.get("since"),
            limit=arguments.get("limit", 10),
        )

    return {
        "today_brief": reading(today),
        "explain_filing": reading(explain),
        "evidence_report": reading(report),
        "health": reading(health),
        "search_filings": reading(search),
    }


def _integrity(ledger) -> dict:
    if not hasattr(ledger, "verify"):
        return {"ok": None, "detail": "this store cannot verify its chain"}
    result = ledger.verify()
    return {
        "ok": result["ok"],
        "records": result["records"],
        "chain_length": result["chain_length"],
        "head": result["head"],
        "problems": len(result["problems"]),
    }


def verify_ledger(ledger_path: str, *, anchor: dict | None = None) -> dict:
    with open_ledger(ledger_path, readonly=True) as ledger:
        return {"ledger": str(ledger_path), **ledger.verify(anchor=anchor)}


def backfill(
    ledger_path: str,
    config: dict,
    start: date,
    end: date,
    *,
    symbols: list[str] | None = None,
    max_filings: int = 500,
    with_bars: bool = False,
    sec_transport: Callable | None = None,
    bars_transport: Callable | None = None,
) -> dict:
    """Historical filings (and optionally bars) into the research ledger, never the forward one."""
    agent = config["sec_user_agent"]
    if not agent:
        raise ValueError(f"Set your SEC name and email first: {paths.APP_NAME} setup")
    if Path(ledger_path).resolve() == settings.ledger_path(config).resolve():
        raise ValueError("backfill writes historical records; use the research ledger")
    if symbols is None and config["universe"] == "watchlist":
        if not config["watchlist"]:
            raise ValueError("The watchlist is empty; pass --symbols or set universe to all")
        symbols = list(config["watchlist"])
    wanted = set(symbols) if symbols is not None else None
    with open_ledger(ledger_path) as ledger:
        result = feeds.backfill(
            ledger,
            agent,
            start,
            end,
            symbols=wanted,
            transport=sec_transport,
            max_filings=max_filings,
        )
        if with_bars:
            names = sorted(
                {
                    event["symbol"]
                    for event in ledger.all("disclosures")
                    if start.isoformat() <= event["published_at"][:10] <= end.isoformat()
                }
                | (wanted or set())
            )
            today = instant(utc_now()).date()
            result["bars"] = bars.fetch_historical(
                ledger,
                names,
                start - timedelta(days=HISTORY_PADDING_DAYS),
                min(end + timedelta(days=LABEL_PADDING_DAYS), today),
                transport=bars_transport,
                feed=config["alpaca_feed"],
            )
    return result


# ---------------------------------------------------------------- service


def program_args(*, ledger: str | None = None, strategy: str | None = None) -> list[str]:
    args = [sys.executable, "-m", paths.APP_NAME]
    if ledger:
        args += ["--db", str(Path(ledger).expanduser().resolve())]
    if strategy:
        args += ["--strategy", str(Path(strategy).expanduser().resolve())]
    return [*args, "daemon"]


def up(
    config: dict,
    *,
    ledger_path: str,
    program: list[str],
    runner: launchd.Runner | None = None,
    home: Path | None = None,
) -> dict:
    """Install and start the LaunchAgent; setup must have created the ledger first."""
    if not config["sec_user_agent"]:
        raise ValueError(f"Run `{paths.APP_NAME} setup` first: the SEC name and email are not set")
    open_ledger(ledger_path).close()
    return launchd.install(program, runner=runner, home=home)


def down(*, runner: launchd.Runner | None = None, home: Path | None = None) -> dict:
    return launchd.uninstall(runner=runner, home=home)


# ---------------------------------------------------------------- doctor


def doctor(
    ledger_path: str | None = None,
    *,
    launchd_runner: launchd.Runner | None = None,
    keychain_runner: secrets.Runner | None = None,
    local_transport: Callable | None = None,
    home: Path | None = None,
) -> dict:
    """Every check that can fail quietly, in one report; names of keys, never values."""
    problems: list[str] = []
    notes: list[str] = []
    checks: dict[str, Any] = {}
    try:
        config = settings.load()
        checks["config"] = {"ok": True, "exists": paths.config_path().is_file()}
        if not checks["config"]["exists"]:
            notes.append(f"No config yet; run `{paths.APP_NAME} setup`")
    except ValueError as exc:
        config = settings.validate({})
        checks["config"] = {"ok": False, "error": str(exc)}
        problems.append(f"Config: {exc}")
    target = ledger_path or str(settings.ledger_path(config))
    checks["sec_user_agent"] = {"ok": bool(config["sec_user_agent"])}
    if not config["sec_user_agent"]:
        problems.append("SEC name and email are not set; collection is skipped")
    checks["ledger"] = _ledger_check(target, problems)
    checks["provider"] = _provider_check(config, problems, local_transport)
    checks["keys"] = _key_check(config, problems, keychain_runner)
    checks["service"] = _service_check(launchd_runner, home, notes)
    return {
        "ok": not problems,
        "app_dir": str(paths.app_dir()),
        "config_path": str(paths.config_path()),
        "ledger_path": target,
        "checks": checks,
        "problems": problems,
        "notes": notes,
    }


def _ledger_check(target: str, problems: list[str]) -> dict:
    try:
        with open_ledger(target, readonly=True) as ledger:
            integrity = ledger.verify()
            runs = {
                job: {key: run.get(key) for key in ("status", "started_at", "error")}
                for job, run in daemon.last_runs(ledger).items()
            }
            health = brief.health(ledger, now=utc_now())
    except (ValueError, sqlite3.Error) as exc:
        problems.append(f"Ledger: {exc}")
        return {"ok": False, "error": str(exc)}
    if not integrity["ok"]:
        problems.append(f"Ledger chain: {len(integrity['problems'])} problem(s); run verify")
    if health["attention"]:
        problems.append(f"Jobs needing attention: {', '.join(health['attention'])}")
    return {
        "ok": integrity["ok"],
        "records": integrity["records"],
        "chain_length": integrity["chain_length"],
        "head": integrity["head"],
        "problems": integrity["problems"][:5],
        "health": health["state"],
        "last_runs": runs,
    }


def _provider_check(config: dict, problems: list[str], transport: Callable | None) -> dict:
    provider, model = config["provider"], settings.resolved_model(config)
    result: dict[str, Any] = {"provider": provider, "model": model}
    if provider == "local":
        health = local.health_check(config["local_base_url"], model, transport=transport)
        result.update(health)
        if not health["ok"]:
            problems.append(f"Local engine: {health['detail']}")
    elif provider in PAID_PROVIDERS:
        prices = model_price(config["model_overrides"])(provider, model)
        result.update(zip(registry.PRICES, prices))
        result["spend_cap_usd_month"] = config["spend_cap_usd_month"]
        if None in prices:
            problems.append(
                f"No input- and output-token prices are declared for {provider}:{model}; the "
                "service keeps paid extraction off until model_overrides declares both"
            )
    return result


def _key_check(config: dict, problems: list[str], runner: secrets.Runner | None) -> dict:
    try:
        secrets.export_to_environ(runner=runner)
    except (RuntimeError, ValueError) as exc:
        problems.append(f"Keychain: {exc}")
    present = [name for name in secrets.KNOWN if os.environ.get(name)]
    needed = [PROVIDER_KEYS[config["provider"]]] if config["provider"] in PROVIDER_KEYS else []
    if config["bars_source"] == "alpaca":
        needed += list(ALPACA_KEYS)
    missing = [name for name in needed if name not in present]
    if missing:
        problems.append(f"Missing keys: {', '.join(missing)} (setup can store them)")
    return {"ok": not missing, "present": present, "missing": missing}


def _service_check(runner: launchd.Runner | None, home: Path | None, notes: list[str]) -> dict:
    try:
        status = launchd.status(runner=runner, home=home)
    except (ValueError, RuntimeError) as exc:
        notes.append(f"Background service: {exc}")
        return {"available": False}
    if not status["loaded"]:
        notes.append(f"The background service is not running; start it with `{paths.APP_NAME} up`")
    return {"available": True, **status}


# ---------------------------------------------------------------- setup


class _Dialog:
    """Prompts with defaults; an invalid answer is explained and asked again."""

    def __init__(self, ask: Callable[[str], str], secret: Callable[[str], str], say):
        self.ask, self.secret, self.say = ask, secret, say

    def text(self, question: str, default: str, parse: Callable[[str], Any]) -> Any:
        shown = f" [{default}]" if default else ""
        for _ in range(MAX_SETUP_TRIES):
            answer = self._read(self.ask, f"{question}{shown}: ").strip()
            try:
                return parse(answer or default)
            except ValueError as exc:
                self.say(f"  {exc}")
        raise ValueError(f"Setup stopped after {MAX_SETUP_TRIES} invalid answers")

    def yes(self, question: str, default: bool) -> bool:
        def parse(answer: str) -> bool:
            if answer.lower() in ("y", "yes"):
                return True
            if answer.lower() in ("n", "no"):
                return False
            raise ValueError("Answer y or n")

        return self.text(f"{question} (y/n)", "y" if default else "n", parse)

    def hidden(self, question: str) -> str:
        return self._read(self.secret, question).strip()

    @staticmethod
    def _read(reader: Callable[[str], str], prompt: str) -> str:
        try:
            return reader(prompt)
        except (EOFError, KeyboardInterrupt):
            raise ValueError("Setup cancelled; nothing was saved") from None


def setup(
    *,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    say: Callable[[str], None] = print,
    keychain_runner: secrets.Runner | None = None,
    launchd_runner: launchd.Runner | None = None,
    local_transport: Callable | None = None,
    program: list[str] | None = None,
    home: Path | None = None,
) -> dict:
    """Interactive onboarding; config is saved only after every answer is valid."""
    dialog = _Dialog(ask, ask_secret, say)
    try:
        config = settings.load()
    except ValueError as exc:
        say(f"The existing config is invalid ({exc}); starting from defaults.")
        config = settings.validate({})
    say(f"{paths.APP_NAME} setup. Research tool, not investment advice; it never places orders.")
    say(SEC_REASON)
    config["sec_user_agent"] = dialog.text(
        "Your name and email for SEC",
        config["sec_user_agent"],
        lambda value: _checked(config, "sec_user_agent", value, required=True),
    )
    config["watchlist"] = dialog.text(
        "Watchlist symbols, comma separated ('none' to clear)",
        ", ".join(config["watchlist"]),
        lambda value: _checked(config, "watchlist", _symbols(value)),
    )
    config["universe"] = dialog.text(
        "Score only the watchlist or every qualifying 8-K (watchlist/all)",
        config["universe"] if config["watchlist"] else "all",
        lambda value: _checked(config, "universe", value.lower()),
    )
    _choose_provider(dialog, config, local_transport)
    alpaca = dialog.yes(
        "Fetch completed daily bars from Alpaca's free market data (needs Alpaca paper-account "
        "keys; without bars nothing can be scored)",
        config["bars_source"] == "alpaca",
    )
    config["bars_source"] = "alpaca" if alpaca else "none"
    config["brief_time"] = dialog.text(
        "Weekday brief time, New York (HH:MM)",
        config["brief_time"],
        lambda value: _checked(config, "brief_time", value),
    )
    config["notify"] = dialog.yes("Show the brief as a macOS notification", config["notify"])
    config = settings.validate(config)
    stored = _store_keys(dialog, config, keychain_runner)
    settings.save(config)
    ledger = settings.ledger_path(config)
    for path in (ledger, paths.research_ledger_path()):
        Ledger(path).close()
    say(f"Saved {paths.config_path()} and created {ledger}.")
    service = None
    if dialog.yes("Start the background service now", True):
        try:
            service = up(
                config,
                ledger_path=str(ledger),
                program=program or program_args(),
                runner=launchd_runner,
                home=home,
            )
            say(f"Started. Stop it any time with `{paths.APP_NAME} down`.")
        except (ValueError, RuntimeError) as exc:
            say(f"The service was not started: {exc}")
    return {
        "config_path": str(paths.config_path()),
        "ledger_path": str(ledger),
        "research_ledger_path": str(paths.research_ledger_path()),
        "provider": config["provider"],
        "model": settings.resolved_model(config),
        "keys_stored": stored,
        "service": service,
    }


def _checked(config: dict, key: str, value: object, *, required: bool = False) -> Any:
    if required and not value:
        raise ValueError(f"{key} is required")
    return settings.validate({**config, key: value})[key]


def _symbols(value: str) -> list[str]:
    if value.strip().lower() == "none":
        return []
    return [part for part in value.replace(",", " ").split() if part]


def _choose_provider(dialog: _Dialog, config: dict, transport: Callable | None) -> None:
    names = [name for name, _ in PRESETS]
    while True:
        for number, (name, text) in enumerate(PRESETS, 1):
            dialog.say(f"  {number}) {name}: {text}")
        choice = dialog.text(
            "Text features from",
            str(names.index(config["provider"]) + 1),
            lambda value: names[int(value) - 1] if value in ("1", "2", "3", "4") else _bad(),
        )
        candidate = {**config, "provider": choice, "model": None}
        if choice == "local":
            candidate["local_base_url"] = dialog.text(
                "Local engine URL (Ollama :11434/v1, LM Studio :1234/v1)",
                config["local_base_url"],
                lambda value: _checked(candidate, "local_base_url", value),
            )
        default_model = settings.DEFAULT_MODELS.get(choice, "")
        current = config["model"] if config["provider"] == choice and config["model"] else ""
        model = default_model  # the rules baseline has exactly one version
        if choice != "rules":
            model = dialog.text(
                "Model",
                current or default_model,
                lambda value: _checked(
                    candidate, "model", value or None, required=choice == "openai"
                ),
            )
        candidate["model"] = None if model == default_model else model
        if choice == "local" and not _local_ready(dialog, candidate, transport):
            continue
        if choice in PAID_PROVIDERS:
            _paid_settings(dialog, candidate)
        config.update(settings.validate(candidate))
        return


def _bad() -> str:
    raise ValueError("Choose 1, 2, 3 or 4")


def _local_ready(dialog: _Dialog, config: dict, transport: Callable | None) -> bool:
    model = settings.resolved_model(config)
    dialog.say(f"Checking {model} at {config['local_base_url']} ...")
    health = local.health_check(config["local_base_url"], model, transport=transport)
    dialog.say(f"  {health['detail']}")
    return health["ok"] or dialog.yes("Keep this engine anyway", False)


def _paid_settings(dialog: _Dialog, config: dict) -> None:
    key = f"{config['provider']}:{settings.resolved_model(config)}"
    config["spend_cap_usd_month"] = dialog.text(
        "Monthly spend cap in USD (0 turns paid extraction off)",
        f"{config['spend_cap_usd_month']:g}",
        lambda value: _checked(config, "spend_cap_usd_month", _number(value)),
    )
    declared = config["model_overrides"].get(key, {})
    dialog.say(
        f"The service estimates spend from declared input and output prices; without both "
        f"it keeps {key} off."
    )
    kept = {k: v for k, v in declared.items() if k not in registry.PRICES}
    for field in registry.PRICES:
        kind = "input" if field == "usd_per_million_input_tokens" else "output"
        price = dialog.text(
            f"USD per million {kind} tokens for {key} ('none' if unknown)",
            "none" if declared.get(field) is None else f"{declared[field]:g}",
            lambda value: None if value.lower() == "none" else _number(value),
        )
        if price is not None:
            kept[field] = price
    overrides = {k: v for k, v in config["model_overrides"].items() if k != key}
    if kept.keys() - {"source"}:
        overrides[key] = kept
    config["model_overrides"] = _checked(config, "model_overrides", overrides)


def _number(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise ValueError("Enter a number") from None


def _store_keys(dialog: _Dialog, config: dict, runner: secrets.Runner | None) -> list[str]:
    needed = [PROVIDER_KEYS[config["provider"]]] if config["provider"] in PROVIDER_KEYS else []
    if config["bars_source"] == "alpaca":
        needed += list(ALPACA_KEYS)
    stored = []
    for name in needed:
        try:
            known = secrets.get(name, runner=runner) is not None
        except (RuntimeError, ValueError):
            known = False
        if known and not dialog.yes(f"{name} is already available; replace it", False):
            continue
        value = dialog.hidden(f"{name} for the Keychain (hidden; blank to skip): ")
        if not value:
            continue
        try:
            secrets.set(name, value, runner=runner)
        except (RuntimeError, ValueError) as exc:
            dialog.say(f"  {name} was not stored: {exc}")
            continue
        stored.append(name)
    return stored
