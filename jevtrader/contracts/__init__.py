"""Typed contracts for the records the ledger stores (ADR-0001, P0-23).

Each TypedDict names the keys a producer writes; ``conforms`` checks a stored
record against one at runtime, which is what the contract tests rely on. This
package must pass ``mypy --strict``. No ``from __future__ import annotations``:
it hides ``NotRequired`` from ``__required_keys__``.
"""

import types
from typing import Any, Literal, NotRequired, TypedDict, Union, get_args, get_origin, get_type_hints

Mode = Literal["historical", "forward", "synthetic"]

__all__ = ["Disclosure", "Extraction", "Forecast", "Mode", "Outcome", "conforms"]


class Disclosure(TypedDict):
    """A source document as ``Ledger.disclosure`` stores it (``sec.collect_filing``)."""

    id: str
    symbol: str
    published_at: str
    first_seen_at: str
    mode: Mode
    source_type: str
    source_url: str
    text: str
    # SEC filings only.
    accepted_at: NotRequired[str]
    after_hours: NotRequired[bool]
    cik: NotRequired[str]
    accession: NotRequired[str]
    form: NotRequired[str]
    items: NotRequired[list[str]]
    document: NotRequired[str]
    document_role: NotRequired[str]
    selection_method: NotRequired[str]
    text_truncated: NotRequired[bool]
    timestamp_basis: NotRequired[str]
    sec_acceptance_raw: NotRequired[str]


class Extraction(TypedDict):
    """Text features from one provider call (``engine.observe``)."""

    id: str
    created_at: str
    spec: dict[str, Any]
    direction: float
    materiality: float
    novelty: float
    uncertainty: float
    raw: dict[str, Any]
    resolved_model: str
    input_tokens: int
    extractor_key: str
    input_chars: int
    text_excerpt: NotRequired[str]


class Forecast(TypedDict):
    """A frozen decision (``engine.observe``)."""

    id: str
    event_id: str
    symbol: str
    decision_at: str
    recorded_at: str
    mode: Mode
    previous_event_id: str | None
    extraction_id: str
    extractor_key: str
    provider: str
    resolved_model: str
    eligibility: str
    eligibility_basis: dict[str, Any] | None
    features: list[float]
    feature_names: list[str]
    market: dict[str, Any]
    calibrator_id: str | None
    expected_return: float | None
    target: str
    action: Literal["WATCH", "PASS", "LONG", "SHORT"]
    reasons: list[str]
    strategy: dict[str, Any]


class Outcome(TypedDict):
    """The realized label for one forecast (``market.outcome`` via ``engine.settle``)."""

    forecast_id: str
    event_id: str
    entry_at: str
    outcome_at: str
    label_available_at: str
    entry_price: float
    exit_price: float
    gross_return: float
    benchmark_return: float
    target: float
    horizon_sessions: int
    bar_ids: list[str]
    label_basis: str
    recorded_at: str


def _matches(value: Any, hint: Any) -> bool:
    """Shallow type check: containers by kind only, ``bool`` never counts as a number."""
    origin = get_origin(hint)
    if hint is Any:
        return True
    if origin is Literal:
        return any(type(value) is type(arg) and value == arg for arg in get_args(hint))
    if origin is Union or origin is types.UnionType:
        return any(_matches(value, arg) for arg in get_args(hint))
    if hint is type(None):
        return value is None
    if origin is not None:
        return isinstance(value, origin)
    if isinstance(value, bool):
        return hint is bool
    if hint is float:
        return isinstance(value, (int, float))
    return isinstance(value, hint)


def conforms(contract: type, record: dict[str, Any]) -> list[str]:
    """Key and shallow type problems of ``record`` against a TypedDict ``contract``."""
    required: frozenset[str] = getattr(contract, "__required_keys__")
    hints = get_type_hints(contract)
    problems = [f"missing key: {key}" for key in sorted(required - set(record))]
    problems += [f"undeclared key: {key}" for key in sorted(set(record) - set(hints))]
    problems += [
        f"wrong type: {key}"
        for key in sorted(set(record) & set(hints))
        if not _matches(record[key], hints[key])
    ]
    return problems
