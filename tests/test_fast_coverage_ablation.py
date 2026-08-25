from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FastCoverageAblationTest(unittest.TestCase):
    def test_pr_ablation_broadens_coverage_without_lowering_edge_floor(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "fast-arb-hourly.yml").read_text(
            encoding="utf-8"
        )
        policy = json.loads(
            (ROOT / "config" / "fast_arb_policy.json").read_text(encoding="utf-8")
        )

        self.assertEqual(policy["mode"], "shadow")
        self.assertFalse(policy["real_order_submission"])
        self.assertEqual(policy["min_net_edge"], 0.0005)

        self.assertIn("markets=600", workflow)
        self.assertIn("min_liquidity=100", workflow)
        self.assertIn("snapshot_refresh_seconds=20", workflow)
        self.assertIn('if [[ "$GITHUB_EVENT_NAME" == "pull_request" ]]', workflow)
        self.assertIn("markets=1000", workflow)
        self.assertIn("min_liquidity=25", workflow)
        self.assertIn("snapshot_refresh_seconds=10", workflow)
        self.assertIn("duration=90", workflow)
        self.assertIn("--policy config/fast_arb_policy.json", workflow)
        self.assertNotIn("fast_arb_policy_research_aggressive.json", workflow)
        self.assertNotIn("real_order_submission=true", workflow)

    def test_hard_evidence_records_per_leg_websocket_freshness(self) -> None:
        runtime_public = (ROOT / "src" / "fast_runtime" / "part2.inc").read_text(
            encoding="utf-8"
        )
        runtime_private = (ROOT / "src" / "fast_runtime" / "part3.inc").read_text(
            encoding="utf-8"
        )
        opportunity = (ROOT / "include" / "pm" / "fast_arb.hpp").read_text(
            encoding="utf-8"
        )

        self.assertIn("book_received_ms_[token] = 0", runtime_public)
        self.assertIn("book_received_ms_[token] = received_ms", runtime_private)
        self.assertIn("max_leg_book_age_ms", runtime_private)
        self.assertIn("leg_book_skew_ms", runtime_private)
        self.assertIn("annotate_hard_freshness_locked(opportunity)", runtime_private)
        self.assertIn("std::int64_t max_leg_book_age_ms = -1", opportunity)
        self.assertIn("std::int64_t leg_book_skew_ms = -1", opportunity)


if __name__ == "__main__":
    unittest.main()
