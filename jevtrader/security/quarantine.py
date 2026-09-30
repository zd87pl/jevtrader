"""Quarantine for adversarial-looking source text (ADR-0001 §D3, #13).

``assess`` flags a disclosure whose sanitizer diff shows concealed, near-white or stripped
content, or whose text holds look-alike letters, a forged marker or directive-like sentences.
The flag is stored on the extraction and copied onto each forecast as ``quarantined``; a
quarantined forecast never counts as evidence, never trains a calibrator and never gets a
paper plan. Erring toward flagging only costs evidence volume, never safety.

``QUOTE_DIRECTIVE`` (alias ``DIRECTIVE``) is the broad skeleton-matched pattern the brief's
quote picker uses to drop sentences (#10, #12). ``assess`` uses the narrow
``QUARANTINE_DIRECTIVE`` instead: a quarantined forecast never counts, so ordinary 8-K
boilerplate ("you should not place undue reliance", "market orders") must not flag it.
Match both against ``skeleton(sentence)``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypedDict

from .. import mcp_server
from .sanitize import _CONFUSABLES, _removable, sanitize_text, skeleton

# The filer writes the text and the lexicon picks which sentences get quoted, so a filer could
# get an instruction quoted to an assistant that reads the card (and may hold trading tools).
# Best effort, erring toward dropping: a dropped sentence only means another one is quoted.
QUOTE_DIRECTIVE = re.compile(
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
DIRECTIVE = QUOTE_DIRECTIVE

# Tool names an injected sentence could ask a reader to call: this server's own tools plus
# order tools of other servers. Bare words ("health") would flag ordinary prose, so only
# snake_case names count.
_TOOL_NAMES = sorted(
    {tool["name"] for tool in mcp_server.TOOLS}
    | {"place_order", "propose_trade_ticket", "approve_order", "create_order_instruction"}
)
QUARANTINE_DIRECTIVE = re.compile(
    r"\b(?:assistants?|chatbots?|llms?|language models?|ai (?:agents?|assistants?|models?)|"
    r"system prompts?|prompt injections?)\b"
    r"|\b(?:ignore|disregard|forget|override)\b[^.!?]{0,40}?"
    r"\b(?:instructions?|prompts?|rules?|guidelines?|above|previous|prior|earlier)\b"
    r"|\bnew instructions?\b"
    r"|^\W*(?:system|assistant|developer|user|human|ai|agent|model)\s*:"
    r"|\b(?:use|call|invoke|run|execute)\b[^.!?]{0,40}\btools?\b"
    r"|\btool[ _-]?calls?\b|\bfunction calls?\b"
    rf"|\b(?:{'|'.join(name for name in _TOOL_NAMES if '_' in name)})\b"
    r"|<\||\|>"
    r"|^\W*(?:please\s+)?(?:buy|sell|short)(?![\w-])",
    re.I,
)
_MARKER = re.compile(r"\b(?:begin|end)\s+untrusted\b")
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|[\r\n]+")
_LOOKALIKES = frozenset(chr(code) for code in _CONFUSABLES)
_WORD = re.compile(r"\w+")
_ASCII_LETTER = re.compile(r"[a-z]", re.I)
# Removed characters that ordinary filings carry: soft hyphens and zero-width joiners from
# word processors, a BOM, and CR / VT / FF line and page breaks. Directive matching runs on
# sanitized text, so a directive split by one of these is still caught.
_BENIGN_REMOVED = frozenset(
    {0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x000B, 0x000C, 0x000D}
)
_FAINT_MIN_CHARS = 20


class Quarantine(TypedDict):
    flagged: bool
    reasons: list[str]


def _count(diff: Mapping[str, Any], key: str) -> int:
    value = diff.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _removed_codes(text: str, diff: Mapping[str, Any]) -> set[int]:
    """Code points the sanitizer removed: from the diff when it has them, else from the text."""
    codes = set()
    removed = diff.get("removed_chars")
    if isinstance(removed, Mapping):
        for key, count in removed.items():
            if isinstance(count, int) and count > 0:
                try:
                    codes.add(int(str(key).removeprefix("U+"), 16))
                except ValueError:
                    codes.add(-1)  # an unreadable key counts as suspicious
    return codes | {ord(ch) for ch in text if _removable(ch)}


def _mixed_script(text: str) -> bool:
    """A word that mixes ASCII Latin letters with a Cyrillic or Greek look-alike, or a
    ticker-shaped word (1 to 5 capitals) made only of look-alikes. A Greek or Cyrillic word
    or symbol on its own (μg, β, Москва) is ordinary text."""
    for word in _WORD.findall(text):
        folded = word.casefold()
        lookalike = [ch in _LOOKALIKES for ch in folded]
        if not any(lookalike):
            continue
        if _ASCII_LETTER.search(word):
            return True
        if all(lookalike) and len(word) <= 5 and word.isupper():
            return True
    return False


def _faint(diff: Mapping[str, Any]) -> bool:
    if _count(diff, "faint_elements") == 0:
        return False
    # A diff from before faint_chars existed cannot tell a spacer from a sentence.
    if "faint_chars" not in diff:
        return True
    return _count(diff, "faint_chars") >= _FAINT_MIN_CHARS


def assess(text: str, diff: Mapping[str, Any] | None = None) -> Quarantine:
    """Reasons to quarantine one disclosure, from its sanitizer diff and its text."""
    if not isinstance(text, str):
        raise TypeError("assess needs the disclosure text")
    reasons = []
    diff = diff if isinstance(diff, Mapping) else {}
    # A sanitize-v2 diff keeps structural drops (head, script, the iXBRL header) apart from
    # concealed body text; an older diff cannot tell them apart, so any drop counts.
    concealed = "concealed_elements" in diff
    if _count(diff, "concealed_elements" if concealed else "hidden_elements") > 0:
        reasons.append("hidden HTML elements removed")
    if _count(diff, "concealed_chars" if concealed else "hidden_chars") > 0:
        reasons.append("hidden characters removed")
    if _faint(diff):
        reasons.append("near-white text")
    folded = skeleton(text)
    lookalike = _mixed_script(sanitize_text(text))
    parts = [part for part in _SENTENCE.split(folded) if part.strip()]
    directive = any(QUARANTINE_DIRECTIVE.search(part) for part in parts)
    codes = _removed_codes(text, diff)
    # Bidi, tag and private-use characters (and other rare invisibles) always flag. Soft
    # hyphens, zero-width joiners, a BOM and CR / VT / FF flag only in text that is already
    # suspicious, where they are the likely means of splitting a directive or a ticker.
    if codes - _BENIGN_REMOVED or (codes and (lookalike or directive)):
        reasons.append("format or control characters removed")
    if lookalike:
        reasons.append("look-alike letters")
    if _MARKER.search(folded):
        reasons.append("untrusted-block marker")
    if directive:
        reasons.append("directive-like sentences")
    return {"flagged": bool(reasons), "reasons": reasons}
