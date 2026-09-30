"""Versioned central sanitizer for untrusted external text (ADR-0001 §D3 step 2, P0-05).

Every fetched document passes through ``sanitize_document`` before the ledger stores its
text, so the same sanitized text reaches providers and display. It hashes the original
bytes, drops hidden HTML (``script``, ``style``, ``noscript``, ``head``, ``template``,
``ix:hidden``, the ``hidden`` attribute, ``display:none`` and ``visibility:hidden``), applies
NFKC and strips format (Cf: zero-width, bidi) and control characters. ``skeleton`` folds
confusable letters for matching only; sanitized text keeps the original letters.

A record without ``sanitizer_version`` was written before the sanitizer and is legacy
(``LEGACY``): its text may still hold hidden-HTML text that cannot be removed now.
Bump ``SANITIZER_VERSION`` whenever the output for any input could change.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from html import unescape
from html.parser import HTMLParser
from typing import Any, TypedDict

SANITIZER_VERSION = "sanitize-v1"
LEGACY = "legacy_unsanitized"
MAX_EXCERPTS = 20
MAX_EXCERPT_CHARS = 200

_KEEP_CONTROLS = frozenset("\n\t")
_BLOCKS = frozenset(
    {"p", "div", "li", "tr", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
)
_HIDDEN_TAGS = frozenset({"script", "style", "noscript", "head", "template", "ix:hidden"})
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", re.I)

# Latin look-alikes from Cyrillic and Greek (a subset of Unicode confusables.txt that covers
# the letters used in English prose). Applied after NFKC and casefold.
_CONFUSABLES = str.maketrans(
    {
        "а": "a", "в": "b", "с": "c", "ԁ": "d", "е": "e", "ё": "e", "һ": "h", "і": "i",
        "ї": "i", "ј": "j", "к": "k", "ӏ": "l", "м": "m", "н": "h", "о": "o", "р": "p",
        "ԛ": "q", "ѕ": "s", "т": "t", "у": "y", "х": "x", "ԝ": "w", "ү": "y", "ь": "b",
        "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o",
        "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "γ": "y", "ω": "w", "ϲ": "c", "ϳ": "j",
    }
)  # fmt: skip


class SanitizeDiff(TypedDict):
    """What the sanitizer removed or changed; stored next to the sanitized text."""

    hidden_elements: int
    hidden_chars: int
    hidden_excerpts: list[str]
    removed_chars: dict[str, int]
    nfkc_changed_chars: int


class SanitizedDocument(TypedDict):
    sanitizer_version: str
    raw_sha256: str
    text: str
    links: list[tuple[str, str]]
    diff: SanitizeDiff


def _removable(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category == "Cf" or (category == "Cc" and ch not in _KEEP_CONTROLS)


def _clean(text: str, removed: Counter[str] | None = None) -> tuple[str, int]:
    changed = sum(1 for ch in text if unicodedata.normalize("NFKC", ch) != ch)
    normalized = unicodedata.normalize("NFKC", text)
    kept = []
    for ch in normalized:
        if _removable(ch):
            if removed is not None:
                removed[f"U+{ord(ch):04X}"] += 1
        else:
            kept.append(ch)
    return "".join(kept), changed


def sanitize_text(text: str) -> str:
    """NFKC, then strip format (zero-width, bidi) and control characters except \\n and \\t."""
    if not isinstance(text, str):
        raise TypeError("sanitize_text needs a str")
    return _clean(text)[0]


def skeleton(text: str) -> str:
    """A matching key: sanitized, casefolded, with Cyrillic/Greek look-alikes folded to Latin."""
    return sanitize_text(text).casefold().translate(_CONFUSABLES)


def sanitization_status(record: dict[str, Any]) -> str:
    """The sanitizer version a record's text went through, or ``LEGACY`` when none did."""
    version = record.get("sanitizer_version")
    return version if isinstance(version, str) and version else LEGACY


def _hidden(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag in _HIDDEN_TAGS:
        return True
    values = dict(attrs)
    return "hidden" in values or bool(_HIDDEN_STYLE.search(values.get("style") or ""))


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.anchor: tuple[str, list[str]] | None = None
        self.hidden_tag: str | None = None
        self.hidden_depth = 0
        self.hidden_parts: list[str] = []
        self.hidden_elements = 0
        self.hidden_chars = 0
        self.excerpts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden_tag is not None:
            if tag == self.hidden_tag:
                self.hidden_depth += 1
            return
        if _hidden(tag, attrs):
            self.hidden_elements += 1
            if tag not in _VOID:
                self.hidden_tag, self.hidden_depth, self.hidden_parts = tag, 1, []
            return
        if tag in _BLOCKS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")
        if tag == "a":
            self.anchor = (dict(attrs).get("href") or "", [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden_tag is None and _hidden(tag, attrs):
            self.hidden_elements += 1
        elif self.hidden_tag is None and tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden_tag is not None:
            if tag == self.hidden_tag:
                self.hidden_depth -= 1
                if self.hidden_depth == 0:
                    self._close_hidden()
            return
        if tag in _BLOCKS:
            self.parts.append("\n")
        if tag == "a" and self.anchor is not None:
            href, text = self.anchor
            if href:
                self.links.append((href, _line("".join(text))))
            self.anchor = None

    def _close_hidden(self) -> None:
        excerpt = _line("".join(self.hidden_parts))[:MAX_EXCERPT_CHARS]
        if excerpt and len(self.excerpts) < MAX_EXCERPTS:
            self.excerpts.append(excerpt)
        self.hidden_tag, self.hidden_parts = None, []

    def handle_data(self, data: str) -> None:
        if self.hidden_tag is not None:
            self.hidden_chars += len(data)
            self.hidden_parts.append(data)
            return
        self.parts.append(data)
        if self.anchor is not None:
            self.anchor[1].append(data)

    def close(self) -> None:
        super().close()
        if self.hidden_tag is not None:
            self._close_hidden()


def _line(text: str) -> str:
    return re.sub(r"\s+", " ", sanitize_text(text)).strip()


def _paragraphs(text: str, pattern: str) -> str:
    lines = [re.sub(pattern, " ", line).strip() for line in text.splitlines()]
    return "\n\n".join(line for line in lines if line)


def sanitize_document(payload: bytes, filename: str) -> SanitizedDocument:
    """Sanitize one fetched document; ``.txt`` without HTML markers is read as plain text."""
    if not isinstance(payload, bytes):
        raise TypeError("sanitize_document needs the original bytes")
    content = payload.decode("utf-8-sig", errors="replace")
    removed: Counter[str] = Counter()
    if filename.lower().endswith(".txt") and not re.search(
        r"<(?:html|body|div|p)\b", content, re.I
    ):
        cleaned, changed = _clean(unescape(content), removed)
        text, links = _paragraphs(cleaned, r"[\t ]+"), []
        hidden: tuple[int, int, list[str]] = (0, 0, [])
    else:
        parser = _Parser()
        parser.feed(content)
        parser.close()
        cleaned, changed = _clean("".join(parser.parts), removed)
        text, links = _paragraphs(cleaned, r"\s+"), parser.links
        hidden = (parser.hidden_elements, parser.hidden_chars, parser.excerpts)
    return {
        "sanitizer_version": SANITIZER_VERSION,
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "text": text,
        "links": links,
        "diff": {
            "hidden_elements": hidden[0],
            "hidden_chars": hidden[1],
            "hidden_excerpts": hidden[2],
            "removed_chars": dict(sorted(removed.items())),
            "nfkc_changed_chars": changed,
        },
    }
