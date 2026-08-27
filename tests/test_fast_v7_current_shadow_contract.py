from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_gate():
    path = ROOT / "scripts" / "v7_fast_runtime_freshness_gate.py"
    spec = importlib.util.spec_from_file_location("v7_fast_runtime_freshness_gate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FastV7CurrentShadowContractTest(unittest.TestCase):
    def test_config_and_policy_follow_current_operator_authority(self) -> None:
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
        self.assertEqual(config["max_trade_fraction"], auth["max_trade_fraction"])
        self.assertEqual(config["max_market_fraction"], auth["max_market_fraction"])
        self.assertEqual(config["max_event_fraction"], auth["max_event_fraction"])
        self.assertEqual(config["max_gross_fraction"], auth["max_gross_fraction"])
        self.assertEqual(config["max_drawdown"], auth["max_drawdown"])
        self.assertFalse(policy["real_order_submission"])
        self.assertTrue(policy["paper_only"])
        self.assertEqual(policy["min_net_edge"], auth["min_net_edge"])
        self.assertEqual(policy["external_uncertainty_penalty"], auth["uncertainty_penalty"])
        self.assertEqual(policy["max_notional_usd"], auth["hard_arb_max_trade_usd_compatibility_sentinel"])

    def test_hourly_shadow_uses_authorized_breadth_and_current_v7_gate(self) -> None:
        workflow = (ROOT / ".github/workflows/fast-arb-hourly.yml").read_text(encoding="utf-8")
        self.assertIn("config/fast_arb_v7_shadow.json", workflow)
        self.assertIn("--markets 1000", workflow)
        self.assertIn("--min-liquidity 2", workflow)
        self.assertIn("--snapshot-refresh-seconds 5", workflow)
        self.assertIn("v7_fast_runtime_freshness_gate.py", workflow)
        for legacy in ("paper_v4", "paper_v5", "paper_v6"):
            self.assertNotIn(legacy, workflow)

    def test_runtime_gate_accepts_fresh_aggregate_row_only_as_research_evidence(self) -> None:
        module = load_gate()
        status = {
            "mode": "shadow",
            "real_order_submission": False,
            "ws_messages": 10,
            "ws_snapshot_ready_tokens": 4,
            "book_freshness_max_age_ms": 5000,
            "book_freshness_max_skew_ms": 1500,
        }
        policy = {
            "paper_only": True,
            "real_order_submission": False,
            "strict_multi_leg_evidence_required": True,
            "max_token_age_ms": 5000,
            "max_cross_leg_skew_ms": 1500,
        }
        rows = [{
            "hard_arbitrage": "1",
            "executable": "1",
            "id": "fresh",
            "exchange_ts_ms": "9000",
            "received_ts_ms": "9500",
            "decision_ts_ms": "10000",
        }]
        report, valid = module.assess(ROOT, status, rows, policy)
        self.assertTrue(valid)
        self.assertEqual(report["state"], "INTERNALLY_FRESHNESS_GATED_HARD_OBSERVATIONS")
        self.assertFalse(report["canonical_per_leg_provenance_serialized"])
        self.assertFalse(report["promotion_allowed"])

    def test_runtime_gate_rejects_stale_hard_row(self) -> None:
        module = load_gate()
        status = {
            "mode": "shadow",
            "real_order_submission": False,
            "ws_messages": 10,
            "ws_snapshot_ready_tokens": 4,
            "book_freshness_max_age_ms": 5000,
            "book_freshness_max_skew_ms": 1500,
        }
        policy = {
            "paper_only": True,
            "real_order_submission": False,
            "strict_multi_leg_evidence_required": True,
            "max_token_age_ms": 5000,
            "max_cross_leg_skew_ms": 1500,
        }
        rows = [{
            "hard_arbitrage": "1",
            "executable": "1",
            "id": "stale",
            "exchange_ts_ms": "1000",
            "received_ts_ms": "9500",
            "decision_ts_ms": "10000",
        }]
        report, valid = module.assess(ROOT, status, rows, policy)
        self.assertFalse(valid)
        self.assertEqual(report["failures"][0]["reason"], "aggregate_exchange_age")


if __name__ == "__main__":
    unittest.main()
