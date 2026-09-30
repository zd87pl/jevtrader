"""Secret scanning (P0-22, #26): the scanner, its canaries and the CI wiring.

Canary secrets are assembled at run time so that no secret-shaped literal is
ever committed, which is also what lets the scanner pass on this file.
"""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNER = ROOT / "tools" / "secret_scan.py"
WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("secret_scan", SCANNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("secret_scan", module)
    spec.loader.exec_module(sys.modules["secret_scan"])
    return sys.modules["secret_scan"]


def _canaries() -> dict[str, str]:
    """One fake secret per rule, split so the source holds no whole secret."""
    return {
        "aws-access-key-id": "AKIA" + "Q" * 4 + "7" * 12,
        "github-token": "gh" + "p_" + "a1B2" * 9,
        "openai-key": "s" + "k-" + "proj-" + "Z9y8" * 8,
        "slack-token": "xo" + "xb-" + "1234567890-" + "abcdefghij",
        "private-key": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
        "alpaca-secret": "APCA_API_" + 'SECRET_KEY="' + "Ab1Cd2" * 7 + '"',
        "typesafe-key": "TYPESAFE_" + 'API_KEY="' + "Tz9Qw8" * 5 + '"',
    }


class RuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scan = _load()

    def test_every_canary_is_caught_by_its_rule(self) -> None:
        for rule, secret in _canaries().items():
            with self.subTest(rule=rule):
                hits = self.scan.scan_text(f"token = {secret}\n")
                self.assertIn(rule, [hit.rule for hit in hits])

    def test_findings_never_carry_the_secret(self) -> None:
        for rule, secret in _canaries().items():
            with self.subTest(rule=rule):
                for hit in self.scan.scan_text(secret):
                    self.assertNotIn(secret, str(hit))
                    self.assertNotIn(secret[8:], str(hit))

    def test_prose_and_placeholders_are_clean(self) -> None:
        clean = (
            '<a href="#ask-your-agent-read-only-mcp">MCP</a>\n'
            "APCA_API_SECRET_KEY=\n"
            'KEY_VALUE = "PKTESTKEYID0000000001"\n'
            "export OPENAI_API_KEY=your-key-here\n"
        )
        self.assertEqual(self.scan.scan_text(clean), [])

    def test_the_apps_own_alpaca_name_is_caught(self) -> None:
        # The app reads ALPACA_API_SECRET_KEY (.env.example, bars.py), not only APCA_*.
        for name in ("ALPACA_API_" + "SECRET_KEY", "APCA_API_" + "SECRET_KEY"):
            with self.subTest(name=name):
                hits = self.scan.scan_text(f"{name}={'Ab1Cd2' * 7}\n")
                self.assertEqual([hit.rule for hit in hits], ["alpaca-secret"])

    def test_empty_env_example_placeholders_stay_clean(self) -> None:
        self.assertEqual(self.scan.scan_text((ROOT / ".env.example").read_text()), [])

    def test_allow_marker_silences_one_line_only(self) -> None:
        secret = _canaries()["aws-access-key-id"]
        text = f"{secret}  # secret-scan: allow\n{secret}\n"
        self.assertEqual([hit.line for hit in self.scan.scan_text(text)], [2])


class CommandTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCANNER), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_canary_file_fails_the_scan_without_echoing_it(self) -> None:
        secret = _canaries()["github-token"]
        with tempfile.TemporaryDirectory() as tmp:
            canary = Path(tmp) / "canary.env"
            canary.write_text(f"GITHUB_TOKEN={secret}\n", encoding="utf-8")
            result = self._run(str(canary))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("github-token", result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_tracked_files_are_clean(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class WorkflowTests(unittest.TestCase):
    def test_ci_runs_the_canary_then_the_repository_scan(self) -> None:
        canary = WORKFLOW.find("secret_scan.py --canary")
        repo = WORKFLOW.find("secret_scan.py\n")
        self.assertGreater(canary, -1, "CI must prove the scanner still fires")
        self.assertGreater(repo, canary, "CI must then scan the tracked files")

    def test_canary_mode_passes_only_when_every_rule_fires(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCANNER), "--canary"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for rule in _canaries():
            with self.subTest(rule=rule):
                self.assertIn(rule, result.stdout)


if __name__ == "__main__":
    unittest.main()
