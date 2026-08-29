from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_cross_sectional_rank_core as core
import v7_cross_sectional_rank_forward as forward


class ForwardRankingObserverTest(unittest.TestCase):
    def test_rank_rows_freeze_top_bottom_quintiles(self) -> None:
        scored = [
            core.ScoreRow(
                ts=100,
                market_id=f"m{i}",
                event_id=f"e{i}",
                group="g",
                probability=0.5,
                features=(0.0,) * len(core.FEATURE_NAMES),
                predicted_logit_move=float(i - 5),
                sigma_logit=0.1,
            )
            for i in range(10)
        ]
        rows = forward.prediction_rank_rows(
            scored,
            {f"m{i}": f"t{i}" for i in range(10)},
            {f"m{i}": 100000 for i in range(10)},
            horizon_minutes=120,
            feature_ts=100,
            published_ts=110,
            exit_buffer_seconds=3600,
        )
        self.assertEqual(len(rows), 10)
        self.assertEqual(sum(row["tail"] == "BOTTOM" for row in rows), 2)
        self.assertEqual(sum(row["tail"] == "TOP" for row in rows), 2)
        self.assertEqual({row["market_id"] for row in rows if row["tail"] == "BOTTOM"}, {"m0", "m1"})
        self.assertEqual({row["market_id"] for row in rows if row["tail"] == "TOP"}, {"m8", "m9"})

    def test_maturity_uses_stored_point_in_time_universe(self) -> None:
        predictions = []
        histories = {}
        due = 7200
        for i in range(20):
            pred = -1.0 + 2.0 * i / 19.0
            future = 0.40 + 0.01 * i
            predictions.append(
                {
                    "market_id": f"m{i}",
                    "event_id": f"e{i}",
                    "group": "g",
                    "yes_token": f"t{i}",
                    "feature_ts": 0,
                    "published_ts": 10,
                    "due_ts": due,
                    "horizon_minutes": 120,
                    "origin_probability": 0.5,
                    "predicted_logit_move": pred,
                    "rank": i,
                    "rank_fraction": i / 19,
                    "tail": "BOTTOM" if i < 4 else "TOP" if i >= 16 else "MIDDLE",
                    "end_ts": 999999,
                }
            )
            histories[f"m{i}"] = {due: future}
        section = {
            "section_id": "xsec-forward:120m:0",
            "feature_ts": 0,
            "published_ts": 10,
            "due_ts": due,
            "horizon_minutes": 120,
            "predictions": predictions,
        }
        result = forward.mature_section(
            section,
            histories,
            min_cross_section=20,
            group_weight=0.5,
            min_group_size=5,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["label_count"], 20)
        self.assertAlmostEqual(result["label_coverage"], 1.0)
        self.assertGreater(result["rank_ic"], 0.99)
        self.assertGreater(result["top_bottom_logit_spread"], 0.0)

    def test_maturity_fails_closed_on_missing_forward_labels(self) -> None:
        predictions = [
            {
                "market_id": f"m{i}",
                "event_id": f"e{i}",
                "group": "g",
                "yes_token": f"t{i}",
                "origin_probability": 0.5,
                "predicted_logit_move": float(i),
                "tail": "TOP" if i >= 16 else "BOTTOM" if i < 4 else "MIDDLE",
            }
            for i in range(20)
        ]
        section = {
            "section_id": "s",
            "feature_ts": 0,
            "published_ts": 10,
            "due_ts": 7200,
            "horizon_minutes": 120,
            "predictions": predictions,
        }
        histories = {f"m{i}": {7200: 0.5} for i in range(10)}
        self.assertIsNone(
            forward.mature_section(
                section,
                histories,
                min_cross_section=10,
                group_weight=0.5,
                min_group_size=5,
            )
        )

    def test_forward_gate_requires_time_and_stable_tail(self) -> None:
        good = {
            "days": 20,
            "completed_sections": 50,
            "median_rank_ic": 0.04,
            "positive_rank_ic_fraction": 0.60,
            "median_top_bottom_logit_spread": 0.002,
            "positive_top_bottom_fraction": 0.60,
        }
        ok, reasons = forward.forward_gate(good)
        self.assertTrue(ok, reasons)
        bad = dict(good)
        bad["positive_top_bottom_fraction"] = 0.40
        ok, reasons = forward.forward_gate(bad)
        self.assertFalse(ok)
        self.assertIn("tail_stability", reasons)

    def test_state_never_carries_execution_authority(self) -> None:
        state = forward.normalize_state(
            {
                "schema": forward.SCHEMA,
                "open_sections": [],
                "completed_sections": [],
                "invalid_sections": [],
                "submitted_orders": 999,
                "authenticated_execution": True,
            }
        )
        self.assertEqual(state["submitted_orders"], 0)
        self.assertFalse(state["authenticated_execution"])
        self.assertTrue(state["paper_only"])
        self.assertTrue(state["research_only"])


if __name__ == "__main__":
    unittest.main()
