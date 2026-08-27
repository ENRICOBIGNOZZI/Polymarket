import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import research_director as rd


def base_config():
    return {
        "schema": "polymarket_research_director_config_v1",
        "paper_only": True,
        "allow_authenticated_execution": False,
        "allow_direct_champion_mutation": False,
        "max_dispatches_per_cycle": 2,
        "dispatch_cooldown_seconds": 1800,
        "max_stagnant_cycles": 2,
        "stagnation_retry_seconds": 7200,
        "economic_stagnation_seconds": 3600,
        "max_alpha_report_age_seconds": 10800,
        "owner_workflows": {
            "external-intelligence.yml": {
                "workflow_name": "Polymarket External Intelligence",
                "priority_bias": 0,
            },
            "fast-arb-hourly.yml": {
                "workflow_name": "Fast Arbitrage Hourly Shadow",
                "priority_bias": 1,
            },
            "arb-theory-hourly.yml": {
                "workflow_name": "Arbitrage Theory Hourly Research",
                "priority_bias": 1,
            },
            "v7-cross-sectional-ranking-research.yml": {
                "workflow_name": "V7 cross-sectional relative tail research",
                "priority_bias": 1,
            },
        },
        "owner_remap": {},
        "experiment_prefix_owner": {
            "external_": "external-intelligence.yml",
            "fast_arb_": "fast-arb-hourly.yml",
            "arb_theory_": "arb-theory-hourly.yml",
            "cross_sectional_": "v7-cross-sectional-ranking-research.yml",
            "ranking_": "v7-cross-sectional-ranking-research.yml",
        },
        "forbidden_workflows": [
            "integration-merge.yml",
            "promotion-controller.yml",
            "control-plane-event-bridge.yml",
            "post-merge-validation.yml",
            "v7-live-paper-validation.yml",
            "v7-deploy-paper-server.yml",
            "v7-paper-server-health.yml",
            "v7-unified-paper-evidence.yml",
        ],
    }


def alpha_report(ts=1000):
    return {
        "generated_ts": ts,
        "diagnostics": {
            "oos": {"selected_trades": 0},
            "b1": {"maker_positive": 0},
            "b2": {"maker_positive": 0},
            "external_intelligence": {"passing_candidates": 0},
        },
        "candidates": [],
        "next_experiments": [
            {
                "experiment_id": "execution_fillability_frontier",
                "priority": 2,
                "hypothesis": "Fillability is the bottleneck.",
                "triggering_evidence": "zero OOS fills",
                "success_metric": "paired fills and markout",
                "owner_workflow": "retired-research-owner.yml",
            },
            {
                "experiment_id": "fast_arb_exact_leg_freshness",
                "priority": 3,
                "hypothesis": "Exact executable-leg freshness can improve structural-arb evidence.",
                "triggering_evidence": "stale leg evidence",
                "success_metric": "fresh executable joint-state observations",
                "owner_workflow": "fast-arb-hourly.yml",
            },
            {
                "experiment_id": "external_terminal_information",
                "priority": 7,
                "hypothesis": "External terminal information may add alpha.",
                "triggering_evidence": "no accepted external probability",
                "success_metric": "OOS calibration and utility",
                "owner_workflow": "alpha-factory.yml",
            },
        ],
    }


class ResearchDirectorTests(unittest.TestCase):
    def test_routes_only_to_current_dispatchable_evidence_workers(self):
        report, _ = rd.build_report(base_config(), alpha_report(), {}, [], 2000)
        self.assertEqual(report["status"], "HEALTHY")
        self.assertEqual(
            [row["workflow_file"] for row in report["dispatch_plan"]],
            ["fast-arb-hourly.yml", "external-intelligence.yml"],
        )
        stale = next(
            row
            for row in report["experiments"]
            if row["experiment_id"] == "execution_fillability_frontier"
        )
        self.assertFalse(stale["eligible"])
        self.assertEqual(stale["reason"], "owner_outside_research_allowlist")
        self.assertTrue(report["invariants"]["bounded_allowlisted_research"])
        self.assertFalse(report["invariants"]["real_order_submission"])
        self.assertLessEqual(
            report["invariants"]["actual_dispatches"],
            report["invariants"]["max_dispatches_per_cycle"],
        )

    def test_running_or_recent_owner_is_not_redispatched(self):
        runs = [
            {
                "workflowName": "Fast Arbitrage Hourly Shadow",
                "headBranch": "main",
                "status": "in_progress",
                "updatedAt": "1970-01-01T00:33:10+00:00",
            },
            {
                "workflowName": "Polymarket External Intelligence",
                "headBranch": "main",
                "status": "completed",
                "conclusion": "success",
                "updatedAt": "1970-01-01T00:30:00+00:00",
            },
        ]
        report, _ = rd.build_report(base_config(), alpha_report(), {}, runs, 2000)
        self.assertEqual(report["dispatch_plan"], [])
        reasons = " ".join(str(row.get("reason")) for row in report["experiments"])
        self.assertIn("owner_workflow_running", reasons)
        self.assertIn("owner_workflow_recent", reasons)

    def test_stagnation_backoff_prevents_repeating_same_experiment_forever(self):
        cfg = base_config()
        cfg["dispatch_cooldown_seconds"] = 0
        cfg["max_stagnant_cycles"] = 2
        cfg["stagnation_retry_seconds"] = 10_000

        report1, state1 = rd.build_report(cfg, alpha_report(1000), {}, [], 1100)
        self.assertTrue(report1["dispatch_plan"])

        report2, state2 = rd.build_report(cfg, alpha_report(1200), state1, [], 1300)
        self.assertTrue(report2["dispatch_plan"])

        report3, _ = rd.build_report(cfg, alpha_report(1400), state2, [], 1500)
        self.assertEqual(report3["dispatch_plan"], [])
        self.assertTrue(
            any("stagnation_backoff" in str(row.get("reason")) for row in report3["experiments"])
        )

    def test_stale_alpha_report_fails_closed(self):
        cfg = base_config()
        cfg["max_alpha_report_age_seconds"] = 100
        report, _ = rd.build_report(cfg, alpha_report(1000), {}, [], 2000)
        self.assertEqual(report["status"], "DEGRADED")
        self.assertFalse(report["alpha_factory_fresh"])
        self.assertEqual(report["dispatch_plan"], [])

    def test_forbidden_research_owner_is_rejected_by_config(self):
        cfg = base_config()
        cfg["owner_workflows"]["v7-deploy-paper-server.yml"] = {
            "workflow_name": "V7 deploy PAPER server"
        }
        with self.assertRaises(ValueError):
            rd.validate_config(cfg)

    def test_economic_progress_becomes_stagnant_without_new_evidence(self):
        cfg = base_config()
        cfg["economic_stagnation_seconds"] = 100
        report1, state1 = rd.build_report(cfg, alpha_report(1000), {}, [], 1100)
        self.assertEqual(report1["economic_progress"]["state"], "LEARNING")
        report2, _ = rd.build_report(cfg, alpha_report(1200), state1, [], 1301)
        self.assertEqual(report2["economic_progress"]["state"], "STAGNANT")

    def test_current_repo_owners_are_registered_existing_and_manually_dispatchable(self):
        cfg = json.loads((ROOT / "config" / "research_director.json").read_text(encoding="utf-8"))
        registry = json.loads((ROOT / "config" / "scheduler_registry.json").read_text(encoding="utf-8"))
        registered = {Path(row["workflow"]).name for row in registry["schedulers"]}
        owners = set(cfg["owner_workflows"])
        forbidden = set(cfg["forbidden_workflows"])
        self.assertTrue(owners)
        self.assertTrue(owners <= registered)
        self.assertFalse(owners & forbidden)
        for workflow in sorted(owners):
            path = ROOT / ".github" / "workflows" / workflow
            self.assertTrue(path.is_file(), workflow)
            text = path.read_text(encoding="utf-8")
            self.assertIn("workflow_dispatch:", text, workflow)

    def test_current_forbidden_set_covers_v7_mutation_authorities(self):
        cfg = json.loads((ROOT / "config" / "research_director.json").read_text(encoding="utf-8"))
        forbidden = set(cfg["forbidden_workflows"])
        self.assertTrue(
            {
                "integration-merge.yml",
                "promotion-controller.yml",
                "control-plane-event-bridge.yml",
                "post-merge-validation.yml",
                "v7-live-paper-validation.yml",
                "v7-deploy-paper-server.yml",
                "v7-paper-server-health.yml",
                "v7-unified-paper-evidence.yml",
            }
            <= forbidden
        )

    def test_dispatch_workflow_is_config_driven_and_preflights_trigger_support(self):
        text = (ROOT / ".github" / "workflows" / "research-queue.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("research-director dispatch contract error", text)
        self.assertIn("owner_workflows", text)
        self.assertIn("workflow_dispatch", text)
        self.assertIn('gh workflow run "$workflow" --ref main', text)


if __name__ == "__main__":
    unittest.main()
