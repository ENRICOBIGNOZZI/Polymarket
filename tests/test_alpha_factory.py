#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("alpha_factory", ROOT / "scripts" / "alpha_factory.py")
assert SPEC and SPEC.loader
alpha_factory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alpha_factory)


class AlphaFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config/alpha_factory.json").read_text(encoding="utf-8"))
        self.now = 1_800_000_000

    def live(self, *, maker_eligible: bool = False) -> dict:
        maker_reasons = [] if maker_eligible else ["insufficient_fills", "cost_stress_unverifiable"]
        return {
            "schema": "polymarket_v7_public_live_smoke_v1",
            "generated_ts": self.now - 60,
            "paper_only": True,
            "authenticated_execution": False,
            "runtime": {
                "present": True,
                "version": 7,
                "paper_only": True,
                "authenticated_execution": False,
                "total_fills": 7,
                "pnl_usd": 2.0,
                "realized_pnl_usd": 1.0,
                "drawdown": 0.01,
                "strategy_count": 5,
            },
            "execution_evidence": {
                "present": True,
                "generated_ts": self.now - 30,
                "eligible_models": ["micro_maker"] if maker_eligible else [],
                "insufficient_models": [] if maker_eligible else ["micro_maker", "relative_value", "graph_hard", "external"],
                "models": {
                    "micro_maker": {
                        "target": "short_horizon_markout",
                        "paper_eligible": maker_eligible,
                        "fills": 30 if maker_eligible else 2,
                        "fill_rate": 0.1,
                        "realized_pnl_observations": 20 if maker_eligible else 2,
                        "net_pnl": 3.0 if maker_eligible else -1.0,
                        "stressed_net_pnl": 2.0 if maker_eligible else None,
                        "forward_markout_observations": 20 if maker_eligible else 1,
                        "mean_forward_markout": 0.01 if maker_eligible else -0.02,
                        "bootstrap_one_sided_pvalue": 0.01 if maker_eligible else None,
                        "active_folds": 2 if maker_eligible else 0,
                        "positive_fold_fraction": 1.0 if maker_eligible else None,
                        "reason_codes": maker_reasons,
                    },
                    "relative_value": {
                        "target": "joint_state_convergence",
                        "paper_eligible": False,
                        "fills": 0,
                        "realized_pnl_observations": 0,
                        "net_pnl": 0.0,
                        "stressed_net_pnl": None,
                        "bootstrap_one_sided_pvalue": None,
                        "active_folds": 0,
                        "positive_fold_fraction": None,
                        "reason_codes": ["insufficient_fills", "cost_stress_unverifiable"],
                    },
                    "graph_hard": {
                        "target": "structural_payout",
                        "paper_eligible": False,
                        "fills": 0,
                        "realized_pnl_observations": 0,
                        "net_pnl": 0.0,
                        "stressed_net_pnl": None,
                        "bootstrap_one_sided_pvalue": None,
                        "active_folds": 0,
                        "positive_fold_fraction": None,
                        "reason_codes": ["insufficient_fills"],
                    },
                    "external": {
                        "target": "terminal_probability",
                        "paper_eligible": False,
                        "fills": 0,
                        "realized_pnl_observations": 0,
                        "net_pnl": 0.0,
                        "stressed_net_pnl": None,
                        "bootstrap_one_sided_pvalue": None,
                        "active_folds": 0,
                        "positive_fold_fraction": None,
                        "reason_codes": ["terminal_calibration_unverifiable"],
                    },
                },
            },
        }

    def forward(self, *, runs: int = 30, paired: int = 25, pnl: float = 2.0) -> dict:
        return {
            "generated_ts": self.now - 60,
            "policies": {
                "join": {
                    "runs": runs,
                    "paired_fills": paired,
                    "one_sided_fill_rate": 0.1,
                    "total_pnl_ex_rewards_usd": pnl,
                    "stressed_2x_pnl_ex_rewards_usd": pnl / 2,
                    "run_pnl_ex_rewards": [0.2, 0.3, 0.1, 0.4],
                    "incremental_utility": 0.1,
                    "single_model_compatible": True,
                }
            },
        }

    def test_benjamini_hochberg_controls_multiple_candidates(self) -> None:
        result = alpha_factory.benjamini_hochberg({"a": 0.01, "b": 0.03, "c": 0.20}, 0.05)
        self.assertTrue(result["a"]["rejected"])
        self.assertTrue(result["b"]["rejected"])
        self.assertFalse(result["c"]["rejected"])

    def test_execution_candidates_are_directly_bound_to_v7_evidence(self) -> None:
        candidates = alpha_factory.execution_candidates(self.live(maker_eligible=True))
        maker = next(row for row in candidates if row["candidate_id"] == "v7_execution:micro_maker")
        self.assertEqual(maker["evidence_type"], "canonical_v7_execution_ledger")
        self.assertEqual(maker["observations"], 30)
        self.assertEqual(maker["metrics"]["oos_net_pnl_usd"], 3.0)
        self.assertTrue(maker["gate_pass_before_fdr"])
        self.assertFalse(maker["integration_evidence_pass"])

    def test_factory_never_recreates_b1_b2_b3_or_walk_forward(self) -> None:
        report, _ = alpha_factory.build_report(self.live(), self.forward(), {}, self.config, self.now)
        text = json.dumps(report).lower()
        self.assertNotIn("\"b1\"", text)
        self.assertNotIn("\"b2\"", text)
        self.assertNotIn("\"b3\"", text)
        self.assertNotIn("walk_forward", text)
        self.assertTrue(report["invariants"]["canonical_v7_execution_evidence_only"])
        self.assertTrue(report["invariants"]["retired_b1_b2_b3_pipeline"])

    def test_forward_maker_policy_remains_research_only(self) -> None:
        candidates = alpha_factory.forward_candidates(self.forward(), self.config)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["candidate_id"], "maker_forward:join")
        self.assertTrue(candidate["gate_pass_before_fdr"])
        self.assertFalse(candidate["integration_evidence_pass"])
        self.assertIn("maker_forward_policy_is_research_only", candidate["integration_reasons"])

    def test_execution_evidence_generates_only_current_v7_research_owners(self) -> None:
        report, _ = alpha_factory.build_report(self.live(), {}, {}, self.config, self.now)
        experiments = {row["experiment_id"]: row["owner_workflow"] for row in report["next_experiments"]}
        self.assertEqual(experiments["maker_candidate_specific_fillability"], "forward-maker-research.yml")
        self.assertEqual(experiments["graph_joint_completion_unwind"], "arb-theory-hourly.yml")
        self.assertEqual(experiments["hard_arb_freshness_recurrence"], "fast-arb-hourly.yml")
        self.assertEqual(experiments["external_probability_mapping"], "external-intelligence.yml")
        allowed = {
            "forward-maker-research.yml",
            "external-intelligence.yml",
            "fast-arb-hourly.yml",
            "arb-theory-hourly.yml",
        }
        self.assertTrue({row["owner_workflow"] for row in report["next_experiments"]} <= allowed)

    def test_stale_or_non_v7_runtime_degrades_evidence(self) -> None:
        live = self.live()
        live["generated_ts"] = self.now - 20000
        report, _ = alpha_factory.build_report(live, self.forward(), {}, self.config, self.now)
        self.assertEqual(report["status"], "DEGRADED_EVIDENCE")
        self.assertFalse(report["diagnostics"]["live_smoke_fresh"])

        live = self.live()
        live["runtime"]["version"] = 6
        report, _ = alpha_factory.build_report(live, self.forward(), {}, self.config, self.now)
        self.assertEqual(report["status"], "DEGRADED_EVIDENCE")
        self.assertFalse(report["diagnostics"]["runtime_v7_valid"])

    def test_consecutive_execution_passes_do_not_bypass_promotion_controller(self) -> None:
        previous: dict = {}
        report = None
        for offset in range(3):
            report, previous = alpha_factory.build_report(
                self.live(maker_eligible=True),
                self.forward(),
                previous,
                self.config,
                self.now + offset,
            )
        assert report is not None
        maker = next(row for row in report["candidates"] if row["candidate_id"] == "v7_execution:micro_maker")
        self.assertEqual(maker["consecutive_passes"], 3)
        self.assertEqual(maker["decision"], "continue_shadow")
        self.assertIn("promotion_controller_exact_source_evidence_required", maker["reasons"])
        self.assertIsNone(report["recommended_canary"])

    def test_config_preserves_paper_only_and_no_direct_mutation(self) -> None:
        self.assertTrue(self.config["paper_only"])
        self.assertFalse(self.config["allow_authenticated_execution"])
        self.assertFalse(self.config["allow_direct_champion_mutation"])
        report, state = alpha_factory.build_report(self.live(), self.forward(), {}, self.config, self.now)
        self.assertFalse(report["direct_champion_mutation"])
        self.assertFalse(report["authenticated_execution"])
        self.assertEqual(report["submitted_orders"], 0)
        self.assertFalse(state["invariants"]["direct_champion_mutation"])
        self.assertTrue(state["invariants"]["promotion_controller_remains_authoritative"])


if __name__ == "__main__":
    unittest.main()
