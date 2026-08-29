from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from v7_micro_target import label_matured_samples


class MicroTargetCausalityTest(unittest.TestCase):
    def test_never_borrows_observation_after_horizon(self) -> None:
        samples = [
            {"ts": 100, "market_id": "m", "mid": 0.50, "y": None},
            {"ts": 105, "market_id": "m", "mid": 0.51, "y": None},
            {"ts": 111, "market_id": "m", "mid": 0.70, "y": None},
        ]
        stats = label_matured_samples(
            samples,
            now=120,
            horizon_seconds=10,
            max_target_staleness_seconds=5,
        )
        self.assertAlmostEqual(samples[0]["y"], 0.01)
        self.assertEqual(samples[0]["target_observation_ts"], 105)
        self.assertEqual(samples[0]["target_staleness_seconds"], 5)
        self.assertGreaterEqual(stats["newly_labeled"], 1)

    def test_post_horizon_only_observation_leaves_origin_unlabeled(self) -> None:
        samples = [
            {"ts": 100, "market_id": "m", "mid": 0.50, "y": None},
            {"ts": 111, "market_id": "m", "mid": 0.70, "y": None},
        ]
        stats = label_matured_samples(
            samples,
            now=120,
            horizon_seconds=10,
            max_target_staleness_seconds=10,
        )
        self.assertIsNone(samples[0]["y"])
        self.assertEqual(stats["missing_pre_horizon_observation"], 1)

    def test_stale_pre_horizon_observation_fails_closed(self) -> None:
        samples = [
            {"ts": 100, "market_id": "m", "mid": 0.50, "y": None},
            {"ts": 102, "market_id": "m", "mid": 0.51, "y": None},
        ]
        stats = label_matured_samples(
            samples,
            now=120,
            horizon_seconds=10,
            max_target_staleness_seconds=3,
        )
        self.assertIsNone(samples[0]["y"])
        self.assertEqual(stats["stale_pre_horizon_observation"], 1)


if __name__ == "__main__":
    unittest.main()
