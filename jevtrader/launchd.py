"""A per-user macOS LaunchAgent for the daemon. The plist never contains secrets.

launchctl is reached only through an injectable runner; the default runs /bin/launchctl.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import paths
from .secrets import KNOWN as SECRET_NAMES

LAUNCHCTL = "/bin/launchctl"
TIMEOUT_SECONDS = 30
THROTTLE_SECONDS = 30
UMASK = 0o077  # the ledger, config and logs the service creates stay owner-only
MAX_DETAIL_CHARS = 200
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_SECRETISH = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL")

Runner = Callable[[list[str]], Any]  # runner(argv) -> .returncode, .stdout, .stderr


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
    )


def plist(
    program_args: list[str],
    *,
    label: str = paths.LAUNCHD_LABEL,
    log_dir: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    """XML plist bytes; ``log_dir`` defaults to paths.log_dir() at call time."""
    args = _program_args(program_args)
    logs = Path(log_dir) if log_dir is not None else paths.log_dir()
    if not logs.is_absolute():
        raise ValueError("log_dir must be an absolute path")
    job: dict[str, Any] = {
        "Label": _label(label),
        "ProgramArguments": args,
        "RunAtLoad": True,
        # Restart after a crash, but not after a clean exit (for example "already running").
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "ThrottleInterval": THROTTLE_SECONDS,
        "Umask": UMASK,
        "StandardOutPath": str(logs / "daemon.out.log"),
        "StandardErrorPath": str(logs / "daemon.err.log"),
    }
    env = _environment(environment or {})
    if env:
        job["EnvironmentVariables"] = env
    return plistlib.dumps(job, fmt=plistlib.FMT_XML, sort_keys=True)


def plist_path(*, label: str = paths.LAUNCHD_LABEL, home: Path | None = None) -> Path:
    base = Path(home) if home is not None else Path.home()
    return base / "Library" / "LaunchAgents" / f"{_label(label)}.plist"


def install(
    program_args: list[str],
    *,
    runner: Runner | None = None,
    home: Path | None = None,
    label: str = paths.LAUNCHD_LABEL,
    log_dir: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict:
    """Write the LaunchAgent plist and bootstrap it into the user's GUI domain."""
    run = _runner(runner)
    env = dict(environment or {})
    if os.environ.get(paths.HOME_ENV, "").strip() and paths.HOME_ENV not in env:
        # The service must use the same app directory as the shell that installed it.
        env[paths.HOME_ENV] = str(paths.app_dir())
    logs = Path(log_dir) if log_dir is not None else paths.log_dir()
    data = plist(program_args, label=label, log_dir=logs, environment=env)
    target = plist_path(label=label, home=home)
    service = _service(label)
    replaced = _call(run, [LAUNCHCTL, "print", service]).returncode == 0
    if replaced:
        _call(run, [LAUNCHCTL, "bootout", service])
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write(target, data)
    # A service disabled earlier (e.g. by `launchctl disable`) refuses to bootstrap.
    _call(run, [LAUNCHCTL, "enable", service])
    result = _call(run, [LAUNCHCTL, "bootstrap", _domain(), str(target)])
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"launchctl bootstrap failed (exit {result.returncode}): {_detail(result)}"
        )
    return {
        "label": _label(label),
        "plist": str(target),
        "domain": _domain(),
        "replaced": replaced,
        "log_dir": str(logs),
    }


def uninstall(
    *, runner: Runner | None = None, home: Path | None = None, label: str = paths.LAUNCHD_LABEL
) -> dict:
    """Stop the service if loaded and remove its plist; safe to repeat."""
    run = _runner(runner)
    target = plist_path(label=label, home=home)
    result = _call(run, [LAUNCHCTL, "bootout", _service(label)])
    removed = target.exists()
    target.unlink(missing_ok=True)
    return {
        "label": _label(label),
        "plist": str(target),
        "unloaded": result.returncode == 0,
        "removed": removed,
    }


def status(
    *, runner: Runner | None = None, home: Path | None = None, label: str = paths.LAUNCHD_LABEL
) -> dict:
    """A short summary of `launchctl print`; never raises for a service that is not loaded."""
    run = _runner(runner)
    result = _call(run, [LAUNCHCTL, "print", _service(label)])
    summary: dict[str, Any] = {
        "label": _label(label),
        "installed": plist_path(label=label, home=home).exists(),
        "loaded": result.returncode == 0,
        "state": None,
        "pid": None,
        "runs": None,
        "last_exit_code": None,
    }
    if result.returncode != 0:
        return summary
    fields = _top_level(getattr(result, "stdout", "") or "")
    summary["state"] = fields.get("state")
    summary["pid"] = _leading_int(fields.get("pid"))
    summary["runs"] = _leading_int(fields.get("runs"))
    summary["last_exit_code"] = _leading_int(fields.get("last exit code"))
    return summary


def _top_level(output: str) -> dict[str, str]:
    # Service properties sit one tab deep; nested sections reuse names like "state".
    fields: dict[str, str] = {}
    for line in output.splitlines():
        match = re.fullmatch(r"\t([a-z][a-z ]*?) = (.*)", line)
        if match and match[1] not in fields:
            fields[match[1]] = match[2].strip()
    return fields


def _leading_int(value: str | None) -> int | None:
    match = re.match(r"-?\d+", value or "")
    return int(match[0]) if match else None


def _label(label: str) -> str:
    # The label becomes a file name under ~/Library/LaunchAgents.
    if not isinstance(label, str) or not _LABEL.fullmatch(label) or ".." in label:
        raise ValueError("launchd label must be reverse-DNS style letters, digits, dots, dashes")
    return label


def _program_args(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ValueError("program_args must be a nonempty list of strings")
    for value in values:
        if not isinstance(value, str) or not value or "\0" in value or "\n" in value:
            raise ValueError("program_args must be nonempty single-line strings")
    if not os.path.isabs(values[0]):
        raise ValueError("The launchd program must be an absolute path")
    _reject_secret_values(values)
    return list(values)


def _environment(values: Mapping[str, str]) -> dict[str, str]:
    result = {}
    for name, value in values.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise ValueError("Environment names must be upper-case shell identifiers")
        if name in SECRET_NAMES or _SECRETISH.search(name):
            # Plists are plain files; keys belong in the Keychain.
            raise ValueError(f"{name} looks like a secret; store it in the Keychain instead")
        if not isinstance(value, str) or "\0" in value or "\n" in value:
            raise ValueError(f"{name} must be a single-line string")
        result[name] = value
    _reject_secret_values(list(result.values()))
    return result


def _reject_secret_values(values: list[str]) -> None:
    secrets = [os.environ.get(name, "") for name in SECRET_NAMES]
    for value in values:
        if any(len(secret) >= 4 and secret in value for secret in secrets):
            raise ValueError("Refusing to write an API key into the launchd plist")


def _write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".jevtrader-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # launchd ignores agent plists that are group- or world-writable.
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _runner(runner: Runner | None) -> Runner:
    if runner is not None:
        return runner
    if sys.platform != "darwin" or not os.path.exists(LAUNCHCTL):
        raise ValueError("launchd is only available on macOS")
    return _run


def _call(runner: Runner, argv: list[str]) -> Any:
    try:
        return runner(argv)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(f"launchctl {argv[1]} could not run") from None


def _detail(result: Any) -> str:
    text = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:MAX_DETAIL_CHARS] or "no output"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _service(label: str) -> str:
    return f"{_domain()}/{_label(label)}"
