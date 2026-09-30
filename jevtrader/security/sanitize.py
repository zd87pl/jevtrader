"""Versioned central sanitizer for untrusted external text (ADR-0001 §D3 step 2, P0-05).

Every fetched document passes through ``sanitize_document`` before the ledger stores its
text, so the same sanitized text reaches providers and display. It hashes the original
bytes, drops hidden HTML (``script``, ``style``, ``noscript``, ``head``, ``template``,
``ix:header``, ``ix:hidden``, the ``hidden`` attribute, and CSS hiding: ``display:none``,
``visibility:hidden``, ``font-size:0``, ``opacity:0``, off-screen offsets and zero height with
``overflow:hidden``, after CSS comments are removed), applies NFKC and strips format (Cf:
zero-width, bidi), control, private-use, unassigned and other default-ignorable characters.
``skeleton`` folds confusable letters for matching only; sanitized text keeps the original
letters.

The diff keeps structural drops (``head``, ``script``, ``style``, the iXBRL header) apart from
``concealed`` body text, which a reader of the rendered page would not see; only the latter,
and near-white text (``faint_elements``, with ``faint_chars`` non-whitespace characters, kept
because the background is unknown), is a quarantine signal (``quarantine.assess``).
``faint_chars`` was added to the diff without a version bump: the stored text is unchanged.

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

SANITIZER_VERSION = "sanitize-v2"
LEGACY = "legacy_unsanitized"
MAX_EXCERPTS = 20
MAX_EXCERPT_CHARS = 200

_KEEP_CONTROLS = frozenset("\n\t")
_BLOCKS = frozenset(
    {"p", "div", "li", "tr", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
)
_STRUCTURAL = frozenset(
    {"script", "style", "noscript", "head", "title", "meta", "link", "base", "ix:header"}
)
_HIDDEN_TAGS = frozenset(
    {"script", "style", "noscript", "ix:header", "head", "template", "ix:hidden"}
)
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
# Any start or end tag; a .txt payload holding one goes through the HTML parser.
_TAG = re.compile(r"</?[a-zA-Z][\w:.-]*(?:[\s/>]|$)")
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.S)
_ZERO = r"-?0*(?:\.0*)?(?:px|pt|em|rem|ex|ch|vh|vw|%)?\s*(?:!\s*important)?\s*(?:;|$)"
_HIDDEN_STYLE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)\b"
    rf"|font-size\s*:\s*{_ZERO}|opacity\s*:\s*{_ZERO}"
    r"|(?:left|top|right|text-indent|margin-left|margin-top)\s*:\s*-\d{3,}(?:\.\d+)?"
    r"\s*(?:px|pt|em|rem|%)?\s*(?:!\s*important)?\s*(?:;|$))",
    re.I,
)
_ZERO_BOX = re.compile(rf"(?:^|;)\s*(?:max-)?(?:height|width)\s*:\s*{_ZERO}", re.I)
_OVERFLOW_HIDDEN = re.compile(r"(?:^|;)\s*overflow(?:-[xy])?\s*:\s*(?:hidden|clip)\b", re.I)
_COLOR = re.compile(r"(?:^|;)\s*color\s*:\s*([^;!]+)", re.I)
_WHITE_NAMES = frozenset({"white", "snow", "ivory", "ghostwhite", "whitesmoke", "floralwhite"})
# Default_Ignorable_Code_Point ranges outside Cf (Unicode DerivedCoreProperties.txt), plus
# U+2800 BRAILLE PATTERN BLANK, which renders as empty space.
_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)

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
    concealed_elements: int
    concealed_chars: int
    structural_elements: int
    faint_elements: int
    faint_chars: int
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
    if category in {"Cf", "Co", "Cn"} or (category == "Cc" and ch not in _KEEP_CONTROLS):
        return True
    code = ord(ch)
    return any(low <= code <= high for low, high in _IGNORABLE_RANGES)


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
    """NFKC, then strip format (zero-width, bidi), private-use, unassigned, default-ignorable
    and control characters except \\n and \\t."""
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


def _style(attrs: list[tuple[str, str | None]]) -> str:
    return _CSS_COMMENT.sub("", dict(attrs).get("style") or "").strip()


def _hidden(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag in _HIDDEN_TAGS or "hidden" in dict(attrs):
        return True
    style = _style(attrs)
    return bool(
        _HIDDEN_STYLE.search(style) or (_ZERO_BOX.search(style) and _OVERFLOW_HIDDEN.search(style))
    )


def _near_white(value: str) -> bool:
    value = value.strip().lower()
    if value in _WHITE_NAMES:
        return True
    hexa = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", value)
    if hexa:
        digits = hexa.group(1)
        if len(digits) == 3:
            digits = "".join(d * 2 for d in digits)
        channels = [int(digits[i : i + 2], 16) for i in (0, 2, 4)]
    else:
        rgb = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,[^)]*)?\)", value)
        if not rgb:
            return False
        channels = [int(rgb.group(i)) for i in (1, 2, 3)]
    return min(channels) >= 0xF0


def _faint(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag == "font" and _near_white(dict(attrs).get("color") or ""):
        return True
    return any(_near_white(match) for match in _COLOR.findall(_style(attrs)))


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.anchor: tuple[str, list[str]] | None = None
        self.hidden_tag: str | None = None
        self.hidden_depth = 0
        self.hidden_elements = 0
        self.hidden_chars = 0
        self.excerpts: list[str] = []
        # Concealed text: hidden body text outside structural tags (head, script, iXBRL header).
        self.structural_depth = 0
        self.concealed_parts: list[str] = []
        self.concealed_elements = 0
        self.concealed_chars = 0
        self.structural_elements = 0
        self.faint_elements = 0
        # Open elements while inside near-white text: (tag, is_faint), and how many are faint.
        self.open: list[tuple[str, bool]] = []
        self.faint_depth = 0
        self.faint_chars = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _STRUCTURAL:
            self.structural_elements += 1
        if self.hidden_tag is not None:
            if tag == self.hidden_tag:
                self.hidden_depth += 1
            if tag in _STRUCTURAL and tag not in _VOID:
                self.structural_depth += 1
            return
        if _hidden(tag, attrs):
            self.hidden_elements += 1
            if tag not in _VOID:
                self.hidden_tag, self.hidden_depth = tag, 1
                self.structural_depth = 1 if tag in _STRUCTURAL else 0
                self.concealed_parts = []
            return
        faint = _faint(tag, attrs)
        if faint:
            self.faint_elements += 1
        if tag not in _VOID and (faint or self.faint_depth):
            self.open.append((tag, faint))
            self.faint_depth += faint
        if tag in _BLOCKS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")
        if tag == "a":
            self.anchor = (dict(attrs).get("href") or "", [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _STRUCTURAL:
            self.structural_elements += 1
        if self.hidden_tag is None and _hidden(tag, attrs):
            self.hidden_elements += 1
        elif self.hidden_tag is None and tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden_tag is not None:
            if tag in _STRUCTURAL and tag not in _VOID and self.structural_depth > 0:
                self.structural_depth -= 1
            if tag == self.hidden_tag:
                self.hidden_depth -= 1
                if self.hidden_depth == 0:
                    self._close_hidden()
            return
        self._close_faint(tag)
        if tag in _BLOCKS:
            self.parts.append("\n")
        if tag == "a" and self.anchor is not None:
            href, text = self.anchor
            if href:
                self.links.append((href, _line("".join(text))))
            self.anchor = None

    def _close_faint(self, tag: str) -> None:
        """Pop open elements down to the matching start tag; a stray end tag changes nothing."""
        if not any(name == tag for name, _ in self.open):
            return
        while self.open:
            name, faint = self.open.pop()
            self.faint_depth -= faint
            if name == tag:
                return

    def _close_hidden(self) -> None:
        excerpt = _line("".join(self.concealed_parts))[:MAX_EXCERPT_CHARS]
        if excerpt:
            self.concealed_elements += 1
            if len(self.excerpts) < MAX_EXCERPTS:
                self.excerpts.append(excerpt)
        self.hidden_tag, self.concealed_parts = None, []
        self.structural_depth = 0

    def handle_data(self, data: str) -> None:
        if self.hidden_tag is not None:
            self.hidden_chars += len(data)
            if self.structural_depth == 0 and data.strip():
                self.concealed_chars += len(data)
                self.concealed_parts.append(data)
            return
        self.parts.append(data)
        if self.faint_depth:
            self.faint_chars += sum(1 for ch in data if not ch.isspace())
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
    """Sanitize one fetched document; ``.txt`` without any tag is read as plain text."""
    if not isinstance(payload, bytes):
        raise TypeError("sanitize_document needs the original bytes")
    content = payload.decode("utf-8-sig", errors="replace")
    removed: Counter[str] = Counter()
    parser = _Parser()
    if filename.lower().endswith(".txt") and not _TAG.search(content):
        cleaned, changed = _clean(unescape(content), removed)
        text, links = _paragraphs(cleaned, r"[\t ]+"), []
    else:
        parser.feed(content)
        parser.close()
        cleaned, changed = _clean("".join(parser.parts), removed)
        text, links = _paragraphs(cleaned, r"\s+"), parser.links
    return {
        "sanitizer_version": SANITIZER_VERSION,
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "text": text,
        "links": links,
        "diff": {
            "hidden_elements": parser.hidden_elements,
            "hidden_chars": parser.hidden_chars,
            "hidden_excerpts": parser.excerpts,
            "concealed_elements": parser.concealed_elements,
            "concealed_chars": parser.concealed_chars,
            "structural_elements": parser.structural_elements,
            "faint_elements": parser.faint_elements,
            "faint_chars": parser.faint_chars,
            "removed_chars": dict(sorted(removed.items())),
            "nfkc_changed_chars": changed,
        },
    }
