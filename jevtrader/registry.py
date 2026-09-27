"""Which forecasts can count as evidence, given what each model could already have learned.

Forward observations always count. A replayed (historical) forecast counts only when its
extractor has no learned knowledge (the rules baseline) or has a published training cutoff
more than BUFFER_DAYS before the filing's date. An undisclosed or undeclared cutoff never
counts for replays. Pure functions: no clock, files or network.
"""

from __future__ import annotations

import copy
import re
from datetime import date

from .common import EASTERN, instant
from .providers import PROVIDERS

# Model cards state cutoffs by month; the first of that month is recorded, so this buffer
# also absorbs the rest of the month and late-crawled pages about earlier events.
BUFFER_DAYS = 92
MODES = ("forward", "historical", "synthetic")
LABELS = (
    "forward",
    "synthetic",
    "no_model_knowledge",
    "post_cutoff",
    "contaminated",
    "unknown_cutoff",
    "adhoc_replay",  # a replay of events the user picked by hand: never evidence
)
EVIDENCE_LABELS = frozenset({"forward", "post_cutoff", "no_model_knowledge"})
MAX_OVERRIDES = 100
MAX_USD_PER_MILLION_TOKENS = 1000.0
PRICES = ("usd_per_million_input_tokens", "usd_per_million_output_tokens")

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DECLARABLE = frozenset({"training_cutoff", "source", *PRICES})


def _entry(cutoff: str | None, source: str, *, open_weights: bool, price: float | None) -> dict:
    return {
        "training_cutoff": cutoff,
        "source": source,
        "open_weights": open_weights,
        "knowledge": "pretrained",
        **dict.fromkeys(PRICES, price),
    }


_GPT_OSS = _entry(
    "2024-06-01",
    "OpenAI gpt-oss model card: knowledge cutoff June 2024",
    open_weights=True,
    price=0.0,
)
MODELS: dict[str, dict] = {
    "rules:rules-v1": {
        "training_cutoff": None,
        "source": "Fixed lexical rules in jevtrader.providers; nothing is learned",
        "open_weights": False,
        "knowledge": "none",
        **dict.fromkeys(PRICES, 0.0),
    },
    "jev:jev-1.13.0": _entry(
        None, "TypeSafe JEV: training cutoff not disclosed", open_weights=False, price=None
    ),
    **{
        f"local:{name}": copy.deepcopy(_GPT_OSS)
        for name in (
            "gpt-oss:120b",
            "gpt-oss-120b",
            "openai/gpt-oss-120b",
            "gpt-oss:20b",
            "gpt-oss-20b",
            "openai/gpt-oss-20b",
        )
    },
    "local:llama3.3:70b": _entry(
        "2023-12-01",
        "Meta Llama 3.3 model card: pretraining data cutoff December 2023",
        open_weights=True,
        price=0.0,
    ),
    "local:gemma3:27b": _entry(
        "2024-08-01",
        "Google Gemma 3 model card: training data cutoff August 2024",
        open_weights=True,
        price=0.0,
    ),
}
# Names match exactly: a fine-tune or renamed build may have learned from later data.
UNREGISTERED: dict[str, dict] = {
    "local": _entry(
        None,
        "Unregistered local model: declare its training cutoff in config",
        open_weights=True,
        price=0.0,
    ),
    "jev": _entry(
        None,
        "Unregistered JEV version: training cutoff not disclosed",
        open_weights=False,
        price=None,
    ),
    "openai": _entry(
        None, "OpenAI model: training cutoff not declared", open_weights=False, price=None
    ),
}


def lookup(provider: str, model: str, overrides: dict | None = None) -> dict:
    """Registry facts for an exact provider/model; unknown names get a conservative entry."""
    _provider(provider)
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("model must be a nonempty string of at most 200 characters")
    key = f"{provider}:{model}"
    declared = validate_overrides(overrides).get(key)
    if key in MODELS:
        entry, origin = copy.deepcopy(MODELS[key]), "builtin"
    elif provider in UNREGISTERED:
        entry, origin = copy.deepcopy(UNREGISTERED[provider]), "unregistered"
    else:
        raise ValueError(f"Unknown {provider} model: {model!r}")
    if declared:
        source = declared.get("source") or (
            f"{entry['source']}; declared in config"
            if origin == "builtin"
            else "declared in config"
        )
        entry.update(declared, source=source)
        origin = "declared"
    return {"key": key, "provider": provider, "model": model, **entry, "origin": origin}


def eligibility(
    provider: str, resolved_model: str, *, mode: str, published_at: str, overrides=None
) -> str:
    """One of LABELS; inputs are validated even when the mode alone decides the label."""
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    try:
        filed = instant(published_at).astimezone(EASTERN).date()
    except OverflowError:
        raise ValueError(f"published_at is out of range: {published_at!r}") from None
    entry = lookup(provider, resolved_model, overrides)
    if mode == "forward":
        return "forward"
    if mode == "synthetic":
        return "synthetic"
    if entry["knowledge"] == "none":
        return "no_model_knowledge"
    if entry["training_cutoff"] is None:
        return "unknown_cutoff"
    # EDGAR dates filings in New York; the whole BUFFER_DAYS-th day is still inside the buffer.
    if (filed - date.fromisoformat(entry["training_cutoff"])).days > BUFFER_DAYS:
        return "post_cutoff"
    return "contaminated"


def counts_as_evidence(label: str) -> bool:
    # Stored forecasts feed this, so anything unrecognized is simply not evidence.
    return isinstance(label, str) and label in EVIDENCE_LABELS


def validate_overrides(overrides: object) -> dict[str, dict]:
    """Normalized copy of config declarations keyed "provider:model"; None means none."""
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ValueError("Model overrides must be an object keyed 'provider:model'")
    if len(overrides) > MAX_OVERRIDES:
        raise ValueError(f"At most {MAX_OVERRIDES} model overrides are supported")
    return {key: _declaration(key, value) for key, value in overrides.items()}


def _declaration(key: object, declared: object) -> dict:
    if not isinstance(key, str) or ":" not in key:
        raise ValueError(f"Model override keys must be 'provider:model': {key!r}")
    provider, _, model = key.partition(":")
    _provider(provider)
    if (
        not model
        or model != model.strip()
        or len(model) > 200
        or any(ord(c) < 32 or ord(c) == 127 for c in model)
    ):
        raise ValueError(f"Model override {key!r} needs a 1–200 character model name")
    if "*" in model:
        raise ValueError(
            f"Declare each resolved model exactly; wildcards are not supported: {key!r}"
        )
    if provider == "rules":
        raise ValueError("The rules baseline has no learned knowledge; nothing to declare")
    if not isinstance(declared, dict):
        raise ValueError(f"{key}: an override must be an object")
    unknown = sorted(map(str, set(declared) - _DECLARABLE))
    if unknown:
        raise ValueError(f"{key}: unknown override fields: {', '.join(unknown)}")
    if not declared.keys() & (_DECLARABLE - {"source"}):
        raise ValueError(
            f"{key}: declare training_cutoff and/or usd_per_million_input_tokens "
            "and usd_per_million_output_tokens"
        )
    result: dict = {}
    if "training_cutoff" in declared:
        result["training_cutoff"] = _cutoff(key, declared["training_cutoff"])
    for field in PRICES:
        if field in declared:
            result[field] = _price(key, field, declared[field])
    if "source" in declared:
        result["source"] = _source(key, declared["source"])
    if provider == "jev" and result.get("training_cutoff") is not None:
        raise ValueError("JEV does not disclose a training cutoff; it counts only when forward")
    if provider == "local" and result.keys() & set(PRICES):
        raise ValueError("Local models run on this machine and have no token price")
    published = MODELS.get(key, {}).get("training_cutoff")
    declared_cutoff = result.get("training_cutoff")
    if published and declared_cutoff and declared_cutoff < published:
        # Moving a cutoff earlier would relabel contaminated replays as evidence.
        raise ValueError(f"{key}: a declared cutoff cannot precede the published {published}")
    return result


def _provider(value: object) -> None:
    if not isinstance(value, str) or value not in PROVIDERS:
        raise ValueError(f"provider must be one of: {', '.join(PROVIDERS)}")


def _cutoff(key: str, value: object) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not _DATE.fullmatch(value):
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"{key}: training_cutoff must be null or a YYYY-MM-DD date") from None


def _price(key: str, field: str, value: object) -> float | None:
    if value is None:
        return None
    # The range test also rejects NaN and infinities.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= MAX_USD_PER_MILLION_TOKENS
    ):
        raise ValueError(f"{key}: {field} must be null or 0–{MAX_USD_PER_MILLION_TOKENS:g}")
    return float(value)


def _source(key: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(f"{key}: source must be 1–200 characters of text")
    return value.strip()
