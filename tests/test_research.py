import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from jevtrader.research import fit_model, predict, walk_forward


def sample_rows(count=60, lag=1):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        feature = float(index % 7 - 3)
        rows.append(
            {
                "event_id": f"event-{index:03d}",
                "symbol": f"T{index % 5}",
                "decision_at": (start + timedelta(days=index)).isoformat(),
                "outcome_at": (start + timedelta(days=index + lag)).isoformat(),
                "features": [feature, 1.0, feature / 3, 0.2, feature / 50, 0.0, 0.03, 0.001],
                "target": feature / 100,
                "extractor_key": "frozen-v1",
                "mode": "forward",
            }
        )
    return rows


class ResearchTests(unittest.TestCase):
    def test_future_and_cutoff_equal_labels_rejected(self):
        rows = sample_rows(5)
        for cutoff in (rows[-1]["outcome_at"], rows[-1]["decision_at"]):
            with self.assertRaisesRegex(ValueError, "strictly before"):
                fit_model(rows, cutoff=cutoff, min_samples=2)

    def test_train_only_scaling_and_constant_features(self):
        rows = sample_rows(5)
        model = fit_model(rows, cutoff="2024-02-01T00:00:00Z", min_samples=2)
        expected = np.array([row["features"] for row in rows]).mean(axis=0)
        np.testing.assert_allclose(model["mean"], expected)
        self.assertEqual(model["scale"][1], 1.0)
        before = copy.deepcopy(model)
        predict(model, [10000.0] * 8)
        self.assertEqual(before, model)
        self.assertTrue(np.isfinite(predict(model, rows[0]["features"])))

    def test_float_noise_in_constant_feature_cannot_move_predictions(self):
        # Means of repeated non-dyadic floats are inexact (30 x 0.8 has std ~2e-16),
        # which must not turn a constant training feature into a huge hidden weight.
        for value in (0.8, 0.35, 0.001):
            with self.subTest(value=value):
                rows = sample_rows(30)
                for index, row in enumerate(rows):
                    row["features"][3] = value
                    row["target"] = ((index * 37) % 11 - 5) / 1000
                model = fit_model(rows, cutoff="2024-03-01T00:00:00Z", min_samples=30)
                base = rows[0]["features"]
                changed = [*base[:3], 0.05, *base[4:]]
                self.assertAlmostEqual(predict(model, base), predict(model, changed), places=12)

    def test_no_penalty_on_intercept(self):
        rows = sample_rows(5)
        for row in rows:
            row["features"] = [1.0] * 8
            row["target"] = 0.123
        model = fit_model(rows, cutoff="2024-02-01T00:00:00Z", min_samples=2, alpha=1000)
        self.assertAlmostEqual(predict(model, [1.0] * 8), 0.123)

    def test_invalid_rows_and_features(self):
        with self.assertRaises(ValueError):
            fit_model([], cutoff="2024-02-01T00:00:00Z")
        with self.assertRaises(ValueError):
            walk_forward([])
        for bad in (float("nan"), float("inf"), True):
            rows = sample_rows(5)
            rows[0]["features"][0] = bad
            with self.assertRaises(ValueError):
                fit_model(rows, cutoff="2024-02-01T00:00:00Z", min_samples=2)
        rows = sample_rows(5)
        rows[1]["event_id"] = rows[0]["event_id"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            walk_forward(rows, min_train=2)
        rows = sample_rows(5)
        rows[0]["extractor_key"] = "different-model"
        with self.assertRaisesRegex(ValueError, "mixed"):
            fit_model(rows, cutoff="2024-02-01T00:00:00Z", min_samples=2)
        model = fit_model(sample_rows(5), cutoff="2024-02-01T00:00:00Z", min_samples=2)
        for features in ([1.0], [float("nan")] * 8):
            with self.assertRaises(ValueError):
                predict(model, features)

    def test_stable_identity_and_exact_data_hashing(self):
        rows = sample_rows(5)
        kwargs = dict(cutoff="2024-02-01T00:00:00Z", min_samples=2)
        first = fit_model(rows, **kwargs)
        self.assertEqual(first, fit_model(list(reversed(rows)), **kwargs))
        self.assertEqual(first["training_modes"], ["forward"])
        json.dumps(first, allow_nan=False)
        changed = copy.deepcopy(rows)
        changed[0]["target"] += 0.00001
        self.assertNotEqual(first["model_id"], fit_model(changed, **kwargs)["model_id"])

    def test_mature_labels_only_and_timestamp_groups_unsplit(self):
        rows = sample_rows(35, lag=4)
        rows[15]["decision_at"] = rows[14]["decision_at"]
        rows[16]["decision_at"] = rows[14]["decision_at"]
        report = walk_forward(rows, min_train=5, test_size=5)
        by_id = {row["event_id"]: row for row in rows}
        group_blocks = []
        for block_index, block in enumerate(report["blocks"]):
            cutoff = datetime.fromisoformat(block["cutoff"].replace("Z", "+00:00"))
            self.assertGreater(block["purged_count"], 0)
            for event_id in block["training_event_ids"]:
                self.assertLess(datetime.fromisoformat(by_id[event_id]["outcome_at"]), cutoff)
            if set(block["test_event_ids"]) & {"event-014", "event-015", "event-016"}:
                group_blocks.append(block_index)
                self.assertTrue(
                    {"event-014", "event-015", "event-016"}.issubset(block["test_event_ids"])
                )
        self.assertEqual(len(group_blocks), 1)
        self.assertEqual(
            len(report["predictions"]), len({row["event_id"] for row in report["predictions"]})
        )

    def test_test_outcomes_cannot_change_same_block_prediction(self):
        rows = sample_rows(30)
        first = walk_forward(rows, min_train=5, test_size=5)
        changed = copy.deepcopy(rows)
        first_ids = first["blocks"][0]["test_event_ids"]
        for row in changed:
            if row["event_id"] in first_ids:
                row["target"] = 1000.0
        second = walk_forward(changed, min_train=5, test_size=5)
        for left, right in zip(first["predictions"], second["predictions"]):
            if left["event_id"] in first_ids:
                self.assertEqual(left["semantic"]["prediction"], right["semantic"]["prediction"])
                self.assertEqual(left["numerical"]["prediction"], right["numerical"]["prediction"])

    def test_numerical_baseline_ignores_semantics(self):
        rows = sample_rows(30)
        first = walk_forward(rows, min_train=5, test_size=5)
        changed = copy.deepcopy(rows)
        for index, row in enumerate(changed):
            row["features"][:4] = [float(index * 100), -42.0, float(index**2), 0.99]
        second = walk_forward(changed, min_train=5, test_size=5)
        for left, right in zip(first["predictions"], second["predictions"]):
            self.assertAlmostEqual(
                left["numerical"]["prediction"], right["numerical"]["prediction"]
            )

    def test_costs_abstention_and_short_disclosure(self):
        rows = sample_rows(20)
        for row in rows:
            row["target"] = -0.01
        long_only = walk_forward(rows, min_train=5, test_size=5)
        self.assertEqual(long_only["strategies"]["semantic"]["trade_count"], 0)
        report = walk_forward(rows, min_train=5, test_size=5, allow_short=True)
        self.assertEqual(report["strategies"]["semantic"]["short_count"], report["evaluated_count"])
        self.assertAlmostEqual(report["strategies"]["semantic"]["mean_net_return"], 0.008)
        self.assertTrue(any("borrow" in item for item in report["limitations"]))
        self.assertNotIn("sharpe", report)
        json.dumps(report, allow_nan=False)

    def test_insufficient_samples_report_no_test_metrics(self):
        rows = sample_rows(5)
        report = walk_forward(rows, min_train=30)
        self.assertEqual(report["evaluated_count"], 0)
        self.assertIsNone(report["strategies"]["semantic"]["mean_net_return"])
        with self.assertRaisesRegex(ValueError, "at least"):
            fit_model(rows, cutoff="2024-02-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
