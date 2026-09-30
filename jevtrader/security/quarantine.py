"""Quarantine for adversarial-looking source text (ADR-0001 §D3, #13).

``assess`` flags a disclosure whose sanitizer diff shows hidden or stripped content, or whose
text holds look-alike letters, a forged untrusted-block marker or directive-like sentences.
The flag is stored on the extraction and copied onto each forecast as ``quarantined``; a
quarantined forecast never counts as evidence, never trains a calibrator and never gets a
paper plan. Erring toward flagging only costs evidence volume, never safety.

``DIRECTIVE`` is the skeleton-matched directive pattern shared with the brief's quote picker
(#10, #12): match it against ``skeleton(sentence)``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypedDict

from .sanitize import _CONFUSABLES, sanitize_text, skeleton

# The filer writes the text and the lexicon picks which sentences get quoted, so a filer could
# get an instruction quoted to an assistant that reads the card (and may hold trading tools).
# Best effort, erring toward dropping: a dropped sentence only means another one is quoted.
DIRECTIVE = re.compile(
    r"\b(?:you|your|yours|yourself|assistants?|chatbots?|llms?|language models?|"
    r"ai (?:agents?|assistants?|models?)|prompts?|instructions?|tool[ _-]?(?:calls?|use)|"
    r"ignore|disregard|forget|override)\b"
    r"|\b(?:system|user|human|assistant|ai|agent|model|developer)\s*:"
    r"|^\W*(?:please\s+)?(?:buy|sell|short|place|submit|execute|cancel|use|call|invoke|tell|"
    r"send|do not|don't|never|always)(?![\w-])"
    r"|\b(?:buy|sell|limit|market|stop)\s+orders?\b"
    r"|\bplac(?:e|ing)\s+(?:an?\s+|the\s+)?(?:\w+\s+)?(?:orders?|trades?)\b"
    r"|\b(?:use|call|invoke)\b[^.!?]{0,40}\btools?\b"
    r"|\b[a-z0-9]+_[a-z0-9_]+\b"  # snake_case reads as a tool or function name
    r"|<\||\|>",
    re.I,
)
_MARKER = re.compile(r"\b(?:begin|end)\s+untrusted\b")
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|[\r\n]+")
_LOOKALIKES = frozenset(chr(code) for code in _CONFUSABLES)


class Quarantine(TypedDict):
    flagged: bool
    reasons: list[str]


def _count(diff: Mapping[str, Any], key: str) -> int:
    value = diff.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def assess(text: str, diff: Mapping[str, Any] | None = None) -> Quarantine:
    """Reasons to quarantine one disclosure, from its sanitizer diff and its text."""
    if not isinstance(text, str):
        raise TypeError("assess needs the disclosure text")
    reasons = []
    diff = diff if isinstance(diff, Mapping) else {}
    if _count(diff, "hidden_elements") > 0:
        reasons.append("hidden HTML elements removed")
    if _count(diff, "hidden_chars") > 0:
        reasons.append("hidden characters removed")
    if diff.get("removed_chars") or sanitize_text(text) != text:
        reasons.append("format or control characters removed")
    if any(ch in _LOOKALIKES for ch in text.casefold()):
        reasons.append("look-alike letters")
    folded = skeleton(text)
    if _MARKER.search(folded):
        reasons.append("untrusted-block marker")
    if any(DIRECTIVE.search(part) for part in _SENTENCE.split(folded) if part.strip()):
        reasons.append("directive-like sentences")
    return {"flagged": bool(reasons), "reasons": reasons}
