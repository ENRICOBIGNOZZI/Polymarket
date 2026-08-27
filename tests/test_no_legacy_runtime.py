from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PATHS = {
    ".github/actions/project-context/action.yml",
    ".github/pull_request_template.md",
    ".github/workflows/admin-supervisor.yml",
    ".github/workflows/arb-theory-hourly.yml",
    ".github/workflows/external-intelligence.yml",
    ".github/workflows/fast-arb-hourly.yml",
    ".github/workflows/integration-merge.yml",
    ".github/workflows/operator-authority-gate.yml",
    ".github/workflows/promotion-controller.yml",
    ".github/workflows/research-policy.yml",
    ".github/workflows/research-queue.yml",
    ".github/workflows/v7-unified-paper-evidence.yml",
    "config/autonomous_research.json",
    "config/experiment_registry.json",
    "config/external_intelligence.json",
    "config/fast_arb_policy.json",
    "config/fast_arb_relations.csv",
    "config/fast_arb_v7_shadow.json",
    "config/project_context.json",
    "config/promotion_policy.json",
    "config/research_director.json",
    "config/scheduler_registry.json",
    "config/v7_evidence_runtime.json",
    "scripts/admin_supervisor_report.py",
    "scripts/arb_theory_scheduler.py",
    "scripts/external_intelligence.py",
    "scripts/external_request_policy.py",
    "scripts/hard_safety_policy.py",
    "scripts/integration_base_gate.py",
    "scripts/integration_gate.py",
    "scripts/project_context_snapshot.py",
    "scripts/promotion_gate.py",
    "scripts/research_common.py",
    "scripts/research_director.py",
    "scripts/research_pr_policy.py",
    "scripts/research_queue_report.py",
    "scripts/run_external_intelligence.py",
    "scripts/validate_project_context.py",
    "scripts/validate_scheduler_registry.py",
    "scripts/v7_canonical_convergence_policy.py",
    "scripts/v7_evidence_candidate_contract.py",
    "src/fast_arb_main.cpp",
    "tests/test_fast_runtime_contract.py",
    "tests/test_v7_canonical_convergence_policy.py",
    "tests/test_v7_control_plane_exact_head.py",
    "tests/test_v7_paper_evidence_router.py",
    "tests/test_v7_paper_entrypoint_cutover.py",
    "tests/test_v7_unified_evidence_runtime.py",
}


class NoLegacyRuntimeTest(unittest.TestCase):
    def test_forbidden_control_plane_and_legacy_surfaces_are_absent(self) -> None:
        present = sorted(path for path in FORBIDDEN_PATHS if (ROOT / path).exists())
        self.assertEqual(present, [])

    def test_no_versioned_v3_v6_paths_remain(self) -> None:
        bad = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix().lower()
            if any(token in rel for token in ("paper_v3", "paper_v4", "paper_v5", "paper_v6", "/v3_", "/v4_", "/v5_", "/v6_", "_v3.", "_v4.", "_v5.", "_v6.")):
                bad.append(rel)
        self.assertEqual(sorted(bad), [])

    def test_workflows_are_v7_or_core_validation_only(self) -> None:
        allowed = {"ci.yml", "monitoring.yml", "private-runtime-single-writer-validation.yml"}
        bad = []
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            if path.name not in allowed and not path.name.startswith("v7-"):
                bad.append(path.name)
        self.assertEqual(sorted(bad), [])

    def test_champion_is_v7_only(self) -> None:
        manifest = json.loads((ROOT / "config/live_champion.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["version"], 7)
        self.assertEqual(manifest["loop"], "scripts/paper_v7_execution_loop.sh")
        self.assertEqual(manifest["config"], "config/paper_v7.json")
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertFalse(manifest["real_order_submission"])
        self.assertFalse(manifest["legacy_fallback_allowed"])

    def test_canonical_v7_surfaces_exist(self) -> None:
        required = (
            "scripts/paper_v7_execution_loop.sh",
            "scripts/v7_execution_ledger.py",
            "scripts/v7_canonical_economics.py",
            "scripts/v7_capital_allocator.py",
            "scripts/v7_portfolio_guard.py",
            "config/paper_v7.json",
            "config/v7_professional_market_maker.json",
            "monitoring/exporter_v7.py",
            "monitoring/grafana/dashboards/polymarket-v7.json",
            ".github/workflows/v7-live-paper-validation.yml",
            ".github/workflows/v7-deploy-paper-server.yml",
            ".github/workflows/v7-paper-server-health.yml",
        )
        missing = [path for path in required if not (ROOT / path).is_file()]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
