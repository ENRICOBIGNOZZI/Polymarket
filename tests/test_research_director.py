#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("research_director", ROOT / "scripts" / "research_director.py")
assert SPEC and SPEC.loader
research_director = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research_director)


class ResearchDirectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config/research_director.json").read_text(encoding="utf-8"))
        self.now = 1_800_000_000

    def alpha(self, experiments: list[dict] | None = None) -> dict:
        return {
            "generated_ts": self.now - 60,
            "status": "RESEARCHING",
            "diagnostics": {
                "runtime_total_fills": 12,
                "execution_evidence_eligible_models": 1,
            },
            "candidates": [
                {
                    "candidate_id": "v7:maker",
                    "decision": "continue_shadow",
                    "observations": 20,
                    "metrics": {"total_pnl_ex_rewards_usd": 1.5},
                }
            ],
            "next_experiments": experiments or [],
        }

    def run(self, experiments: list[dict], previous: dict | None = None, runs: list[dict] | None = None):
        return research_director.build_report(
            self.config,
            self.alpha(experiments),
            previous or {},
            runs or [],
            self.now,
        )

    def experiment(self, experiment_id: str, owner: str = "", priority: int = 1) -> dict:
        row = {
            "experiment_id": experiment_id,
            "priority": priority,
            "hypothesis": f"test {experiment_id}",
            "triggering_evidence": "V7 evidence gap",
            "success_metric": "prospective executable evidence improves",
        }
        if owner:
            row["owner_workflow"] = owner
        return row

    def test_config_is_v7_research_only(self) -> None:
        research_director.validate_config(self.config)
        self.assertEqual(
            set(self.config["owner_workflows"]),
            {
                "forward-maker-research.yml",
                "external-intelligence.yml",
                "fast-arb-hourly.yml",
                "arb-theory-hourly.yml",
            },
        )
        text = json.dumps(self.config).lower()
        self.assertNotIn("v6", text)
        self.assertFalse(self.config["allow_authenticated_execution"])
        self.assertFalse(self.config["allow_direct_champion_mutation"])

    def test_prefix_routing_maps_only_to_registered_v7_workers(self) -> None:
        cases = {
            "maker_queue_repricing": "forward-maker-research.yml",
            "execution_markout_labels": "forward-maker-research.yml",
            "external_probability_mapping": "external-intelligence.yml",
            "hard_arb_freshness": "fast-arb-hourly.yml",
            "graph_joint_completion": "arb-theory-hourly.yml",
            "relative_value_unwind": "arb-theory-hourly.yml",
        }
        for experiment_id, expected in cases.items():
            owner = research_director.resolve_owner(self.experiment(experiment_id), self.config)
            self.assertEqual(owner, expected, experiment_id)

    def test_unknown_experiment_is_fail_closed(self) -> None:
        report, _ = self.run([self.experiment("pca_unowned_research")])
        row = report["experiments"][0]
        self.assertFalse(row["eligible"])
        self.assertEqual(row["owner_workflow"], "")
        self.assertIn("no_registered_v7_research_owner", row["reason"])
        self.assertEqual(report["dispatch_plan"], [])

    def test_explicit_registered_owner_is_allowed(self) -> None:
        report, _ = self.run([
            self.experiment("maker_candidate_specific_flow", "forward-maker-research.yml")
        ])
        self.assertEqual(
            [row["workflow_file"] for row in report["dispatch_plan"]],
            ["forward-maker-research.yml"],
        )
        self.assertTrue(report["invariants"]["canonical_v7_research_owners_only"])

    def test_non_research_authority_is_never_dispatchable(self) -> None:
        for owner in self.config["forbidden_workflows"]:
            report, _ = self.run([self.experiment("maker_bad_owner", owner)])
            self.assertEqual(report["dispatch_plan"], [])

    def test_one_dispatch_per_owner_per_cycle(self) -> None:
        report, _ = self.run([
            self.experiment("maker_a", priority=1),
            self.experiment("maker_b", priority=2),
            self.experiment("graph_a", priority=3),
        ])
        workflows = [row["workflow_file"] for row in report["dispatch_plan"]]
        self.assertEqual(workflows, ["forward-maker-research.yml", "arb-theory-hourly.yml"])
        self.assertEqual(len(workflows), len(set(workflows)))

    def test_experiment_cooldown_blocks_immediate_repeat(self) -> None:
        previous = {
            "experiments": {
                "maker_a": {
                    "experiment_id": "maker_a",
                    "owner_workflow": "forward-maker-research.yml",
                    "last_dispatch_ts": self.now - 60,
                }
            }
        }
        report, _ = self.run([self.experiment("maker_a")], previous=previous)
        self.assertEqual(report["dispatch_plan"], [])
        self.assertIn("experiment_cooldown", report["experiments"][0]["reason"])

    def test_running_owner_is_not_duplicated(self) -> None:
        runs = [
            {
                "databaseId": 1,
                "workflowName": "forward-maker-alpha-research",
                "status": "in_progress",
                "conclusion": "",
                "headBranch": "main",
                "updatedAt": self.now - 10,
            }
        ]
        report, _ = self.run([self.experiment("maker_a")], runs=runs)
        self.assertEqual(report["dispatch_plan"], [])
        self.assertIn("owner_workflow_already_running", report["experiments"][0]["reason"])

    def test_stale_alpha_factory_blocks_all_dispatches(self) -> None:
        alpha = self.alpha([self.experiment("maker_a")])
        alpha["generated_ts"] = self.now - 20000
        report, _ = research_director.build_report(self.config, alpha, {}, [], self.now)
        self.assertEqual(report["status"], "DEGRADED")
        self.assertEqual(report["dispatch_plan"], [])
        self.assertFalse(report["alpha_factory_fresh"])

    def test_economic_progress_uses_v7_runtime_and_candidate_evidence(self) -> None:
        report, state = self.run([])
        progress = report["economic_progress"]
        self.assertEqual(progress["candidate_observations"], 20)
        self.assertEqual(progress["positive_pnl_candidates"], 1)
        self.assertEqual(progress["runtime_total_fills"], 12)
        self.assertEqual(progress["execution_evidence_eligible_models"], 1)
        self.assertEqual(progress["state"], "PROGRESSING")
        self.assertEqual(state["economic_progress"]["signature"], progress["signature"])

    def test_same_signature_becomes_stagnant_after_threshold(self) -> None:
        prior = {
            "economic_progress": {
                "signature": {
                    "ready_candidates": 0,
                    "candidate_observations": 20,
                    "positive_pnl_candidates": 1,
                    "runtime_total_fills": 12,
                    "execution_evidence_eligible_models": 1,
                },
                "last_progress_ts": self.now - 8000,
            }
        }
        report, _ = self.run([], previous=prior)
        self.assertEqual(report["economic_progress"]["state"], "STAGNANT")
        self.assertGreaterEqual(report["economic_progress"]["seconds_since_progress"], 7200)


if __name__ == "__main__":
    unittest.main()
