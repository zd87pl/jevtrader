"""Public-command checks: no credentials or network are used."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from jevtrader.cli import main
from jevtrader.common import load_strategy, timestamp
from jevtrader.market import normalize_bar
from jevtrader.research import FEATURE_NAMES
from jevtrader.store import Ledger


def seed_market(ledger, count=40):
    """Aligned historical ABC/SPY fixture sessions; not an exchange calendar."""
    days, day = [], date(2026, 1, 5)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    for index, session in enumerate(days):
        for ticker in ("ABC", "SPY"):
            price = 100 + index
            bar = normalize_bar(
                {
                    "symbol": ticker,
                    "session": session,
                    "open_at": f"{session}T14:30:00Z",
                    "close_at": f"{session}T21:00:00Z",
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price + 0.5,
                    "volume": 1_000_000,
                },
                mode="historical",
            )
            ledger.put("bars", bar["id"], bar)
    return days


def historical_event(identity, session, *, symbol="ABC"):
    return {
        "id": identity,
        "symbol": symbol,
        "text": f"Raised guidance and strong demand. Record revenue. Update {identity}.",
        "source_url": f"https://example.test/{identity}",
        "mode": "historical",
        "published_at": f"{session}T21:15:00Z",
        "first_seen_at": f"{session}T21:30:00Z",
    }


def jev_response():
    return {
        "model": "jev-1.13.0",
        "answers": {
            "direction": {
                "type": "choice",
                "choice": "improving",
                "confidence": 0.75,
                "probabilities": {
                    "improving": 0.8,
                    "unchanged": 0.05,
                    "deteriorating": 0.1,
                    "unclear": 0.05,
                },
            },
            "materiality": {"type": "noul", "noul": 0.9},
            "novelty": {"type": "noul", "noul": 0.8},
        },
        "usage": {"input_tokens": 122, "output_tokens": 10},
    }


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "ledger.sqlite")

    def command(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--db", self.db, *args])
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def test_init_status_and_bad_record(self):
        code, result, error = self.command("init")
        self.assertEqual(code, 0, error)
        self.assertFalse(result["strategy"]["allow_short"])
        self.assertEqual(self.command("status")[1]["counts"], {})
        self.assertEqual(self.command("show", "forecasts", "missing")[0], 2)

    def test_json_import_is_idempotent_and_no_claimed_forward(self):
        path = Path(self.temp.name) / "events.jsonl"
        event = {
            "id": "example",
            "symbol": "AAPL",
            "text": "Example disclosure.",
            "source_url": "https://example.test/filing",
            "mode": "historical",
            "published_at": "2024-01-02T22:00:00Z",
            "first_seen_at": "2024-01-02T22:01:00Z",
        }
        path.write_text(json.dumps(event) + "\n")
        self.assertEqual(self.command("import-disclosures", str(path))[1]["added"], 1)
        self.assertEqual(self.command("import-disclosures", str(path))[1]["added"], 0)
        event["mode"] = "forward"
        path.write_text(json.dumps(event) + "\n")
        self.assertEqual(self.command("import-disclosures", str(path))[0], 2)

    def test_provider_selection_and_limit_fail_before_network(self):
        with patch("jevtrader.cli.observe") as observe, patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.command("observe", "--provider", "openai")[0], 2)
            self.assertEqual(self.command("observe", "--limit", "201")[0], 2)
            self.assertEqual(self.command("observe", "--as-of", "2024-01-01T00:00:00Z")[0], 2)
            observe.assert_not_called()

    def test_replay_does_not_mark_forward_observation_done(self):
        strategy = load_strategy()
        event = {
            "id": "collected",
            "symbol": "AAPL",
            "text": "An operating update.",
            "source_url": "https://example.test/collected",
            "mode": "forward",
            "published_at": "2024-01-02T22:00:00Z",
            "first_seen_at": "2024-01-02T22:01:00Z",
        }
        forecast = {
            "id": "replay",
            "event_id": event["id"],
            "mode": "historical",
            "provider": "rules",
            "strategy": strategy,
            "calibrator_id": None,
            "extraction_id": "extraction",
            "extractor_key": "key",
            "action": "WATCH",
            "reasons": [],
            "expected_return": None,
        }
        with Ledger(self.db) as ledger:
            ledger.disclosure(event, imported=False)
            ledger.put("extractions", "extraction", {"spec": {"requested_model": "rules-v1"}})
            ledger.put("forecasts", forecast["id"], forecast)
        with patch(
            "jevtrader.cli.observe", return_value={**forecast, "id": "live", "mode": "forward"}
        ) as observe:
            code, result, error = self.command("observe")
            self.assertEqual(code, 0, error)
            self.assertEqual(len(result["forecasts"]), 1)
            self.assertEqual(result["forecasts"][0]["mode"], "forward")
            observe.assert_called_once()
            self.assertIsNone(observe.call_args.kwargs["as_of"])
        with patch("jevtrader.cli.observe") as observe:
            code, result, error = self.command("observe", "--replay")
            self.assertEqual(code, 0, error)
            self.assertEqual(result["forecasts"], [])
            observe.assert_not_called()
        with Ledger(self.db) as ledger:
            ledger.put("forecasts", "live", {**forecast, "id": "live", "mode": "forward"})
        with patch("jevtrader.cli.observe") as observe:
            code, result, error = self.command("observe")
            self.assertEqual(code, 0, error)
            self.assertEqual(result["forecasts"], [])
            observe.assert_not_called()

    def test_live_observe_never_decides_historical_events_now(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            event = historical_event("old", days[25])
            ledger.disclosure(event)
        now = f"{days[39]}T22:00:00.000000Z"
        with patch("jevtrader.engine.utc_now", return_value=now):
            code, result, error = self.command("observe")
        self.assertEqual(code, 0, error)
        self.assertEqual(result["forecasts"], [])
        self.assertEqual(result["skipped"]["requires_replay"], 1)
        code, result, error = self.command("observe", "--replay")
        self.assertEqual(code, 0, error)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["old"])
        with Ledger(self.db) as ledger:
            forecast = ledger.get("forecasts", result["forecasts"][0]["id"])
        self.assertEqual(forecast["decision_at"], timestamp(event["first_seen_at"]))
        code, _, error = self.command("observe", "--event", "old")
        self.assertEqual(code, 1)

    def test_locally_rejected_event_does_not_block_paid_queue(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            ledger.disclosure(historical_event("no-bars", days[24], symbol="XYZ"))
            ledger.disclosure(historical_event("valid", days[25]))
        with (
            patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-secret"}),
            patch("jevtrader.providers._post_json", return_value=jev_response()) as post,
        ):
            code, result, error = self.command(
                "observe", "--replay", "--provider", "jev", "--limit", "1"
            )
        self.assertEqual(code, 1, error)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["valid"])
        self.assertEqual([e["event_id"] for e in result["errors"]], ["no-bars"])
        post.assert_called_once()

    def test_calibrator_queue_skips_events_it_cannot_score(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            for index in range(21, 26):
                ledger.disclosure(historical_event(f"event-{index}", days[index]))
        code, result, error = self.command("observe", "--replay")
        self.assertEqual(code, 0, error)
        with Ledger(self.db) as ledger:
            ledger.put(
                "models",
                "calibrator",
                {
                    "model_id": "calibrator",
                    "extractor_key": result["forecasts"][0]["extractor_key"],
                    "cutoff": timestamp(f"{days[24]}T00:00:00Z"),
                    "training_modes": ["historical"],
                    "training_event_ids": ["event-25"],
                    "feature_names": list(FEATURE_NAMES),
                    "coefficients": [0.0] * len(FEATURE_NAMES),
                    "mean": [0.0] * len(FEATURE_NAMES),
                    "scale": [1.0] * len(FEATURE_NAMES),
                    "intercept": 0.0,
                },
            )
        code, result, error = self.command(
            "observe", "--replay", "--calibrator", "calibrator", "--limit", "1"
        )
        self.assertEqual(code, 0, error)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["event-24"])
        self.assertEqual(result["skipped"]["calibrator_ineligible"], 4)

    def test_propose_checks_output_and_credentials_before_reserving_budget(self):
        trial = {
            "id": "trial",
            "type": "trial_completed",
            "candidate": load_strategy(),
            "development_until": "2026-01-01T00:00:00.000000Z",
            "score": 0.0,
            "report": {"strategies": {}, "evaluated_count": 10},
        }
        with Ledger(self.db) as ledger:
            ledger.put("experiments", "trial", trial)
        missing_dir = Path(self.temp.name) / "missing" / "candidate.json"
        writable = Path(self.temp.name) / "candidate.json"
        propose = ["propose", "--trial", "trial", "--model", "proposal-model", "--output"]
        with patch("jevtrader.lab.propose_strategy") as proposer:
            with patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret"}):
                code, _, error = self.command(*propose, str(missing_dir))
            self.assertEqual(code, 2)
            self.assertIn("directory", error)
            with patch.dict("os.environ", {}, clear=True):
                code, _, error = self.command(*propose, str(writable))
            self.assertEqual(code, 2)
            self.assertIn("OPENAI_API_KEY", error)
            proposer.assert_not_called()
        with Ledger(self.db) as ledger:
            kinds = [row.get("type") for row in ledger.all("experiments")]
        self.assertNotIn("proposal_started", kinds)

    def test_fit_and_evaluate_forward_explicit_mode(self):
        for command, function in (("fit", "train"), ("evaluate", "evaluate")):
            with (
                self.subTest(command=command),
                patch(f"jevtrader.cli.{function}", return_value={"modes": ["forward"]}) as call,
            ):
                code, result, error = self.command(
                    command, "--extractor-key", "key", "--mode", "forward"
                )
                self.assertEqual(code, 0, error)
                self.assertEqual(result["modes"], ["forward"])
                self.assertEqual(call.call_args.kwargs["mode"], "forward")

    def test_full_offline_demo_and_no_overwrite(self):
        with patch(
            "urllib.request.OpenerDirector.open", side_effect=AssertionError("Unexpected network")
        ):
            code, result, error = self.command("demo")
        self.assertEqual(code, 0, error)
        self.assertEqual(result["mode"], "synthetic")
        self.assertGreater(result["counts"]["forecasts"], 70)
        self.assertGreater(result["evaluation"]["evaluated_count"], 10)
        self.assertTrue(result["paper_plan_example"]["simulation_only"])
        self.assertIn("semantic", result["evaluation"]["strategies"])
        counts = self.command("status")[1]["counts"]
        code, _, error = self.command("demo")
        self.assertEqual(code, 2)
        self.assertIn("empty ledger", error)
        self.assertEqual(self.command("status")[1]["counts"], counts)
        forecast = result["paper_plan_example"]["forecast_id"]
        code, plan, error = self.command("paper-plan", "--forecast", forecast, "--equity", "3000")
        self.assertEqual(code, 0, error)
        self.assertTrue(plan["simulation_only"])
        self.assertEqual(
            self.command("show", "paper_plans", plan["id"])[1]["forecast_id"], forecast
        )


if __name__ == "__main__":
    unittest.main()
