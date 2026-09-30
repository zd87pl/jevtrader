"""User settings, never secrets: strictly validated, written atomically with owner-only access."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
from datetime import time
from pathlib import Path

from . import local, paths, registry
from .common import sec_symbol
from .providers import PROVIDERS
from .sec import _EMAIL

DEFAULTS: dict = {
    "version": 1,
    "sec_user_agent": "",
    "watchlist": [],
    "universe": "watchlist",
    "provider": "rules",
    "model": None,
    "local_base_url": local.DEFAULT_BASE_URL,
    "bars_source": "none",
    "alpaca_feed": "sip",
    "brief_time": "08:45",
    "notify": True,
    "spend_cap_usd_month": 5.0,
    "ledger": None,
    # {"provider:model": {"training_cutoff": "YYYY-MM-DD" | null, "source": str,
    #  "usd_per_million_input_tokens": number, "usd_per_million_output_tokens": number}}:
    # facts the model registry lacks.
    "model_overrides": {},
    # A fitted ridge model id (see `status`); without one every service decision is WATCH.
    "calibrator": None,
}
DEFAULT_MODELS = {"rules": "rules-v1", "local": local.DEFAULT_MODEL, "jev": "jev-1.13.0"}
MAX_WATCHLIST = 500
MAX_SPEND_USD_MONTH = 1000.0

_CHOICES = {
    "universe": ("watchlist", "all"),
    "provider": PROVIDERS,
    "bars_source": ("none", "alpaca"),
    # IEX volume is a small slice of consolidated volume and would break liquidity gates.
    "alpaca_feed": ("sip",),
}
_TIME = re.compile(r"(\d{1,2}):(\d{2})")


def load(path: Path | None = None) -> dict:
    target = Path(path) if path is not None else paths.config_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Config {target} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from None
    return validate(data)


def save(config: dict, path: Path | None = None) -> None:
    normalized = validate(config)
    target = Path(path) if path is not None else paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkstemp creates the file 0600, so no reader ever sees a partial or wider-mode config.
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(normalized, output, indent=2, sort_keys=True, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def validate(config: dict) -> dict:
    """Return a complete normalized copy; missing keys take their defaults."""
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object")
    unknown = sorted(set(config) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"Unknown config keys: {', '.join(map(str, unknown))}")
    result = {**copy.deepcopy(DEFAULTS), **copy.deepcopy(config)}
    if type(result["version"]) is not int or result["version"] != 1:
        raise ValueError("Config version must be 1")
    result["sec_user_agent"] = _user_agent(result["sec_user_agent"])
    result["watchlist"] = _watchlist(result["watchlist"])
    for key, allowed in _CHOICES.items():
        if result[key] not in allowed:
            raise ValueError(f"{key} must be one of: {', '.join(allowed)}")
    result["model"] = _model(result["model"], result["provider"])
    result["local_base_url"] = _loopback_url(result["local_base_url"])
    result["brief_time"] = _clock_time(result["brief_time"])
    if type(result["notify"]) is not bool:
        raise ValueError("notify must be true or false")
    result["spend_cap_usd_month"] = _spend_cap(result["spend_cap_usd_month"])
    result["ledger"] = _ledger(result["ledger"])
    result["model_overrides"] = registry.validate_overrides(result["model_overrides"])
    result["calibrator"] = _calibrator(result["calibrator"])
    return result


def resolved_model(config: dict) -> str:
    config = validate(config)
    return config["model"] or DEFAULT_MODELS[config["provider"]]


def ledger_path(config: dict) -> Path:
    config = validate(config)
    return Path(config["ledger"]) if config["ledger"] else paths.ledger_path()


def _user_agent(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("sec_user_agent must be text")
    value = value.strip()
    if value and (
        len(value) > 250
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or not _EMAIL.search(value)
    ):
        raise ValueError(
            "sec_user_agent must be a contact for SEC with an email address, ideally a "
            "dedicated alias such as 'jevtrader sec-alias@example.com' (at most 250 chars)"
        )
    return value


def _watchlist(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("watchlist must be a list of ticker symbols")
    # SEC's form (BRK-B), so a BRK.B entry still matches the filings SEC maps to BRK-B.
    result = list(dict.fromkeys(sec_symbol(item) for item in value))
    if len(result) > MAX_WATCHLIST:
        raise ValueError(f"watchlist is limited to {MAX_WATCHLIST} symbols")
    return result


def _model(value: object, provider: str) -> str | None:
    if value is None:
        if provider not in DEFAULT_MODELS:
            raise ValueError(f"Provider {provider} requires an explicit model")
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("model must be null or a 1–200 character name")
    return value.strip()


def _loopback_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("local_base_url must be text")
    try:
        # The engine's own rule, so config never accepts a URL the engine would refuse.
        return local.normalize_base_url(value)
    except ValueError:
        raise ValueError(
            "local_base_url must be http(s) on 127.0.0.1, localhost or [::1] with a plain path "
            "and no credentials, query or fragment"
        ) from None


def clock_time(value: object) -> time:
    """An HH:MM wall-clock time (24-hour, America/New_York)."""
    match = _TIME.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError("brief_time must be HH:MM (24-hour, America/New_York)")
    return time(int(match[1]), int(match[2]))


def _clock_time(value: object) -> str:
    return clock_time(value).strftime("%H:%M")


def _calibrator(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or not value.isprintable()
    ):
        raise ValueError("calibrator must be null or a model id from `status`")
    return value.strip()


def _spend_cap(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("spend_cap_usd_month must be a number")
    if not 0 <= value <= MAX_SPEND_USD_MONTH:
        raise ValueError(f"spend_cap_usd_month must be between 0 and {MAX_SPEND_USD_MONTH:g}")
    return float(value)


def _ledger(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ledger must be null or a path")
    result = Path(value.strip()).expanduser()
    if not result.is_absolute():
        # The background service does not share a shell's working directory.
        raise ValueError("ledger must be an absolute path")
    return str(result)
