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
from textwrap import dedent
from unittest.mock import patch

import pytest

from jevtrader import daemon, local, notify, paths, secrets
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
            secrets.run(["/usr/bin/security", "find-generic-password"])
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

    def test_wrappers_cannot_launch_system_commands(self):
        wrapped = [
            ["env", "security", "list-keychains"],
            ["/usr/bin/env", "-i", "PATH=/usr/bin", "/bin/launchctl", "list"],
            ["sh", "-c", "osascript -e 'beep'"],
            ["/bin/sh", "-c", "cd / && exec /usr/bin/security dump-keychain"],
            [b"env", b"launchctl", b"print", b"system"],
        ]
        for argv in wrapped:
            with self.assertRaisesRegex(AssertionError, "run "):
                subprocess.run(argv, check=False)
        with self.assertRaisesRegex(AssertionError, "run "):
            subprocess.run("true; security list-keychains", shell=True, check=False)
        self.assertEqual(subprocess.run(["env", "true"], check=False).returncode, 0)
        self.assertEqual(len(self.violations), 6)
        self.violations.clear()

    def test_other_spawn_routes_are_refused(self):
        attempts = [
            lambda: os.system("security list-keychains"),
            lambda: os.system("env launchctl list"),
            lambda: os.posix_spawn("/usr/bin/security", ["security", "list"], os.environ),
            lambda: os.posix_spawnp("env", ["env", "osascript", "-e", "1"], os.environ),
            lambda: os.spawnv(os.P_WAIT, sys.executable, [sys.executable, "-c", "pass"]),
            lambda: os.spawnlp(os.P_WAIT, "launchctl", "launchctl", "list"),
            lambda: os.execv(sys.executable, [sys.executable, "-c", "pass"]),
            lambda: os.execvp("security", ["security", "list"]),
            lambda: os.execle("/usr/bin/true", "true", {}),
        ]
        for attempt in attempts:
            with self.assertRaises(AssertionError):
                attempt()
        self.assertEqual(len(self.violations), len(attempts))
        self.violations.clear()
        self.assertEqual(os.system("true"), 0)  # a harmless shell command still runs
        pid = os.posix_spawn("/usr/bin/true", ["true"], os.environ)
        self.assertEqual(os.waitpid(pid, 0)[1], 0)

    def test_udp_beyond_loopback_is_refused(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(probe.close)
        with self.assertRaisesRegex(AssertionError, "send to '192.0.2.1'"):
            probe.sendto(b"x", ("192.0.2.1", 53))
        with self.assertRaisesRegex(AssertionError, "send to '192.0.2.2'"):
            probe.sendto(b"x", 0, ("192.0.2.2", 53))
        with self.assertRaisesRegex(AssertionError, "send to '192.0.2.3'"):
            probe.sendmsg([b"x"], [], 0, ("192.0.2.3", 53))
        self.refused("send to '192.0.2.1'", "send to '192.0.2.2'", "send to '192.0.2.3'")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(("127.0.0.1", 0))
            probe.sendto(b"ok", server.getsockname())
            probe.sendmsg([b"ok"], [], 0, server.getsockname())
            self.assertEqual(server.recv(8), b"ok")

    def test_broker_hosts_and_gateway_ports_are_refused(self):
        for host in ("paper-api.alpaca.markets", "API.Alpaca.Markets", "api.ibkr.com"):
            with self.assertRaisesRegex(AssertionError, "reach broker host"):
                socket.getaddrinfo(host, 443)
        with self.assertRaisesRegex(AssertionError, "reach broker host"):
            urllib.request.urlopen("https://www.interactivebrokers.com/", timeout=30)
        for port in (7496, 7497, 4001, 4002):  # TWS and IB Gateway listen on loopback
            probe = socket.socket()
            self.addCleanup(probe.close)
            with self.assertRaisesRegex(AssertionError, f"broker gateway port {port}"):
                probe.connect(("127.0.0.1", port))
        with self.assertRaisesRegex(AssertionError, "broker gateway port 7497"):
            socket.socket().connect_ex(("localhost", 7497))
        self.assertEqual(len(self.violations), 9)
        self.violations.clear()

    def test_child_interpreters_inherit_the_guards(self):
        script = dedent(
            """
            import os, socket, subprocess
            # Built at run time: the parent refuses argv that names a system command outright.
            keychain, agents = "secu" + "rity", "launch" + "ctl"
            try:
                socket.getaddrinfo("www.sec.gov", 443)
            except AssertionError:
                pass  # swallowed: the parent still sees it
            try:
                subprocess.run(["env", keychain, "list"], check=False)
            except AssertionError:
                pass
            try:
                os.system(agents + " list")
            except AssertionError:
                pass
            """
        )
        for environment in (None, {"PATH": os.environ.get("PATH", "")}):
            done = subprocess.run(
                [sys.executable, "-c", script], env=environment, capture_output=True, check=False
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout, b"")
        expected = [
            "resolve 'www.sec.gov'",
            "run 'security'",
            "run 'launchctl'",
        ]
        self.assertEqual(self.violations.children(), [f"(child) {v}" for v in expected] * 2)
        self.violations.clear()
        clean = subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True)
        self.assertEqual(clean.stdout, b"1\n")
        self.assertEqual(self.violations.children(), [])

    def test_credentials_and_identity_are_scrubbed_for_every_test(self):
        for name in (*secrets.KNOWN, "SEC_USER_AGENT", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
            self.assertNotIn(name, os.environ)
        self.assertFalse([name for name in os.environ if name.endswith("_API_KEY")])

    def test_guard_constants_match_the_package(self):
        from conftest import GUARD

        self.assertEqual(GUARD.LOOPBACK_HOSTS, local.LOOPBACK_HOSTS)
        self.assertEqual(GUARD.SYSTEM_COMMANDS, {"security", "launchctl", "osascript"})
        self.assertGreaterEqual(GUARD.BROKER_DOMAINS, {"alpaca.markets", "ibkr.com"})


if __name__ == "__main__":
    unittest.main()
