"""macOS notifications: fixed script, text only as argv, never the real osascript in tests."""

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jevtrader import notify

SCRIPT_ARGV = [
    "/usr/bin/osascript",
    "-e",
    "on run argv",
    "-e",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "-e",
    "end run",
    "--",
]


class FakeRunner:
    def __init__(self, returncode=0, error=None):
        self.calls, self.returncode, self.error = [], returncode, error

    def __call__(self, argv, input=None):
        self.calls.append((list(argv), input))
        if self.error:
            raise self.error
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="")


class NotifyTests(unittest.TestCase):
    def setUp(self):
        # Any path that reaches the real subprocess fails loudly instead of notifying.
        guard = patch.object(notify.subprocess, "run", side_effect=AssertionError("real osascript"))
        guard.start()
        self.addCleanup(guard.stop)
        platform = patch.object(notify.sys, "platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)

    def test_passes_text_as_arguments_after_end_of_options(self):
        runner = FakeRunner()
        self.assertTrue(notify.macos("Brief ready", "3 new filings.", runner=runner))
        self.assertEqual(runner.calls, [(SCRIPT_ARGV + ["Brief ready", "3 new filings."], None)])

    def test_option_or_applescript_lookalikes_stay_data(self):
        runner = FakeRunner()
        title = "-e"
        body = 'x" & (do shell script "touch /tmp/pwned") & "'
        self.assertTrue(notify.macos(title, body, runner=runner))
        argv = runner.calls[0][0]
        self.assertEqual(argv[: len(SCRIPT_ARGV)], SCRIPT_ARGV)
        self.assertEqual(argv[len(SCRIPT_ARGV) :], [title, body])
        self.assertTrue(all(body not in part for part in argv[: len(SCRIPT_ARGV)]))

    def test_control_and_bidi_characters_are_flattened_and_lengths_capped(self):
        runner = FakeRunner()
        notify.macos("Line one\nline\ttwo‮", "b" * 500, runner=runner)
        title, body = runner.calls[0][0][-2:]
        self.assertEqual(title, "Line one line two")
        self.assertEqual(len(body), notify.MAX_BODY_CHARS)
        self.assertTrue(body.endswith("…"))
        self.assertEqual(len(notify.argv("t" * 200, "b")[-2]), notify.MAX_TITLE_CHARS)

    def test_rejects_non_text_or_empty_values(self):
        for title, body in ((None, "b"), ("t", 5), ("   ", "b"), ("t", "\n\x00‏")):
            with self.subTest(title=title, body=body), self.assertRaises(ValueError):
                notify.macos(title, body, runner=FakeRunner())

    def test_failures_return_false(self):
        self.assertFalse(notify.macos("t", "b", runner=FakeRunner(returncode=1)))
        self.assertFalse(notify.macos("t", "b", runner=FakeRunner(error=FileNotFoundError())))
        self.assertFalse(
            notify.macos("t", "b", runner=FakeRunner(error=subprocess.TimeoutExpired("x", 10)))
        )
        self.assertFalse(notify.macos("t", "b", runner=lambda argv, input: object()))

    def test_is_a_no_op_off_macos(self):
        runner = FakeRunner()
        with patch.object(notify.sys, "platform", "linux"):
            self.assertFalse(notify.macos("t", "b", runner=runner))
            self.assertFalse(notify.macos("t", "b"))
        self.assertEqual(runner.calls, [])

    def test_default_runner_uses_subprocess_without_a_shell(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with patch.object(notify.subprocess, "run", return_value=completed) as run:
            self.assertTrue(notify.macos("Title", "Body"))
        argv = run.call_args.args[0]
        self.assertEqual(argv, SCRIPT_ARGV + ["Title", "Body"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["timeout"], notify.TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
