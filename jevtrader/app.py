"""The local app's wiring: config-driven ledgers, the daemon's real adapters, read-only MCP
handlers, guided setup and a doctor.

Nothing here places an order. Key values are only ever passed to the Keychain; results
report key names, never values. Read-only views open the ledger read-only per request.
"""

from __future__ import annotations

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
    registry,
    secrets,
    web,
)
from . import config as settings
from .common import instant, timestamp, utc_now
from .providers import PAID_PROVIDERS
from .research import VERSION
from .store import Ledger

HISTORY_PADDING_DAYS = 45  # >= 21 sessions of history before the first backfilled filing
LABEL_PADDING_DAYS = 30  # >= 10 sessions of outcome after the last one
PROVIDER_KEYS = {"jev": "TYPESAFE_API_KEY", "openai": "OPENAI_API_KEY"}
ALPACA_KEYS = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
MAX_SETUP_TRIES = 5
SEC_REASON = (
    "SEC's fair-access policy asks automated tools to declare a contact. Use a dedicated "
    "alias, such as 'jevtrader sec-alias@your-domain.example', rather than a personal "
    "address. It is sent only to sec.gov, in the User-Agent header, and kept in "
    "config.json on this Mac."
)
NO_BARS = (
    "bars_source is none: filings are collected but never scored (no market data); choose "
    f"Alpaca bars in `{paths.APP_NAME} setup` or import forward bars yourself"
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


observe_options = daemon.observe_options


def daemon_context(ledger, config: dict, strategy: dict, **adapters: Any) -> daemon.Context:
    """The daemon's Context with config-aware pricing; Context wires observation from config."""
    config = settings.validate(config)
    wiring: dict[str, Any] = {"price": model_price(config["model_overrides"])}
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
        with daemon.single_writer(role="job"):
            with open_ledger(ledger_path) as ledger:
                return daemon.run_job(job, daemon_context(ledger, config, strategy, **adapters))
    except daemon.AlreadyRunning as exc:
        if exc.holder != "daemon":
            raise ValueError(f"{exc}; try again when it finishes") from None
        raise ValueError(
            f"The background service is running and does the {job} job itself; "
            f"see `{paths.APP_NAME} doctor`"
        ) from None


def run_daemon(ledger_path: str, config: dict, strategy: dict, **adapters: Any) -> dict:
    """Run until SIGTERM/SIGINT; another running daemon is a clean exit, not a crash.

    A one-off job holding the lock is waited for; if it outlasts the wait, AlreadyRunning
    propagates and the command fails, so launchd (which restarts only failed exits) retries.
    """
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
                if exc.holder != "daemon":
                    raise
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
            watchlist=watchlist,
        )

    return {
        "today_brief": reading(today),
        "explain_filing": reading(explain),
        "evidence_report": reading(report),
        "health": reading(health),
        "search_filings": reading(search),
    }


def _integrity(ledger) -> dict:
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
    benchmark: str = bars.BENCHMARK,
    sec_transport: Callable | None = None,
    bars_transport: Callable | None = None,
) -> dict:
    """Historical filings (and optionally bars, with the strategy's ``benchmark``) into the
    research ledger, never the forward one."""
    agent = config["sec_user_agent"]
    if not agent:
        raise ValueError(f"Declare a contact for SEC first: {paths.APP_NAME} setup")
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
                benchmark=benchmark,
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
        raise ValueError(f"Run `{paths.APP_NAME} setup` first: no contact for SEC is declared")
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
    """Every check that can fail quietly, in one report; names of keys, never values.

    A config that does not load skips the checks that read it: their defaults would report
    problems that are not there.
    """
    problems: list[str] = []
    notes: list[str] = []
    checks: dict[str, Any] = {}
    config: dict | None
    try:
        config = settings.load()
        checks["config"] = {"ok": True, "exists": paths.config_path().is_file()}
        if not checks["config"]["exists"]:
            notes.append(f"No config yet; run `{paths.APP_NAME} setup`")
    except ValueError as exc:
        config = None
        checks["config"] = {"ok": False, "error": str(exc)}
        problems.append(f"Config: {exc}")
    skipped = {"ok": None, "skipped": "the config did not load"}
    target = ledger_path or (str(settings.ledger_path(config)) if config is not None else None)
    if config is not None:
        checks["sec_user_agent"] = {"ok": bool(config["sec_user_agent"])}
        if not config["sec_user_agent"]:
            problems.append("No contact for SEC is declared; collection is skipped")
    checks["ledger"] = _ledger_check(target, config, problems, notes) if target else skipped
    if config is None:
        for name in ("sec_user_agent", "provider", "keys", "bars"):
            checks[name] = skipped
    else:
        checks["provider"] = _provider_check(config, problems, local_transport)
        checks["keys"] = _key_check(config, problems, keychain_runner)
        checks["bars"] = {"ok": config["bars_source"] != "none", "source": config["bars_source"]}
        if config["bars_source"] == "none":
            problems.append(NO_BARS)
    checks["service"] = _service_check(launchd_runner, home, problems, notes)
    return {
        "ok": not problems,
        "app_dir": str(paths.app_dir()),
        "config_path": str(paths.config_path()),
        "ledger_path": target,
        "checks": checks,
        "problems": problems,
        "notes": notes,
    }


def _ledger_check(target: str, config: dict | None, problems: list[str], notes: list[str]) -> dict:
    try:
        with open_ledger(target, readonly=True) as ledger:
            integrity = ledger.verify()
            runs = {
                job: {key: run.get(key) for key in ("status", "started_at", "error")}
                for job, run in daemon.last_runs(ledger).items()
            }
            health = brief.health(ledger, now=utc_now())
            calibrator = (
                _calibrator_check(ledger, target, config, problems, notes) if config else None
            )
    except (ValueError, sqlite3.Error) as exc:
        problems.append(f"Ledger: {exc}")
        return {"ok": False, "error": str(exc)}
    if not integrity["ok"]:
        problems.append(f"Ledger chain: {len(integrity['problems'])} problem(s); run verify")
    jobs = [job for job in health["attention"] if job != brief.SERVICE_ATTENTION]
    if jobs:
        problems.append(f"Jobs needing attention: {', '.join(jobs)}")
    if health["stale"]:
        problems.append(health["stale_note"])
    return {
        "ok": integrity["ok"],
        "records": integrity["records"],
        "chain_length": integrity["chain_length"],
        "head": integrity["head"],
        "problems": integrity["problems"][:5],
        "health": health["state"],
        "last_runs": runs,
        "calibrator": calibrator,
    }


def _calibrator_check(
    ledger, target: str, config: dict, problems: list[str], notes: list[str]
) -> dict:
    """The configured calibrator must exist here, be current and fit the service's features."""
    model_id = config["calibrator"]
    if model_id is None:
        notes.append(
            "No calibrator is configured, so every service decision is WATCH and no call counts "
            "toward the evidence gate. Once enough forward outcomes have matured, fit one on "
            f"this ledger (`{paths.APP_NAME} --db {target} fit --extractor-key KEY --mode "
            'forward`), set "calibrator" in config.json and restart the service; see the README'
        )
        return {"id": None}
    model = ledger.get("models", model_id)
    if model is None:
        problems.append(
            f"Calibrator {model_id} is not in {target}; the service's observe runs fail until "
            "config names a model from `status` on this ledger"
        )
        return {"id": model_id, "ok": False}
    if model.get("version") != VERSION:
        problems.append(f"Calibrator {model_id} was fit by {model.get('version')}; re-fit it")
        return {"id": model_id, "ok": False}
    # The extractor key digests the provider and requested model, so one forecast names them.
    trained = next(
        (f for f in ledger.all("forecasts") if f["extractor_key"] == model["extractor_key"]),
        None,
    )
    extraction = ledger.get("extractions", trained["extraction_id"]) if trained else None
    spec = extraction["spec"] if extraction else {}
    fitted = (spec.get("provider"), spec.get("requested_model"))
    service = (config["provider"], settings.resolved_model(config))
    if extraction and fitted != service:
        problems.append(
            f"Calibrator {model_id} was fit on {fitted[0]}:{fitted[1]} features, but the "
            f"service uses {service[0]}:{service[1]}; every decision would be rejected"
        )
        return {"id": model_id, "ok": False}
    return {"id": model_id, "ok": True, "cutoff": model.get("cutoff")}


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


def _service_check(
    runner: launchd.Runner | None, home: Path | None, problems: list[str], notes: list[str]
) -> dict:
    try:
        status = launchd.status(runner=runner, home=home)
    except (ValueError, RuntimeError) as exc:
        notes.append(f"Background service: {exc}")
        return {"available": False}
    log = paths.log_dir() / "daemon.err.log"
    code = status["last_exit_code"]
    exited = f" (last exit code {code})" if code not in (None, 0) else ""
    if not status["loaded"]:
        if status["installed"]:
            problems.append(
                f"The background service is installed but not loaded; start it with "
                f"`{paths.APP_NAME} up`"
            )
        else:
            notes.append(
                f"The background service is not running; start it with `{paths.APP_NAME} up`"
            )
    elif status["state"] != "running":
        state = status["state"] or "in an unknown state"
        problems.append(f"The background service is loaded but {state}{exited}; see {log}")
    elif exited:
        notes.append(f"The background service restarted after an exit{exited}; see {log}")
    return {"available": True, **status}


# ---------------------------------------------------------------- setup


class _Cancelled(ValueError):
    """End of input or Ctrl-C at a prompt; setup saves nothing until the last answer."""


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
            raise _Cancelled("Setup cancelled; nothing was saved") from None


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
    """Interactive onboarding. Keys, config and ledgers are saved only once every answer is
    in, so a cancelled setup saves nothing; cancelling the final question (start the
    service?) keeps what was saved and leaves the service stopped."""
    dialog = _Dialog(ask, ask_secret, say)
    try:
        config = settings.load()
    except ValueError as exc:
        say(f"The existing config is invalid ({exc}); starting from defaults.")
        config = settings.validate({})
    say(f"{paths.APP_NAME} setup. Research tool, not investment advice; it never places orders.")
    say(SEC_REASON)
    config["sec_user_agent"] = dialog.text(
        "A contact for SEC (a dedicated alias is recommended)",
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
    keys = _ask_keys(dialog, config, keychain_runner)
    # Every answer is in: from here on things are saved, so no prompt may cancel them.
    stored = _store_keys(dialog, keys, keychain_runner)
    settings.save(config)
    ledger = settings.ledger_path(config)
    for path in (ledger, paths.research_ledger_path()):
        Ledger(path).close()
    say(f"Saved {paths.config_path()} and created {ledger}.")
    if config["bars_source"] == "none":
        say(f"Note: {NO_BARS}.")
    service = None
    try:
        start = dialog.yes("Start the background service now", True)
    except ValueError:  # cancelled, or no valid answer: the saved setup stands
        start = False
        say(f"The service was not started; start it any time with `{paths.APP_NAME} up`.")
    if start:
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


def _ask_keys(
    dialog: _Dialog, config: dict, runner: secrets.Runner | None
) -> list[tuple[str, str]]:
    """Key values to store, asked before anything is saved; nothing is stored here."""
    needed = [PROVIDER_KEYS[config["provider"]]] if config["provider"] in PROVIDER_KEYS else []
    if config["bars_source"] == "alpaca":
        needed += list(ALPACA_KEYS)
    typed = []
    for name in needed:
        try:
            known = secrets.get(name, runner=runner) is not None
        except (RuntimeError, ValueError):
            known = False
        if known and not dialog.yes(f"{name} is already available; replace it", False):
            continue
        value = dialog.hidden(f"{name} for the Keychain (hidden; blank to skip): ")
        if value:
            typed.append((name, value))
    return typed


def _store_keys(
    dialog: _Dialog, keys: list[tuple[str, str]], runner: secrets.Runner | None
) -> list[str]:
    stored = []
    for name, value in keys:
        try:
            secrets.set(name, value, runner=runner)
        except (RuntimeError, ValueError) as exc:
            dialog.say(f"  {name} was not stored: {exc}")
            continue
        stored.append(name)
    return stored
