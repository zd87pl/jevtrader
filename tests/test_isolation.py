"""The suite-wide guards in conftest: escapes fail at once instead of hanging or leaking."""

import os
import pwd
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from jevtrader import daemon, notify, paths, secrets
from jevtrader.common import load_strategy
from jevtrader.store import Ledger

NOW = "2026-09-28T13:30:00.000000Z"


def idle_context(ledger):
    return daemon.Context(
        ledger=ledger,
        config={},
        strategy=load_strategy(),
        clock=lambda: NOW,
        poll=lambda *a, **k: {},
        bars=lambda *a, **k: {},
        observe=lambda *a, **k: {"forecasts": [], "errors": []},
        settle=lambda *a, **k: {},
        brief=lambda *a, **k: {"filings": []},
        render=lambda report: ("title", "body"),
        notify=lambda title, body: True,
        reconcile=lambda *a, **k: [],
        log=lambda line: None,
    )


class GuardTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def _guards(self, _offline_system, tmp_path):
        self.violations, self.dir = _offline_system, tmp_path

    def refused(self, *expected):
        self.assertEqual(self.violations, list(expected))
        self.violations.clear()  # tripped on purpose; the fixture's teardown check stays clean

    def test_network_beyond_loopback_is_refused_at_once(self):
        started = time.monotonic()
        with self.assertRaisesRegex(AssertionError, "resolve 'www.sec.gov'"):
            urllib.request.urlopen("https://www.sec.gov/", timeout=30)
        with self.assertRaisesRegex(AssertionError, "resolve '192.0.2.1'"):
            socket.create_connection(("192.0.2.1", 80), timeout=30)
        probe = socket.socket()
        self.addCleanup(probe.close)
        with self.assertRaisesRegex(AssertionError, "connect to '192.0.2.1'"):
            probe.connect(("192.0.2.1", 80))
        self.assertLess(time.monotonic() - started, 1)
        self.refused("resolve 'www.sec.gov'", "resolve '192.0.2.1'", "connect to '192.0.2.1'")

    def test_loopback_stays_reachable(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            with socket.create_connection(server.getsockname(), timeout=5):
                pass
        self.assertEqual(self.violations, [])

    def test_system_commands_are_refused_but_other_programs_run(self):
        with self.assertRaisesRegex(AssertionError, "run 'security'"):
            subprocess.run(["/usr/bin/security", "list-keychains"], check=False)
        with self.assertRaisesRegex(AssertionError, "run 'launchctl'"):
            subprocess.run(("launchctl", "list"), check=False)
        with patch.object(notify.sys, "platform", "darwin"):
            with self.assertRaisesRegex(AssertionError, "run 'osascript'"):
                notify.macos("title", "body")
        with self.assertRaisesRegex(AssertionError, "a real system command"):
            secrets._run(["/usr/bin/security", "find-generic-password"])
        with patch.dict(os.environ):
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertIsNone(secrets.get("OPENAI_API_KEY"))  # the Keychain reads as absent
        done = subprocess.run([sys.executable, "-c", "pass"], check=False)
        self.assertEqual(done.returncode, 0)
        self.refused(
            "run 'security'", "run 'launchctl'", "run 'osascript'", "run a real system command"
        )

    def test_daemon_loop_cannot_sleep_for_real_or_forever(self):
        with Ledger(":memory:") as ledger:
            started = time.monotonic()
            with self.assertRaisesRegex(AssertionError, "sleep for real"):
                daemon.run_forever(idle_context(ledger), lock_path=self.dir / "a.lock")
            with self.assertRaisesRegex(AssertionError, "past 1000 ticks"):
                daemon.run_forever(
                    idle_context(ledger), sleep=lambda s: None, lock_path=self.dir / "b.lock"
                )
            self.assertLess(time.monotonic() - started, 10)
            stop = threading.Event()
            daemon.run_forever(
                idle_context(ledger),
                sleep=lambda s: stop.set(),
                stop=stop,
                lock_path=self.dir / "c",
            )
        self.refused(
            "sleep for real in the daemon loop; pass sleep= and stop=",
            "run the daemon loop past 1000 ticks",
        )

    def test_home_and_app_directory_are_temporary(self):
        real = Path(pwd.getpwuid(os.getuid()).pw_dir)
        self.assertEqual(Path.home(), self.dir / "home")
        self.assertEqual(paths.app_dir(), self.dir / "jevtrader-home")
        with patch.dict(os.environ, {paths.HOME_ENV: ""}):
            self.assertFalse(paths.app_dir().is_relative_to(real))


if __name__ == "__main__":
    unittest.main()
