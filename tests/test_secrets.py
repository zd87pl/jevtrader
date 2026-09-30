"""Keychain access through an injected runner; these tests never run /usr/bin/security."""

import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jevtrader import secrets

VALUE = "sk-test_Value.123+/="
CLEAN = {name: "" for name in secrets.KNOWN}
DEFAULT_RUN = secrets.run  # Captured before setUp replaces it with a guard.


class FakeKeychain:
    """Records every call; stores items only when `security -i` receives a valid add command."""

    def __init__(self, items=None, *, fail=None, ignore_adds=False):
        self.items = dict(items or {})
        self.calls = []
        self.fail = fail
        self.ignore_adds = ignore_adds

    def __call__(self, argv, input=None):
        self.calls.append((list(argv), input))
        if self.fail is not None:
            if isinstance(self.fail, BaseException):
                raise self.fail
            return SimpleNamespace(returncode=self.fail, stdout="", stderr="boom")
        if argv[1] == "-i":
            words = input.split()
            if not self.ignore_adds and words[0] == "add-generic-password":
                self.items[words[words.index("-a") + 1]] = words[words.index("-w") + 1]
            return SimpleNamespace(returncode=0, stdout="security> ", stderr="")
        name = argv[argv.index("-a") + 1]
        if name not in self.items:
            return SimpleNamespace(returncode=44, stdout="", stderr="not found")
        if argv[1] == "delete-generic-password":
            del self.items[name]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=self.items[name] + "\n", stderr="")


class SecretsTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, CLEAN)
        environment.start()
        self.addCleanup(environment.stop)
        for name in secrets.KNOWN:
            os.environ.pop(name, None)
        # A missing runner must never reach the real Keychain from a test.
        guard = patch.object(secrets, "run", side_effect=AssertionError("real security call"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_environment_takes_precedence(self):
        keychain = FakeKeychain({"OPENAI_API_KEY": "from-keychain"})
        os.environ["OPENAI_API_KEY"] = "from-env"
        self.assertEqual(secrets.get("OPENAI_API_KEY", runner=keychain), "from-env")
        self.assertEqual(keychain.calls, [])

    def test_get_reads_the_service_item_and_reports_missing(self):
        keychain = FakeKeychain({"TYPESAFE_API_KEY": VALUE})
        self.assertEqual(secrets.get("TYPESAFE_API_KEY", runner=keychain), VALUE)
        self.assertEqual(
            keychain.calls[0][0],
            [
                secrets.SECURITY,
                "find-generic-password",
                "-s",
                secrets.SERVICE,
                "-a",
                "TYPESAFE_API_KEY",
                "-w",
            ],
        )
        self.assertIsNone(secrets.get("OPENAI_API_KEY", runner=keychain))

    def test_set_sends_the_value_only_through_stdin_and_verifies(self):
        keychain = FakeKeychain()
        secrets.set("ALPACA_API_SECRET_KEY", VALUE, runner=keychain)
        self.assertEqual(keychain.items, {"ALPACA_API_SECRET_KEY": VALUE})
        (argv, stdin), (lookup, _) = keychain.calls
        self.assertEqual(argv, [secrets.SECURITY, "-i"])
        self.assertEqual(
            stdin,
            f"add-generic-password -U -s {secrets.SERVICE} -a ALPACA_API_SECRET_KEY -w {VALUE}\n",
        )
        self.assertEqual(lookup[1], "find-generic-password")
        for command, _ in keychain.calls:
            self.assertFalse(any(VALUE in word for word in command))

    def test_set_rejects_values_that_would_need_quoting_without_echoing_them(self):
        keychain = FakeKeychain()
        for value in ("", "has space", "quote'd", 'dq"x', "new\nline", "-leading", "é", "x" * 513):
            with self.subTest(value=value[:12]):
                with self.assertRaises(ValueError) as caught:
                    secrets.set("OPENAI_API_KEY", value, runner=keychain)
                if value:
                    self.assertNotIn(value, str(caught.exception))
        with self.assertRaises(ValueError):
            secrets.set("OPENAI_API_KEY", None, runner=keychain)
        self.assertEqual(keychain.calls, [])

    def test_unconfirmed_store_fails_without_the_value_in_the_error(self):
        for keychain in (FakeKeychain(ignore_adds=True), FakeKeychain(fail=1)):
            with self.subTest(keychain=keychain.ignore_adds):
                with self.assertRaisesRegex(RuntimeError, "did not store") as caught:
                    secrets.set("OPENAI_API_KEY", VALUE, runner=keychain)
                self.assertNotIn(VALUE, str(caught.exception))

    def test_unknown_names_are_refused_before_any_command(self):
        keychain = FakeKeychain()
        for call in (
            lambda: secrets.get("HOME", runner=keychain),
            lambda: secrets.set("PATH", VALUE, runner=keychain),
            lambda: secrets.delete("-a", runner=keychain),
            lambda: secrets.export_to_environ(["SHELL"], runner=keychain),
        ):
            with self.assertRaisesRegex(ValueError, "Unknown secret name"):
                call()
        self.assertEqual(keychain.calls, [])

    def test_delete_reports_whether_an_item_existed(self):
        keychain = FakeKeychain({"OPENAI_API_KEY": VALUE})
        self.assertTrue(secrets.delete("OPENAI_API_KEY", runner=keychain))
        self.assertEqual(
            keychain.calls[0][0],
            [
                secrets.SECURITY,
                "delete-generic-password",
                "-s",
                secrets.SERVICE,
                "-a",
                "OPENAI_API_KEY",
            ],
        )
        self.assertFalse(secrets.delete("OPENAI_API_KEY", runner=keychain))

    def test_keychain_errors_fail_closed_without_command_output(self):
        for failure in (36, OSError("secret-ish detail"), subprocess.TimeoutExpired("x", 30)):
            keychain = FakeKeychain(fail=failure)
            with self.subTest(failure=failure):
                with self.assertRaises(RuntimeError) as caught:
                    secrets.get("OPENAI_API_KEY", runner=keychain)
                self.assertNotIn("boom", str(caught.exception))
                self.assertNotIn("secret-ish", str(caught.exception))
                with self.assertRaises(RuntimeError):
                    secrets.delete("OPENAI_API_KEY", runner=keychain)

    def test_export_fills_only_unset_names_and_returns_names(self):
        keychain = FakeKeychain({"TYPESAFE_API_KEY": VALUE, "OPENAI_API_KEY": "from-keychain"})
        os.environ["OPENAI_API_KEY"] = "from-env"
        loaded = secrets.export_to_environ(runner=keychain)
        self.assertEqual(loaded, ["TYPESAFE_API_KEY"])
        self.assertEqual(os.environ["TYPESAFE_API_KEY"], VALUE)
        self.assertEqual(os.environ["OPENAI_API_KEY"], "from-env")
        self.assertNotIn("ALPACA_API_KEY_ID", os.environ)
        looked_up = [argv[argv.index("-a") + 1] for argv, _ in keychain.calls]
        self.assertEqual(
            looked_up, ["TYPESAFE_API_KEY", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"]
        )

    def test_without_a_keychain_reads_use_only_the_environment(self):
        with patch.object(secrets, "keychain_available", return_value=False):
            self.assertIsNone(secrets.get("OPENAI_API_KEY"))
            self.assertEqual(secrets.export_to_environ(), [])
            with self.assertRaisesRegex(ValueError, "export OPENAI_API_KEY"):
                secrets.set("OPENAI_API_KEY", VALUE)
            with self.assertRaisesRegex(ValueError, "Keychain is unavailable"):
                secrets.delete("OPENAI_API_KEY")
            os.environ["OPENAI_API_KEY"] = "from-env"
            self.assertEqual(secrets.get("OPENAI_API_KEY"), "from-env")

    def test_default_runner_passes_stdin_and_a_timeout(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(secrets.subprocess, "run", return_value=completed) as run:
            result = DEFAULT_RUN([secrets.SECURITY, "-i"], "command\n")
        self.assertIs(result, completed)
        run.assert_called_once_with(
            [secrets.SECURITY, "-i"],
            input="command\n",
            capture_output=True,
            text=True,
            timeout=secrets.TIMEOUT_SECONDS,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
