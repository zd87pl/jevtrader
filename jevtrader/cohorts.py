"""Cohort pre-registration: the stop-gap that keeps hand-assembled replays out of evidence.

A replay of an imported or backfilled filing counts only when a cohort listing that filing
was appended to the ledger before the filing itself was (ADR-0007). Otherwise a user could
import only filings that were followed by large moves and replay them as evidence. The full
trial registry (P0-27) replaces this in Phase 2. Pure ledger reads; no clock unless ``now``
is omitted, no files or network.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, TypedDict

from .common import timestamp, utc_now

# An unregistered cohort was assembled by hand, like a named replay: never evidence.
LABEL = "adhoc_replay"
KIND = "cohorts"
MAX_EVENTS = 10_000
MAX_TEXT = 200
MAX_RULE = 2_000


class Cohort(TypedDict):
    id: str
    rule: str
    event_ids: list[str]
    registered_at: str


def register(
    ledger: Any,
    cohort_id: str,
    *,
    event_ids: list[str],
    rule: str,
    now: str | None = None,
) -> Cohort:
    """Append a cohort: the filings a later replay will cover and the rule that chose them.

    Registering the same cohort again is a no-op; changing a registered one is refused."""
    if not _text(cohort_id, MAX_TEXT):
        raise ValueError(f"A cohort id must be a 1-{MAX_TEXT} character string")
    if not isinstance(rule, str) or not rule.strip() or len(rule) > MAX_RULE:
        raise ValueError(f"A cohort rule must be 1-{MAX_RULE} characters of text")
    if (
        not isinstance(event_ids, list)
        or not 1 <= len(event_ids) <= MAX_EVENTS
        or not all(_text(item, MAX_TEXT) for item in event_ids)
    ):
        raise ValueError(f"A cohort needs 1-{MAX_EVENTS} event ids, each a nonempty string")
    record: Cohort = {
        "id": cohort_id,
        "rule": rule,
        "event_ids": sorted(set(event_ids)),
        "registered_at": timestamp(now or utc_now()),
    }
    existing = ledger.get(KIND, cohort_id)
    if existing is not None and (existing.get("event_ids"), existing.get("rule")) == (
        record["event_ids"],
        rule,
    ):
        return {
            "id": cohort_id,
            "rule": rule,
            "event_ids": record["event_ids"],
            "registered_at": str(existing["registered_at"]),
        }
    ledger.put(KIND, cohort_id, dict(record))
    return record


def preregistered(ledger: Any, event_id: str) -> str | None:
    """The id of a cohort that lists ``event_id`` and was appended before the filing."""
    filed = ledger.sequence("disclosures", event_id)
    if filed is None:
        return None
    for cohort in ledger.all(KIND):
        if event_id not in cohort.get("event_ids", ()):
            continue
        seq = ledger.sequence(KIND, cohort["id"])
        if seq is not None and seq < filed:
            return str(cohort["id"])
    return None


def label_mix(forecasts: Iterable[Mapping[str, object]]) -> dict[str, int]:
    """How many forecasts carry each evidence label, so a run shows what it counted."""
    counts = Counter(
        label if isinstance(label := f.get("eligibility"), str) and label else "unlabelled"
        for f in forecasts
    )
    return dict(sorted(counts.items()))


def _text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= limit
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )
