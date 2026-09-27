"""Suite-wide guards: no test may reach the real Keychain, launchctl, osascript, the network,
the real home directory or a daemon loop that never ends.

Every guard raises at once and also records the attempt, so a test fails in teardown even when
the code under test swallows the error (a daemon job records failures and carries on). Tests
that exercise these defaults patch them again or call a captured original.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading

import pytest

from jevtrader import daemon, launchd, local, secrets

SYSTEM_COMMANDS = frozenset({"security", "launchctl", "osascript"})
MAX_LOOP_TICKS = 1_000  # far more than any test's fake clock needs


def _program(args: object) -> str:
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    first = next(iter(args), "") if isinstance(args, (list, tuple)) else ""
    return os.path.basename(os.fsdecode(first)) if first else ""


def _loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    return host in (None, "") or host in local.LOOPBACK_HOSTS


@pytest.fixture(autouse=True)
def _offline_system(monkeypatch, tmp_path):
    violations: list[str] = []

    def refuse(what: str) -> AssertionError:
        violations.append(what)
        return AssertionError(f"a test tried to {what}")

    def refuse_command(*_args, **_kwargs):
        raise refuse("run a real system command")

    monkeypatch.setattr(secrets, "_keychain_available", lambda: False)
    monkeypatch.setattr(secrets, "_run", refuse_command)
    monkeypatch.setattr(launchd, "_run", refuse_command)

    # Any other route to these programs (notify's osascript, a new helper) ends here.
    popen_init = subprocess.Popen.__init__

    def guarded_popen(self, args, *rest, **kwargs):
        program = _program(args)
        if program in SYSTEM_COMMANDS or (
            kwargs.get("shell") and any(name in str(args) for name in SYSTEM_COMMANDS)
        ):
            raise refuse(f"run {program or args!r}")
        popen_init(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen)

    # Loopback servers in test_local and test_web stay reachable; nothing else is.
    resolve, connect, connect_ex = (
        socket.getaddrinfo,
        socket.socket.connect,
        socket.socket.connect_ex,
    )

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _loopback(host):
            raise refuse(f"resolve {host!r}")
        return resolve(host, *args, **kwargs)

    def target(address: object) -> object:
        return address[0] if isinstance(address, tuple) else None  # AF_UNIX paths are local

    def guarded_connect(sock, address):
        if not _loopback(target(address)):
            raise refuse(f"connect to {target(address)!r}")
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _loopback(target(address)):
            raise refuse(f"connect to {target(address)!r}")
        return connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    # A lost stop event or a lock regression must fail, not sleep 10 s per tick forever.
    run_forever = daemon.run_forever

    def bounded_run_forever(ctx, *, sleep=None, stop=None, **kwargs):
        stop = stop or threading.Event()
        ticks = 0

        def bounded_sleep(seconds):
            nonlocal ticks
            ticks += 1
            if sleep is None:
                raise refuse("sleep for real in the daemon loop; pass sleep= and stop=")
            if ticks > MAX_LOOP_TICKS:
                raise refuse(f"run the daemon loop past {MAX_LOOP_TICKS} ticks")
            return sleep(seconds)

        return run_forever(ctx, sleep=bounded_sleep, stop=stop, **kwargs)

    monkeypatch.setattr(daemon, "run_forever", bounded_run_forever)

    # Code that falls back to ~ (the default app dir, ~/Library/LaunchAgents) stays in tmp.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("JEVTRADER_HOME", str(tmp_path / "jevtrader-home"))
    yield violations
    assert not violations, f"isolation guard tripped: {violations}"
