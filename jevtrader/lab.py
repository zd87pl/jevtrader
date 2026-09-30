"""Bounded question research inspired by autoresearch's fixed evaluation harness.

One development cutoff is locked per ledger. Only question/name changes are
accepted; the harness, controls, universe and trial budget cannot evolve here.
No candidate is automatically promoted. The ledger remains human-owned.
"""

from __future__ import annotations

import difflib

from .common import digest, instant, timestamp, utc_now, validate_strategy
from .engine import evaluate, observe, settle
from .providers import propose_strategy, require_credentials
from .research import FEATURE_NAMES, VERSION, walk_forward
from .security.question_lint import lint_questions


MAX_TRIALS = 5
MAX_EVENTS = 200
MIN_EVALUATED = 10


class ProposalRejected(ValueError):
    """A paid proposal failed local validation; it is recorded and used its slot."""


def _evaluable_count(labelled: list[tuple[dict, dict]], baseline: dict) -> int:
    """Dry-run the purged walk-forward on label timing alone; no provider calls.

    Purging depends only on decision and label-availability times, which a trial
    reproduces exactly, so features and targets are placeholders here.
    """
    rows = [
        {
            "event_id": forecast["event_id"],
            "symbol": "DRY-RUN",
            "decision_at": forecast["decision_at"],
            "outcome_at": timestamp(
                max(instant(label["outcome_at"]), instant(label["label_available_at"])).isoformat()
            ),
            "features": [0.0] * len(FEATURE_NAMES),
            "target": 0.0,
            "extractor_key": "dry-run",
            "mode": "historical",
        }
        for forecast, label in labelled
    ]
    report = walk_forward(
        rows, min_train=baseline["min_train_samples"], alpha=baseline["ridge_alpha"]
    )
    return report["evaluated_count"]


def development_feedback(trial: dict) -> dict:
    if trial.get("type") != "trial_completed":
        raise ValueError("Proposal feedback must be a completed development trial")
    return {
        "development_until": trial["development_until"],
        "score": trial["score"],
        "metrics": trial["report"]["strategies"],
        "sample_count": trial["report"]["evaluated_count"],
        "instruction": "Propose one economic hypothesis using only question changes; these are adaptive development results.",
    }


def questions_sha256(questions: dict) -> str:
    return digest(questions)


def question_diff(questions: dict, baseline: dict) -> str:
    """A unified diff of question text, one ``name: text`` line per question."""

    def lines(values: dict) -> list[str]:
        return [f"{name}: {values[name]}" for name in sorted(values)]

    return "\n".join(
        difflib.unified_diff(
            lines(baseline), lines(questions), "baseline", "candidate", lineterm=""
        )
    )


def question_approved(ledger, questions: dict) -> bool:
    record = ledger.get("experiments", f"question-approval:{questions_sha256(questions)}")
    return record is not None and record.get("type") == "question_approval"


def record_question_approval(ledger, questions: dict, baseline: dict) -> dict:
    """Record a human's approval of a question set (P0-06). Only the CLI calls this.

    It is never exposed as an MCP or LLM tool: an approval stands for a person having
    read the diff. Approving the same set again returns the existing record.
    """
    sha = questions_sha256(questions)
    identity = f"question-approval:{sha}"
    existing = ledger.get("experiments", identity)
    if existing is not None:
        return existing
    record = {
        "id": identity,
        "type": "question_approval",
        "questions_sha256": sha,
        "approved_at": utc_now(),
        "approved_by": "cli",
        "diff": question_diff(questions, baseline),
    }
    ledger.put("experiments", identity, record)
    return record


def generate_proposal(ledger, trial: dict, model: str) -> dict:
    feedback = development_feedback(trial)
    attempts = [r for r in ledger.all("experiments") if r.get("type") == "proposal_started"]
    if len(attempts) >= MAX_TRIALS - 1:
        raise ValueError(
            "Proposal budget exhausted; at most four paid proposal attempts per ledger"
        )
    require_credentials("openai")
    slot_id = f"proposal-slot:{len(attempts)}"
    reservation = {
        "id": slot_id,
        "type": "proposal_started",
        "parent_trial": trial["id"],
        "model": model,
        "created_at": utc_now(),
    }
    if not ledger.put("experiments", slot_id, reservation):
        raise ValueError("Proposal slot is already reserved")
    metadata: dict = {}
    candidate, error = None, None
    try:
        candidate = propose_strategy(trial["candidate"], feedback, model, metadata=metadata)
        validate_strategy(candidate)
        problems = lint_questions(candidate["questions"])
        if problems:
            raise ValueError("; ".join(problems))
    except ValueError as exc:
        if candidate is None and not metadata:
            raise  # No response came back to record.
        error = str(exc)
    proposal = {
        "type": "proposal" if error is None else "proposal_rejected",
        "candidate": candidate,
        "metadata": metadata,
        "parent_trial": trial["id"],
        "reservation_id": slot_id,
        "created_at": utc_now(),
    }
    if error is not None:
        # The paid response is kept for audit, but never as a usable candidate.
        proposal["error"] = error
    proposal["id"] = digest(proposal)
    ledger.put("experiments", proposal["id"], proposal)
    if error is not None:
        raise ProposalRejected(
            f"Proposal {proposal['id']} was rejected ({error}); it used {slot_id}"
        )
    return proposal


def autoresearch(
    ledger,
    baseline: dict,
    *,
    provider: str,
    model: str,
    proposal_model: str,
    development_until: str,
    rounds: int = 1,
) -> dict:
    if type(rounds) is not int or not 1 <= rounds <= MAX_TRIALS:
        raise ValueError(f"rounds must be between 1 and {MAX_TRIALS}")
    # experiment() checks the extraction key itself (a completed trial needs none). Later
    # rounds pay for proposals and then extraction, so check both keys before anything spends.
    if rounds > 1:
        require_credentials("openai")
        require_credentials(provider)
    best = experiment(
        ledger,
        baseline,
        baseline,
        provider=provider,
        model=model,
        development_until=development_until,
    )
    trials, rejected = [best["id"]], []
    awaiting = None
    for _ in range(rounds - 1):
        if sum(r.get("type") == "trial_started" for r in ledger.all("experiments")) >= MAX_TRIALS:
            break
        try:
            proposal = generate_proposal(ledger, best, proposal_model)
        except ProposalRejected as exc:
            rejected.append(str(exc))  # Recorded in the ledger; the round is used.
            continue
        candidate = proposal["candidate"]
        if not question_approved(ledger, candidate["questions"]):
            # LLM-written questions are drafts until a person approves the diff (P0-06).
            awaiting = proposal["id"]
            break
        trial = experiment(
            ledger,
            candidate,
            baseline,
            provider=provider,
            model=model,
            development_until=development_until,
        )
        trials.append(trial["id"])
        if trial["score"] > best["score"]:
            best = trial
    return {
        "trials": trials,
        "rejected_proposals": rejected,
        "awaiting_approval": awaiting,
        "best_trial_id": best["id"],
        "candidate": best["candidate"],
        "development_score": best["score"],
        "promoted": False,
        "warning": "Winner is selected on development data. Freeze it and collect new forward observations.",
    }


def experiment(
    ledger, candidate: dict, baseline: dict, *, provider: str, model: str, development_until: str
) -> dict:
    validate_strategy(candidate)
    validate_strategy(baseline)
    if provider == "rules":
        raise ValueError("Rules ignore questions; use jev or openai for question experiments")
    for key in baseline:
        if key not in {"name", "questions"} and candidate[key] != baseline[key]:
            raise ValueError(f"Experiments may edit only name/questions, not {key}")
    cutoff = timestamp(development_until)
    # Reuse a frozen event universe from existing baseline observations, not selected winners.
    protocol = ledger.get("experiments", "protocol-v1")
    settings = {
        "baseline": baseline,
        "provider": provider,
        "model": model,
        "development_until": cutoff,
        "max_trials": MAX_TRIALS,
    }
    if protocol is None:
        # The lock cannot be undone, so a missing key must not leave one behind.
        require_credentials(provider)
        seen, universe, labelled = set(), [], []
        forecasts = sorted(ledger.all("forecasts"), key=lambda f: (f["decision_at"], f["id"]))
        for forecast in forecasts:
            if forecast["strategy"] != baseline:
                continue
            if forecast["event_id"] in seen or instant(forecast["decision_at"]) >= instant(cutoff):
                continue
            # Only matured development labels enter any experiment. No held-out documents sent.
            label = ledger.get("outcomes", forecast["id"])
            if label is None or instant(label["label_available_at"]) >= instant(cutoff):
                continue
            seen.add(forecast["event_id"])
            universe.append(
                {"event_id": forecast["event_id"], "decision_at": forecast["decision_at"]}
            )
            labelled.append((forecast, label))
        if not baseline["min_train_samples"] + MIN_EVALUATED <= len(universe) <= MAX_EVENTS:
            raise ValueError(
                f"Research requires min_train_samples+10 to {MAX_EVENTS} mature development events"
            )
        # A locked universe that fails these checks would fail every trial after paying for it.
        if len({f["mode"] == "synthetic" for f, _ in labelled}) > 1:
            raise ValueError("Development universe mixes real and synthetic observations")
        evaluable = _evaluable_count(labelled, baseline)
        if evaluable < MIN_EVALUATED:
            raise ValueError(
                f"Development universe leaves only {evaluable} evaluated events after purging; "
                f"at least {MIN_EVALUATED} are required"
            )
        protocol = {"id": "protocol-v1", "settings": settings, "universe": universe}
        ledger.put("experiments", "protocol-v1", protocol)
    elif protocol["settings"] != settings:
        raise ValueError(
            "Research cutoff, baseline, provider and trial budget are already locked for this ledger"
        )
    # Scores from another evaluator version are not comparable; never reuse them.
    identity = digest({"protocol": protocol, "candidate": candidate, "evaluator": VERSION})
    existing = ledger.get("experiments", identity)
    if existing:
        return existing
    # Trials recorded before ids named the evaluator stay valid if their report matches it.
    legacy = ledger.get("experiments", digest({"protocol": protocol, "candidate": candidate}))
    if legacy and legacy["report"].get("version") == VERSION:
        return legacy
    if candidate["questions"] != baseline["questions"] and not question_approved(
        ledger, candidate["questions"]
    ):
        # Checked before any slot reservation or paid call (P0-06); a completed
        # trial of the same candidate is returned above without spending anything.
        sha = questions_sha256(candidate["questions"])
        raise ValueError(
            f"Question set {sha[:8]} is not approved; review its diff with "
            "`jevtrader approve-questions` before any paid trial"
        )
    stored = ledger.all("experiments")
    attempts = [r for r in stored if r.get("type") == "trial_started"]
    # The protocol is locked per ledger, so the candidate identifies its trials under any
    # evaluator or id scheme; a start without a matching completion must never be re-run.
    if sum(r["candidate"] == candidate for r in attempts) > sum(
        r.get("type") == "trial_completed" and r["candidate"] == candidate for r in stored
    ):
        raise ValueError(
            "This trial already started and did not complete; no automatic retry of paid calls"
        )
    if len(attempts) >= MAX_TRIALS:
        raise ValueError(f"Research budget exhausted ({MAX_TRIALS} candidates); no automatic reset")
    start_id = f"start:{identity}"
    # Unique slot insertion prevents two processes from reserving the last slot.
    slots = [r for r in stored if r.get("type") == "trial_slot"]
    if len(slots) >= MAX_TRIALS:
        raise ValueError("Research trial slots exhausted")
    require_credentials(provider)
    slot_id = f"trial-slot:{len(slots)}"
    if not ledger.put(
        "experiments", slot_id, {"id": slot_id, "type": "trial_slot", "trial_id": identity}
    ):
        raise ValueError("Research slot is already reserved")
    ledger.put(
        "experiments",
        start_id,
        {
            "id": start_id,
            "type": "trial_started",
            "candidate": candidate,
            "created_at": utc_now(),
            "max_provider_calls": len(protocol["universe"]),
        },
    )
    # Check model drift after every call, so a change costs one request, not the universe.
    pinned = ledger.get("experiments", "resolved-model-v1")
    records: list[dict] = []
    for item in protocol["universe"]:
        record = observe(
            ledger,
            item["event_id"],
            candidate,
            provider=provider,
            model=model,
            as_of=item["decision_at"],
        )
        if records and record["extractor_key"] != records[0]["extractor_key"]:
            raise ValueError(
                "Provider resolved to different models within the trial; comparisons rejected"
            )
        resolved = record.get("resolved_model", model)
        if pinned and resolved != pinned["resolved_model"]:
            raise ValueError(
                f"Resolved provider model changed from pinned {pinned['resolved_model']} "
                f"to {resolved}; comparisons rejected"
            )
        records.append(record)
    extractor_key = records[0]["extractor_key"]
    resolved_record = {
        "id": "resolved-model-v1",
        "resolved_model": records[0].get("resolved_model", model),
    }
    ledger.put("experiments", "resolved-model-v1", resolved_record)
    settle(ledger, as_of=cutoff)
    identity_set = {item["event_id"] for item in protocol["universe"]}
    modes = {r.get("mode", "historical") for r in records}
    if len(modes) != 1:
        raise ValueError("Development universe mixes real and synthetic observations")
    report = evaluate(
        ledger, extractor_key, candidate, before=cutoff, event_ids=identity_set, mode=modes.pop()
    )
    if report["evaluated_count"] < MIN_EVALUATED:
        raise ValueError("Too few purged evaluation events; no score produced")
    tested = {item["event_id"] for item in report["predictions"]}
    trained = {event for block in report["blocks"] for event in block["training_event_ids"]}
    if not (tested | trained).issubset(identity_set):
        raise ValueError("Evaluation included events outside the locked development universe")
    score = report["strategies"]["semantic"]["mean_net_return_per_opportunity"]
    result = {
        "id": identity,
        "type": "trial_completed",
        "created_at": utc_now(),
        "candidate": candidate,
        "development_until": cutoff,
        "score": score,
        "score_basis": "development net excess return per opportunity",
        "report": report,
        "promoted": False,
        "warning": "Adaptive development score, not an unbiased held-out estimate; no auto-promotion.",
    }
    ledger.put("experiments", identity, result)
    return result
