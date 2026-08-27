from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7FastAuthoritySentinelContractTest(unittest.TestCase):
    def test_fast_shadow_matches_current_operator_authority(self) -> None:
        directives = json.loads((ROOT / "config" / "operator_directives.json").read_text(encoding="utf-8"))
        auth = directives["paper_v7_authorization"]
        shadow = json.loads((ROOT / "config" / "fast_arb_v7_shadow.json").read_text(encoding="utf-8"))
        policy = json.loads((ROOT / "config" / "fast_arb_policy.json").read_text(encoding="utf-8"))

        self.assertTrue(shadow["paper_only"])
        self.assertFalse(shadow["authenticated_execution"])
        self.assertFalse(policy["real_order_submission"])
        self.assertEqual(shadow["market_limit"], auth["market_limit"])
        self.assertEqual(shadow["min_liquidity"], auth["min_liquidity"])
        self.assertEqual(shadow["min_net_edge"], auth["min_net_edge"])
        self.assertEqual(policy["min_net_edge"], auth["min_net_edge"])
        self.assertEqual(shadow["uncertainty_penalty"], auth["uncertainty_penalty"])
        self.assertEqual(policy["external_uncertainty_penalty"], 0.0)
        self.assertLessEqual(shadow["fractional_kelly"], auth["fractional_kelly_ceiling"])
        self.assertEqual(shadow["max_market_fraction"], auth["max_market_fraction"])
        self.assertEqual(shadow["max_event_fraction"], auth["max_event_fraction"])
        self.assertEqual(shadow["max_gross_fraction"], auth["max_gross_fraction"])
        self.assertEqual(shadow["max_drawdown"], auth["max_drawdown"])
        self.assertFalse(auth["fixed_dollar_trade_cap_enabled"])
        self.assertEqual(policy["max_notional_usd"], auth["max_trade_usd_compatibility_sentinel"])

    def test_compatibility_sentinel_is_not_spendable_notional(self) -> None:
        source = (ROOT / "src" / "fast_runtime" / "part4.inc").read_text(encoding="utf-8")
        for token in (
            "capital_fraction_ceiling",
            "config.starting_capital * capital_fraction_ceiling",
            "policy.max_notional_usd > capital_ceiling",
            "policy.max_notional_usd = capital_ceiling",
            "invalid PAPER capital ceiling for fast shadow",
        ):
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_registered_fast_shadow_uses_authorized_breadth(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "fast-arb-hourly.yml").read_text(encoding="utf-8")
        self.assertIn("--config config/fast_arb_v7_shadow.json", workflow)
        self.assertIn("--markets 1000", workflow)
        self.assertIn("--min-liquidity 2", workflow)
        self.assertNotIn("--markets 600", workflow)
        self.assertNotIn("--min-liquidity 10", workflow)
        self.assertIn("max_trade_usd_compatibility_sentinel", workflow)
        self.assertIn("capital_required", workflow)


if __name__ == "__main__":
    unittest.main()
