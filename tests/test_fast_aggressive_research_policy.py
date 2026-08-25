from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FastAggressiveResearchPolicyTest(unittest.TestCase):
    def test_research_policy_lowers_only_the_positive_edge_gate(self) -> None:
        base = json.loads((ROOT / "config" / "fast_arb_policy.json").read_text(encoding="utf-8"))
        research = json.loads(
            (ROOT / "config" / "fast_arb_policy_research_aggressive.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(research["mode"], "shadow")
        self.assertFalse(research["real_order_submission"])
        self.assertGreater(research["min_net_edge"], 0.0)
        self.assertLess(research["min_net_edge"], base["min_net_edge"])
        self.assertEqual(research["slippage_bps"], base["slippage_bps"])
        self.assertEqual(research["latency_penalty_bps"], base["latency_penalty_bps"])
        self.assertEqual(research["maker_one_sided_penalty_bps"], base["maker_one_sided_penalty_bps"])
        self.assertEqual(research["conversion_fixed_cost_usd"], base["conversion_fixed_cost_usd"])
        self.assertLessEqual(research["max_notional_usd"], base["max_notional_usd"])

    def test_pull_request_probe_is_broader_but_production_schedule_is_unchanged(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "fast-arb-hourly.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("markets=600", workflow)
        self.assertIn("min_liquidity=100", workflow)
        self.assertIn("snapshot_refresh_seconds=20", workflow)
        self.assertIn('if [[ "$GITHUB_EVENT_NAME" == "pull_request" ]]', workflow)
        self.assertIn("markets=1000", workflow)
        self.assertIn("min_liquidity=25", workflow)
        self.assertIn("snapshot_refresh_seconds=10", workflow)
        self.assertIn("runtime_policy=config/fast_arb_policy_research_aggressive.json", workflow)
        self.assertIn("--policy \"$runtime_policy\"", workflow)
        self.assertNotIn("real_order_submission=true", workflow)


if __name__ == "__main__":
    unittest.main()
