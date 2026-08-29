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
            "forward-maker-research.yml": {
                "workflow_name": "forward-maker-alpha-research",
                "priority_bias": 0,
            },
            "external-intelligence.yml": {
                "workflow_name": "Polymarket External Intelligence",
                "priority_bias": 0,
            },
            "v6-research-smoke.yml": {
                "workflow_name": "V6 live-data research smoke",
                "priority_bias": 0,
            },
        },
        "owner_remap": {"alpha-factory.yml": "v6-research-smoke.yml"},
        "experiment_prefix_owner": {
            "external_": "external-intelligence.yml",
            "b2_": "v6-research-smoke.yml",
        },
        "forbidden_workflows": [
            "integration-merge.yml",
            "deploy-paper-server.yml",
            "server-health.yml",
            "promotion-controller.yml",
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
                "owner_workflow": "forward-maker-research.yml",
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
    def test_routes_to_actual_evidence_workers_and_bounds_dispatches(self):
        report, _ = rd.build_report(base_config(), alpha_report(), {}, [], 2000)
        self.assertEqual(report["status"], "HEALTHY")
        self.assertEqual(
            [row["workflow_file"] for row in report["dispatch_plan"]],
            ["forward-maker-research.yml", "external-intelligence.yml"],
        )
        self.assertTrue(report["invariants"]["bounded_allowlisted_research"])
        self.assertFalse(report["invariants"]["real_order_submission"])
        self.assertLessEqual(
            report["invariants"]["actual_dispatches"],
            report["invariants"]["max_dispatches_per_cycle"],
        )

    def test_running_or_recent_owner_is_not_redispatched(self):
        runs = [
            {
                "workflowName": "forward-maker-alpha-research",
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
        cfg["owner_workflows"]["deploy-paper-server.yml"] = {
            "workflow_name": "deploy-paper-server"
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


if __name__ == "__main__":
    unittest.main()
