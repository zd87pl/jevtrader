"""Lint for LLM-proposed research questions (P0-06, ADR-0001 §D3).

For JEV the questions become the model's instructions, so a proposal must not name
tickers, ask about returns or trades, tell the model to ignore the evidence, or name a
tool. Phrases match on ``skeleton`` (sanitized, casefolded, confusables folded), so
zero-width characters and Cyrillic/Greek look-alikes do not slip past. The lint is a
tripwire, not a proof: every question change still needs human approval of its diff.
"""

from __future__ import annotations

import re
from typing import Any

from .sanitize import sanitize_text, skeleton

ACRONYMS = frozenset(
    {"SEC", "CEO", "CFO", "COO", "EPS", "GAAP", "US", "USD", "FDA", "EBITDA", "IPO", "AI"}
)
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ticker", re.compile(r"\$[a-z]{1,5}\b")),
    (
        "return or price language",
        re.compile(
            r"\b(?:return|price|outperform|underperform)|\bstock will\b"
            r"|\b(?:buy|buys|buying|sell|sells|selling|short|shorting)\b|\blong position"
        ),
    ),
    (
        "ignore-evidence phrasing",
        re.compile(r"\b(?:ignore|disregard)|regardless of the evidence|previous instructions"),
    ),
    (
        "tool name",
        re.compile(r"place_order|propose_trade_ticket|\bapprove\b|\btool call|\bfunction call"),
    ),
)
_WORD = re.compile(r"\w+")


def _tickers(text: str) -> list[str]:
    found = []
    for token in _WORD.findall(sanitize_text(text)):
        if 2 <= len(token) <= 5 and token.isalpha() and token.isupper():
            folded = skeleton(token).upper()
            if folded not in ACRONYMS:
                found.append(folded)
    return found


def lint_questions(questions: Any) -> list[str]:
    """Problems found in a question mapping; an empty list means it passed."""
    if not isinstance(questions, dict):
        return ["questions must be a mapping of text"]
    problems = []
    for name in sorted(questions):
        text = questions[name]
        if not isinstance(text, str):
            problems.append(f"{name}: question must be text")
            continue
        key = skeleton(text)
        problems.extend(f"{name}: {rule}" for rule, pattern in _RULES if pattern.search(key))
        problems.extend(f"{name}: ticker-like token {token}" for token in _tickers(text))
    return problems
