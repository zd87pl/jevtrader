"""macOS notifications through osascript; text travels as argv, never as AppleScript source.

The script is a fixed run handler. ``--`` ends osascript's option parsing, so a title such
as ``-e ...`` stays data instead of becoming another script statement. Failures return
False: a missed notification must never stop the daemon.
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata
from collections.abc import Callable
from typing import Any

from .security import childenv

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT_SECONDS = 10
MAX_TITLE_CHARS = 80
MAX_BODY_CHARS = 240
SCRIPT = (
    "-e",
    "on run argv",
    "-e",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "-e",
    "end run",
)

Runner = Callable[..., Any]  # runner(argv: list[str], input: str | None) -> .returncode


def _run(argv: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        input=input,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
        env=childenv.scrubbed(),
    )


def _clean(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Notification {name} must be a string")
    # Control and bidi-override characters could disguise what the notification says.
    text = "".join(
        " " if unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} else ch for ch in value
    )
    text = " ".join(text.split())
    if not text:
        raise ValueError(f"Notification {name} must not be empty")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def argv(title: str, body: str) -> list[str]:
    return [
        OSASCRIPT,
        *SCRIPT,
        "--",
        _clean(title, "title", MAX_TITLE_CHARS),
        _clean(body, "body", MAX_BODY_CHARS),
    ]


def macos(title: str, body: str, *, runner: Runner | None = None) -> bool:
    command = argv(title, body)
    if sys.platform != "darwin":
        return False
    try:
        result = (runner or _run)(command, None)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(result, "returncode", 1) == 0
