import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "v7_fast_structural_gate.py"
spec = importlib.util.spec_from_file_location("v7_fast_structural_gate", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SHA = "a" * 40


def candidate(theory_ready=True):
    return {
        "schema_version": 1,
        "mode": "research_only",
        "real_order_submission": False,
        "research_ready": True,
        "promotion_ready": theory_ready,
        "candidate_policy": {
            "real_order_submission": False,
            "promotion_ready": theory_ready,
            "min_net_edge": 0.0005,
        },
    }


def valid_execution():
    return {
        "schema": module.EXECUTION_SCHEMA,
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "point_in_time": True,
        "authoritative_fees": True,
        "depth_executable": True,
        "partial_unwind_accounted": True,
        "joint_state_observations": 30,
        "realized_pnl_observations": 30,
        "completed_baskets": 25,
        "fill_conditioned_net_pnl": 4.0,
        "cost_stress_1_5x_net_pnl": 2.0,
        "cost_stress_2x_net_pnl": 1.0,
    }


class V7FastStructuralGateTest(unittest.TestCase):
    def test_missing_execution_evidence_blocks_promotion(self):
        gated = module.gate_candidate(candidate(True), {}, expected_sha=SHA)
        self.assertTrue(gated["quoted_theory_promotion_ready"])
        self.assertFalse(gated["promotion_ready"])
        self.assertFalse(gated["candidate_policy"]["promotion_ready"])
        self.assertIn("joint_execution_evidence_missing", gated["promotion_gate"]["reasons"])

    def test_mixed_sha_blocks_promotion(self):
        execution = valid_execution()
        execution["model_sha"] = "b" * 40
        gated = module.gate_candidate(candidate(True), execution, expected_sha=SHA)
        self.assertFalse(gated["promotion_ready"])
        self.assertIn("mixed_or_wrong_sha_execution_evidence", gated["promotion_gate"]["reasons"])

    def test_positive_same_sha_joint_execution_can_pass(self):
        gated = module.gate_candidate(candidate(True), valid_execution(), expected_sha=SHA)
        self.assertTrue(gated["promotion_ready"])
        self.assertEqual(gated["promotion_gate"]["reasons"], [])
        self.assertEqual(gated["model_sha"], SHA)

    def test_theory_gate_still_required(self):
        gated = module.gate_candidate(candidate(False), valid_execution(), expected_sha=SHA)
        self.assertFalse(gated["promotion_ready"])
        self.assertFalse(gated["quoted_theory_promotion_ready"])

    def test_header_is_forced_fail_closed_without_execution(self):
        gated = module.gate_candidate(candidate(True), {}, expected_sha=SHA)
        with tempfile.TemporaryDirectory() as directory:
            header = Path(directory) / "candidate.hpp"
            header.write_text(
                "#pragma once\nnamespace x {\n"
                "inline constexpr bool kPromotionReady = true;\n"
                "inline constexpr bool kRealOrderSubmission = false;\n"
                "} // namespace pm::fast::generated\n",
                encoding="utf-8",
            )
            module.rewrite_header(header, promotion_ready=gated["promotion_ready"], expected_sha=SHA)
            text = header.read_text(encoding="utf-8")
            self.assertIn("kPromotionReady = false", text)
            self.assertIn("kPromotionModelSha", text)
            self.assertIn(SHA, text)

    def test_fast_workflow_uses_v7_config_and_gate(self):
        fast = (ROOT / ".github" / "workflows" / "fast-arb-hourly.yml").read_text(encoding="utf-8")
        theory = (ROOT / ".github" / "workflows" / "arb-theory-hourly.yml").read_text(encoding="utf-8")
        self.assertIn("--config config/paper_v7.json", fast)
        for legacy in ("paper_v3.json", "paper_v4.json", "paper_v5.json", "paper_v6.json"):
            self.assertNotIn(legacy, fast)
        self.assertIn("v7_fast_structural_gate.py", fast)
        self.assertIn("v7_fast_structural_gate.py", theory)
        self.assertIn("headSha", theory)
        self.assertIn("TARGET_SHA", theory)


if __name__ == "__main__":
    unittest.main()
