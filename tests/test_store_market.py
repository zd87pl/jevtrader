"""Point-in-time ledger and market-label tests with local synthetic fixtures."""

import copy
import csv
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from jevtrader.common import canonical, digest, timestamp
from jevtrader.market import import_bars, normalize_bar, outcome, snapshot
from jevtrader.store import Ledger


STRATEGY = {
    "benchmark": "SPY",
    "min_history_sessions": 21,
    "max_market_age_hours": 96,
    "spread_bps": 8,
    "horizon_sessions": 3,
}


def sessions(count=25):
    days = []
    day = date(2026, 1, 5)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


DAYS = sessions()


def raw_bar(ticker="ABC", session=DAYS[0], *, price=100.0, **changes):
    row = {
        "symbol": ticker,
        "session": session,
        "open_at": f"{session}T14:30:00Z",
        "close_at": f"{session}T21:00:00Z",
        "available_at": f"{session}T21:01:00Z",
        "open": price,
        "high": price + 2,
        "low": price - 2,
        "close": price + 1,
        "volume": 1_000,
        "split_ratio": 1,
        "cash_dividend": 0,
    }
    row.update(changes)
    return row


def bar(ticker="ABC", session=DAYS[0], *, mode="synthetic", **changes):
    raw = raw_bar(ticker, session, **changes)
    if mode == "forward":
        with patch("jevtrader.market.utc_now", return_value=raw["available_at"]):
            return normalize_bar(raw, mode=mode)
    return normalize_bar(raw, mode=mode)


def disclosure(**changes):
    event = {
        "id": "sec-accession-001",
        "symbol": "ABC",
        "text": "The company disclosed a new material agreement.",
        "source_url": "https://www.sec.gov/Archives/example.htm",
        "published_at": "2026-01-05T20:00:00Z",
        "first_seen_at": "2026-01-05T20:05:00Z",
        "mode": "historical",
    }
    event.update(changes)
    return event


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)

    def test_insert_is_idempotent_and_conflicting_payload_is_rejected(self):
        payload = {"id": "forecast-1", "score": 0.25, "nested": {"reason": "new fact"}}
        self.assertTrue(self.ledger.put("forecasts", "forecast-1", payload))
        self.assertFalse(self.ledger.put("forecasts", "forecast-1", copy.deepcopy(payload)))
        with self.assertRaisesRegex(ValueError, "Immutable record conflict"):
            self.ledger.put("forecasts", "forecast-1", {**payload, "score": 0.5})
        self.assertEqual(self.ledger.get("forecasts", "forecast-1"), payload)
        self.assertEqual(self.ledger.counts(), {"forecasts": 1})
        self.assertIsNone(self.ledger.get("forecasts", "missing"))

    def test_input_and_return_mutation_cannot_change_persisted_record(self):
        payload = {"nested": {"score": 0.25}}
        self.ledger.put("forecasts", "a", payload)
        payload["nested"]["score"] = 99
        received = self.ledger.get("forecasts", "a")
        received["nested"]["score"] = 100
        self.assertEqual(self.ledger.get("forecasts", "a"), {"nested": {"score": 0.25}})

    def test_sql_update_and_delete_are_blocked(self):
        self.ledger.put("forecasts", "a", {"score": 1})
        for query in (
            "UPDATE records SET payload='{}' WHERE id='a'",
            "DELETE FROM records WHERE id='a'",
        ):
            with (
                self.subTest(query=query),
                self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"),
            ):
                self.ledger.db.execute(query)
        self.assertEqual(self.ledger.get("forecasts", "a"), {"score": 1})

    def test_sql_replace_cannot_overwrite_an_immutable_record(self):
        self.ledger.put("forecasts", "a", {"score": 1})
        changed = {"score": 999}
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.db.execute(
                "INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?)",
                (
                    "forecasts",
                    "a",
                    canonical(changed),
                    digest(changed),
                    timestamp("2026-01-06T00:00:00Z"),
                ),
            )
        self.assertEqual(self.ledger.get("forecasts", "a"), {"score": 1})

    def test_repeat_live_sec_observation_keeps_original_first_seen(self):
        original = disclosure(mode="forward")
        with patch("jevtrader.store.utc_now", return_value="2026-01-07T22:00:00Z"):
            self.assertTrue(self.ledger.disclosure(original, imported=False))
            self.assertFalse(
                self.ledger.disclosure(
                    {**original, "first_seen_at": "2026-01-07T21:00:00Z"},
                    imported=False,
                )
            )
        stored = self.ledger.get("disclosures", original["id"])
        self.assertEqual(stored["first_seen_at"], timestamp(original["first_seen_at"]))
        self.assertEqual(len(self.ledger.all("disclosures")), 1)

    def test_repeat_disclosure_cannot_change_source_text_or_mode(self):
        original = disclosure()
        self.ledger.disclosure(original)
        for change in (
            {"text": "Rewritten version"},
            {"symbol": "XYZ"},
            {"mode": "synthetic"},
            {"source_url": "https://example.com/different"},
        ):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "content changed"):
                self.ledger.disclosure({**original, **change})

    def test_reimport_with_corrected_first_seen_is_a_conflict_not_a_no_op(self):
        original = disclosure()
        self.assertTrue(self.ledger.disclosure(original))
        self.assertFalse(self.ledger.disclosure(copy.deepcopy(original)))
        for change in ({"first_seen_at": "2026-01-06T14:31:00Z"}, {"source_type": "fixture"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "content changed"):
                self.ledger.disclosure({**original, **change})
        stored = self.ledger.get("disclosures", original["id"])
        self.assertEqual(stored["first_seen_at"], timestamp(original["first_seen_at"]))

    def test_historical_import_cannot_claim_forward_collection(self):
        with self.assertRaisesRegex(ValueError, "live collector"):
            self.ledger.disclosure(disclosure(mode="forward"))
        self.assertEqual(self.ledger.counts(), {})

    def test_disclosure_timestamp_normalization(self):
        event = disclosure(
            symbol=" abc ",
            published_at="2026-01-05T14:00:00-06:00",
            first_seen_at="2026-01-05T14:05:00-06:00",
        )
        self.ledger.disclosure(event)
        stored = self.ledger.get("disclosures", event["id"])
        self.assertEqual(stored["symbol"], "ABC")
        self.assertEqual(stored["published_at"], "2026-01-05T20:00:00.000000Z")

    def test_naive_reversed_and_future_disclosure_timestamps_are_rejected(self):
        for change in (
            {"published_at": "2026-01-05T20:00:00"},
            {"first_seen_at": "2026-01-05T19:00:00Z"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.ledger.disclosure(disclosure(**change))
        with patch("jevtrader.store.utc_now", return_value="2026-01-05T20:02:00Z"):
            with self.assertRaisesRegex(ValueError, "future"):
                self.ledger.disclosure(disclosure(mode="forward"), imported=False)


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)

    def add(self, value):
        self.ledger.put("bars", value["id"], value)
        return value

    def history(self, count=21, *, mode="synthetic"):
        for index, session in enumerate(DAYS[:count]):
            self.add(bar("ABC", session, price=100 + index, mode=mode))
            self.add(bar("SPY", session, price=200 + index, mode=mode))

    def forecast(self, **changes):
        result = {
            "id": "forecast-1",
            "event_id": "event-1",
            "symbol": "ABC",
            "mode": "synthetic",
            "decision_at": f"{DAYS[0]}T21:30:00Z",
            "strategy": copy.deepcopy(STRATEGY),
        }
        result.update(changes)
        return result

    def test_raw_bars_are_normalized_without_price_adjustment(self):
        value = normalize_bar(
            raw_bar(
                "abc",
                open="100",
                high="102",
                low="98",
                close="101",
                volume="1000",
                split_ratio="2",
                cash_dividend="0.5",
            )
        )
        self.assertEqual(value["symbol"], "ABC")
        self.assertEqual(value["id"], f"ABC:{DAYS[0]}")
        self.assertEqual(value["open"], 100.0)
        self.assertEqual(value["close"], 101.0)
        self.assertEqual(value["split_ratio"], 2.0)
        self.assertEqual(value["cash_dividend"], 0.5)
        self.assertEqual(value["mode"], "historical")
        self.assertEqual(value["available_at"], timestamp(f"{DAYS[0]}T21:01:00Z"))

    def test_forward_bar_records_receipt_time_not_imported_claim(self):
        received = f"{DAYS[1]}T22:00:00.000000Z"
        with patch("jevtrader.market.utc_now", return_value=received):
            value = normalize_bar(raw_bar(available_at="2000-01-01T00:00:00Z"), mode="forward")
        self.assertEqual(value["available_at"], received)

    def test_bad_ohlc_volume_dividend_and_split_are_rejected(self):
        for change in (
            {"open": 0},
            {"low": 101},
            {"high": 99},
            {"close": 103},
            {"volume": -1},
            {"volume": True},
            {"open": float("nan")},
            {"close": float("inf")},
            {"cash_dividend": -1},
            {"split_ratio": -1},
            {"split_ratio": "0"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_bar(raw_bar(**change))

    def test_explicit_zero_split_must_not_be_treated_as_missing(self):
        with self.assertRaises(ValueError):
            normalize_bar(raw_bar(split_ratio=0))

    def test_boolean_corporate_actions_are_not_numeric_defaults(self):
        for change in ({"split_ratio": False}, {"cash_dividend": False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_bar(raw_bar(**change))

    def test_bar_timestamps_require_timezone_order_and_post_close_availability(self):
        for change in (
            {"open_at": f"{DAYS[0]}T14:30:00"},
            {"close_at": f"{DAYS[0]}T14:30:00Z"},
            {"available_at": f"{DAYS[0]}T20:59:00Z"},
            {"close_at": f"{DAYS[1]}T21:00:00Z"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_bar(raw_bar(**change))

    def test_snapshot_excludes_later_bars_and_records_both_histories(self):
        self.history()
        self.add(bar("ABC", DAYS[21], price=999))
        self.add(bar("SPY", DAYS[21], price=999))
        result = snapshot(self.ledger, "ABC", f"{DAYS[20]}T22:00:00Z", STRATEGY, mode="synthetic")
        self.assertEqual(result["price"], 121.0)
        self.assertEqual(result["session"], DAYS[20])
        self.assertEqual(len(result["bar_ids"]), 42)
        self.assertNotIn(f"ABC:{DAYS[21]}", result["bar_ids"])
        self.assertAlmostEqual(result["momentum"], 121 / 101 - 1)
        self.assertAlmostEqual(result["reaction"], 121 / 120 - 221 / 220)
        self.assertEqual(result["dollar_volume"], sum(range(102, 122)) * 1000 / 20)
        self.assertEqual(result["spread"], 0.0008)

    def test_snapshot_does_not_use_bar_received_after_decision(self):
        self.history()
        self.add(bar("ABC", DAYS[21], price=999, available_at=f"{DAYS[22]}T22:00:00Z"))
        self.add(bar("SPY", DAYS[21], price=999, available_at=f"{DAYS[22]}T22:00:00Z"))
        result = snapshot(self.ledger, "ABC", f"{DAYS[21]}T22:00:00Z", STRATEGY, mode="synthetic")
        self.assertEqual(result["session"], DAYS[20])
        self.assertEqual(result["price"], 121)

    def test_snapshot_rejects_misaligned_benchmark_history(self):
        for session in DAYS[:21]:
            self.add(bar("ABC", session))
        for session in DAYS[1:22]:
            self.add(bar("SPY", session))
        with self.assertRaisesRegex(ValueError, "do not align"):
            snapshot(self.ledger, "ABC", f"{DAYS[21]}T22:00:00Z", STRATEGY, mode="synthetic")

    def test_snapshot_requires_21_visible_bars_and_fresh_data(self):
        self.history(count=20)
        with self.assertRaisesRegex(ValueError, "21 visible"):
            snapshot(self.ledger, "ABC", f"{DAYS[19]}T22:00:00Z", STRATEGY, mode="synthetic")
        self.add(bar("ABC", DAYS[20]))
        self.add(bar("SPY", DAYS[20]))
        with self.assertRaisesRegex(ValueError, "stale"):
            snapshot(self.ledger, "ABC", "2026-03-01T22:00:00Z", STRATEGY, mode="synthetic")

    def test_forward_snapshot_cannot_use_historical_bars(self):
        self.history(mode="historical")
        with self.assertRaisesRegex(ValueError, "matching mode"):
            snapshot(self.ledger, "ABC", f"{DAYS[20]}T22:00:00Z", STRATEGY, mode="forward")

    def test_forward_snapshot_accepts_observed_forward_bars(self):
        self.history(mode="forward")
        result = snapshot(self.ledger, "ABC", f"{DAYS[20]}T22:00:00Z", STRATEGY, mode="forward")
        self.assertEqual(result["session"], DAYS[20])

    def test_snapshot_daily_returns_account_for_splits_and_dividends(self):
        for index, session in enumerate(DAYS[:21]):
            price = 50 if index == 20 else 100
            self.add(
                bar(
                    "ABC",
                    session,
                    price=price,
                    close=price,
                    split_ratio=2 if index == 20 else 1,
                    cash_dividend=1 if index == 20 else 0,
                )
            )
            self.add(bar("SPY", session, price=100, close=100))
        result = snapshot(self.ledger, "ABC", f"{DAYS[20]}T22:00:00Z", STRATEGY, mode="synthetic")
        self.assertAlmostEqual(result["momentum"], 0.02)
        self.assertAlmostEqual(result["reaction"], 0.02)

    def test_outcome_enters_strictly_after_decision_at_next_open(self):
        self.history(count=4)
        # A decision timestamp exactly at an open is too late to claim that fill.
        forecast = self.forecast(decision_at=f"{DAYS[0]}T14:30:00Z")
        result = outcome(self.ledger, forecast, f"{DAYS[3]}T22:00:00Z")
        self.assertEqual(result["entry_at"], timestamp(f"{DAYS[1]}T14:30:00Z"))
        self.assertEqual(result["outcome_at"], timestamp(f"{DAYS[3]}T21:00:00Z"))
        self.assertEqual(result["entry_price"], 101)
        self.assertEqual(result["exit_price"], 104)
        self.assertAlmostEqual(result["gross_return"], 104 / 101 - 1)
        self.assertAlmostEqual(result["benchmark_return"], 204 / 201 - 1)
        self.assertAlmostEqual(result["target"], 104 / 101 - 204 / 201)
        self.assertEqual(result["horizon_sessions"], 3)

    def test_outcome_never_shortens_horizon_when_final_session_is_unavailable(self):
        self.history(count=3)
        self.assertIsNone(outcome(self.ledger, self.forecast(), f"{DAYS[4]}T22:00:00Z"))
        self.add(bar("SPY", DAYS[3]))
        self.assertIsNone(outcome(self.ledger, self.forecast(), f"{DAYS[4]}T22:00:00Z"))

    def test_outcome_does_not_skip_a_missing_stock_session(self):
        for session in DAYS[:5]:
            self.add(bar("SPY", session))
            if session != DAYS[2]:
                self.add(bar("ABC", session))
        self.assertIsNone(outcome(self.ledger, self.forecast(), f"{DAYS[4]}T22:00:00Z"))

    def test_outcome_waits_for_close_and_receipt_of_every_bar(self):
        for index, session in enumerate(DAYS[:4]):
            self.add(bar("SPY", session))
            changes = {"available_at": f"{DAYS[4]}T22:00:00Z"} if index == 3 else {}
            self.add(bar("ABC", session, **changes))
        forecast = self.forecast()
        self.assertIsNone(outcome(self.ledger, forecast, f"{DAYS[3]}T20:59:00Z"))
        self.assertIsNone(outcome(self.ledger, forecast, f"{DAYS[3]}T22:00:00Z"))
        result = outcome(self.ledger, forecast, f"{DAYS[4]}T22:00:00Z")
        self.assertEqual(result["label_available_at"], timestamp(f"{DAYS[4]}T22:00:00Z"))

    def test_outcome_split_dividend_accounting_excludes_entry_ex_date(self):
        # Enter at raw $100 after an entry-day split/ex-dividend event. Entry
        # investors get neither the earlier split factor nor that $7 dividend.
        self.add(bar("ABC", DAYS[1], price=100, close=100, split_ratio=3, cash_dividend=7))
        self.add(bar("ABC", DAYS[2], price=50, close=50, split_ratio=2, cash_dividend=1))
        self.add(bar("ABC", DAYS[3], price=55, close=55, cash_dividend=0.5))
        for session in DAYS[1:4]:
            self.add(bar("SPY", session, price=200, close=200))
        result = outcome(self.ledger, self.forecast(), f"{DAYS[3]}T22:00:00Z")
        # Two shares worth $110, plus $2 and $1 cash dividends, on $100 entry.
        self.assertAlmostEqual(result["gross_return"], 0.13)
        self.assertEqual(result["benchmark_return"], 0.0)
        self.assertAlmostEqual(result["target"], 0.13)
        self.assertEqual(result["entry_price"], 100)
        self.assertEqual(result["exit_price"], 55)

    def test_outcome_does_not_use_stock_open_before_decision(self):
        for session in DAYS[1:4]:
            self.add(bar("SPY", session))
            changes = {"open_at": f"{DAYS[1]}T13:30:00Z"} if session == DAYS[1] else {}
            self.add(bar("ABC", session, **changes))
        forecast = self.forecast(decision_at=f"{DAYS[1]}T14:00:00Z")
        self.assertIsNone(outcome(self.ledger, forecast, f"{DAYS[3]}T22:00:00Z"))

    def test_outcome_does_not_skip_missing_benchmark_session(self):
        for session in DAYS[1:5]:
            self.add(bar("ABC", session))
            if session != DAYS[2]:
                self.add(bar("SPY", session))
        self.assertIsNone(outcome(self.ledger, self.forecast(), f"{DAYS[4]}T22:00:00Z"))

    def test_forward_outcome_cannot_be_resolved_with_historical_or_synthetic_bars(self):
        self.history(count=4, mode="historical")
        forecast = self.forecast(mode="forward")
        self.assertIsNone(outcome(self.ledger, forecast, f"{DAYS[3]}T22:00:00Z"))


class ImportBarsTests(unittest.TestCase):
    """``import_bars`` (market.py): all-or-nothing, idempotent, first receipt kept (P0-20)."""

    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bars.csv"

    def write(self, rows):
        with self.path.open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return str(self.path)

    def test_import_counts_new_bars_and_reimport_is_a_no_op(self):
        path = self.write([raw_bar("ABC", day) for day in DAYS[:3]])
        self.assertEqual(import_bars(self.ledger, path), 3)
        self.assertEqual(import_bars(self.ledger, path), 0)
        stored = self.ledger.get("bars", f"ABC:{DAYS[0]}")
        self.assertEqual(stored["mode"], "historical")
        self.assertEqual(stored["available_at"], timestamp(f"{DAYS[0]}T21:01:00Z"))
        self.assertEqual(self.ledger.counts()["bars"], 3)

    def test_one_invalid_row_rejects_the_whole_file(self):
        rows = [raw_bar("ABC", DAYS[0]), raw_bar("ABC", DAYS[1], low=500)]
        with self.assertRaisesRegex(ValueError, "Inconsistent OHLC range"):
            import_bars(self.ledger, self.write(rows))
        self.assertEqual(self.ledger.counts(), {})

    def test_invalid_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid bar mode"):
            import_bars(self.ledger, self.write([raw_bar()]), mode="live")
        self.assertEqual(self.ledger.counts(), {})

    def test_changed_historical_bar_is_an_immutable_conflict(self):
        import_bars(self.ledger, self.write([raw_bar(price=100.0)]))
        with self.assertRaisesRegex(ValueError, "Immutable record conflict"):
            import_bars(self.ledger, self.write([raw_bar(price=101.0)]))

    def test_forward_reimport_keeps_the_first_receipt_time(self):
        path = self.write([raw_bar()])
        first, later = f"{DAYS[0]}T21:05:00Z", f"{DAYS[0]}T23:00:00Z"
        with patch("jevtrader.market.utc_now", return_value=first):
            self.assertEqual(import_bars(self.ledger, path, mode="forward"), 1)
        with patch("jevtrader.market.utc_now", return_value=later):
            self.assertEqual(import_bars(self.ledger, path, mode="forward"), 0)
        stored = self.ledger.get("bars", f"ABC:{DAYS[0]}")
        self.assertEqual((stored["mode"], stored["available_at"]), ("forward", first))


if __name__ == "__main__":
    unittest.main()
