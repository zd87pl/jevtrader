"""Cross-platform secret stores through injected runners; no real Keychain, libsecret or Windows."""

import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jevtrader import secrets

CANARY = "canary-Zq7.Secret+/="
CLEAN = {name: "" for name in secrets.KNOWN}


class FakeSecretTool:
    """A secret-tool stand-in keyed by the account attribute; records argv and stdin."""

    def __init__(self, items=None, *, fail=None):
        self.items = dict(items or {})
        self.calls = []
        self.fail = fail

    def __call__(self, argv, input=None):
        self.calls.append((list(argv), input))
        if self.fail is not None:
            if isinstance(self.fail, BaseException):
                raise self.fail
            return SimpleNamespace(returncode=self.fail, stdout="", stderr="boom " + CANARY)
        name = argv[argv.index("account") + 1]
        if argv[1] == "store":
            self.items[name] = input
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1] == "clear":
            self.items.pop(name, None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if name not in self.items:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=self.items[name], stderr="")


class FakePowerShell:
    """Decodes the fixed script, then acts on the operation and name read from stdin."""

    def __init__(self, items=None):
        self.items = dict(items or {})
        self.calls = []

    def __call__(self, argv, input=None):
        self.calls.append((list(argv), input))
        script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
        assert "CredRead" in script and "CredWrite" in script and "CredDelete" in script
        lines = (input or "").split("\n")
        op, name = lines[0], lines[1]
        if op == "set":
            self.items[name] = lines[2]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if name not in self.items:
            return SimpleNamespace(returncode=secrets.NOT_FOUND, stdout="", stderr="")
        if op == "delete":
            del self.items[name]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=self.items[name] + "\r\n", stderr="")


class StoreTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, CLEAN)
        environment.start()
        self.addCleanup(environment.stop)
        for name in (*secrets.KNOWN, secrets.SECRETS_DIR_ENV):
            os.environ.pop(name, None)
        guard = patch.object(secrets, "run", side_effect=AssertionError("real secret-store call"))
        guard.start()
        self.addCleanup(guard.stop)

    def assert_value_only_on_stdin(self, calls):
        for argv, _ in calls:
            self.assertFalse(any(CANARY in word for word in argv), argv)

    def test_libsecret_round_trip_keeps_the_value_on_stdin(self):
        tool = FakeSecretTool()
        store = secrets.LibsecretStore(runner=tool)
        secrets.set("OPENAI_API_KEY", CANARY, store=store)
        self.assertEqual(tool.items, {"OPENAI_API_KEY": CANARY})
        argv, stdin = tool.calls[0]
        self.assertEqual(
            argv,
            [
                secrets.SECRET_TOOL,
                "store",
                "--label",
                "jevtrader OPENAI_API_KEY",
                "service",
                secrets.SERVICE,
                "account",
                "OPENAI_API_KEY",
            ],
        )
        self.assertEqual(stdin, CANARY)
        self.assertEqual(secrets.get("OPENAI_API_KEY", store=store), CANARY)
        self.assertEqual(
            tool.calls[-1][0],
            [
                secrets.SECRET_TOOL,
                "lookup",
                "service",
                secrets.SERVICE,
                "account",
                "OPENAI_API_KEY",
            ],
        )
        self.assertIsNone(secrets.get("TYPESAFE_API_KEY", store=store))
        self.assertTrue(secrets.delete("OPENAI_API_KEY", store=store))
        self.assertIn(
            [secrets.SECRET_TOOL, "clear", "service", secrets.SERVICE, "account", "OPENAI_API_KEY"],
            [argv for argv, _ in tool.calls],
        )
        self.assertFalse(secrets.delete("OPENAI_API_KEY", store=store))
        self.assert_value_only_on_stdin(tool.calls)

    def test_libsecret_failures_never_echo_the_value(self):
        for failure in (5, OSError(CANARY), subprocess.TimeoutExpired("x", 30)):
            store = secrets.LibsecretStore(runner=FakeSecretTool(fail=failure))
            with self.subTest(failure=type(failure).__name__):
                for call in (
                    lambda: store.get("OPENAI_API_KEY"),
                    lambda: store.set("OPENAI_API_KEY", CANARY),
                    lambda: store.delete("OPENAI_API_KEY"),
                ):
                    with self.assertRaises(RuntimeError) as caught:
                        call()
                    self.assertNotIn(CANARY, str(caught.exception))
                    self.assertNotIn("boom", str(caught.exception))

    def test_windows_store_uses_a_fixed_encoded_script_and_stdin(self):
        shell = FakePowerShell()
        store = secrets.WindowsStore(runner=shell)
        secrets.set("ALPACA_API_SECRET_KEY", CANARY, store=store)
        self.assertEqual(shell.items, {"ALPACA_API_SECRET_KEY": CANARY})
        self.assertEqual(secrets.get("ALPACA_API_SECRET_KEY", store=store), CANARY)
        self.assertIsNone(secrets.get("OPENAI_API_KEY", store=store))
        self.assertTrue(secrets.delete("ALPACA_API_SECRET_KEY", store=store))
        self.assertFalse(secrets.delete("ALPACA_API_SECRET_KEY", store=store))
        scripts = {tuple(argv) for argv, _ in shell.calls}
        self.assertEqual(len(scripts), 1)  # one fixed command line for every operation
        (argv,) = scripts
        self.assertEqual(argv[:3], (secrets.POWERSHELL, "-NoProfile", "-NonInteractive"))
        self.assertEqual(shell.calls[0][1], f"set\nALPACA_API_SECRET_KEY\n{CANARY}\n")
        self.assert_value_only_on_stdin(shell.calls)

    def test_windows_store_fails_closed(self):
        def broken(argv, input=None):
            return SimpleNamespace(returncode=7, stdout="", stderr=CANARY)

        store = secrets.WindowsStore(runner=broken)
        with self.assertRaises(RuntimeError) as caught:
            store.get("OPENAI_API_KEY")
        self.assertNotIn(CANARY, str(caught.exception))
        with self.assertRaisesRegex(RuntimeError, "did not store"):
            store.set("OPENAI_API_KEY", CANARY)

    def test_docker_store_reads_lowercased_files_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "openai_api_key").write_text(CANARY + "\n")
            store = secrets.DockerSecretsStore(directory)
            self.assertEqual(secrets.get("OPENAI_API_KEY", store=store), CANARY)
            self.assertIsNone(secrets.get("TYPESAFE_API_KEY", store=store))
            with self.assertRaisesRegex(ValueError, "read-only"):
                secrets.set("OPENAI_API_KEY", "other", store=store)
            with self.assertRaisesRegex(ValueError, "read-only"):
                secrets.delete("OPENAI_API_KEY", store=store)
            self.assertEqual((directory / "openai_api_key").read_text(), CANARY + "\n")
            os.environ["OPENAI_API_KEY"] = "from-env"
            self.assertEqual(secrets.get("OPENAI_API_KEY", store=store), "from-env")

    def test_export_uses_the_given_store_and_returns_names_only(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "typesafe_api_key").write_text(CANARY)
            loaded = secrets.export_to_environ(store=secrets.DockerSecretsStore(Path(temp)))
        self.assertEqual(loaded, ["TYPESAFE_API_KEY"])
        self.assertEqual(os.environ["TYPESAFE_API_KEY"], CANARY)

    def test_choose_store_prefers_docker_then_the_platform_store(self):
        with tempfile.TemporaryDirectory() as temp:
            os.environ[secrets.SECRETS_DIR_ENV] = temp
            store = secrets.choose_store()
            self.assertIsInstance(store, secrets.DockerSecretsStore)
            self.assertEqual(store.directory, Path(temp))
            del os.environ[secrets.SECRETS_DIR_ENV]
        missing = Path(tempfile.gettempdir()) / "jevtrader-no-such-secrets-dir"
        with patch.object(secrets, "DOCKER_SECRETS_DIR", missing):
            with patch.object(secrets, "keychain_available", return_value=True):
                with patch.object(secrets.sys, "platform", "darwin"):
                    self.assertIsInstance(secrets.choose_store(), secrets.KeychainStore)
            with (
                patch.object(secrets.sys, "platform", "linux"),
                patch.object(secrets.shutil, "which", return_value="/usr/bin/secret-tool"),
                patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}),
            ):
                self.assertIsInstance(secrets.choose_store(), secrets.LibsecretStore)
            with (
                patch.object(secrets.sys, "platform", "linux"),
                patch.object(secrets.shutil, "which", return_value=None),
            ):
                self.assertIsNone(secrets.choose_store())
            with (
                patch.object(secrets.sys, "platform", "win32"),
                patch.object(secrets.shutil, "which", return_value="C:/ps/powershell.exe"),
            ):
                self.assertIsInstance(secrets.choose_store(), secrets.WindowsStore)
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(secrets, "DOCKER_SECRETS_DIR", Path(temp)):
                self.assertIsInstance(secrets.choose_store(), secrets.DockerSecretsStore)

    def test_without_any_store_writes_are_refused(self):
        with patch.object(secrets, "choose_store", return_value=None):
            self.assertIsNone(secrets.get("OPENAI_API_KEY"))
            with self.assertRaisesRegex(ValueError, "export OPENAI_API_KEY"):
                secrets.set("OPENAI_API_KEY", CANARY)
            self.assertNotIn(CANARY, str(os.environ))


if __name__ == "__main__":
    unittest.main()
