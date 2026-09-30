"""A systemd user service for the daemon on Linux. The unit file never contains secrets.

systemctl is reached only through an injectable runner; the default runs ``systemctl --user``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import paths
from .secrets import KNOWN as SECRET_NAMES

SYSTEMCTL = "systemctl"
UNIT_NAME = f"{paths.APP_NAME}.service"
TIMEOUT_SECONDS = 30
RESTART_SECONDS = 30
MAX_DETAIL_CHARS = 200
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_SECRETISH = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL")

Runner = Callable[[list[str]], Any]  # runner(argv) -> .returncode, .stdout, .stderr


def run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
    )


def unit_text(
    program_args: list[str],
    *,
    log_dir: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """The unit file text; ``log_dir`` defaults to paths.log_dir() at call time."""
    args = _program_args(program_args)
    logs = Path(log_dir) if log_dir is not None else paths.log_dir()
    if not logs.is_absolute():
        raise ValueError("log_dir must be an absolute path")
    env = _environment(environment or {})
    lines = [
        "[Unit]",
        f"Description={paths.APP_NAME} SEC 8-K research daemon",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(_quote(arg) for arg in args),
        # Restart after a crash, but not after a clean exit (for example "already running").
        "Restart=on-failure",
        f"RestartSec={RESTART_SECONDS}",
        "UMask=0077",
        f"StandardOutput=append:{_escape(str(logs / 'daemon.out.log'))}",
        f"StandardError=append:{_escape(str(logs / 'daemon.err.log'))}",
    ]
    lines += [f"Environment={_quote(f'{name}={value}')}" for name, value in env.items()]
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def unit_path(*, home: Path | None = None) -> Path:
    """``$XDG_CONFIG_HOME/systemd/user`` when absolute and no home is given, else ~/.config."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if home is None and xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        base = (Path(home) if home is not None else Path.home()) / ".config"
    return base / "systemd" / "user" / UNIT_NAME


def install(
    program_args: list[str],
    *,
    runner: Runner | None = None,
    home: Path | None = None,
    log_dir: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict:
    """Write the user unit, reload systemd and enable the service now."""
    run = _runner(runner)
    env = dict(environment or {})
    if os.environ.get(paths.HOME_ENV, "").strip() and paths.HOME_ENV not in env:
        # The service must use the same app directory as the shell that installed it.
        env[paths.HOME_ENV] = str(paths.app_dir())
    logs = Path(log_dir) if log_dir is not None else paths.log_dir()
    text = unit_text(program_args, log_dir=logs, environment=env)
    target = unit_path(home=home)
    replaced = _call(run, ["is-active", "--quiet", UNIT_NAME]).returncode == 0
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write(target, text)
    _check(_call(run, ["daemon-reload"]), "daemon-reload", target)
    _check(_call(run, ["enable", "--now", UNIT_NAME]), "enable", target)
    if replaced:
        _check(_call(run, ["restart", UNIT_NAME]), "restart", None)
    return {"unit": str(target), "replaced": replaced, "log_dir": str(logs)}


def uninstall(*, runner: Runner | None = None, home: Path | None = None) -> dict:
    """Stop and disable the service if present and remove its unit; safe to repeat."""
    run = _runner(runner)
    target = unit_path(home=home)
    result = _call(run, ["disable", "--now", UNIT_NAME])
    removed = target.exists()
    target.unlink(missing_ok=True)
    _call(run, ["daemon-reload"])
    return {"unit": str(target), "stopped": result.returncode == 0, "removed": removed}


def status(*, runner: Runner | None = None, home: Path | None = None) -> dict:
    """A short summary of `systemctl --user show`; never raises for a missing service."""
    run = _runner(runner)
    properties = "ActiveState,SubState,MainPID,NRestarts,ExecMainStatus"
    result = _call(run, ["show", UNIT_NAME, f"--property={properties}"])
    fields: dict[str, str] = {}
    if result.returncode == 0:
        for line in (getattr(result, "stdout", "") or "").splitlines():
            key, _, value = line.partition("=")
            fields.setdefault(key.strip(), value.strip())
    pid = _int(fields.get("MainPID"))
    return {
        "unit": UNIT_NAME,
        "installed": unit_path(home=home).exists(),
        "active": fields.get("ActiveState") == "active",
        "state": fields.get("SubState"),
        "pid": pid or None,
        "restarts": _int(fields.get("NRestarts")),
        "last_exit_code": _int(fields.get("ExecMainStatus")),
    }


def _int(value: str | None) -> int | None:
    return int(value) if value is not None and re.fullmatch(r"-?\d+", value) else None


def _escape(value: str) -> str:
    # systemd expands %-specifiers everywhere and $VARS in ExecStart.
    return value.replace("%", "%%")


def _quote(value: str) -> str:
    escaped = _escape(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return f'"{escaped}"'


def _program_args(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ValueError("program_args must be a nonempty list of strings")
    for value in values:
        if not isinstance(value, str) or not value or "\0" in value or "\n" in value:
            raise ValueError("program_args must be nonempty single-line strings")
    if not os.path.isabs(values[0]):
        raise ValueError("The systemd program must be an absolute path")
    _reject_secret_values(values)
    return list(values)


def _environment(values: Mapping[str, str]) -> dict[str, str]:
    result = {}
    for name, value in values.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise ValueError("Environment names must be upper-case shell identifiers")
        if name in SECRET_NAMES or _SECRETISH.search(name):
            # Unit files are plain files; keys belong in the OS secret store.
            raise ValueError(f"{name} looks like a secret; store it in the secret store instead")
        if not isinstance(value, str) or "\0" in value or "\n" in value:
            raise ValueError(f"{name} must be a single-line string")
        result[name] = value
    _reject_secret_values(list(result.values()))
    return result


def _reject_secret_values(values: list[str]) -> None:
    secrets = [os.environ.get(name, "") for name in SECRET_NAMES]
    for value in values:
        if any(len(secret) >= 4 and secret in value for secret in secrets):
            raise ValueError("Refusing to write an API key into the systemd unit")


def _write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".jevtrader-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _runner(runner: Runner | None) -> Runner:
    if runner is not None:
        return runner
    if not sys.platform.startswith("linux") or not shutil.which(SYSTEMCTL):
        raise ValueError(
            "Background services are only available on macOS (launchd) or Linux (systemd)"
        )
    return run


def _call(runner: Runner, args: list[str]) -> Any:
    try:
        return runner([SYSTEMCTL, "--user", *args])
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(f"systemctl {args[0]} could not run") from None


def _check(result: Any, verb: str, cleanup: Path | None) -> None:
    if result.returncode == 0:
        return
    if cleanup is not None:
        cleanup.unlink(missing_ok=True)
    raise RuntimeError(f"systemctl {verb} failed (exit {result.returncode}): {_detail(result)}")


def _detail(result: Any) -> str:
    text = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:MAX_DETAIL_CHARS] or "no output"
