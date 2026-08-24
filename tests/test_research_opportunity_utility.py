#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "research_opportunity_utility.py"
SPEC = importlib.util.spec_from_file_location("research_opportunity_utility", MODULE_PATH)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class RobustOpportunityUtilityTest(unittest.TestCase):
    def base_row(self, **updates):
        row = {
            "timestamp": 1000,
            "source": "b2",
            "candidate_id": "candidate",
            "event_id": "event",
            "raw_edge": 0.020,
            "net_edge": 0.015,
            "capital_required": 100.0,
            "fills": 80,
            "probes": 100,
            "adverse_selection_edge": 0.001,
            "latency_edge": 0.0005,
            "uncertainty_edge": 0.0005,
            "concentration_edge": 0.0005,
            "holding_hours": 1.0,
            "realized_net_pnl": 1.0,
        }
        row.update(updates)
        return row

    def test_raw_positive_but_cost_negative_fails_closed(self):
        scored = MOD.score_row(
            self.base_row(
                raw_edge=0.006,
                net_edge=-0.002,
                adverse_selection_edge=0.0,
                latency_edge=0.0,
                uncertainty_edge=0.0,
                concentration_edge=0.0,
            )
        )
        self.assertEqual(scored["research_eligible"], 0)
        self.assertIn("nonpositive_net_edge", scored["failure_reasons"])
        self.assertIn("nonpositive_2.0x_stress", scored["failure_reasons"])
        self.assertEqual(scored["edge_bottleneck"], "execution_cost_bound")

    def test_execution_cost_bottleneck_quantifies_required_improvement(self):
        scored = MOD.score_row(
            self.base_row(
                raw_edge=0.0053866,
                net_edge=-0.00262469,
                adverse_selection_edge=0.0,
                latency_edge=0.0,
                uncertainty_edge=0.0,
                concentration_edge=0.0,
            )
        )
        self.assertAlmostEqual(scored["execution_cost_wedge"], 0.00801129, places=8)
        self.assertGreater(scored["execution_cost_fraction_of_raw"], 1.0)
        self.assertAlmostEqual(
            scored["cost_reduction_fraction_to_break_even"],
            0.32762389078413084,
            places=10,
        )
        self.assertEqual(scored["edge_bottleneck"], "execution_cost_bound")

    def test_missing_forward_fill_evidence_never_becomes_eligible(self):
        scored = MOD.score_row(self.base_row(fills=0, probes=0))
        self.assertEqual(scored["fill_lower_bound"], 0.0)
        self.assertEqual(scored["research_eligible"], 0)
        self.assertIn("missing_forward_fill_evidence", scored["failure_reasons"])
        self.assertEqual(scored["edge_bottleneck"], "missing_execution_evidence")

    def test_missing_execution_penalty_evidence_fails_closed(self):
        row = self.base_row()
        del row["latency_edge"]
        scored = MOD.score_row(row)
        self.assertEqual(scored["research_eligible"], 0)
        self.assertIn("missing_latency_edge", scored["failure_reasons"])
        self.assertEqual(scored["edge_bottleneck"], "missing_execution_evidence")

    def test_positive_candidate_must_survive_two_x_cost_and_penalties(self):
        scored = MOD.score_row(self.base_row())
        self.assertGreater(scored["stress_2_0_edge"], 0.0)
        self.assertGreater(scored["fill_lower_bound"], 0.0)
        self.assertGreater(scored["conservative_expected_pnl"], 0.0)
        self.assertGreater(scored["utility_per_capital_hour"], 0.0)
        self.assertEqual(scored["research_eligible"], 1)
        self.assertEqual(scored["edge_bottleneck"], "robust_candidate")

    def test_capital_occupancy_penalizes_slow_candidate(self):
        fast = MOD.score_row(self.base_row(candidate_id="fast", holding_hours=0.5))
        slow = MOD.score_row(self.base_row(candidate_id="slow", holding_hours=8.0))
        self.assertGreater(
            fast["utility_per_capital_hour"], slow["utility_per_capital_hour"]
        )

    def test_ranker_prefers_robust_utility_over_reported_net_edge(self):
        fragile = self.base_row(
            candidate_id="fragile",
            raw_edge=0.025,
            net_edge=0.020,
            adverse_selection_edge=0.012,
            holding_hours=1.0,
        )
        robust = self.base_row(
            candidate_id="robust",
            raw_edge=0.018,
            net_edge=0.016,
            adverse_selection_edge=0.001,
            holding_hours=0.5,
        )
        ranked = MOD.rank_rows([fragile, robust])
        self.assertEqual(ranked[0]["candidate_id"], "robust")
        self.assertGreater(fragile["net_edge"], robust["net_edge"])

    def test_current_like_b2_maker_edge_fails_pair_fill_hurdle_on_42_probes(self):
        result = MOD.paired_execution_feasibility(
            maker_edge=0.0130666,
            taker_fallback_edge=-0.0332757,
            pair_fills=0,
            pair_probes=42,
        )
        self.assertGreater(result["required_pair_fill_probability"], 0.70)
        self.assertLess(result["pair_fill_upper_bound"], 0.09)
        self.assertLess(result["optimistic_pair_execution_edge"], -0.02)
        self.assertFalse(result["paired_execution_feasible"])

    def test_pair_fill_hurdle_is_integrated_into_eligibility(self):
        scored = MOD.score_row(
            self.base_row(
                taker_fallback_edge=-0.02,
                pair_fills=0,
                pair_probes=20,
            )
        )
        self.assertEqual(scored["research_eligible"], 0)
        self.assertIn("paired_fill_hurdle_not_met", scored["failure_reasons"])
        self.assertGreater(scored["required_pair_fill_probability"], 0.0)
        self.assertLess(
            scored["pair_fill_upper_bound"],
            scored["required_pair_fill_probability"],
        )
        self.assertEqual(scored["edge_bottleneck"], "paired_fill_bound")

    def test_high_pair_fill_rate_can_clear_hurdle(self):
        result = MOD.paired_execution_feasibility(
            maker_edge=0.015,
            taker_fallback_edge=-0.01,
            pair_fills=90,
            pair_probes=100,
        )
        self.assertLess(result["required_pair_fill_probability"], 0.5)
        self.assertGreater(
            result["pair_fill_upper_bound"],
            result["required_pair_fill_probability"],
        )
        self.assertGreater(result["optimistic_pair_execution_edge"], 0.0)
        self.assertTrue(result["paired_execution_feasible"])

    def test_missing_pair_evidence_fails_closed_when_fallback_is_supplied(self):
        scored = MOD.score_row(
            self.base_row(
                taker_fallback_edge=-0.01,
                pair_fills=0,
                pair_probes=0,
            )
        )
        self.assertEqual(scored["research_eligible"], 0)
        self.assertIn("missing_paired_fill_evidence", scored["failure_reasons"])
        self.assertEqual(scored["edge_bottleneck"], "missing_execution_evidence")

    def test_cost_stress_fragility_is_distinct_from_current_cost_failure(self):
        scored = MOD.score_row(
            self.base_row(
                raw_edge=0.010,
                net_edge=0.004,
                adverse_selection_edge=0.0,
                latency_edge=0.0,
                uncertainty_edge=0.0,
                concentration_edge=0.0,
            )
        )
        self.assertGreater(scored["net_edge"], 0.0)
        self.assertLess(scored["stress_2_0_edge"], 0.0)
        self.assertEqual(scored["edge_bottleneck"], "cost_stress_fragile")
        self.assertEqual(scored["research_eligible"], 0)

    def test_chronological_oos_reports_incumbent_ablation(self):
        rows = []
        for i in range(12):
            ts = 1000 + 100 * i
            rows.append(
                self.base_row(
                    timestamp=ts,
                    candidate_id=f"robust-{i}",
                    raw_edge=0.020,
                    net_edge=0.015,
                    holding_hours=0.5,
                    realized_net_pnl=2.0,
                )
            )
            rows.append(
                self.base_row(
                    timestamp=ts + 1,
                    candidate_id=f"fragile-{i}",
                    raw_edge=0.030,
                    net_edge=0.025,
                    adverse_selection_edge=0.030,
                    realized_net_pnl=-1.0,
                )
            )
        report = MOD.chronological_oos(
            MOD.rank_rows(rows), min_train_rows=4, test_rows=4, purge_seconds=0
        )
        self.assertGreaterEqual(report["fold_count"], 2)
        self.assertGreater(
            report["robust_realized_net_pnl"],
            report["baseline_net_edge_realized_net_pnl"],
        )
        self.assertGreater(report["incremental_realized_net_pnl"], 0.0)
        self.assertTrue(report["evidence_ready"])
        starts = [fold["test_start_ts"] for fold in report["folds"]]
        self.assertEqual(starts, sorted(starts))

    def test_wilson_lower_bound_is_conservative(self):
        lower = MOD.wilson_lower_bound(80, 100)
        self.assertGreater(lower, 0.0)
        self.assertLess(lower, 0.8)
        self.assertTrue(math.isfinite(lower))

    def test_two_sided_wilson_upper_tightens_with_more_zero_fill_probes(self):
        _, upper14 = MOD.wilson_bounds(0, 14)
        _, upper42 = MOD.wilson_bounds(0, 42)
        self.assertAlmostEqual(upper14, 0.2153108027376358, places=12)
        self.assertAlmostEqual(upper42, 0.08379879086570391, places=12)
        self.assertLess(upper42, upper14)


if __name__ == "__main__":
    unittest.main()
