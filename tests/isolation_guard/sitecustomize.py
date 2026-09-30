"""Isolation guards shared by the test suite and every child interpreter it starts.

conftest.py loads this file and installs the guards with monkeypatch. It also puts this directory
first on the PYTHONPATH of every process a test starts, so a child Python imports it as
`sitecustomize` and installs the same guards before it runs any test code. A child records each
trip in the file named by LOG_ENV, and the parent fails the test in teardown.

Standard library only: a child may not be able to import jevtrader.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from collections.abc import Callable
from typing import Any

SYSTEM_COMMANDS = frozenset({"security", "launchctl", "osascript"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})  # jevtrader.local.LOOPBACK_HOSTS
BROKER_DOMAINS = frozenset({"alpaca.markets", "interactivebrokers.com", "ibkr.com", "ibllc.com"})
BROKER_PORTS = frozenset({7496, 7497, 4001, 4002})  # TWS and IB Gateway, both on loopback
LOG_ENV = "JEVTRADER_TEST_GUARD_LOG"
GUARD_DIR = os.path.dirname(os.path.abspath(__file__))
EXEC_NAMES = ("execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe")
SPAWN_NAMES = tuple(
    name
    for name in ("spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp")
    + ("spawnvpe",)
    if hasattr(os, name)
)
_WORDS = re.compile(r"[\s;&|()`$<>'\"=]+")

Refuse = Callable[[str], AssertionError]
Setter = Callable[[Any, str, Any], None]


def system_command(args: object) -> str | None:
    """The first system command named anywhere in args, wrappers and shell strings included."""
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    try:
        items = list(args)  # type: ignore[call-overload]
    except TypeError:
        return None
    for item in items:
        try:
            text = os.fsdecode(item)
        except TypeError:
            continue
        for word in _WORDS.split(text):
            if os.path.basename(word) in SYSTEM_COMMANDS:
                return os.path.basename(word)
    return None


def loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    return host in (None, "") or host in LOOPBACK_HOSTS


def broker_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if not isinstance(host, str):
        return False
    name = host.lower().rstrip(".")
    return any(name == domain or name.endswith("." + domain) for domain in BROKER_DOMAINS)


def guarded_env(env: Any) -> dict[str, str]:
    """The child environment with this directory first on PYTHONPATH and the log path set."""
    merged = dict(os.environ if env is None else env)
    path = [part for part in merged.get("PYTHONPATH", "").split(os.pathsep) if part]
    merged["PYTHONPATH"] = os.pathsep.join([GUARD_DIR, *(p for p in path if p != GUARD_DIR)])
    if os.environ.get(LOG_ENV):
        merged[LOG_ENV] = os.environ[LOG_ENV]
    return merged


def install(refuse: Refuse, setter: Setter) -> None:
    """Patch every process and network route this suite knows of; setter does the patching."""
    popen_init = subprocess.Popen.__init__

    def guarded_popen(self: Any, args: Any, *rest: Any, **kwargs: Any) -> None:
        program = system_command(args)
        if program or (kwargs.get("shell") and any(name in str(args) for name in SYSTEM_COMMANDS)):
            raise refuse(f"run {program or args!r}")
        if len(rest) < 10:  # env is the eleventh positional parameter
            kwargs["env"] = guarded_env(kwargs.get("env"))
        popen_init(self, args, *rest, **kwargs)

    setter(subprocess.Popen, "__init__", guarded_popen)

    system, posix_spawn, posix_spawnp = os.system, os.posix_spawn, os.posix_spawnp

    def guarded_system(command: Any) -> int:
        program = system_command(command)
        if program:
            raise refuse(f"run {program!r}")
        return system(command)

    def spawn_guard(original: Callable[..., int]) -> Callable[..., int]:
        def guarded(path: Any, argv: Any, env: Any, *args: Any, **kwargs: Any) -> int:
            program = system_command([path, *argv])
            if program:
                raise refuse(f"run {program!r}")
            return original(path, argv, guarded_env(env), *args, **kwargs)

        return guarded

    setter(os, "system", guarded_system)
    setter(os, "posix_spawn", spawn_guard(posix_spawn))
    setter(os, "posix_spawnp", spawn_guard(posix_spawnp))

    def refuse_replace(name: str) -> Callable[..., Any]:
        def refused(*_args: Any, **_kwargs: Any) -> Any:
            raise refuse(f"call os.{name}; start programs with subprocess")

        return refused

    for name in (*EXEC_NAMES, *SPAWN_NAMES):
        setter(os, name, refuse_replace(name))

    resolve = socket.getaddrinfo
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    sendto, sendmsg = socket.socket.sendto, socket.socket.sendmsg

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if broker_host(host):
            raise refuse(f"reach broker host {host!r}")
        if not loopback(host):
            raise refuse(f"resolve {host!r}")
        return resolve(host, *args, **kwargs)

    def check(address: object, verb: str) -> None:
        if not isinstance(address, tuple):
            return  # AF_UNIX paths are local
        host = address[0]
        if broker_host(host):
            raise refuse(f"reach broker host {host!r}")
        if not loopback(host):
            raise refuse(f"{verb} {host!r}")
        if len(address) > 1 and address[1] in BROKER_PORTS:
            raise refuse(f"reach broker gateway port {address[1]}")

    def guarded_connect(sock: Any, address: Any) -> Any:
        check(address, "connect to")
        return connect(sock, address)

    def guarded_connect_ex(sock: Any, address: Any) -> Any:
        check(address, "connect to")
        return connect_ex(sock, address)

    def guarded_sendto(sock: Any, data: Any, *args: Any) -> Any:
        check(args[-1] if args else None, "send to")
        return sendto(sock, data, *args)

    def guarded_sendmsg(sock: Any, buffers: Any, *args: Any, **kwargs: Any) -> Any:
        check(args[2] if len(args) > 2 else kwargs.get("address"), "send to")
        return sendmsg(sock, buffers, *args, **kwargs)

    setter(socket, "getaddrinfo", guarded_getaddrinfo)
    setter(socket.socket, "connect", guarded_connect)
    setter(socket.socket, "connect_ex", guarded_connect_ex)
    setter(socket.socket, "sendto", guarded_sendto)
    setter(socket.socket, "sendmsg", guarded_sendmsg)


def _install_in_child() -> None:
    log = os.environ.get(LOG_ENV)

    def refuse(what: str) -> AssertionError:
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(f"(child) {what}\n")
        return AssertionError(f"a test's child process tried to {what}")

    install(refuse, setattr)


if __name__ == "sitecustomize":
    _install_in_child()
