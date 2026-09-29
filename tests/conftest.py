"""Suite-wide guards: no test may reach the real Keychain, launchctl, osascript, the network,
a broker, the real home directory, real credentials or a daemon loop that never ends.

Every guard raises at once and also records the attempt, so a test fails in teardown even when
the code under test swallows the error (a daemon job records failures and carries on). Child
Python processes install the same guards (tests/isolation_guard/sitecustomize.py) and report
their trips to the parent through a log file. Tests that exercise these defaults patch them
again or call a captured original.
"""

from __future__ import annotations

import importlib.util
import os
import re
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest

from jevtrader import daemon, launchd, secrets

_GUARD_FILE = Path(__file__).with_name("isolation_guard") / "sitecustomize.py"
_spec = importlib.util.spec_from_file_location("jevtrader_isolation_guard", _GUARD_FILE)
assert _spec and _spec.loader
GUARD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(GUARD)

SYSTEM_COMMANDS = GUARD.SYSTEM_COMMANDS
MAX_LOOP_TICKS = 1_000  # far more than any test's fake clock needs
# Real keys and the declared SEC contact never reach a test, whichever file it lives in.
SCRUBBED_ENV = frozenset(
    {*secrets.KNOWN, "SEC_USER_AGENT", "OPENAI_MODEL", "XDG_DATA_HOME", "PYTHONSTARTUP"}
)
_SECRET_NAME = re.compile(r"^APCA_|^IB_|_API_KEY$|_API_KEY_ID$|_SECRET(_KEY)?$|_TOKEN$|PASSWORD")


def scrubbed_names(environ: Mapping[str, str]) -> list[str]:
    """The variables in ``environ`` that every test runs without: keys, contact, credentials."""
    return [name for name in environ if name in SCRUBBED_ENV or _SECRET_NAME.search(name)]


class Violations(list):
    """Guard trips in this process, plus those child processes wrote to the log."""

    def __init__(self, log: Path) -> None:
        super().__init__()
        self.log = log

    def children(self) -> list[str]:
        """Move child trips from the log into this list and return them."""
        if not self.log.exists():
            return []
        found = self.log.read_text(encoding="utf-8").splitlines()
        self.log.write_text("", encoding="utf-8")
        self.extend(found)
        return found


@pytest.fixture(autouse=True)
def _offline_system(monkeypatch, tmp_path):
    violations = Violations(tmp_path / "isolation-guard.log")

    def refuse(what: str) -> AssertionError:
        violations.append(what)
        return AssertionError(f"a test tried to {what}")

    def refuse_command(*_args, **_kwargs):
        raise refuse("run a real system command")

    for name in scrubbed_names(os.environ):
        monkeypatch.delenv(name)

    monkeypatch.setattr(secrets, "keychain_available", lambda: False)
    monkeypatch.setattr(secrets, "run", refuse_command)
    monkeypatch.setattr(launchd, "run", refuse_command)

    # Every other route to a program or the network ends here: Popen (wrappers such as env and
    # sh -c included), os.system, posix_spawn, exec*, spawn*, DNS, TCP and UDP. Loopback servers
    # in test_local and test_web stay reachable; broker hosts and gateway ports do not.
    monkeypatch.setenv(GUARD.LOG_ENV, str(violations.log))
    monkeypatch.setenv("PYTHONPATH", GUARD.guarded_env(None)["PYTHONPATH"])
    GUARD.install(refuse, monkeypatch.setattr)

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
    violations.children()
    assert not violations, f"isolation guard tripped: {violations}"
