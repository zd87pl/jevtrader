"""CI supply chain (P0-22, #26): pip-audit, pinned test deps, Dependabot, macOS wheel.

Workflow and Dependabot files are read as text so no YAML parser is needed.
requirements/test-constraints.txt is the only place exact tool versions live:
Dependabot bumps that one file, so a second pin anywhere else would conflict.
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


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _satisfies(version: str, specifiers: str) -> bool:
    """Check a plain X.Y.Z version against comma-separated >=, <, <=, >, == bounds."""
    compare = {
        ">=": lambda a, b: a >= b,
        "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b,
        "<": lambda a, b: a < b,
        "==": lambda a, b: a == b,
    }
    have = _version(version)
    for spec in filter(None, (part.strip() for part in specifiers.split(","))):
        match = re.fullmatch(r"(>=|<=|==|>|<)\s*(\d+(?:\.\d+)*)", spec)
        if match is None:
            raise AssertionError(f"unsupported specifier {spec!r}")
        want = _version(match.group(2))
        width = max(len(have), len(want))
        padded_have = have + (0,) * (width - len(have))
        padded_want = want + (0,) * (width - len(want))
        if not compare[match.group(1)](padded_have, padded_want):
            return False
    return True


def _dev_extra() -> dict[str, str]:
    """Map each dev-extra requirement name to its specifier string."""
    extra = {}
    for requirement in PYPROJECT["project"]["optional-dependencies"]["dev"]:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)\s*(.*)", requirement)
        assert match is not None, requirement
        extra[match.group(1).lower().replace("_", "-")] = match.group(2)
    return extra


def _pip_install_lines() -> list[str]:
    """Every workflow line that installs packages with pip, except the built wheel."""
    lines = [line for line in WORKFLOW.splitlines() if re.search(r'\bpip"? install ', line)]
    return [line for line in lines if "dist/" not in line]


class ConstraintsTests(unittest.TestCase):
    def test_every_dev_extra_tool_is_pinned_exactly(self) -> None:
        pins = _pins()
        for requirement in PYPROJECT["project"]["optional-dependencies"]["dev"]:
            name = re.split(r"[<>=!~\[ ]", requirement, maxsplit=1)[0].lower()
            with self.subTest(name=name):
                self.assertIn(name, pins)

    def test_every_ci_tool_is_pinned_exactly(self) -> None:
        pins = _pins()
        for tool in ("ruff", "mypy", "pip-audit", "build"):
            with self.subTest(tool=tool):
                self.assertIn(tool, pins)
                self.assertRegex(pins[tool], r"^\d+(\.\d+)+$")

    def test_pins_satisfy_the_dev_extra_ranges(self) -> None:
        # A pin outside its range would make pip fail to resolve '.[dev]'.
        pins = _pins()
        for name, specifiers in _dev_extra().items():
            with self.subTest(name=name):
                self.assertTrue(
                    _satisfies(pins[name], specifiers),
                    f"{name}=={pins[name]} is outside the dev extra range {specifiers!r}",
                )

    def test_dev_extra_keeps_ranges_not_exact_pins(self) -> None:
        for name, specifiers in _dev_extra().items():
            with self.subTest(name=name):
                self.assertNotIn("==", specifiers)

    def test_workflow_has_no_inline_version_pins(self) -> None:
        # An inline pin conflicts with the constraints file on every bump.
        self.assertEqual(re.findall(r"[A-Za-z0-9_.-]+==\d[^\s'\"]*", WORKFLOW), [])

    def test_runtime_dependency_is_not_pinned_here(self) -> None:
        # numpy stays a range in pyproject so every supported Python resolves.
        self.assertNotIn("numpy", _pins())

    def test_test_and_audit_jobs_install_with_the_constraints(self) -> None:
        for job in ("test", "audit"):
            with self.subTest(job=job):
                self.assertIn("-c requirements/test-constraints.txt", _job(job))

    def test_every_pip_install_uses_the_constraints(self) -> None:
        lines = _pip_install_lines()
        self.assertGreaterEqual(len(lines), 4)
        for line in lines:
            with self.subTest(line=line.strip()):
                self.assertIn("-c requirements/test-constraints.txt", line)


class AuditTests(unittest.TestCase):
    def test_pip_audit_is_pinned_and_run(self) -> None:
        job = _job("audit")
        self.assertRegex(job, r"pip install [^\n]*\bpip-audit\b")
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

    def test_build_tool_comes_from_the_constraints(self) -> None:
        self.assertRegex(
            _job("package"), r"pip install build -c requirements/test-constraints\.txt"
        )


class ActionPinTests(unittest.TestCase):
    def test_every_action_is_pinned_by_sha(self) -> None:
        for use in re.findall(r"uses: (\S+)", WORKFLOW):
            with self.subTest(use=use):
                self.assertRegex(use, r"@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
