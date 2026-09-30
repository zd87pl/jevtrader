"""One explicit CLI. It never places orders; paid provider calls happen only when chosen.

Research commands keep their ledger default (data/jevtrader.sqlite); app commands use the
ledger named by config. Only init, demo and setup create a ledger; read-only commands open it
read-only. Keys come from the environment or the macOS Keychain and are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
from datetime import date
from pathlib import Path

from . import app as local_app
from . import config as settings
from . import mcp_server, paths, secrets, web
from .common import digest, load_strategy, symbol, timestamp, utc_now
from .demo import run_demo
from .engine import evaluate, observe, settle, train
from .lab import (
    autoresearch,
    experiment,
    generate_proposal,
    question_diff,
    questions_sha256,
    record_question_approval,
)
from .local import DEFAULT_MODEL as LOCAL_MODEL
from .market import import_bars
from .paper import plan_order
from .pipeline import check_options, observe_queue
from .providers import PROVIDERS
from .research import VERSION
from .sec import collect_disclosures
from .security.question_lint import lint_questions
from .store import KINDS, Ledger

APP = paths.APP_NAME
RESEARCH_DB = "data/jevtrader.sqlite"
CREATES = frozenset({"init", "demo"})
READ_ONLY = frozenset({"status", "show", "evaluate"})
APP_COMMANDS = frozenset(
    {
        "setup",
        "up",
        "down",
        "daemon",
        "poll",
        "bars",
        "brief",
        "serve",
        "mcp",
        "doctor",
        "verify",
        "backfill",
    }
)
# Commands that can use API keys; the Keychain is read only for these.
USES_KEYS = frozenset(
    {"observe", "experiment", "autoresearch", "propose", "bars", "daemon", "backfill"}
)


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(description="Auditable disclosure research and paper order plans")
    app.add_argument(
        "--db",
        help=(
            f"SQLite ledger (default: {RESEARCH_DB} for research commands, the config ledger "
            "for app commands, the research ledger for backfill)"
        ),
    )
    app.add_argument("--strategy", help="Strategy JSON; otherwise packaged conservative defaults")
    commands = app.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create a ledger and display defaults")
    commands.add_parser("demo", help="Run synthetic end-to-end demo in a new, empty ledger")
    commands.add_parser("status", help="Counts and available extractor/model ids")
    show = commands.add_parser("show", help="Inspect one immutable JSON record")
    show.add_argument("kind", choices=sorted(KINDS))
    show.add_argument("id")
    docs = commands.add_parser("import-disclosures", help="Import historical or synthetic JSONL")
    docs.add_argument("file")
    bars = commands.add_parser("import-bars", help="Import raw OHLCV session CSV")
    bars.add_argument("file")
    bars.add_argument(
        "--mode",
        choices=["historical", "forward", "synthetic"],
        default="historical",
        help="Forward stamps actual receipt time; never backdates availability",
    )
    collect = commands.add_parser("collect", help="Read selected non-earnings SEC 8-K disclosures")
    collect.add_argument("--cik", required=True)
    collect.add_argument("--symbol", required=True)
    collect.add_argument("--limit", type=int, default=5)
    collect.add_argument(
        "--user-agent",
        default=None,
        help="Deprecated: argv is visible to other processes; declare the contact for SEC "
        "in config with `setup` (or SEC_USER_AGENT)",
    )
    observations = commands.add_parser(
        "observe", help="Record frozen features and optional calibrated decisions"
    )
    observations.add_argument("--event", help="One disclosure id; otherwise process pending events")
    observations.add_argument("--limit", type=int, default=10)
    replay = observations.add_mutually_exclusive_group()
    replay.add_argument(
        "--replay",
        action="store_true",
        help="Use each source's first_seen time; explicitly historical",
    )
    replay.add_argument("--as-of", help="Explicit replay timestamp; requires --event")
    observations.add_argument(
        "--calibrator", help="Stored ridge model id; without one all events are WATCH"
    )
    observations.add_argument(
        "--max-scan",
        type=int,
        default=200,
        help="Stop after this many local rejections (nothing sent; not counted in --limit)",
    )
    observations.add_argument(
        "--retry-failed",
        action="store_true",
        help="Also queue events whose earlier paid request failed (and may have been billed)",
    )
    _provider_options(observations, local=True)
    labels = commands.add_parser("settle", help="Attach matured next-open/horizon-close labels")
    labels.add_argument("--as-of", help="Evaluation timestamp; default now")
    fit = commands.add_parser("fit", help="Fit a ridge calibrator using only matured labels")
    fit.add_argument("--extractor-key", required=True)
    fit.add_argument("--cutoff", help="Training cutoff; default now")
    fit.add_argument(
        "--mode",
        choices=["forward", "historical", "synthetic"],
        help="Required when the extractor has more than one observation mode",
    )
    evaluate_cmd = commands.add_parser(
        "evaluate", help="Purged walk-forward event comparison, not portfolio P&L"
    )
    evaluate_cmd.add_argument("--extractor-key", required=True)
    evaluate_cmd.add_argument(
        "--before", help="Only labels available before this time; default now"
    )
    evaluate_cmd.add_argument(
        "--mode",
        choices=["forward", "historical", "synthetic"],
        help="Required when the extractor has more than one observation mode",
    )
    paper = commands.add_parser(
        "paper-plan", help="Size one simulation-only order; never sends an order"
    )
    paper.add_argument("--forecast", required=True)
    paper.add_argument("--equity", type=float, default=3000)
    paper.add_argument("--cash", type=float)
    paper.add_argument("--peak-equity", type=float)
    paper.add_argument(
        "--positions", help="JSON list of existing positions: symbol,side,quantity,price"
    )
    paper.add_argument(
        "--shortable",
        action="store_true",
        help="Explicit simulation assumption of available borrow",
    )
    propose = commands.add_parser(
        "propose", help="Ask OpenAI for one question-only strategy candidate"
    )
    propose.add_argument(
        "--trial", required=True, help="Completed development trial id, never a holdout report"
    )
    propose.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    propose.add_argument(
        "--output", required=True, help="New candidate JSON file; existing files are preserved"
    )
    approve = commands.add_parser(
        "approve-questions",
        help="Approve a changed question set after reading its diff (a human step; no LLM tool)",
    )
    source = approve.add_mutually_exclusive_group(required=True)
    source.add_argument("--proposal", help="Proposal id printed by propose or autoresearch")
    source.add_argument("--candidate", help="Candidate strategy JSON file")
    trial = commands.add_parser(
        "experiment", help="Test one question candidate on a locked development universe"
    )
    trial.add_argument("--candidate", required=True)
    trial.add_argument("--development-until", required=True)
    _provider_options(trial, default="jev")
    auto = commands.add_parser(
        "autoresearch", help="Bounded OpenAI-question/JEV-evaluation loop; no auto-promotion"
    )
    auto.add_argument("--development-until", required=True)
    auto.add_argument(
        "--rounds", type=int, default=1, help="1–5 total rounds including baseline; default 1"
    )
    auto.add_argument("--proposal-model", default=os.environ.get("OPENAI_MODEL"))
    _provider_options(auto, default="jev")
    _app_commands(commands)
    return app


def _app_commands(commands) -> None:
    commands.add_parser("setup", help="Guided setup: SEC contact, watchlist, provider, keys")
    commands.add_parser("up", help="Install and start the background service (launchd or systemd)")
    commands.add_parser("down", help="Stop and remove the background service")
    commands.add_parser("daemon", help="Run the background schedule in the foreground")
    commands.add_parser("poll", help="Collect new qualifying 8-Ks once (as the service does)")
    commands.add_parser("bars", help="Fetch completed daily bars once (as the service does)")
    brief = commands.add_parser("brief", help="The pre-market brief as JSON")
    brief.add_argument("--since", help="Filings first seen after this time; default 3 days")
    brief.add_argument("--notify", action="store_true", help="Also show a macOS notification")
    serve = commands.add_parser("serve", help="Read-only localhost web view")
    serve.add_argument("--host", choices=sorted(web.BIND_HOSTS), default="127.0.0.1")
    serve.add_argument("--port", type=int, default=web.DEFAULT_PORT)
    commands.add_parser("mcp", help="Read-only MCP server on stdio")
    commands.add_parser("doctor", help="Check config, ledger, engine, keys and the service")
    verify = commands.add_parser("verify", help="Recompute every record hash and the chain")
    verify.add_argument("--anchor-seq", type=int, help="Seq of an earlier head to check against")
    verify.add_argument("--anchor-hash", help="Chain hash of that earlier head")
    backfill = commands.add_parser(
        "backfill", help="Historical 8-Ks into the research ledger (assumed availability)"
    )
    backfill.add_argument("--start", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    backfill.add_argument("--end", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    backfill.add_argument("--symbols", help="Comma-separated symbols; default: config scope")
    backfill.add_argument("--max-filings", type=int, default=500)
    backfill.add_argument(
        "--bars", action="store_true", help="Also fetch historical Alpaca bars for them"
    )


def _provider_options(command, default="rules", *, local=False):
    choices = [name for name in PROVIDERS if local or name != "local"]
    command.add_argument("--provider", choices=choices, default=default)
    command.add_argument(
        "--model", help="Explicit provider model; OpenAI requires this or OPENAI_MODEL"
    )


def _model(args) -> str:
    result = (
        args.model
        or {
            "rules": "rules-v1",
            "local": LOCAL_MODEL,
            "jev": "jev-1.13.0",
            "openai": os.environ.get("OPENAI_MODEL"),
        }[args.provider]
    )
    if not result:
        raise ValueError("Choose an OpenAI model using --model or OPENAI_MODEL")
    return result


def dispatch(args, ledger: Ledger, strategy: dict) -> dict:
    command = args.command
    if command == "init":
        return {"db": args.db, "strategy": strategy, "counts": ledger.counts()}
    if command == "demo":
        return run_demo(ledger)
    if command == "status":
        forecasts = ledger.all("forecasts")
        return {
            "counts": ledger.counts(),
            "extractor_keys": sorted({f["extractor_key"] for f in forecasts}),
            "models": [
                {
                    key: model.get(key)
                    for key in (
                        "model_id",
                        "version",
                        "extractor_key",
                        "training_count",
                        "training_modes",
                        "cutoff",
                    )
                }
                for model in ledger.all("models")
            ],
            "modes": sorted({f["mode"] for f in forecasts}),
            # Paid requests that failed and may have been billed; `show attempts ID` for detail.
            "failed_attempts": [
                {
                    key: attempt[key]
                    for key in ("id", "event_id", "provider", "requested_model", "attempted_at")
                }
                for attempt in ledger.all("attempts")
            ],
        }
    if command == "show":
        record = ledger.get(args.kind, args.id)
        if record is None:
            raise ValueError("Record not found")
        return record
    if command == "import-disclosures":
        with open(args.file, encoding="utf-8") as source:
            records = [json.loads(line) for line in source if line.strip()]
        return {"added": sum(ledger.disclosure(record) for record in records)}
    if command == "import-bars":
        return {"added": import_bars(ledger, args.file, mode=args.mode)}
    if command == "collect":
        records = collect_disclosures(
            args.cik,
            args.symbol,
            user_agent=_sec_contact(args.user_agent),
            limit=args.limit,
            verify_acceptance=True,
        )
        return {
            "collected": len(records),
            "added": sum(ledger.disclosure(r, imported=False) for r in records),
            "event_ids": [r["id"] for r in records],
        }
    if command == "observe":
        # Option errors keep precedence over model resolution, as before the move.
        check_options(limit=args.limit, max_scan=args.max_scan, event=args.event, as_of=args.as_of)
        model = _model(args)
        options = local_app.observe_options({**settings.load(), "provider": args.provider})
        return observe_queue(
            ledger,
            strategy,
            provider=args.provider,
            model=model,
            replay=args.replay,
            event=args.event,
            as_of=args.as_of,
            calibrator=args.calibrator,
            limit=args.limit,
            max_scan=args.max_scan,
            retry_failed=args.retry_failed,
            observer=observe,  # Looked up here so patching cli.observe still intercepts it.
            **{key: value for key, value in options.items() if value},
        )
    if command == "settle":
        return settle(ledger, as_of=args.as_of)
    if command == "fit":
        return train(ledger, args.extractor_key, args.cutoff or utc_now(), strategy, mode=args.mode)
    if command == "evaluate":
        return evaluate(
            ledger, args.extractor_key, strategy, before=args.before or utc_now(), mode=args.mode
        )
    if command == "paper-plan":
        forecast = ledger.get("forecasts", args.forecast)
        if forecast is None:
            raise ValueError("Forecast not found")
        if forecast.get("calibrator_id"):
            # Observation refuses calibrators from another evaluator; so must sizing.
            scorer = ledger.get("models", forecast["calibrator_id"])
            version = scorer.get("version") if scorer else None
            if version != VERSION:
                raise ValueError(
                    f"Forecast was scored by {version}, not {VERSION}; re-fit and re-observe"
                )
        positions = json.loads(Path(args.positions).read_text()) if args.positions else []
        result = plan_order(
            forecast,
            forecast["strategy"],
            equity=args.equity,
            cash=args.cash,
            peak_equity=args.peak_equity,
            positions=positions,
            shortable=args.shortable,
        )
        result["forecast_id"] = forecast["id"]
        identity = digest(result)
        ledger.put("paper_plans", identity, result)
        return {"id": identity, **result}
    if command == "propose":
        if not args.model:
            raise ValueError("Choose --model or OPENAI_MODEL")
        if Path(args.output).exists():
            raise ValueError("Candidate output already exists; choose a new path")
        output_dir = Path(args.output).parent
        if not output_dir.is_dir() or not os.access(output_dir, os.W_OK):
            # Checked before the paid call: a failed write would lose a proposal slot.
            raise ValueError(f"Output directory does not exist or is not writable: {output_dir}")
        trial = ledger.get("experiments", args.trial)
        if trial is None:
            raise ValueError("Trial not found")
        proposal = generate_proposal(ledger, trial, args.model)
        candidate = proposal["candidate"]
        with open(args.output, "x", encoding="utf-8") as target:
            json.dump(candidate, target, indent=2, allow_nan=False)
            target.write("\n")
        return {
            "proposal_id": proposal["id"],
            "output": str(Path(args.output).resolve()),
            "promoted": False,
        }
    if command == "approve-questions":
        if args.proposal:
            pending = ledger.get("experiments", args.proposal)
            if pending is None or pending.get("type") != "proposal":
                raise ValueError("Proposal not found; rejected proposals cannot be approved")
            questions = pending["candidate"]["questions"]
        else:
            questions = load_strategy(args.candidate)["questions"]
        problems = lint_questions(questions)
        if problems:
            raise ValueError("Question set fails the lint: " + "; ".join(problems))
        sha = questions_sha256(questions)
        # The diff and prompt go to stderr, so stdout stays machine-readable JSON.
        print(question_diff(questions, strategy["questions"]), file=sys.stderr)
        print(
            f"Question set {sha}. Type its first 8 characters to approve: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        if sys.stdin.readline().strip() != sha[:8]:
            raise ValueError(f"Question set {sha[:8]} not approved")
        return record_question_approval(ledger, questions, strategy["questions"])
    if command == "experiment":
        return experiment(
            ledger,
            load_strategy(args.candidate),
            strategy,
            provider=args.provider,
            model=_model(args),
            development_until=timestamp(args.development_until),
        )
    if command == "autoresearch":
        if args.rounds > 1 and not args.proposal_model:
            raise ValueError("Multiple rounds require --proposal-model or OPENAI_MODEL")
        return autoresearch(
            ledger,
            strategy,
            provider=args.provider,
            model=_model(args),
            proposal_model=args.proposal_model or "unused",
            rounds=args.rounds,
            development_until=timestamp(args.development_until),
        )
    raise ValueError(f"Unknown command: {command}")


def app_dispatch(args, strategy: dict) -> dict | None:
    """App commands open their own ledgers: the config's, or the research ledger."""
    command = args.command
    if command == "setup":
        # Setup writes the config's ledger; `up --db` can pin another one for the service.
        return local_app.setup(program=local_app.program_args(strategy=args.strategy))
    if command == "doctor":
        return local_app.doctor(args.db)
    if command == "down":
        return local_app.down()
    config = settings.load()
    db = args.db or str(settings.ledger_path(config))
    if command == "up":
        program = local_app.program_args(ledger=args.db, strategy=args.strategy)
        return local_app.up(config, ledger_path=db, program=program)
    if command == "daemon":
        return local_app.run_daemon(db, config, strategy)
    if command in ("poll", "bars"):
        return local_app.run_once(command, db, config, strategy)
    if command == "brief":
        since = timestamp(args.since) if args.since else None
        return local_app.compose_brief(db, config, now=utc_now(), since=since, send=args.notify)
    if command == "serve":
        web.serve(db, host=args.host, port=args.port, watchlist=config["watchlist"])
        return None
    if command == "mcp":
        mcp_server.serve(local_app.mcp_handlers(db, config))
        return None  # stdout carried the protocol; nothing else may follow it
    if command == "verify":
        if (args.anchor_seq is None) != (args.anchor_hash is None):
            raise ValueError("--anchor-seq and --anchor-hash go together")
        anchor = (
            None
            if args.anchor_seq is None
            else {"seq": args.anchor_seq, "chain_hash": args.anchor_hash}
        )
        return local_app.verify_ledger(db, anchor=anchor)
    if command == "backfill":
        symbols = (
            [symbol(part) for part in args.symbols.split(",") if part.strip()]
            if args.symbols
            else None
        )
        return local_app.backfill(
            args.db or str(paths.research_ledger_path()),
            config,
            args.start,
            args.end,
            symbols=symbols,
            max_filings=args.max_filings,
            with_bars=args.bars,
            benchmark=strategy["benchmark"],
        )
    raise ValueError(f"Unknown command: {command}")


def _ledger(args, *, defaulted: bool = False) -> Ledger:
    if args.command in CREATES:
        return Ledger(args.db)
    if args.db != ":memory:" and not Path(args.db).is_file():
        raise ValueError(_missing(args.db, args.command, defaulted=defaulted))
    return Ledger(args.db, readonly=args.command in READ_ONLY, create=False)


def _missing(db: str, command: str, *, defaulted: bool) -> str:
    """Point at the ledger the user most likely meant, not at an init that makes a stray one."""
    if not defaulted:
        return (
            f"No ledger at {db}; check --db, or create one with `{APP} --db {shlex.quote(db)} init`"
        )
    try:
        service = settings.ledger_path(settings.load())
    except ValueError:
        service = None
    if service is not None and service.is_file():
        return (
            f"No research ledger at {db} (the default here). The service's ledger is {service}; "
            f"use `{APP} --db {shlex.quote(str(service))} {command} ...`"
        )
    return (
        f"No ledger at {db}; run `{APP} setup` for the service's ledger, or `{APP} init` to "
        "start a research ledger here"
    )


def _exit_code(command: str, result: dict) -> int:
    if command == "observe" and result["skipped"]["missing_credentials"]:
        return 2  # A configuration error stopped the run; partial results are printed.
    if command in ("poll", "bars"):
        return 0 if result.get("status") == "ok" else 1
    if command in ("doctor", "verify"):
        return 0 if result.get("ok") else 1
    return 1 if result.get("errors") else 0


def _sec_contact(flag: str | None) -> str:
    """The declared contact for SEC: config first, then SEC_USER_AGENT. There is no default.
    ``--user-agent`` still works but is deprecated, because argv is visible to other
    processes (``ps``)."""
    if flag is not None:
        print(
            "jevtrader: --user-agent is deprecated (argv is visible to other processes); "
            "declare the contact for SEC in config with `jevtrader setup` instead",
            file=sys.stderr,
        )
        return flag
    return settings.load()["sec_user_agent"] or os.environ.get("SEC_USER_AGENT", "")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in USES_KEYS:
            secrets.export_to_environ()
        strategy = load_strategy(args.strategy)
        if args.command in APP_COMMANDS:
            result = app_dispatch(args, strategy)
        else:
            defaulted = args.db is None
            args.db = args.db or RESEARCH_DB
            with _ledger(args, defaulted=defaulted) as ledger:
                result = dispatch(args, ledger, strategy)
        if result is None:
            return 0
        print(json.dumps(result, indent=2, allow_nan=False))
        return _exit_code(args.command, result)
    except (ValueError, KeyError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"jevtrader: {exc}", file=sys.stderr)
        return 2
