"""Least-privilege keys and scrubbed child environments (P0-42, issue #46, ADR-0008)."""

import os
import unittest
from unittest.mock import patch

from jevtrader import cli, launchd, notify, secrets, systemd
from jevtrader.security import childenv

ALPACA = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
FAKE = {name: "fake-value-not-a-key" for name in secrets.KNOWN}


class ScrubbedEnvironmentTests(unittest.TestCase):
    def test_keeps_only_the_allowlist(self):
        source = {
            "PATH": "/usr/bin",
            "HOME": "/h",
            "LANG": "C",
            "LC_ALL": "C",
            "LC_CTYPE": "UTF-8",
            "TMPDIR": "/t",
            "USER": "u",
            "LOGNAME": "u",
            "SHELL": "/bin/sh",
            "PYTHONPATH": "/x",
            "SEC_USER_AGENT": "contact",
            "GITHUB_TOKEN": "t",
            **FAKE,
        }
        result = childenv.scrubbed(source)
        self.assertEqual(
            set(result),
            {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME", "SHELL"},
        )
        self.assertEqual(result["PATH"], "/usr/bin")

    def test_extra_names_never_admit_key_like_names(self):
        source = {"DBUS_SESSION_BUS_ADDRESS": "unix:x", "OPENAI_API_KEY": "v", "LC_SECRET": "v"}
        result = childenv.scrubbed(source, extra=("DBUS_SESSION_BUS_ADDRESS", "OPENAI_API_KEY"))
        self.assertEqual(result, {"DBUS_SESSION_BUS_ADDRESS": "unix:x"})

    def test_defaults_to_the_process_environment(self):
        with patch.dict(os.environ, {"PATH": "/p", **FAKE}):
            result = childenv.scrubbed()
        self.assertEqual(result["PATH"], "/p")
        self.assertFalse(set(secrets.KNOWN) & set(result))


class RunnerEnvironmentTests(unittest.TestCase):
    def assert_scrubbed(self, target: str, call) -> dict:
        ok = type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch.dict(os.environ, {"PATH": "/p", **FAKE}), patch(target, return_value=ok) as run:
            call()
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PATH"], "/p")
        self.assertFalse(set(secrets.KNOWN) & set(env))
        return env

    # conftest replaces secrets.run and launchd.run; their unpatched originals stay as aliases.
    def test_secret_store_runner(self):
        env = self.assert_scrubbed("jevtrader.secrets.subprocess.run", lambda: secrets._run(["x"]))
        self.assertNotIn("SEC_USER_AGENT", env)

    def test_launchd_runner(self):
        self.assert_scrubbed("jevtrader.launchd.subprocess.run", lambda: launchd._run(["x"]))

    def test_systemd_runner(self):
        self.assert_scrubbed("jevtrader.systemd.subprocess.run", lambda: systemd.run(["x"]))

    def test_notifier_runner(self):
        with patch.object(notify.sys, "platform", "darwin"):
            self.assert_scrubbed(
                "jevtrader.notify.subprocess.run", lambda: notify.macos("title", "body")
            )


class KeysForCommandTests(unittest.TestCase):
    def keys(self, *argv: str, provider: str = "rules") -> tuple[str, ...]:
        args = cli.parser().parse_args(list(argv))
        return cli.keys_for(args, {"provider": provider})

    def test_commands_without_keys(self):
        for argv in (["status"], ["verify"], ["poll"], ["brief"], ["mcp"], ["init"]):
            self.assertEqual(self.keys(*argv, provider="openai"), (), argv)

    def test_observe_and_experiment_get_only_the_provider_in_use(self):
        self.assertEqual(self.keys("observe"), ())
        self.assertEqual(self.keys("observe", "--provider", "local"), ())
        self.assertEqual(self.keys("observe", "--provider", "openai"), ("OPENAI_API_KEY",))
        self.assertEqual(
            self.keys("experiment", "--candidate", "c", "--development-until", "2024-01-01"),
            ("TYPESAFE_API_KEY",),
        )

    def test_autoresearch_gets_the_proposal_and_evaluation_keys(self):
        keys = self.keys("autoresearch", "--development-until", "2024-01-01")
        self.assertEqual(set(keys), {"OPENAI_API_KEY", "TYPESAFE_API_KEY"})

    def test_propose_gets_openai_only(self):
        keys = self.keys("propose", "--trial", "t", "--output", "o", provider="jev")
        self.assertEqual(keys, ("OPENAI_API_KEY",))

    def test_bars_daemon_and_backfill(self):
        self.assertEqual(self.keys("bars", provider="jev"), ALPACA)
        self.assertEqual(self.keys("daemon", provider="jev"), (*ALPACA, "TYPESAFE_API_KEY"))
        self.assertEqual(self.keys("daemon", provider="local"), ALPACA)
        backfill = ("backfill", "--start", "2024-01-01", "--end", "2024-02-01")
        self.assertEqual(self.keys(*backfill), ())
        self.assertEqual(self.keys(*backfill, "--bars"), ALPACA)

    def test_main_exports_only_the_needed_keys(self):
        with patch("jevtrader.secrets.export_to_environ", side_effect=RuntimeError("stop")) as ex:
            code = cli.main(["observe", "--provider", "openai"])
        self.assertEqual(code, 2)
        self.assertEqual(list(ex.call_args.args[0]), ["OPENAI_API_KEY"])

    def test_main_skips_the_store_when_no_key_is_needed(self):
        with patch("jevtrader.secrets.export_to_environ") as ex:
            cli.main(["--db", os.devnull + "-missing", "status"])
        ex.assert_not_called()


class DoctorKeyTests(unittest.TestCase):
    def test_doctor_loads_only_the_configured_keys(self):
        from jevtrader import app

        config = {"provider": "openai", "bars_source": "none"}
        with patch("jevtrader.secrets.export_to_environ", return_value=[]) as ex:
            app._key_check(config, [], None)
        self.assertEqual(list(ex.call_args.args[0]), ["OPENAI_API_KEY"])

    def test_export_with_no_names_never_opens_a_store(self):
        with patch("jevtrader.secrets.choose_store") as choose:
            self.assertEqual(secrets.export_to_environ([]), [])
        choose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
