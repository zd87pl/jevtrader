"""The threat model and secrets policy cite code that exists, at lines that still say it."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THREAT_MODEL = ROOT / "docs" / "security" / "threat-model.md"
SECRETS_POLICY = ROOT / "docs" / "security" / "secrets-policy.md"
DOCS = (THREAT_MODEL, SECRETS_POLICY)

CITATION = re.compile(
    r"`((?:jevtrader|tools|tests|docs)/[\w/.-]+\.(?:py|md|json|toml|yml))(?::(\d+)(?:-(\d+))?)?`"
)

# (document, citation, text the cited range must contain): pins the load-bearing claims.
ANCHORS = [
    (THREAT_MODEL, "jevtrader/secrets.py:79-81", "add-generic-password"),
    (THREAT_MODEL, "jevtrader/secrets.py:99-114", "os.environ[name] = value"),
    (THREAT_MODEL, "jevtrader/cli.py:56-58", "USES_KEYS"),
    (THREAT_MODEL, "jevtrader/cli.py:515-516", "export_to_environ"),
    (THREAT_MODEL, "jevtrader/app.py:466-468", "export_to_environ"),
    (THREAT_MODEL, "jevtrader/security/sanitize.py:34", '"head", "template", "ix:hidden"'),
    (THREAT_MODEL, "jevtrader/sec.py:177-196", "approved HTTPS URL"),
    (THREAT_MODEL, "jevtrader/providers.py:282-302", "instructions"),
    (THREAT_MODEL, "jevtrader/providers.py:411-419", "_questions_text(questions)"),
    (THREAT_MODEL, "jevtrader/lab.py:141-156", "experiment("),
    (THREAT_MODEL, "jevtrader/brief.py:53", "_DIRECTIVE"),
    (THREAT_MODEL, "jevtrader/mcp_server.py:41", "EXCERPT_TOOLS"),
    (THREAT_MODEL, "jevtrader/mcp_server.py:181-183", "KNOWN"),
    (THREAT_MODEL, "jevtrader/mcp_server.py:280-285", "a credential"),
    (THREAT_MODEL, "jevtrader/mcp_server.py:485-496", "stdin"),
    (THREAT_MODEL, "jevtrader/web.py:29", "127.0.0.1"),
    (THREAT_MODEL, "jevtrader/web.py:323-328", "405"),
    (THREAT_MODEL, "jevtrader/web.py:417-418", "loopback only"),
    (THREAT_MODEL, "jevtrader/local.py:45", "LOOPBACK_HOSTS"),
    (THREAT_MODEL, "jevtrader/notify.py:16", "osascript"),
    (THREAT_MODEL, "jevtrader/providers.py:203-207", "redirect"),
    (THREAT_MODEL, "jevtrader/bars.py:177-178", "APCA-API-SECRET-KEY"),
    (SECRETS_POLICY, "jevtrader/secrets.py:15", "KNOWN"),
    (SECRETS_POLICY, "jevtrader/secrets.py:50", "find-generic-password"),
    (SECRETS_POLICY, "jevtrader/secrets.py:59-68", "environment wins"),
    (SECRETS_POLICY, "jevtrader/secrets.py:79-81", "stdin"),
    (SECRETS_POLICY, "jevtrader/secrets.py:99-114", "export_to_environ"),
    (SECRETS_POLICY, "jevtrader/launchd.py:191-203", "store it in the Keychain"),
    (SECRETS_POLICY, "jevtrader/launchd.py:206-210", "Refusing to write an API key"),
    (SECRETS_POLICY, "jevtrader/config.py:1", "never secrets"),
    (SECRETS_POLICY, "jevtrader/config.py:81", "0o600"),
    (SECRETS_POLICY, "jevtrader/config.py:125-137", "dedicated alias"),
    (SECRETS_POLICY, "jevtrader/providers.py:186-190", "os.environ.get(variable"),
    (SECRETS_POLICY, "jevtrader/mcp_server.py:181-183", "KNOWN"),
    (SECRETS_POLICY, "jevtrader/app.py:557", "getpass"),
    (SECRETS_POLICY, "tools/secret_scan.py:94-97", "_canary"),
]

THREAT_SECTIONS = (
    "## Assets",
    "## Trust zones",
    "## Actors",
    "## Entry points",
    "## Threats",
    "## Mitigations",
    "## Residual risks",
)
POLICY_SECTIONS = (
    "## Inventory",
    "## Where each secret lives",
    "## Who may read",
    "## Never",
    "## Rotation",
    "## Canary testing",
    "## Owner identity",
)


def _lines(path: str) -> list[str]:
    return (ROOT / path).read_text(encoding="utf-8").splitlines()


class SecurityDocTests(unittest.TestCase):
    def test_documents_exist_with_their_sections(self):
        for doc, sections in ((THREAT_MODEL, THREAT_SECTIONS), (SECRETS_POLICY, POLICY_SECTIONS)):
            text = doc.read_text(encoding="utf-8")
            for heading in sections:
                self.assertIn(heading, text, f"{doc.name} lacks {heading}")

    def test_every_citation_names_an_existing_file_and_line_range(self):
        for doc in DOCS:
            found = CITATION.findall(doc.read_text(encoding="utf-8"))
            self.assertGreater(len(found), 10, doc.name)
            for path, start, end in found:
                self.assertTrue((ROOT / path).is_file(), f"{doc.name}: {path} is missing")
                if not start:
                    continue
                first, last = int(start), int(end or start)
                self.assertLessEqual(first, last, f"{doc.name}: {path}:{start}-{end}")
                self.assertGreaterEqual(first, 1)
                self.assertLessEqual(last, len(_lines(path)), f"{doc.name}: {path}:{last}")

    def test_load_bearing_citations_still_point_at_their_code(self):
        for doc, citation, needle in ANCHORS:
            self.assertIn(f"`{citation}`", doc.read_text(encoding="utf-8"), citation)
            path, _, span = citation.partition(":")
            first, _, last = span.partition("-")
            lines = _lines(path)[int(first) - 1 : int(last or first)]
            self.assertIn(needle, "\n".join(lines), citation)

    def test_threat_model_maps_mitigations_to_the_security_issues(self):
        text = THREAT_MODEL.read_text(encoding="utf-8")
        for issue in ("#9", "#10", "#11", "#12", "#13", "#28", "#46"):
            self.assertRegex(text, rf"{issue}(?!\d)", issue)
        for actor in ("malicious filer", "prompt injection"):
            self.assertIn(actor, text.lower())

    def test_policy_forbids_every_leak_channel(self):
        text = SECRETS_POLICY.read_text(encoding="utf-8").lower()
        for channel in ("config", "argv", "logs", "ledger", "prompts", "mcp", "error text"):
            self.assertIn(channel, text)
        for store in ("keychain", "libsecret", "credential manager", "docker secrets"):
            self.assertIn(store, text)


if __name__ == "__main__":
    unittest.main()
