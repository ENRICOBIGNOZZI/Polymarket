#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hf_forward_maker_policy_rejection.py"
SPEC = importlib.util.spec_from_file_location("hf_forward_maker_policy_rejection", SCRIPT)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


def report(
    policy: str,
    *,
    fills: int,
    pair_fills: int,
    one_sided: int,
    total_pnl: float,
    lcb: float,
    ucb: float,
    markout_60: float | None = None,
    markout_300: float | None = None,
) -> dict:
    return {
        "policy": policy,
        "any_fills": fills,
        "pair_fills": pair_fills,
        "one_sided_only": one_sided,
        "total_pnl_ex_rewards_usd": total_pnl,
        "block_bootstrap_lcb_mean_pnl_ex_rewards_per_probe_usd": lcb,
        "block_bootstrap_ucb_mean_pnl_ex_rewards_per_probe_usd": ucb,
        "filled_share_weighted_markout_60_bid_per_share": markout_60,
        "filled_share_weighted_markout_300_bid_per_share": markout_300,
    }


class ForwardMakerEconomicRejectionTests(unittest.TestCase):
    def test_negative_upper_bound_rejects_fill_active_policy(self):
        value = report(
            "improve1",
            fills=5,
            pair_fills=0,
            one_sided=5,
            total_pnl=-3.8928,
            lcb=-0.0103,
            ucb=-0.0007,
            markout_60=-0.0040,
            markout_300=-0.0042,
        )
        audited = MOD.classify_policy(value, min_fills_for_rejection=3)
        self.assertEqual(audited["research_state"], "REJECT_CURRENT_SAMPLE")
        self.assertTrue(audited["economically_rejected_on_current_sample"])
        self.assertIn(
            "negative_ex_reward_pnl_with_negative_block_bootstrap_ucb",
            audited["reasons"],
        )
        self.assertIn("all_observed_fills_one_sided", audited["reasons"])
        self.assertIn("negative_fill_weighted_markout_60", audited["reasons"])
        self.assertIn("negative_fill_weighted_markout_300", audited["reasons"])

    def test_single_negative_fill_stays_more_evidence_required(self):
        value = report(
            "join",
            fills=1,
            pair_fills=0,
            one_sided=1,
            total_pnl=-0.1389,
            lcb=-0.00052,
            ucb=0.0,
            markout_60=-0.002,
        )
        audited = MOD.classify_policy(value, min_fills_for_rejection=3)
        self.assertEqual(audited["research_state"], "MORE_EVIDENCE_REQUIRED")
        self.assertFalse(audited["economically_rejected_on_current_sample"])
        self.assertIn("too_few_fills_for_hard_economic_rejection", audited["reasons"])

    def test_negative_point_estimate_without_negative_ucb_is_not_rejected(self):
        value = report(
            "candidate",
            fills=8,
            pair_fills=4,
            one_sided=4,
            total_pnl=-0.2,
            lcb=-0.01,
            ucb=0.004,
        )
        audited = MOD.classify_policy(value, min_fills_for_rejection=3)
        self.assertEqual(audited["research_state"], "MORE_EVIDENCE_REQUIRED")
        self.assertFalse(audited["economically_rejected_on_current_sample"])

    def test_edge_erasing_two_leg_improvement_is_rejected_before_fill_model(self):
        # Exact shape from the 2026-08-26 Le Pen forward-maker window:
        # join YES+NO = 0.998; improve1 spends one 0.001 tick on each leg,
        # taking the complete-set quote sum to 1.000 and erasing all locked edge.
        audited = MOD.audit_quote_improvement(
            0.998,
            1.0,
            min_residual_edge_per_share=0.00005,
        )
        self.assertEqual(audited["research_state"], "REJECT_EDGE_ERASING_IMPROVEMENT")
        self.assertTrue(audited["edge_erasing_improvement"])
        self.assertAlmostEqual(audited["base_locked_edge_per_matched_share"], 0.002)
        self.assertAlmostEqual(audited["improvement_cost_per_matched_share"], 0.002)
        self.assertAlmostEqual(audited["improved_locked_edge_per_matched_share"], 0.0)

    def test_positive_residual_edge_is_only_a_necessary_condition(self):
        # Exact BNB shape from the same window: join sum 0.92, improve1 sum
        # 0.94. The improvement preserves 6c/share locked pair edge, so the
        # structural edge-budget guard alone does not reject it. The realized
        # one-sided fill is nevertheless economically rejected by the forward
        # PnL/markout audit, demonstrating why residual edge is not sufficient.
        audited = MOD.audit_quote_improvement(
            0.92,
            0.94,
            min_residual_edge_per_share=0.00005,
        )
        self.assertEqual(audited["research_state"], "RESIDUAL_EDGE_PRESERVED_ONLY")
        self.assertFalse(audited["edge_erasing_improvement"])
        self.assertAlmostEqual(audited["improved_locked_edge_per_matched_share"], 0.06)
        self.assertIn("not sufficient", audited["note"])

    def test_audit_is_read_only_and_never_real_money_eligible(self):
        calibration = {
            "by_policy": {
                "improve1": report(
                    "improve1",
                    fills=5,
                    pair_fills=0,
                    one_sided=5,
                    total_pnl=-3.0,
                    lcb=-0.01,
                    ucb=-0.001,
                ),
                "join": report(
                    "join",
                    fills=1,
                    pair_fills=0,
                    one_sided=1,
                    total_pnl=-0.1,
                    lcb=-0.001,
                    ucb=0.0,
                ),
            }
        }
        audited = MOD.audit(calibration, min_fills_for_rejection=3)
        self.assertEqual(audited["rejected_policies"], ["improve1"])
        self.assertTrue(audited["read_only"])
        self.assertFalse(audited["real_money_eligible"])
        self.assertEqual(audited["production_action"], "no_change")


if __name__ == "__main__":
    unittest.main()
