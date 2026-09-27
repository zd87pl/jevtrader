"""LaunchAgent plists and exact launchctl argv; these tests never run launchctl or touch ~/Library."""

import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jevtrader import launchd, paths, secrets

PROGRAM = ["/usr/bin/python3", "-m", "jevtrader", "daemon"]
LABEL = paths.LAUNCHD_LABEL
UID = os.getuid()
SERVICE = f"gui/{UID}/{LABEL}"
DEFAULT_RUN = launchd._run  # Captured before setUp replaces it with a guard.

PRINT_OUTPUT = f"""{SERVICE} = {{
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/{LABEL}.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /usr/bin/python3
\targuments = {{
\t\t/usr/bin/python3
\t}}

\truns = 3
\tpid = 4242
\tlast exit code = 78: EX_CONFIG
\tendpoints = {{
\t\tstate = active
\t}}
}}
"""


class FakeLaunchctl:
    """Records argv; `loaded` controls what `print` reports, `fail` maps verb -> exit code."""

    def __init__(self, *, loaded=False, fail=None, output=PRINT_OUTPUT):
        self.calls = []
        self.loaded = loaded
        self.fail = dict(fail or {})
        self.output = output

    def __call__(self, argv):
        self.calls.append(list(argv))
        verb = argv[1]
        if verb in self.fail:
            code = self.fail[verb]
            if isinstance(code, BaseException):
                raise code
            return SimpleNamespace(returncode=code, stdout="", stderr=f"{verb} failed\x1b[0m")
        if verb == "print":
            if not self.loaded:
                return SimpleNamespace(returncode=113, stdout="", stderr="Could not find service")
            return SimpleNamespace(returncode=0, stdout=self.output, stderr="")
        if verb == "bootout":
            code = 0 if self.loaded else 3
            self.loaded = False
            return SimpleNamespace(returncode=code, stdout="", stderr="")
        if verb == "bootstrap":
            self.loaded = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        environment = patch.dict(os.environ, {paths.HOME_ENV: str(self.home / "app")}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in secrets.KNOWN:
            os.environ.pop(name, None)
        # A missing runner must never reach the real launchctl from a test.
        guard = patch.object(launchd, "_run", side_effect=AssertionError("real launchctl call"))
        guard.start()
        self.addCleanup(guard.stop)
        self.logs = self.home / "logs"

    def target(self):
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def test_plist_contents(self):
        data = launchd.plist(PROGRAM, log_dir=self.logs)
        self.assertTrue(data.startswith(b"<?xml"))
        self.assertEqual(
            plistlib.loads(data),
            {
                "Label": LABEL,
                "ProgramArguments": PROGRAM,
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ProcessType": "Background",
                "ThrottleInterval": launchd.THROTTLE_SECONDS,
                "Umask": 0o077,
                "StandardOutPath": str(self.logs / "daemon.out.log"),
                "StandardErrorPath": str(self.logs / "daemon.err.log"),
            },
        )

    def test_plist_log_dir_defaults_to_app_logs_at_call_time(self):
        job = plistlib.loads(launchd.plist(PROGRAM))
        self.assertEqual(
            job["StandardErrorPath"], str(self.home / "app" / "logs" / "daemon.err.log")
        )
        with patch.dict(os.environ, {paths.HOME_ENV: str(self.home / "other")}):
            job = plistlib.loads(launchd.plist(PROGRAM))
        self.assertEqual(
            job["StandardOutPath"], str(self.home / "other" / "logs" / "daemon.out.log")
        )

    def test_plist_rejects_bad_program_arguments(self):
        for bad in (
            [],
            "python -m jevtrader",
            ["python3", "-m", "jevtrader"],
            ["/bin/x", ""],
            ["/bin/x", "a\nb"],
            ["/bin/x", "a\0b"],
            ["/bin/x", 3],
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                launchd.plist(bad, log_dir=self.logs)
        with self.assertRaises(ValueError):
            launchd.plist(PROGRAM, log_dir=Path("relative/logs"))

    def test_plist_rejects_unsafe_labels(self):
        for label in ("", "../evil", "a/b", "io..x", ".hidden", "x" * 200, "a b", "a\nb"):
            with self.subTest(label=label), self.assertRaises(ValueError):
                launchd.plist(PROGRAM, label=label, log_dir=self.logs)

    def test_plist_never_carries_secrets(self):
        for name in ("OPENAI_API_KEY", "ALPACA_API_SECRET_KEY", "GITHUB_TOKEN", "DB_PASSWORD"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Keychain"):
                launchd.plist(PROGRAM, log_dir=self.logs, environment={name: "value"})
        with self.assertRaises(ValueError):
            launchd.plist(PROGRAM, log_dir=self.logs, environment={"lower": "x"})
        with self.assertRaises(ValueError):
            launchd.plist(PROGRAM, log_dir=self.logs, environment={"PYTHONPATH": "a\nb"})
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-live-value-123"}):
            with self.assertRaisesRegex(ValueError, "API key"):
                launchd.plist([*PROGRAM, "--key", "sk-live-value-123"], log_dir=self.logs)
            with self.assertRaisesRegex(ValueError, "API key"):
                launchd.plist(
                    PROGRAM, log_dir=self.logs, environment={"EXTRA": "sk-live-value-123"}
                )
        job = plistlib.loads(
            launchd.plist(PROGRAM, log_dir=self.logs, environment={"PYTHONPATH": "/src"})
        )
        self.assertEqual(job["EnvironmentVariables"], {"PYTHONPATH": "/src"})

    def test_install_writes_plist_and_bootstraps(self):
        runner = FakeLaunchctl()
        result = launchd.install(PROGRAM, runner=runner, home=self.home, log_dir=self.logs)
        target = self.target()
        self.assertEqual(
            runner.calls,
            [
                [launchd.LAUNCHCTL, "print", SERVICE],
                [launchd.LAUNCHCTL, "enable", SERVICE],
                [launchd.LAUNCHCTL, "bootstrap", f"gui/{UID}", str(target)],
            ],
        )
        self.assertEqual(
            result,
            {
                "label": LABEL,
                "plist": str(target),
                "domain": f"gui/{UID}",
                "replaced": False,
                "log_dir": str(self.logs),
            },
        )
        job = plistlib.loads(target.read_bytes())
        self.assertEqual(job["ProgramArguments"], PROGRAM)
        # The installing shell's app directory follows the service.
        self.assertEqual(job["EnvironmentVariables"], {paths.HOME_ENV: str(self.home / "app")})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertTrue(self.logs.is_dir())
        self.assertEqual(stat.S_IMODE(self.logs.stat().st_mode), 0o700)
        self.assertEqual(list(target.parent.glob(".jevtrader-*")), [])

    def test_install_without_home_override_sets_no_environment(self):
        os.environ.pop(paths.HOME_ENV)
        launchd.install(PROGRAM, runner=FakeLaunchctl(), home=self.home, log_dir=self.logs)
        self.assertNotIn("EnvironmentVariables", plistlib.loads(self.target().read_bytes()))

    def test_install_replaces_a_loaded_service(self):
        runner = FakeLaunchctl(loaded=True)
        result = launchd.install(PROGRAM, runner=runner, home=self.home, log_dir=self.logs)
        self.assertTrue(result["replaced"])
        self.assertEqual(
            [call[1] for call in runner.calls], ["print", "bootout", "enable", "bootstrap"]
        )
        self.assertEqual(runner.calls[1], [launchd.LAUNCHCTL, "bootout", SERVICE])

    def test_install_failure_removes_plist_and_reports_briefly(self):
        runner = FakeLaunchctl(fail={"bootstrap": 5})
        with self.assertRaisesRegex(RuntimeError, r"bootstrap failed \(exit 5\): bootstrap failed"):
            launchd.install(PROGRAM, runner=runner, home=self.home, log_dir=self.logs)
        self.assertFalse(self.target().exists())

    def test_invalid_program_fails_before_any_launchctl_call(self):
        runner = FakeLaunchctl()
        with self.assertRaises(ValueError):
            launchd.install(["python3"], runner=runner, home=self.home, log_dir=self.logs)
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.target().exists())

    def test_runner_os_errors_become_short_runtime_errors(self):
        runner = FakeLaunchctl(fail={"print": OSError("secret detail")})
        with self.assertRaisesRegex(RuntimeError, "launchctl print could not run") as raised:
            launchd.status(runner=runner, home=self.home)
        self.assertNotIn("secret detail", str(raised.exception))
        runner = FakeLaunchctl(fail={"print": subprocess.TimeoutExpired("x", 30)})
        with self.assertRaises(RuntimeError):
            launchd.status(runner=runner, home=self.home)

    def test_uninstall_boots_out_and_removes(self):
        runner = FakeLaunchctl()
        launchd.install(PROGRAM, runner=runner, home=self.home, log_dir=self.logs)
        runner.calls.clear()
        result = launchd.uninstall(runner=runner, home=self.home)
        self.assertEqual(runner.calls, [[launchd.LAUNCHCTL, "bootout", SERVICE]])
        self.assertEqual(
            result,
            {"label": LABEL, "plist": str(self.target()), "unloaded": True, "removed": True},
        )
        self.assertFalse(self.target().exists())
        again = launchd.uninstall(runner=runner, home=self.home)
        self.assertEqual((again["unloaded"], again["removed"]), (False, False))

    def test_status_summarizes_top_level_fields(self):
        runner = FakeLaunchctl(loaded=True)
        self.assertEqual(
            launchd.status(runner=runner, home=self.home),
            {
                "label": LABEL,
                "installed": False,
                "loaded": True,
                "state": "running",
                "pid": 4242,
                "runs": 3,
                "last_exit_code": 78,
            },
        )
        self.assertEqual(runner.calls, [[launchd.LAUNCHCTL, "print", SERVICE]])

    def test_status_of_a_stopped_or_missing_service(self):
        never = (
            PRINT_OUTPUT.replace("\tpid = 4242\n", "")
            .replace("78: EX_CONFIG", "(never exited)")
            .replace("state = running", "state = not running")
        )
        summary = launchd.status(runner=FakeLaunchctl(loaded=True, output=never), home=self.home)
        self.assertEqual(
            (summary["state"], summary["pid"], summary["last_exit_code"]),
            ("not running", None, None),
        )
        missing = launchd.status(runner=FakeLaunchctl(), home=self.home)
        self.assertEqual(
            missing,
            {
                "label": LABEL,
                "installed": False,
                "loaded": False,
                "state": None,
                "pid": None,
                "runs": None,
                "last_exit_code": None,
            },
        )

    def test_default_runner_is_macos_only(self):
        with patch.object(launchd.sys, "platform", "linux"):
            for call in (
                lambda: launchd.install(PROGRAM, home=self.home, log_dir=self.logs),
                lambda: launchd.uninstall(home=self.home),
                lambda: launchd.status(home=self.home),
            ):
                with self.assertRaisesRegex(ValueError, "macOS"):
                    call()
        self.assertFalse(self.target().exists())

    def test_default_runner_uses_absolute_launchctl_without_a_shell(self):
        with patch.object(launchd.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            DEFAULT_RUN([launchd.LAUNCHCTL, "print", SERVICE])
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/bin/launchctl", "print", SERVICE])
        self.assertEqual(kwargs["timeout"], launchd.TIMEOUT_SECONDS)
        self.assertNotIn("shell", kwargs)


if __name__ == "__main__":
    unittest.main()
