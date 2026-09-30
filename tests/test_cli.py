"""Public-command checks: no credentials or network are used."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from jevtrader.cli import main
from jevtrader.common import digest, load_strategy, timestamp
from jevtrader.engine import ObservationRejected
from jevtrader.market import normalize_bar
from jevtrader.providers import ProviderError, ProviderInputError
from jevtrader.research import FEATURE_NAMES, VERSION
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


def calibrator(cutoff, *, training_event_ids=(), training_modes=("historical",), **changes):
    return {
        "model_id": "calibrator",
        "version": VERSION,
        "extractor_key": "key",
        "cutoff": timestamp(cutoff),
        "training_modes": list(training_modes),
        "training_event_ids": list(training_event_ids),
        "feature_names": list(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "mean": [0.0] * len(FEATURE_NAMES),
        "scale": [1.0] * len(FEATURE_NAMES),
        "intercept": 0.0,
        **changes,
    }


def completed_trial():
    return {
        "id": "trial",
        "type": "trial_completed",
        "candidate": load_strategy(),
        "development_until": "2026-01-01T00:00:00.000000Z",
        "score": 0.0,
        "report": {"strategies": {}, "evaluated_count": 10},
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
        self.assertEqual(self.command("import-disclosures", "missing.jsonl")[0], 2)
        self.assertFalse(Path(self.db).exists())  # Only init and demo create a ledger.
        self.assertEqual(self.command("init")[0], 0)
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
            self.assertEqual(self.command("observe", "--max-scan", "0")[0], 2)
            self.assertEqual(self.command("observe", "--as-of", "2024-01-01T00:00:00Z")[0], 2)
            observe.assert_not_called()

    def test_replay_does_not_mark_forward_observation_done(self):
        strategy = load_strategy()
        event = {
            "id": "collected",
            "symbol": "ABC",
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
            seed_market(ledger)
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
        code, result, error = self.command("observe")
        self.assertEqual(code, 0, error)
        self.assertEqual(result["skipped"]["requires_replay"], 0)  # Already replayed.
        code, result, _ = self.command("observe", "--event", "old")
        self.assertEqual(code, 1)
        self.assertIn("--replay", result["errors"][0]["error"])

    def test_locally_rejected_event_does_not_block_paid_queue(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            ledger.disclosure(historical_event("too-early", days[5]))  # Too little history.
            ledger.disclosure(historical_event("no-bars", days[24], symbol="XYZ"))
            ledger.disclosure(historical_event("valid", days[25]))
        with (
            patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-secret"}),
            patch("jevtrader.providers.post_json", return_value=jev_response()) as post,
        ):
            code, result, error = self.command(
                "observe", "--replay", "--provider", "jev", "--limit", "1"
            )
        self.assertEqual(code, 1, error)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["valid"])
        self.assertEqual([e["event_id"] for e in result["errors"]], ["too-early"])
        self.assertEqual(result["skipped"]["no_market_data"], 1)
        post.assert_called_once()

    def test_events_without_market_data_cannot_wedge_the_queue(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            for index in range(250):
                ledger.disclosure(historical_event(f"no-bars-{index:03d}", days[24], symbol="XYZ"))
            ledger.disclosure(historical_event("valid", days[25]))
        code, result, error = self.command("observe", "--replay", "--limit", "1")
        self.assertEqual(code, 0, error)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["valid"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["skipped"]["no_market_data"], 250)
        self.assertFalse(result["skipped"]["scan_truncated"])

    def test_calibrator_queue_skips_events_it_cannot_score(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            for index in range(21, 26):
                ledger.disclosure(historical_event(f"event-{index}", days[index]))
        code, result, error = self.command("observe", "--replay")
        self.assertEqual(code, 0, error)
        key = result["forecasts"][0]["extractor_key"]
        with Ledger(self.db) as ledger:
            model = calibrator(
                f"{days[24]}T00:00:00Z", training_event_ids=["event-25"], extractor_key=key
            )
            ledger.put("models", "calibrator", model)
            stale = {**model, "model_id": "stale", "version": "ridge-event-v1"}
            ledger.put("models", "stale", stale)
        versions = {m["model_id"]: m["version"] for m in self.command("status")[1]["models"]}
        self.assertEqual(versions, {"calibrator": VERSION, "stale": "ridge-event-v1"})
        code, result, error = self.command(
            "observe", "--replay", "--calibrator", "calibrator", "--limit", "1"
        )
        self.assertEqual(code, 0, error)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["event-24"])
        self.assertEqual(result["skipped"]["calibrator_ineligible"], 4)
        with patch("jevtrader.cli.observe") as observe:
            code, _, error = self.command("observe", "--replay", "--calibrator", "stale")
        self.assertEqual(code, 2)
        self.assertIn("re-fit", error)
        observe.assert_not_called()

    def test_live_calibrator_queue_scores_events_first_seen_before_its_cutoff(self):
        with Ledger(self.db) as ledger:
            seed_market(ledger)
            for identity, seen in (("trained", "21:10"), ("fresh", "21:30")):
                event = {
                    "id": identity,
                    "symbol": "ABC",
                    "text": "An operating update.",
                    "source_url": f"https://example.test/{identity}",
                    "mode": "forward",
                    "published_at": "2024-01-02T21:00:00Z",
                    "first_seen_at": f"2024-01-02T{seen}:00Z",
                }
                ledger.disclosure(event, imported=False)
            cutoff = "2024-01-02T22:00:00Z"
            model = calibrator(cutoff, training_event_ids=["trained"], training_modes=["forward"])
            ledger.put("models", "calibrator", model)
            synthetic = {**model, "model_id": "synthetic", "training_modes": ["synthetic"]}
            ledger.put("models", "synthetic", synthetic)
        forecast = {
            "id": "forecast",
            "event_id": "fresh",
            "mode": "forward",
            "action": "PASS",
            "reasons": [],
            "expected_return": 0.0,
            "extractor_key": "key",
        }
        # A live decision happens now, after the cutoff, whenever the event was first seen.
        cases = [
            ((), ["fresh"], 1),
            (("--replay",), [], 2),
            (("--calibrator", "synthetic"), [], 2),
        ]
        for extra, expected, ineligible in cases:
            with self.subTest(extra), patch("jevtrader.cli.observe", return_value=forecast) as call:
                args = ["observe", "--calibrator", "calibrator", *extra]
                code, result, error = self.command(*args)
                self.assertEqual(code, 0, error)
                self.assertEqual([c.args[1] for c in call.call_args_list], expected)
                self.assertEqual(result["skipped"]["calibrator_ineligible"], ineligible)

    def test_failed_paid_request_stops_the_run_and_is_not_billed_again(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            ledger.disclosure(historical_event("failing", days[24]))
            ledger.disclosure(historical_event("valid", days[25]))
        paid = ["observe", "--replay", "--provider", "jev"]
        responses = [ProviderError("Provider HTTP error 500"), jev_response(), jev_response()]
        with (
            patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-secret"}),
            patch("jevtrader.providers.post_json", side_effect=responses) as post,
        ):
            code, result, error = self.command(*paid)
            self.assertEqual(code, 1, error)
            self.assertEqual(result["forecasts"], [])
            self.assertEqual([e["event_id"] for e in result["errors"]], ["failing"])
            self.assertEqual(post.call_count, 1)
            code, result, error = self.command(*paid)
            self.assertEqual(code, 0, error)
            self.assertEqual([f["event_id"] for f in result["forecasts"]], ["valid"])
            self.assertEqual(result["skipped"]["failed_before"], 1)
            self.assertEqual(post.call_count, 2)
            # Another edge threshold would send the identical request, so it stays skipped.
            edge = load_strategy()
            edge["min_edge_bps"] += 1
            path = Path(self.temp.name) / "edge.json"
            path.write_text(json.dumps(edge))
            code, result, error = self.command("--strategy", str(path), *paid)
            self.assertEqual(code, 0, error)
            self.assertEqual(result["skipped"]["failed_before"], 1)
            self.assertEqual([f["event_id"] for f in result["forecasts"]], ["valid"])  # Cached.
            self.assertEqual(post.call_count, 2)
            code, result, error = self.command(*paid, "--retry-failed")
            self.assertEqual(code, 0, error)
            self.assertEqual([f["event_id"] for f in result["forecasts"]], ["failing"])
            self.assertEqual(post.call_count, 3)

    def test_missing_key_stops_uncached_runs_but_cached_replays_need_none(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            ledger.disclosure(historical_event("a-cached", days[25]))
            ledger.disclosure(historical_event("b-uncached", days[26]))
        renamed = load_strategy()
        renamed["name"] = "renamed"  # Same extraction spec, so a-cached needs no request.
        path = Path(self.temp.name) / "renamed.json"
        path.write_text(json.dumps(renamed))
        paid = ["observe", "--replay", "--provider", "jev"]
        with patch("jevtrader.providers.post_json", return_value=jev_response()) as post:
            with patch.dict("os.environ", {}, clear=True):
                code, result, error = self.command(*paid)
            self.assertEqual(code, 2, error)
            self.assertEqual(result["forecasts"], [])
            self.assertIn("TYPESAFE_API_KEY", result["errors"][0]["error"])
            self.assertTrue(result["skipped"]["missing_credentials"])
            post.assert_not_called()
            with patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-secret"}):
                code, first, error = self.command(*paid, "--event", "a-cached")
            self.assertEqual(code, 0, error)
            with patch.dict("os.environ", {}, clear=True):
                code, repeated, error = self.command(*paid, "--event", "a-cached")
                self.assertEqual(code, 0, error)
                self.assertEqual(repeated["forecasts"], first["forecasts"])
                # A stopped run still prints the forecasts it recorded before stopping.
                code, partial, error = self.command("--strategy", str(path), *paid)
            self.assertEqual(code, 2, error)
            self.assertEqual([f["event_id"] for f in partial["forecasts"]], ["a-cached"])
            self.assertEqual([e["event_id"] for e in partial["errors"]], ["b-uncached"])
            self.assertTrue(partial["skipped"]["missing_credentials"])
            post.assert_called_once()

    def test_failed_before_applies_only_to_the_same_paid_request(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            ledger.disclosure(historical_event("failing", days[24]))
        questions = load_strategy()
        questions["questions"]["novelty"] = "A different novelty question about the same event."
        path = Path(self.temp.name) / "questions.json"
        path.write_text(json.dumps(questions))
        paid = ["observe", "--replay", "--provider", "jev"]
        responses = [ProviderError("Provider HTTP error 500"), *[jev_response()] * 3]
        with (
            patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-secret"}),
            patch("jevtrader.providers.post_json", side_effect=responses) as post,
        ):
            self.assertEqual(self.command(*paid)[0], 1)
            for label, args in (
                ("model", [*paid, "--model", "jev-1.14.0"]),
                ("questions", ["--strategy", str(path), *paid]),
            ):
                with self.subTest(label):
                    code, result, error = self.command(*args)
                    self.assertEqual(code, 0, error)
                    self.assertEqual(result["skipped"]["failed_before"], 0)
                    self.assertEqual([f["event_id"] for f in result["forecasts"]], ["failing"])
            self.assertEqual(post.call_count, 3)
            # Naming the event is an explicit retry of the same request.
            code, result, error = self.command(*paid, "--event", "failing")
            self.assertEqual(code, 0, error)
            self.assertEqual([f["event_id"] for f in result["forecasts"]], ["failing"])
            self.assertEqual(post.call_count, 4)
            # The retry cached the extraction, so a new calibrator scores it without a request.
            key = result["forecasts"][0]["extractor_key"]
            with Ledger(self.db) as ledger:
                model = calibrator(f"{days[23]}T00:00:00Z", extractor_key=key)
                ledger.put("models", "calibrator", model)
            code, result, error = self.command(*paid, "--calibrator", "calibrator")
            self.assertEqual(code, 0, error)
            self.assertEqual(result["skipped"]["failed_before"], 0)
            self.assertEqual([f["event_id"] for f in result["forecasts"]], ["failing"])
            self.assertEqual(post.call_count, 4)

    def test_interrupted_paid_request_is_recorded_and_listed(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            ledger.disclosure(historical_event("interrupted", days[24]))
        paid = ["observe", "--replay", "--provider", "jev"]
        with (
            patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-secret"}),
            patch(
                "jevtrader.providers.post_json", side_effect=[KeyboardInterrupt, jev_response()]
            ) as post,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.command(*paid)
            code, result, error = self.command(*paid)
            self.assertEqual(code, 0, error)
            self.assertEqual(result["skipped"]["failed_before"], 1)
            self.assertEqual(post.call_count, 1)
        [attempt] = self.command("status")[1]["failed_attempts"]
        self.assertEqual(attempt["event_id"], "interrupted")
        code, record, error = self.command("show", "attempts", attempt["id"])
        self.assertEqual(code, 0, error)
        self.assertEqual(record["error_type"], "KeyboardInterrupt")

    def test_local_rejections_cannot_scan_an_unbounded_queue(self):
        with Ledger(self.db) as ledger:
            seed_market(ledger)
            for index in range(205):
                ledger.disclosure(historical_event(f"event-{index:03d}", "2026-01-05"))
        stale, too_long = ObservationRejected("Market data is stale"), ProviderInputError("model")
        # The scan bound is independent of --limit, which also allows paid calls.
        cases = [
            (("--limit", "1"), stale, 200, True),
            (("--limit", "200"), stale, 200, True),
            (("--limit", "1", "--max-scan", "300"), stale, 205, False),
            (("--limit", "1"), too_long, 200, True),
        ]
        for extra, failure, scanned, truncated in cases:
            with (
                self.subTest(extra=extra, failure=failure),
                patch("jevtrader.cli.observe", side_effect=failure) as call,
            ):
                code, result, error = self.command("observe", "--replay", *extra)
                self.assertEqual(code, 1, error)
                self.assertEqual(call.call_count, scanned)
                self.assertEqual(len(result["errors"]), scanned)
                self.assertIs(result["skipped"]["scan_truncated"], truncated)

    def test_paper_plan_refuses_forecasts_scored_by_another_evaluator(self):
        with Ledger(self.db) as ledger:
            stale = calibrator("2026-01-01T00:00:00Z", model_id="stale", version="ridge-event-v1")
            ledger.put("models", "stale", stale)
            forecast = {"id": "scored", "calibrator_id": "stale", "strategy": load_strategy()}
            ledger.put("forecasts", "scored", forecast)
        with patch("jevtrader.cli.plan_order") as plan:
            code, _, error = self.command("paper-plan", "--forecast", "scored")
        self.assertEqual(code, 2)
        self.assertIn("re-fit", error)
        plan.assert_not_called()

    def test_strategy_with_padded_questions_providers_accept_can_be_replayed(self):
        with Ledger(self.db) as ledger:
            days = seed_market(ledger)
            ledger.disclosure(historical_event("event", days[25]))
        padded = load_strategy()
        padded["questions"]["novelty"] = "   New info?     "
        path = Path(self.temp.name) / "padded.json"
        path.write_text(json.dumps(padded))
        code, result, error = self.command("--strategy", str(path), "observe", "--replay")
        self.assertEqual(code, 0, error)
        self.assertEqual([f["event_id"] for f in result["forecasts"]], ["event"])
        padded["questions"]["novelty"] = "Short?"  # Still fewer than ten characters.
        path.write_text(json.dumps(padded))
        code, _, error = self.command("--strategy", str(path), "observe", "--replay")
        self.assertEqual(code, 2)
        self.assertIn("Questions", error)

    def test_propose_checks_output_and_credentials_before_reserving_budget(self):
        with Ledger(self.db) as ledger:
            ledger.put("experiments", "trial", completed_trial())
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

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root bypasses permissions")
    def test_propose_rejects_read_only_output_directory_before_reserving_budget(self):
        with Ledger(self.db) as ledger:
            ledger.put("experiments", "trial", completed_trial())
        read_only = Path(self.temp.name) / "read-only"
        read_only.mkdir()
        read_only.chmod(0o500)
        self.addCleanup(read_only.chmod, 0o700)
        with (
            patch("jevtrader.lab.propose_strategy") as proposer,
            patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret"}),
        ):
            code, _, error = self.command(
                "propose",
                "--trial",
                "trial",
                "--model",
                "proposal-model",
                "--output",
                str(read_only / "candidate.json"),
            )
        self.assertEqual(code, 2)
        self.assertIn("not writable", error)
        proposer.assert_not_called()
        with Ledger(self.db) as ledger:
            self.assertEqual([r["id"] for r in ledger.all("experiments")], ["trial"])

    def approve_questions(self, proposal_id, typed):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(typed)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["--db", self.db, "approve-questions", "--proposal", proposal_id])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_approve_questions_needs_the_typed_digest_prefix(self):
        candidate = load_strategy()
        candidate["questions"]["novelty"] = "Is there new supply evidence versus the prior filing?"
        proposal = {"id": "proposal-1", "type": "proposal", "candidate": candidate}
        with Ledger(self.db) as ledger:
            ledger.put("experiments", "proposal-1", proposal)
            ledger.put("experiments", "rejected-1", {**proposal, "type": "proposal_rejected"})
        sha = digest(candidate["questions"])
        code, out, error = self.approve_questions("proposal-1", "deadbeef\n")
        self.assertEqual(code, 2)
        self.assertIn("+novelty: Is there new supply evidence", error)
        self.assertIn(sha[:8], error)
        self.assertIn("not approved", error)
        for missing in ("missing", "rejected-1"):
            self.assertEqual(self.approve_questions(missing, sha[:8] + "\n")[0], 2)
        with Ledger(self.db) as ledger:
            types = [row.get("type") for row in ledger.all("experiments")]
        self.assertNotIn("question_approval", types)
        code, out, error = self.approve_questions("proposal-1", sha[:8] + "\n")
        self.assertEqual(code, 0, error)
        result = json.loads(out)
        self.assertEqual(result["questions_sha256"], sha)
        self.assertEqual(result["approved_by"], "cli")
        with Ledger(self.db) as ledger:
            [row] = [r for r in ledger.all("experiments") if r.get("type") == "question_approval"]
        self.assertEqual(row["questions_sha256"], sha)
        self.assertIn("-novelty:", row["diff"])

    def test_fit_and_evaluate_forward_explicit_mode(self):
        self.assertEqual(self.command("init")[0], 0)
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
