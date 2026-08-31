from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from v7_micro_target import (
    TARGET_SEMANTICS_VERSION, label_matured_horizon_probes,
    label_matured_samples,
)


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

    def test_multi_horizon_probes_are_causal_and_do_not_replace_primary_target(self) -> None:
        samples = [
            sample(100, yes_bid=0.49, yes_ask=0.50),
            sample(110, yes_bid=0.52, yes_ask=0.53),
            sample(120, yes_bid=0.58, yes_ask=0.59),
        ]
        stats = label_matured_horizon_probes(
            samples, now=130, horizons_seconds=(10, 20),
            max_target_staleness_seconds=0,
        )
        self.assertIsNone(samples[0]["y"])
        targets = samples[0]["research_horizon_targets"]
        self.assertAlmostEqual(targets["10"]["YES"]["net_pnl_per_share"], 0.02)
        self.assertAlmostEqual(targets["20"]["YES"]["net_pnl_per_share"], 0.08)
        self.assertEqual(targets["10"]["target_observation_ts"], 110)
        self.assertEqual(targets["20"]["target_observation_ts"], 120)
        self.assertEqual(stats["execution_authority"], "RESEARCH_ONLY_ZERO_AUTHORITY")

    def test_primary_target_is_reused_without_duplicate_state_payload(self) -> None:
        samples = [
            sample(100, yes_bid=0.49, yes_ask=0.50),
            sample(110, yes_bid=0.52, yes_ask=0.53),
        ]
        label_matured_samples(
            samples, now=110, horizon_seconds=10,
            max_target_staleness_seconds=0,
        )
        expected = samples[0]["target_execution"]
        for key in ("yes_bids", "yes_asks", "no_bids", "no_asks"):
            samples[0].pop(key, None)
        stats = label_matured_horizon_probes(
            samples, now=120, horizons_seconds=(10,),
            max_target_staleness_seconds=0,
        )
        self.assertNotIn("research_horizon_targets", samples[0])
        self.assertEqual(stats["horizons"]["10"]["primary_target_reused"], 1)
        self.assertEqual(samples[0]["target_execution"]["YES"], expected["YES"])


if __name__ == "__main__":
    unittest.main()
