"""CI supply chain (P0-22, #26): pip-audit, pinned test deps, Dependabot, macOS wheel.

Workflow and Dependabot files are read as text so no YAML parser is needed.
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
DEPENDABOT = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
CONSTRAINTS = ROOT / "requirements" / "test-constraints.txt"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _job(name: str) -> str:
    """Return the text of one top-level job in the workflow."""
    match = re.search(rf"^  {re.escape(name)}:\n(?:(?:    [^\n]*)?\n)*", WORKFLOW, flags=re.M)
    if match is None:
        raise AssertionError(f"no CI job {name!r}")
    return match.group(0)


def _pins() -> dict[str, str]:
    pins = {}
    for raw in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            name, sep, version = line.partition("==")
            if not sep:
                raise AssertionError(f"constraint is not an exact pin: {raw!r}")
            pins[name.strip().lower().replace("_", "-")] = version.strip()
    return pins


class ConstraintsTests(unittest.TestCase):
    def test_every_dev_extra_tool_is_pinned_exactly(self) -> None:
        pins = _pins()
        for requirement in PYPROJECT["project"]["optional-dependencies"]["dev"]:
            name = re.split(r"[<>=!~\[ ]", requirement, maxsplit=1)[0].lower()
            with self.subTest(name=name):
                self.assertIn(name, pins)

    def test_pins_agree_with_the_dev_extra_and_the_lint_job(self) -> None:
        pins = _pins()
        self.assertEqual(pins["mypy"], "1.20.1")
        self.assertEqual(pins["ruff"], "0.15.9")
        self.assertIn(f"ruff=={pins['ruff']}", _job("lint"))

    def test_runtime_dependency_is_not_pinned_here(self) -> None:
        # numpy stays a range in pyproject so every supported Python resolves.
        self.assertNotIn("numpy", _pins())

    def test_test_and_audit_jobs_install_with_the_constraints(self) -> None:
        for job in ("test", "audit"):
            with self.subTest(job=job):
                self.assertIn("-c requirements/test-constraints.txt", _job(job))


class AuditTests(unittest.TestCase):
    def test_pip_audit_is_pinned_and_run(self) -> None:
        job = _job("audit")
        self.assertRegex(job, r"pip-audit==\d+\.\d+\.\d+")
        self.assertRegex(job, r"(?m)^\s+(- run: )?pip-audit( |$)")

    def test_secret_scan_runs_in_ci(self) -> None:
        self.assertIn("tools/secret_scan.py", _job("audit"))


class DependabotTests(unittest.TestCase):
    def test_pip_and_actions_are_both_watched(self) -> None:
        self.assertIn("package-ecosystem: github-actions", DEPENDABOT)
        self.assertIn("package-ecosystem: pip", DEPENDABOT)


class WheelSmokeTests(unittest.TestCase):
    def test_wheel_smoke_runs_on_ubuntu_and_macos(self) -> None:
        job = _job("package")
        self.assertIn("runs-on: ${{ matrix.os }}", job)
        self.assertIn("ubuntu-latest", job)
        self.assertIn("macos-latest", job)

    def test_build_tool_is_pinned(self) -> None:
        self.assertRegex(_job("package"), r"pip install build==\d+\.\d+\.\d+")


class ActionPinTests(unittest.TestCase):
    def test_every_action_is_pinned_by_sha(self) -> None:
        for use in re.findall(r"uses: (\S+)", WORKFLOW):
            with self.subTest(use=use):
                self.assertRegex(use, r"@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
