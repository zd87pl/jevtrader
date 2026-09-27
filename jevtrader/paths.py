"""Where the app keeps its files; the product name lives only here. Nothing is created."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "jevtrader"
HOME_ENV = "JEVTRADER_HOME"
LAUNCHD_LABEL = "io.github.jevtrader.daemon"


def app_dir() -> Path:
    override = os.environ.get(HOME_ENV, "").strip()
    if override:
        # A relative home would move with the working directory, which launchd does not share.
        result = Path(override).expanduser()
        if not result.is_absolute():
            raise ValueError(f"{HOME_ENV} must be an absolute path")
        return result
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    data = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(data) if data and Path(data).is_absolute() else Path.home() / ".local" / "share"
    return base / APP_NAME


def config_path() -> Path:
    return app_dir() / "config.json"


def ledger_path() -> Path:
    return app_dir() / "forward.sqlite"


def research_ledger_path() -> Path:
    # Historical bars reuse symbol/session ids, so they cannot share the forward ledger.
    return app_dir() / "research.sqlite"


def log_dir() -> Path:
    return app_dir() / "logs"


def lock_path() -> Path:
    return app_dir() / "daemon.lock"
