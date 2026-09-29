"""CI merge gates (P0-21, #25): coverage floor, exact mypy baseline, hang exit.

The workflow is read as text so the check needs no YAML parser in the dev extra.
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _step(name: str) -> str:
    """Return the text of the workflow step whose name starts with ``name``."""
    # The step runs until the next line indented no deeper than its "- name:".
    match = re.search(
        rf"^( *)- name: {re.escape(name)}[^\n]*\n(?:\1  [^\n]*\n|[ \t]*\n)*", WORKFLOW, flags=re.M
    )
    if match is None:
        raise AssertionError(f"no CI step named {name!r}")
    return match.group(0)


def _pytest_command() -> str:
    lines = [line for line in WORKFLOW.splitlines() if "python -m pytest" in line]
    if len(lines) != 1:
        raise AssertionError(f"expected one pytest command, found {lines!r}")
    return lines[0]


class NoSoftGateTests(unittest.TestCase):
    def test_no_step_or_job_may_fail_softly(self) -> None:
        # continue-on-error turns any gate (mypy, pytest, audit) into a warning.
        self.assertNotIn("continue-on-error", WORKFLOW)


class DevExtraTests(unittest.TestCase):
    def test_dev_extra_declares_the_gate_tools(self) -> None:
        dev = " ".join(PYPROJECT["project"]["optional-dependencies"]["dev"])
        for tool in ("pytest-cov", "pytest-timeout", "mypy==1.20.1"):
            with self.subTest(tool=tool):
                self.assertIn(tool, dev)

    def test_lint_job_pins_the_same_mypy_as_the_dev_extra(self) -> None:
        self.assertIn("mypy==1.20.1", WORKFLOW)


class CoverageFloorTests(unittest.TestCase):
    def test_pytest_measures_package_coverage(self) -> None:
        self.assertIn("--cov=jevtrader", _pytest_command())

    def test_coverage_floor_is_at_least_95_percent(self) -> None:
        match = re.search(r"--cov-fail-under=(\d+)", _pytest_command())
        self.assertIsNotNone(match, "pytest must fail below a coverage floor")
        assert match is not None
        self.assertGreaterEqual(int(match.group(1)), 95)


class HangExitTests(unittest.TestCase):
    def test_a_hung_test_ends_the_run(self) -> None:
        command = _pytest_command()
        match = re.search(r"--timeout=(\d+)", command)
        self.assertIsNotNone(match, "a per-test timeout must exist")
        assert match is not None
        self.assertLessEqual(int(match.group(1)), 300)
        # The thread method dumps stacks and exits the process; signal would
        # only raise inside the test and can be swallowed.
        self.assertIn("--timeout-method=thread", command)


class MypyBaselineTests(unittest.TestCase):
    def test_mypy_step_does_not_hide_crashes(self) -> None:
        step = _step("mypy")
        self.assertNotIn("|| true", step)
        # mypy exits 0 (clean) or 1 (type errors); anything else is a crash.
        self.assertRegex(step, r"status.*-gt 1|-gt 1.*status")

    def test_mypy_step_requires_the_exact_baseline(self) -> None:
        step = _step("mypy")
        self.assertRegex(step, r"MYPY_BASELINE: \d+")
        self.assertIn('-eq "$MYPY_BASELINE"', step)
        self.assertNotIn("-le", step)

    def test_mypy_step_reads_the_summary_line(self) -> None:
        # A run that prints no summary did not finish checking.
        self.assertIn("Found", _step("mypy"))

    def test_mypy_status_is_captured_right_after_mypy(self) -> None:
        # "status=0" or a command in between would make every run look clean.
        lines = [line.strip() for line in _step("mypy").splitlines()]
        runs = [i for i, line in enumerate(lines) if line.startswith("mypy jevtrader")]
        self.assertEqual(len(runs), 1, lines)
        self.assertEqual(lines[runs[0] + 1], "status=$?")
        self.assertEqual(lines[runs[0] + 2], "set -e")

    def test_mypy_baseline_is_zero(self) -> None:
        # P0-23 (#27): the package type-checks clean, so no error is tolerated.
        self.assertRegex(_step("mypy"), r"MYPY_BASELINE: 0\n")


if __name__ == "__main__":
    unittest.main()
