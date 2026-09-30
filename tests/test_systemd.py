"""systemd user units and exact systemctl argv; these tests never run systemctl."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jevtrader import paths, secrets, systemd

PROGRAM = ["/usr/bin/python3", "-m", "jevtrader", "daemon"]
CANARY = "canary-Zq7.Secret+/="
UNIT = systemd.UNIT_NAME


class FakeSystemctl:
    """Records argv; `fail` maps verb -> exit code or exception."""

    def __init__(self, *, active=False, fail=None, show="ActiveState=active\nMainPID=42\n"):
        self.calls = []
        self.active = active
        self.fail = dict(fail or {})
        self.show = show

    def __call__(self, argv):
        self.calls.append(list(argv))
        verb = argv[2]
        if verb in self.fail:
            code = self.fail[verb]
            if isinstance(code, BaseException):
                raise code
            return SimpleNamespace(returncode=code, stdout="", stderr=f"{verb} failed\x1b[0m")
        if verb == "is-active":
            return SimpleNamespace(returncode=0 if self.active else 3, stdout="", stderr="")
        if verb == "show":
            return SimpleNamespace(returncode=0, stdout=self.show, stderr="")
        if verb == "enable":
            self.active = True
        if verb == "disable":
            code = 0 if self.active else 1
            self.active = False
            return SimpleNamespace(returncode=code, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class SystemdTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        environment = patch.dict(os.environ, {paths.HOME_ENV: str(self.home / "app")})
        environment.start()
        self.addCleanup(environment.stop)
        for name in (*secrets.KNOWN, "XDG_CONFIG_HOME"):
            os.environ.pop(name, None)
        guard = patch.object(systemd, "run", side_effect=AssertionError("real systemctl call"))
        guard.start()
        self.addCleanup(guard.stop)
        self.logs = self.home / "logs"

    def target(self):
        return self.home / ".config" / "systemd" / "user" / UNIT

    def test_unit_text_is_a_restarting_user_service(self):
        text = systemd.unit_text(PROGRAM, log_dir=self.logs)
        self.assertIn("[Service]", text)
        self.assertIn("Type=simple", text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("UMask=0077", text)
        self.assertIn('ExecStart="/usr/bin/python3" "-m" "jevtrader" "daemon"', text)
        self.assertIn(f"StandardOutput=append:{self.logs / 'daemon.out.log'}", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertNotIn("Environment=", text)

    def test_unit_text_escapes_specifiers_and_quotes(self):
        text = systemd.unit_text(["/opt/p y/python", 'a"b\\c', "100%", "$HOME"])
        self.assertIn('ExecStart="/opt/p y/python" "a\\"b\\\\c" "100%%" "$$HOME"', text)

    def test_unit_text_rejects_bad_arguments_and_secrets(self):
        for bad in ([], ["python"], ["/bin/x", "a\nb"], ["/bin/x", ""], "/bin/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    systemd.unit_text(bad)
        os.environ["OPENAI_API_KEY"] = CANARY
        with self.assertRaises(ValueError) as caught:
            systemd.unit_text(["/bin/x", f"--key={CANARY}"])
        self.assertNotIn(CANARY, str(caught.exception))
        for name in ("OPENAI_API_KEY", "MY_TOKEN", "lower"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    systemd.unit_text(PROGRAM, environment={name: "x"})
        with self.assertRaises(ValueError) as caught:
            systemd.unit_text(PROGRAM, environment={"OTHER": CANARY})
        self.assertNotIn(CANARY, str(caught.exception))

    def test_environment_holds_only_plain_settings(self):
        text = systemd.unit_text(PROGRAM, environment={paths.HOME_ENV: "/srv/j app"})
        self.assertIn(f'Environment="{paths.HOME_ENV}=/srv/j app"', text)

    def test_install_writes_the_unit_and_enables_it(self):
        os.environ["TYPESAFE_API_KEY"] = CANARY
        runner = FakeSystemctl()
        result = systemd.install(PROGRAM, runner=runner, home=self.home, log_dir=self.logs)
        target = self.target()
        self.assertEqual(result["unit"], str(target))
        self.assertFalse(result["replaced"])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        text = target.read_text()
        self.assertNotIn(CANARY, text)
        self.assertIn(f'Environment="{paths.HOME_ENV}={self.home / "app"}"', text)
        self.assertEqual(
            runner.calls,
            [
                [systemd.SYSTEMCTL, "--user", "is-active", "--quiet", UNIT],
                [systemd.SYSTEMCTL, "--user", "daemon-reload"],
                [systemd.SYSTEMCTL, "--user", "enable", "--now", UNIT],
            ],
        )
        for argv in runner.calls:
            self.assertNotIn(CANARY, " ".join(argv))
        self.assertTrue(self.logs.is_dir())

    def test_install_restarts_a_running_service(self):
        runner = FakeSystemctl(active=True)
        result = systemd.install(PROGRAM, runner=runner, home=self.home, log_dir=self.logs)
        self.assertTrue(result["replaced"])
        self.assertEqual(runner.calls[-1], [systemd.SYSTEMCTL, "--user", "restart", UNIT])

    def test_failed_enable_removes_the_unit_and_reports_sanitized_detail(self):
        runner = FakeSystemctl(fail={"enable": 1})
        with self.assertRaisesRegex(RuntimeError, r"enable failed \(exit 1\): enable failed") as c:
            systemd.install(PROGRAM, runner=runner, home=self.home, log_dir=self.logs)
        self.assertNotIn("\x1b", str(c.exception))
        self.assertFalse(self.target().exists())

    def test_runner_errors_become_short_runtime_errors(self):
        for failure in (OSError("detail"), subprocess.TimeoutExpired("x", 30)):
            runner = FakeSystemctl(fail={"is-active": failure})
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaisesRegex(RuntimeError, "systemctl is-active could not run"):
                    systemd.install(PROGRAM, runner=runner, home=self.home, log_dir=self.logs)

    def test_uninstall_is_safe_to_repeat(self):
        runner = FakeSystemctl()
        systemd.install(PROGRAM, runner=runner, home=self.home, log_dir=self.logs)
        removed = systemd.uninstall(runner=runner, home=self.home)
        self.assertEqual(removed, {"unit": str(self.target()), "stopped": True, "removed": True})
        self.assertIn([systemd.SYSTEMCTL, "--user", "disable", "--now", UNIT], runner.calls)
        self.assertEqual(runner.calls[-1], [systemd.SYSTEMCTL, "--user", "daemon-reload"])
        again = systemd.uninstall(runner=runner, home=self.home)
        self.assertEqual(again["removed"], False)
        self.assertEqual(again["stopped"], False)

    def test_status_parses_show_output(self):
        show = "ActiveState=active\nSubState=running\nMainPID=4242\nNRestarts=2\nExecMainStatus=0\n"
        runner = FakeSystemctl(show=show)
        summary = systemd.status(runner=runner, home=self.home)
        self.assertEqual(
            summary,
            {
                "unit": UNIT,
                "installed": False,
                "active": True,
                "state": "running",
                "pid": 4242,
                "restarts": 2,
                "last_exit_code": 0,
            },
        )
        idle = systemd.status(runner=FakeSystemctl(show="ActiveState=inactive\nMainPID=0\n"))
        self.assertFalse(idle["active"])
        self.assertIsNone(idle["pid"])
        broken = systemd.status(runner=FakeSystemctl(fail={"show": 1}))
        self.assertFalse(broken["active"])

    def test_unit_path_follows_xdg_config_home(self):
        os.environ["XDG_CONFIG_HOME"] = str(self.home / "xdg")
        self.assertEqual(systemd.unit_path(), self.home / "xdg" / "systemd" / "user" / UNIT)
        os.environ["XDG_CONFIG_HOME"] = "relative"
        self.assertEqual(systemd.unit_path(home=self.home), self.target())

    def test_default_runner_needs_linux_and_systemctl(self):
        with patch.object(systemd.sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "systemd"):
                systemd.status()
        with (
            patch.object(systemd.sys, "platform", "linux"),
            patch.object(systemd.shutil, "which", return_value=None),
        ):
            with self.assertRaisesRegex(ValueError, "systemd"):
                systemd.status()

    def test_default_run_uses_a_timeout(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(systemd.subprocess, "run", return_value=completed) as run:
            self.assertIs(DEFAULT_RUN(["systemctl", "--user", "show"]), completed)
        run.assert_called_once_with(
            ["systemctl", "--user", "show"],
            capture_output=True,
            text=True,
            timeout=systemd.TIMEOUT_SECONDS,
            check=False,
            env=systemd.childenv.scrubbed(extra=systemd.CHILD_ENV_EXTRA),  # no keys (P0-42)
        )


DEFAULT_RUN = systemd.run

if __name__ == "__main__":
    unittest.main()
