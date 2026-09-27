"""API keys from the environment or the macOS Keychain; values never reach argv, logs or errors."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from typing import Any

from . import paths

SERVICE = paths.APP_NAME
KNOWN = ("TYPESAFE_API_KEY", "OPENAI_API_KEY", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
SECURITY = "/usr/bin/security"
NOT_FOUND = 44  # errSecItemNotFound as a `security` exit status.
TIMEOUT_SECONDS = 30
# `security -i` tokenizes its input line; this set needs no quoting and cannot start an option.
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~+/=:-]{0,511}")

Runner = Callable[..., Any]  # runner(argv: list[str], input: str | None) -> .returncode, .stdout


def _run(argv: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, input=input, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
    )


def _keychain_available() -> bool:
    return sys.platform == "darwin" and os.path.exists(SECURITY)


def _name(name: str) -> str:
    if name not in KNOWN:
        raise ValueError(f"Unknown secret name; expected one of: {', '.join(KNOWN)}")
    return name


def _call(runner: Runner, argv: list[str], stdin: str | None = None) -> Any:
    try:
        return runner(argv, stdin)
    except (OSError, subprocess.SubprocessError):
        # Exception text can echo the command's input; report only what failed.
        raise RuntimeError(f"Keychain command {argv[1]} could not run") from None


def _lookup(name: str, runner: Runner) -> str | None:
    argv = [SECURITY, "find-generic-password", "-s", SERVICE, "-a", name, "-w"]
    result = _call(runner, argv)
    if result.returncode == NOT_FOUND:
        return None
    if result.returncode != 0:
        raise RuntimeError(f"Keychain lookup for {name} failed (exit {result.returncode})")
    return (result.stdout or "").rstrip("\r\n") or None


def get(name: str, *, runner: Runner | None = None) -> str | None:
    """The environment wins so a shell or launchd override needs no Keychain entry."""
    name = _name(name)
    if os.environ.get(name):
        return os.environ[name]
    if runner is None:
        if not _keychain_available():
            return None
        runner = _run
    return _lookup(name, runner)


def set(name: str, value: str, *, runner: Runner | None = None) -> None:
    name = _name(name)
    if not isinstance(value, str) or not _VALUE.fullmatch(value):
        raise ValueError(
            f"{name} must be 1–512 characters of letters, digits and ._~+/=:- "
            "starting with a letter or digit"
        )
    runner = runner or _require_keychain(name)
    # Values go through stdin: argv is visible to every local process via `ps`.
    command = f"add-generic-password -U -s {SERVICE} -a {name} -w {value}\n"
    result = _call(runner, [SECURITY, "-i"], command)
    # Interactive mode can exit 0 after a failed command; confirm by reading it back.
    if result.returncode != 0 or _lookup(name, runner) != value:
        raise RuntimeError(f"Keychain did not store {name}")


def delete(name: str, *, runner: Runner | None = None) -> bool:
    name = _name(name)
    runner = runner or _require_keychain(name)
    argv = [SECURITY, "delete-generic-password", "-s", SERVICE, "-a", name]
    result = _call(runner, argv)
    if result.returncode == NOT_FOUND:
        return False
    if result.returncode != 0:
        raise RuntimeError(f"Keychain delete for {name} failed (exit {result.returncode})")
    return True


def export_to_environ(names: Iterable[str] = KNOWN, *, runner: Runner | None = None) -> list[str]:
    """Fill unset variables from the Keychain; returns the names loaded, never values."""
    names = [_name(name) for name in names]
    if runner is None:
        if not _keychain_available():
            return []
        runner = _run
    loaded = []
    for name in names:
        if os.environ.get(name):
            continue
        value = _lookup(name, runner)
        if value:
            os.environ[name] = value
            loaded.append(name)
    return loaded


def _require_keychain(name: str) -> Runner:
    if not _keychain_available():
        raise ValueError(f"The macOS Keychain is unavailable here; export {name} instead")
    return _run
