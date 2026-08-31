from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from v7_micro_target import TARGET_SEMANTICS_VERSION, label_matured_samples


def sample(ts: int, *, yes_bid: float, yes_ask: float) -> dict:
    no_bid, no_ask = 1.0 - yes_ask, 1.0 - yes_bid
    return {
        "ts": ts, "market_id": "m", "mid": 0.5 * (yes_bid + yes_ask),
        "y": None, "target_semantics_version": TARGET_SEMANTICS_VERSION,
        "label_probe_shares": 5.0, "label_horizon_seconds": 10,
        "round_trip_slippage_bps": 0.0, "adverse_markout_bps": 0.0,
        "capital_cost_bps_per_hour": 0.0,
        "fee": {"authoritative": True, "rate": 0.0, "exponent": 1.0},
        "yes_bids": [[yes_bid, 100.0]], "yes_asks": [[yes_ask, 100.0]],
        "no_bids": [[no_bid, 100.0]], "no_asks": [[no_ask, 100.0]],
    }


class MicroTargetCausalityTest(unittest.TestCase):
    def test_never_borrows_observation_after_horizon(self) -> None:
        samples = [
            sample(100, yes_bid=0.49, yes_ask=0.50),
            sample(105, yes_bid=0.52, yes_ask=0.53),
            sample(111, yes_bid=0.70, yes_ask=0.71),
        ]
        stats = label_matured_samples(
            samples,
            now=120,
            horizon_seconds=10,
            max_target_staleness_seconds=5,
        )
        self.assertGreater(samples[0]["y"], 0.0)
        self.assertEqual(samples[0]["target_action"], "BUY_YES")
        self.assertAlmostEqual(samples[0]["yes_executable_net_pnl_per_share"], 0.02)
        self.assertEqual(samples[0]["target_observation_ts"], 105)
        self.assertEqual(samples[0]["target_staleness_seconds"], 5)
        self.assertGreaterEqual(stats["newly_labeled"], 1)

    def test_post_horizon_only_observation_leaves_origin_unlabeled(self) -> None:
        samples = [
            sample(100, yes_bid=0.49, yes_ask=0.50),
            sample(111, yes_bid=0.70, yes_ask=0.71),
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
            sample(100, yes_bid=0.49, yes_ask=0.50),
            sample(102, yes_bid=0.52, yes_ask=0.53),
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
