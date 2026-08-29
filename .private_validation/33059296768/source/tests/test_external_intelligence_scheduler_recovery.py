#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "meta_supervisor_v2", SCRIPTS / "meta_supervisor_v2.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ExternalIntelligenceSchedulerRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config" / "alpha_factory.json").read_text(encoding="utf-8"))
        self.now = 1_800_000_000
        self.main = "a" * 40

    def workflow_run(self, workflow_name: str, run_id: int) -> dict:
        return {
            "databaseId": run_id,
            "workflowName": workflow_name,
            "status": "completed",
            "conclusion": "success",
            "headSha": self.main,
            "headBranch": "main",
            "event": "schedule",
            "createdAt": self.now - 70,
            "updatedAt": self.now - 60,
            "url": f"https://example.invalid/run/{run_id}",
        }

    def test_external_intelligence_is_allowlisted_for_bounded_recovery(self) -> None:
        coordination = self.config["coordination"]
        spec = coordination["workflows"]["external-intelligence.yml"]
        self.assertEqual(spec["name"], "Polymarket External Intelligence")
        self.assertTrue(spec["requires_current_main"])
        self.assertTrue(spec["dispatchable"])
        self.assertEqual(spec["max_age_seconds"], 7200)
        self.assertEqual(spec["dependencies"], ["live-smoke.yml"])
        self.assertIn("external-intelligence.yml", coordination["allowlisted_dispatches"])
        self.assertNotIn("external-intelligence.yml", coordination["forbidden_dispatches"])
        self.assertTrue(self.config["paper_only"])
        self.assertFalse(self.config["allow_authenticated_execution"])

    def test_missing_external_intelligence_run_enters_recovery_plan(self) -> None:
        runs = []
        for index, (filename, spec) in enumerate(self.config["coordination"]["workflows"].items(), start=1):
            if filename == "external-intelligence.yml":
                continue
            runs.append(self.workflow_run(spec["name"], index))

        snapshot = {
            "main_sha": self.main,
            "paper_validated_sha": self.main,
            "paper_validated_is_ancestor": True,
            "server_deploy_enabled": False,
            "runs": runs,
        }
        report = module.build_report(self.config, snapshot, self.now)
        state = report["workflow_status"]["external-intelligence.yml"]
        self.assertEqual(state["state"], "missing")
        self.assertTrue(state["dispatch_needed"])
        self.assertIn(
            "external-intelligence.yml",
            [item["workflow_file"] for item in report["dispatch_plan"]],
            report,
        )
        self.assertFalse(report["invariants"]["authenticated_execution"])


if __name__ == "__main__":
    unittest.main()
