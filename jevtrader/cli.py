"""One explicit CLI; no daemons, broker credentials, or hidden provider calls."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .common import digest, instant, load_strategy, timestamp, utc_now
from .demo import run_demo
from .engine import ObservationRejected, evaluate, observe, settle, train
from .lab import autoresearch, experiment, generate_proposal
from .market import import_bars
from .paper import plan_order
from .providers import ProviderInputError, require_credentials
from .sec import collect_disclosures
from .store import KINDS, Ledger


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(description="Auditable disclosure research and paper order plans")
    app.add_argument(
        "--db",
        default="data/jevtrader.sqlite",
        help="SQLite ledger (default: data/jevtrader.sqlite)",
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
    collect.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT", ""))
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
    _provider_options(observations)
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
    return app


def _provider_options(command, default="rules"):
    command.add_argument("--provider", choices=["rules", "jev", "openai"], default=default)
    command.add_argument(
        "--model", help="Explicit provider model; OpenAI requires this or OPENAI_MODEL"
    )


def _model(args) -> str:
    result = (
        args.model
        or {"rules": "rules-v1", "jev": "jev-1.13.0", "openai": os.environ.get("OPENAI_MODEL")}[
            args.provider
        ]
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
                    key: model[key]
                    for key in (
                        "model_id",
                        "extractor_key",
                        "training_count",
                        "training_modes",
                        "cutoff",
                    )
                }
                for model in ledger.all("models")
            ],
            "modes": sorted({f["mode"] for f in forecasts}),
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
            args.cik, args.symbol, user_agent=args.user_agent, limit=args.limit
        )
        return {
            "collected": len(records),
            "added": sum(ledger.disclosure(r, imported=False) for r in records),
            "event_ids": [r["id"] for r in records],
        }
    if command == "observe":
        if not 1 <= args.limit <= 200:
            raise ValueError("--limit must be between 1 and 200")
        if args.as_of and not args.event:
            raise ValueError("--as-of requires one --event")
        model = _model(args)
        require_credentials(args.provider)
        calibrator = None
        if args.calibrator:
            calibrator = ledger.get("models", args.calibrator)
            if calibrator is None:
                raise ValueError(f"Unknown calibrator: {args.calibrator}")
        events = sorted(ledger.all("disclosures"), key=lambda e: (e["first_seen_at"], e["id"]))
        skipped = {"requires_replay": 0, "calibrator_ineligible": 0}
        if args.event:
            events = [e for e in events if e["id"] == args.event]
            if not events:
                raise ValueError("Disclosure not found")
        else:
            if not args.replay:
                # A live clock never decides historical or synthetic records; replay them.
                skipped["requires_replay"] = sum(e["mode"] != "forward" for e in events)
                events = [e for e in events if e["mode"] == "forward"]
            done = {
                (f["event_id"], f["mode"])
                for f in ledger.all("forecasts")
                if f["provider"] == args.provider
                and f["strategy"] == strategy
                and f["calibrator_id"] == args.calibrator
                and ledger.get("extractions", f["extraction_id"])["spec"]["requested_model"]
                == model
            }
            events = [
                e
                for e in events
                if (
                    e["id"],
                    "synthetic"
                    if e["mode"] == "synthetic"
                    else "historical"
                    if args.replay or e["mode"] == "historical"
                    else "forward",
                )
                not in done
            ]
            if calibrator:
                # Events first seen by the model's cutoff (or used to train it) cannot get a
                # timely calibrated decision; keep them from consuming --limit.
                eligible = [
                    e
                    for e in events
                    if instant(e["first_seen_at"]) > instant(calibrator["cutoff"])
                    and e["id"] not in calibrator["training_event_ids"]
                ]
                skipped["calibrator_ineligible"] = len(events) - len(eligible)
                events = eligible
        records, errors, attempted = [], [], 0
        for event in events:
            if attempted >= args.limit:
                break
            try:
                result = observe(
                    ledger,
                    event["id"],
                    strategy,
                    provider=args.provider,
                    model=model,
                    as_of=event["first_seen_at"] if args.replay else args.as_of,
                    calibrator_id=args.calibrator,
                )
                records.append(
                    {
                        key: result[key]
                        for key in (
                            "id",
                            "event_id",
                            "mode",
                            "action",
                            "reasons",
                            "expected_return",
                            "extractor_key",
                        )
                    }
                )
            except (ObservationRejected, ProviderInputError) as exc:
                # Nothing was sent, so report it without letting it block later events.
                errors.append({"event_id": event["id"], "error": str(exc)})
                continue
            except (ValueError, RuntimeError) as exc:
                attempted += 1
                errors.append({"event_id": event["id"], "error": str(exc)})
                if args.provider != "rules":
                    break  # Bound surprise costs after a provider or validation failure.
            else:
                attempted += 1
        return {"forecasts": records, "errors": errors, "skipped": skipped}
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


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        strategy = load_strategy(args.strategy)
        with Ledger(args.db) as ledger:
            result = dispatch(args, ledger, strategy)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 1 if result.get("errors") else 0
    except (ValueError, KeyError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"jevtrader: {exc}", file=sys.stderr)
        return 2
