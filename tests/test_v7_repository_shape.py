from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEXT_SCAN_ALLOWLIST = {
    "artifacts/v7_repository_convergence_audit.json",  # immutable forensic inventory
    "config/live_champion.json",          # explicit prohibition/history
    "config/operator_directives.json",    # explicit prohibition/history
    "scripts/v7_archive_market_universe.py",  # archive boundary documentation
}
RETIRED_TEXT = re.compile(
    r"(?i)(?:paper[_-]?v[1-6]|v[1-6][_-](?:runtime|broker|ledger|scheduler|config|paper)|"
    r"(?:fallback|start|run)[_-]?v[1-6]|v6_local_factor_intents)"
)

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
    "scripts/v7_market_maker_worker.py",
    "scripts/v7_local_factor_core_base.py",
    "scripts/v7_pca_stat_arb_core_base.py",
    "src/fast_arb_main.cpp",
    "tests/test_fast_runtime_contract.py",
    "tests/test_v7_canonical_convergence_policy.py",
    "tests/test_v7_control_plane_exact_head.py",
    "tests/test_v7_paper_evidence_router.py",
    "tests/test_v7_paper_entrypoint_cutover.py",
    "tests/test_v7_unified_evidence_runtime.py",
}


class V7RepositoryShapeTest(unittest.TestCase):
    def test_forbidden_control_plane_and_retired_surfaces_are_absent(self) -> None:
        present = sorted(path for path in FORBIDDEN_PATHS if (ROOT / path).exists())
        self.assertEqual(present, [])

    def test_no_versioned_v3_v6_paths_remain(self) -> None:
        bad = []
        repository_paths = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
        ).decode("utf-8").split("\0")
        for raw in repository_paths:
            if raw.startswith(".private_validation/"):
                continue
            rel = raw.lower()
            if any(token in rel for token in ("paper_v3", "paper_v4", "paper_v5", "paper_v6", "/v3_", "/v4_", "/v5_", "/v6_", "_v3.", "_v4.", "_v5.", "_v6.")):
                bad.append(rel)
        self.assertEqual(sorted(bad), [])

    def test_all_tracked_operational_text_has_no_retired_generation_surface(self) -> None:
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT
        ).decode("utf-8").split("\0")
        bad: list[str] = []
        for rel in tracked:
            if not rel or rel in TEXT_SCAN_ALLOWLIST or rel.startswith("tests/"):
                continue
            path = ROOT / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            match = RETIRED_TEXT.search(text)
            if match:
                bad.append(f"{rel}:{match.group(0)}")
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
        self.assertEqual(manifest["deployment_ref"], "main")
        self.assertEqual(manifest["promotion_policy"], "operator_approved_exact_main_sha")
        self.assertEqual(
            set(manifest),
            {
                "schema_version", "enabled", "version", "loop", "config", "run_root",
                "deployment_ref", "promotion_policy", "paper_only", "authenticated_execution",
                "real_order_submission", "candidate_only_until_promoted", "reason",
            },
        )

    def test_canonical_v7_surfaces_exist(self) -> None:
        required = (
            "scripts/paper_v7_execution_loop.sh",
            "scripts/v7_execution_ledger.py",
            "scripts/v7_canonical_economics.py",
            "scripts/v7_capital_allocator.py",
            "scripts/v7_portfolio_guard.py",
            "scripts/v7_local_factor_primitives.py",
            "scripts/v7_pca_stat_arb_primitives.py",
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

    def test_main_ruleset_is_machine_actionable_and_requires_unique_v7_checks(self) -> None:
        ruleset = json.loads((ROOT / "artifacts/github_main_ruleset.json").read_text(encoding="utf-8"))
        self.assertEqual(ruleset["target"], "branch")
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        by_type = {rule["type"]: rule for rule in ruleset["rules"]}
        self.assertIn("deletion", by_type)
        self.assertIn("non_fast_forward", by_type)
        self.assertIn("required_linear_history", by_type)
        required = by_type["required_status_checks"]["parameters"]["required_status_checks"]
        self.assertEqual(
            {row["context"] for row in required},
            {"ci-v7-Release", "ci-v7-Debug", "monitoring-v7", "single-writer-v7"},
        )

    def test_forensic_audit_classifies_every_remote_branch_and_external_blocker(self) -> None:
        audit = json.loads((ROOT / "artifacts/v7_repository_convergence_audit.json").read_text(encoding="utf-8"))
        branches = audit["remote_branches"]["items"]
        self.assertEqual(len(branches), audit["remote_branches"]["initial_count_including_main"])
        self.assertEqual([row["name"] for row in branches if row["classification"] == "KEEP_CANONICAL"], ["main"])
        self.assertTrue(all(row["classification"] == "DELETE" for row in branches if row["name"] != "main"))
        self.assertEqual(audit["remote_branches"]["target_count"], 1)
        self.assertEqual(audit["github_governance"]["classification"], "BLOCKER")


if __name__ == "__main__":
    unittest.main()
