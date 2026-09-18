from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "v7_native_critical_path_policy.json"
DOC = ROOT / "docs" / "v7_native_critical_path_policy.md"


class NativeCriticalPathPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = json.loads(POLICY.read_text(encoding="utf-8"))

    def test_canonical_engine_and_language(self) -> None:
        self.assertEqual(self.policy["engine"], "CRYPTO_SETTLEMENT_ENGINE")
        self.assertEqual(self.policy["critical_path_language"], "C++")
        self.assertGreaterEqual(int(self.policy["minimum_cpp_standard"]), 20)
        self.assertTrue(self.policy["paper_only_until_explicit_separate_promotion"])
        self.assertFalse(self.policy["real_order_submission_default"])

    def test_all_reaction_stages_are_native(self) -> None:
        required = {
            "VENUE_FRAME_DECODE", "CAUSAL_STATE_APPLY", "FEATURE_UPDATE",
            "SIGNAL_POLICY", "CANDIDATE_CONSTRUCTION", "PORTFOLIO_ARBITRATION",
            "RISK_CAPITAL_ADMISSION", "OMS_INTENT_HANDOFF",
        }
        self.assertEqual(set(self.policy["mandatory_native_stages"]), required)
        self.assertTrue(self.policy["event_driven_required"])
        self.assertTrue(self.policy["bounded_queues_required"])
        self.assertTrue(self.policy["fail_closed_on_gap_or_overflow"])
        self.assertEqual(self.policy["hot_path_heap_allocation_target"], 0)

    def test_slow_dependencies_are_forbidden(self) -> None:
        forbidden = set(self.policy["forbidden_hot_path_dependencies"])
        for token in (
            "PYTHON_INTERPRETER", "FILESYSTEM_POLLING", "SYNCHRONOUS_REST_MARKET_DATA",
            "DATABASE_IO", "PROCESS_SPAWN", "CROSS_PROCESS_JSON_IPC",
            "SYNCHRONOUS_TELEMETRY", "UNBOUNDED_ALLOCATION", "FIXED_SLEEP_POLLING",
        ):
            self.assertIn(token, forbidden)
        self.assertTrue(self.policy["bounded_exchange_json_parse_in_cpp_allowed"])

    def test_single_owner_and_benchmark_gate(self) -> None:
        for key in (
            "single_execution_owner_required", "single_risk_owner_required",
            "single_oms_owner_required", "single_inventory_owner_required",
        ):
            self.assertTrue(self.policy[key], key)
        gate = self.policy["alternative_technology_gate"]
        self.assertIn("AF_XDP", gate["allowed"])
        self.assertIn("DPDK", gate["allowed"])
        self.assertTrue(gate["promotion_requires_same_semantics"])
        self.assertTrue(gate["promotion_requires_zero_authority_change"])
        self.assertTrue(gate["promotion_requires_zero_correctness_regression"])
        self.assertTrue(gate["promotion_requires_p999_non_regression"])
        self.assertTrue(gate["promotion_requires_zero_silent_drop"])
        self.assertGreater(float(gate["promotion_requires_p99_improvement_fraction"]), 0.0)

    def test_only_canonical_engine_may_be_hot_path(self) -> None:
        self.assertEqual(
            self.policy["canonical_hot_path_process_id"],
            "crypto_settlement_engine",
        )
        legacy = set(self.policy["forbidden_hot_path_process_ids"])
        for process_id in (
            "external_venue_runtime", "external_fair_router", "ledger_router",
            "global_portfolio_coordinator", "lead_lag_taker_v1",
            "authorized_maker_paper_executor",
        ):
            self.assertIn(process_id, legacy)
        surfaces = set(self.policy["forbidden_hot_path_surface_tokens"])
        for token in (
            "structural", "arbitrage", "legacy", "grafana", "prometheus",
            "exporter", "retention", "research", "report",
        ):
            self.assertIn(token, surfaces)

    def test_latency_targets_and_doc_match_policy(self) -> None:
        target = self.policy["latency_targets_us"]
        self.assertLessEqual(int(target["trigger_to_admission_p99"]), 300)
        self.assertLessEqual(int(target["trigger_to_admission_stretch_p99"]), 100)
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("canonical crypto critical path is native C++", text)
        self.assertIn("single-owner cutover", text)
        self.assertIn("AF_XDP", text)


if __name__ == "__main__":
    unittest.main()
