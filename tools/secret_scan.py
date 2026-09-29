"""Offline secret scanner for tracked files (P0-22, #26). Standard library only.

    python tools/secret_scan.py            scan every file ``git ls-files`` lists
    python tools/secret_scan.py PATH ...   scan the given files
    python tools/secret_scan.py --canary   prove every rule still fires

Exit 0 when clean, 1 on a finding (or a canary that did not fire), 2 on a usage
or git error. Findings name the file, line and rule, never the matched text.
A line that must hold a secret-shaped value can end with ``secret-scan: allow``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARKER = "secret-scan: allow"
MAX_BYTES = 2_000_000

RULES: dict[str, re.Pattern[str]] = {
    "aws-access-key-id": re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    "github-token": re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,}"),
    "openai-key": re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
    "slack-token": re.compile(r"(?<![A-Za-z0-9])xox[abposr]-[A-Za-z0-9-]{10,}"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY( BLOCK)?-----"),
    # Alpaca secrets are 40 characters; only a quoted or bare value that long counts,
    # so an empty placeholder such as ``APCA_API_SECRET_KEY=`` stays clean.
    "alpaca-secret": re.compile(
        r"APCA_API_SECRET_KEY['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9/+]{32,}", re.IGNORECASE
    ),
}


def _canary(rule: str) -> str:
    """Build a fake secret for ``rule`` at run time so none is ever committed."""
    parts = {
        "aws-access-key-id": ("AK", "IA", "Q" * 4, "7" * 12),
        "github-token": ("gh", "p_", "a1B2" * 9),
        "openai-key": ("s", "k-", "proj-", "Z9y8" * 8),
        "slack-token": ("xo", "xb-", "1234567890-", "abcdefghij"),
        "private-key": ("-----BEGIN ", "RSA PRIVATE", " KEY-----"),
        "alpaca-secret": ("APCA_API_", 'SECRET_KEY="', "Ab1Cd2" * 7, '"'),
    }[rule]
    return "".join(parts)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def scan_text(text: str, path: str = "<text>") -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for rule, pattern in RULES.items():
            if pattern.search(line):
                findings.append(Finding(path, number, rule))
    return findings


def scan_file(path: Path) -> list[Finding]:
    try:
        data = path.read_bytes()[:MAX_BYTES]
    except (FileNotFoundError, IsADirectoryError):
        return []
    if b"\0" in data[:8192]:
        return []  # binary
    return scan_text(data.decode("utf-8", errors="replace"), str(path))


def tracked_files() -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, check=True, timeout=30
    ).stdout
    return [Path(name) for name in output.decode("utf-8").split("\0") if name]


def run_canary() -> int:
    missed = []
    for rule in RULES:
        caught = {finding.rule for finding in scan_text(f"value = {_canary(rule)}")}
        print(f"canary {rule}: {'caught' if rule in caught else 'MISSED'}")
        if rule not in caught:
            missed.append(rule)
    return 1 if missed else 0


def main(argv: list[str]) -> int:
    if argv == ["--canary"]:
        return run_canary()
    if any(arg.startswith("-") for arg in argv):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        paths = [Path(arg) for arg in argv] or tracked_files()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"secret scan: cannot list tracked files: {type(exc).__name__}", file=sys.stderr)
        return 2
    findings = [finding for path in paths for finding in scan_file(path)]
    for finding in findings:
        print(finding)
    print(f"secret scan: {len(paths)} files, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
