"""When the ledger first knew a record: the knowledge time that ``Ledger.as_of`` filters on.

Each kind names the payload field that says when its content became available (ADR-0004).
A kind without such a field, or a payload whose field is missing or malformed, falls back to
``recorded_at``, the instant the ledger stored it, which is never earlier than the truth."""

from __future__ import annotations

from collections.abc import Mapping

from .time import Instant

# Every listed field must be present and valid; the knowledge time is the latest of them.
KNOWLEDGE_FIELDS: dict[str, tuple[str, ...]] = {
    "disclosures": ("first_seen_at",),
    "bars": ("available_at",),
    "forecasts": ("decision_at",),
    "extractions": ("created_at",),
    "outcomes": ("outcome_at", "label_available_at"),
    "securities": ("known_at",),
}


def knowledge_time(kind: str, payload: Mapping[str, object], recorded_at: str) -> Instant:
    fields = KNOWLEDGE_FIELDS.get(kind)
    if fields:
        try:
            return max(Instant.parse(_text(payload.get(name))) for name in fields)
        except ValueError:
            pass
    return Instant.parse(recorded_at)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Knowledge field must be a timestamp string")
    return value
