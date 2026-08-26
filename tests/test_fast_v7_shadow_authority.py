from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_gate(name: str):
    path = ROOT / "scripts/v7_fast_shadow_evidence_gate.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FastV7ShadowAuthorityTest(unittest.TestCase):
    def test_shadow_config_and_policy_match_current_operator_authority(self) -> None:
        directive = json.loads((ROOT / "config/operator_directives.json").read_text(encoding="utf-8"))
        auth = directive["paper_v7_authorization"]
        config = json.loads((ROOT / "config/fast_arb_v7_shadow.json").read_text(encoding="utf-8"))
        policy = json.loads((ROOT / "config/fast_arb_policy.json").read_text(encoding="utf-8"))
        self.assertTrue(config["paper_only"])
        self.assertFalse(config["authenticated_execution"])
        self.assertTrue(config["scan_only"])
        self.assertEqual(config["market_limit"], auth["market_limit"])
        self.assertEqual(config["min_liquidity"], auth["min_liquidity"])
        self.assertEqual(config["min_net_edge"], auth["min_net_edge"])
        self.assertEqual(config["uncertainty_penalty"], auth["uncertainty_penalty"])
        self.assertEqual(config["fractional_kelly"], auth["fractional_kelly_ceiling"])
        self.assertEqual(config["max_market_fraction"], auth["max_market_fraction"])
        self.assertEqual(config["max_event_fraction"], auth["max_event_fraction"])
        self.assertEqual(config["max_gross_fraction"], auth["max_gross_fraction"])
        self.assertEqual(config["max_drawdown"], auth["max_drawdown"])
        self.assertFalse(policy["real_order_submission"])
        self.assertEqual(policy["min_net_edge"], auth["min_net_edge"])
        self.assertEqual(policy["external_uncertainty_penalty"], auth["uncertainty_penalty"])
        self.assertEqual(policy["max_notional_usd"], auth["hard_arb_max_trade_usd_compatibility_sentinel"])
        self.assertTrue(policy["require_per_token_receive_timestamp"])
        self.assertTrue(policy["require_per_token_exchange_timestamp"])
        self.assertLessEqual(policy["max_token_age_ms"], 5000)
        self.assertLessEqual(policy["max_cross_leg_skew_ms"], 1000)

    def test_hourly_shadow_uses_v7_config_and_authorized_breadth(self) -> None:
        workflow = (ROOT / ".github/workflows/fast-arb-hourly.yml").read_text(encoding="utf-8")
        self.assertIn("config/fast_arb_v7_shadow.json", workflow)
        self.assertIn("--markets 1000", workflow)
        self.assertIn("--min-liquidity 2", workflow)
        self.assertIn("--snapshot-refresh-seconds 5", workflow)
        self.assertIn("v7_fast_shadow_evidence_gate.py", workflow)
        for legacy in ("paper_v4", "paper_v5", "paper_v6"):
            self.assertNotIn(legacy, workflow)

    def test_gate_fails_closed_without_per_token_freshness(self) -> None:
        module = load_gate("v7_gate_missing")
        status = {"ws_messages": 10, "book_updates": 10, "feed_stale_ms": 5, "rest_resyncs": 2}
        policy = {"strict_multi_leg_evidence_required": True, "max_token_age_ms": 5000, "max_cross_leg_skew_ms": 1000}
        rows = [{"hard_arbitrage": "1", "executable": "1", "id": "x", "decision_ts_ms": "10000", "per_token_freshness": ""}]
        report, valid = module.assess(status, rows, policy)
        self.assertFalse(valid)
        self.assertEqual(report["state"], "STRICT_HARD_EVIDENCE_BLOCKED")
        self.assertEqual(report["failures"][0]["reason"], "per_token_freshness_not_serialized")

    def test_gate_accepts_fresh_synchronized_per_token_proof(self) -> None:
        module = load_gate("v7_gate_ok")
        status = {"ws_messages": 10, "book_updates": 10, "feed_stale_ms": 5, "rest_resyncs": 2}
        policy = {"strict_multi_leg_evidence_required": True, "max_token_age_ms": 5000, "max_cross_leg_skew_ms": 1000}
        rows = [{"hard_arbitrage": "1", "executable": "1", "id": "x", "decision_ts_ms": "10000", "per_token_freshness": "a:9000:9500|b:9100:9600"}]
        report, valid = module.assess(status, rows, policy)
        self.assertTrue(valid)
        self.assertEqual(report["state"], "STRICT_HARD_EVIDENCE_VALID")


if __name__ == "__main__":
    unittest.main()
