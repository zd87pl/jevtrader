"""Contract tests for the typed ledger records (P0-23, #27).

Each TypedDict in ``jevtrader.contracts`` must describe what the code actually
writes: every record the deterministic demo stores conforms to its contract, and
the SEC collector's literal disclosure keys are all declared.
"""

import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path

from jevtrader import contracts
from jevtrader.demo import run_demo
from jevtrader.store import Ledger

ROOT = Path(__file__).resolve().parents[2]


def _demo_records() -> dict[str, list[dict]]:
    with tempfile.TemporaryDirectory() as folder:
        with Ledger(Path(folder) / "ledger.sqlite") as ledger:
            run_demo(ledger)
            return {
                name: ledger.all(name)
                for name in ("disclosures", "extractions", "forecasts", "outcomes")
            }


RECORDS = _demo_records()


class ConformsTests(unittest.TestCase):
    def test_reports_missing_and_undeclared_keys(self) -> None:
        record = {"forecast_id": "f", "surprise": 1}
        problems = contracts.conforms(contracts.Outcome, record)
        self.assertIn("missing key: event_id", problems)
        self.assertIn("undeclared key: surprise", problems)

    def test_optional_keys_may_be_absent(self) -> None:
        record = dict(RECORDS["extractions"][0])
        record.pop("text_excerpt", None)
        self.assertEqual(contracts.conforms(contracts.Extraction, record), [])
        record["text_excerpt"] = "excerpt"
        self.assertEqual(contracts.conforms(contracts.Extraction, record), [])


class RecordContractTests(unittest.TestCase):
    def _check(self, contract: type, collection: str) -> None:
        records = RECORDS[collection]
        self.assertTrue(records, f"the demo wrote no {collection}")
        for record in records:
            self.assertEqual(contracts.conforms(contract, record), [], record.get("id"))

    def test_disclosure(self) -> None:
        self._check(contracts.Disclosure, "disclosures")

    def test_sec_disclosure_keys_are_declared(self) -> None:
        tree = ast.parse((ROOT / "jevtrader" / "sec.py").read_text(encoding="utf-8"))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "collect_filing"
        )
        returned = [
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
        ]
        self.assertEqual(len(returned), 1)
        keys = {key.value for key in returned[0].keys if isinstance(key, ast.Constant)}
        declared = set(contracts.Disclosure.__annotations__)
        self.assertEqual(keys - declared, set())
        self.assertLessEqual(set(contracts.Disclosure.__required_keys__), keys)

    def test_extraction(self) -> None:
        self._check(contracts.Extraction, "extractions")

    def test_forecast(self) -> None:
        self._check(contracts.Forecast, "forecasts")

    def test_outcome(self) -> None:
        self._check(contracts.Outcome, "outcomes")


@unittest.skipUnless(importlib.util.find_spec("mypy"), "mypy is not installed")
class StrictTypingTests(unittest.TestCase):
    def test_contracts_pass_mypy_strict(self) -> None:
        from mypy import api

        with tempfile.TemporaryDirectory() as cache:
            out, err, status = api.run(
                ["--strict", "--cache-dir", cache, str(ROOT / "jevtrader" / "contracts")]
            )
        self.assertEqual(status, 0, out + err)


if __name__ == "__main__":
    unittest.main()
