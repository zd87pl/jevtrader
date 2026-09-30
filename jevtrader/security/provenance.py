"""Provenance labels for external-derived values (#12).

A value that came from a filer, a model provider or a failed job leaves any read surface
(MCP results, the web view's JSON) wrapped as
``{"untrusted": True, "source": <source>, "value": <value>}`` so a reader can tell it from
code-built fields. Labels are applied by key, so a handler cannot forge or drop them.
"""

from __future__ import annotations

from collections.abc import Mapping

MAX_DEPTH = 32
WRAPPER_KEYS = frozenset({"untrusted", "source", "value"})

PROVENANCE: dict[str, str] = {
    "untrusted_filing_excerpts": "sec-filing",
    "items": "sec-filing",
    "source_url": "sec-filing",
    "document": "sec-filing",
    "filename": "sec-filing",
    "resolved_model": "provider",
    "error": "job-error",
}


def wrap(value: object, source: str) -> dict[str, object]:
    """One external value with its source label."""
    return {"untrusted": True, "source": source, "value": value}


def is_wrapped(value: object) -> bool:
    """True only for the exact wrapper shape that ``wrap`` builds."""
    return (
        isinstance(value, Mapping)
        and set(value) == WRAPPER_KEYS
        and value["untrusted"] is True
        and isinstance(value["source"], str)
    )


def label(value: object, extra: Mapping[str, str] | None = None) -> object:
    """A copy of ``value`` with every non-None value under a labeled key wrapped once.

    Keys in PROVENANCE, plus any in ``extra``, are wrapped wherever they appear in nested
    dicts and lists; values already wrapped are left alone. Deeper nesting than MAX_DEPTH
    raises ValueError rather than passing unlabeled data.
    """
    sources = {**PROVENANCE, **(extra or {})}
    return _label(value, sources, 0)


def _label(value: object, sources: Mapping[str, str], depth: int) -> object:
    if depth > MAX_DEPTH:
        raise ValueError("nesting deeper than the provenance limit")
    if is_wrapped(value):
        return value
    if isinstance(value, Mapping):
        labeled: dict[object, object] = {}
        for key, item in value.items():
            item = _label(item, sources, depth + 1)
            source = sources.get(key) if isinstance(key, str) else None
            if source is not None and item is not None and not is_wrapped(item):
                item = wrap(item, source)
            labeled[key] = item
        return labeled
    if isinstance(value, (list, tuple)):
        return [_label(item, sources, depth + 1) for item in value]
    return value
