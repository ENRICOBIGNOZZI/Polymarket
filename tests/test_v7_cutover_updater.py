from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7CutoverUpdaterContractTest(unittest.TestCase):
    def test_platform_wrappers_delegate_only_to_exact_sha_v7_updater(self) -> None:
        for rel in ("ops/update_server.sh", "ops/update_server_macos.sh"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("EXPECTED_VALIDATED_SHA", text)
            self.assertIn("origin/main", text)
            self.assertIn("origin/paper-validated", text)
            self.assertIn("ops/update_server_v7.sh", text)
            self.assertIn('git show "$EXPECTED_SHA:ops/update_server_v7.sh"', text)
            for forbidden in (
                "paper_latest_loop.sh",
                "runtime_contract_health.py",
                "monitoring/exporter.py",
                "monitoring/exporter_latest.py",
                "test_monitoring_exporter.py",
                "test_grafana_multi_strategy_contract.py",
            ):
                self.assertNotIn(forbidden, text)

    def test_canonical_updater_is_v7_only_same_sha_and_fail_closed(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        for required in (
            '[[ "$MAIN_SHA" == "$EXPECTED_SHA" ]]',
            '[[ "$VALIDATED_SHA" == "$EXPECTED_SHA" ]]',
            "scripts/v7_cutover_contract.py",
            "monitoring/v7_monitoring_manifest.json",
            "monitoring/exporter_v7.py",
            "monitoring/v7_ledger_metrics.py",
            "polymarket-v7.json",
            "assert_no_legacy_writer",
            "stop_v7_runtime",
            "failed_health",
            "authenticated_execution",
            "drawdown",
        ):
            self.assertIn(required, text)
        self.assertNotIn("git merge-base --is-ancestor \"$VALIDATED_SHA\" \"$MAIN_SHA\"", text)
        for forbidden in (
            "tests/test_monitoring_exporter.py",
            "tests/test_monitoring_latest_exporter.py",
            "tests/test_runtime_contract_health.py",
            "tests/test_grafana_multi_strategy_contract.py",
            "monitoring/exporter.py monitoring/exporter_latest.py",
            "v5_runtime_readiness.py",
        ):
            self.assertNotIn(forbidden, text)

        guard_start = text.index("assert_no_legacy_writer(){")
        guard_end = text.index("\n}\n\nstop_v7_runtime", guard_start) + 2
        guard = text[guard_start:guard_end]
        outside_guard = text[:guard_start] + text[guard_end:]
        self.assertIn('pgrep -af "$pattern"', guard)
        for legacy_writer in (
            "scripts/paper_v3_loop.sh",
            "scripts/paper_v4_loop.sh",
            "scripts/paper_v5_loop.sh",
            "scripts/paper_v6_loop.sh",
            "scripts/paper_latest_loop.sh",
        ):
            self.assertIn(legacy_writer, guard)
            self.assertNotIn(
                legacy_writer,
                outside_guard,
                f"{legacy_writer} may appear only in the fail-closed legacy-writer detector",
            )

    def test_candidate_validation_happens_before_active_checkout_mutation(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        validate = text.index("Validating exact V7 candidate")
        checkout = text.index('git checkout --detach "$EXPECTED_SHA"')
        self.assertLess(validate, checkout)
        health_fail = text.index("failed_health")
        stop_after = text.rfind("stop_v7_runtime", 0, health_fail)
        self.assertGreater(stop_after, checkout)

    def test_deploy_and_health_workflows_require_same_sha_identity(self) -> None:
        deploy = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text(encoding="utf-8")
        health = (ROOT / ".github/workflows/v7-paper-server-health.yml").read_text(encoding="utf-8")
        for text in (deploy, health):
            self.assertIn('test "$main_sha" = "$EXPECTED_VALIDATED_SHA"', text)
            self.assertIn('test "$validated_sha" = "$EXPECTED_VALIDATED_SHA"', text)
            self.assertNotIn('git merge-base --is-ancestor "$validated_sha" "$main_sha"', text)
        self.assertIn('git show "$validated_sha:ops/update_server_v7.sh"', deploy)
        self.assertNotIn("update_server_macos.sh", deploy)
        self.assertNotIn("updater_path=ops/update_server.sh", deploy)

    def test_automatic_deploy_transition_mismatches_are_noops_not_false_failures(self) -> None:
        deploy = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text(encoding="utf-8")
        self.assertIn(
            'if [[ "$GITHUB_EVENT_NAME" == "schedule" || "$GITHUB_EVENT_NAME" == "workflow_run" ]]; then',
            deploy,
        )
        self.assertIn("reconciliation_event=true", deploy)
        self.assertIn("no_op awaiting_same_sha", deploy)
        self.assertIn("no_op upstream_validation_did_not_advance", deploy)
        self.assertIn("no_op awaiting_enabled_v7_champion", deploy)
        self.assertIn('if [[ "$reconciliation_event" == "true" ]]; then', deploy)
        self.assertIn('echo "V7 deploy blocked: main and paper-validated are not the same SHA" >&2', deploy)
        self.assertIn('echo "V7 deploy blocked: canonical refs do not match the requested validated SHA" >&2', deploy)
        self.assertNotIn('reconciliation_event=true # workflow_dispatch', deploy)

    def test_automatic_health_transition_mismatches_are_noops_not_false_failures(self) -> None:
        health = (ROOT / ".github/workflows/v7-paper-server-health.yml").read_text(encoding="utf-8")
        self.assertIn(
            'if [[ "$GITHUB_EVENT_NAME" == "schedule" || "$GITHUB_EVENT_NAME" == "workflow_run" ]]; then',
            health,
        )
        self.assertIn("reconciliation_event=true", health)
        self.assertIn("no_op awaiting_same_sha", health)
        self.assertIn("no_op upstream_deploy_did_not_advance", health)
        self.assertIn("no_op awaiting_enabled_v7_champion", health)
        self.assertIn('echo "V7 health blocked: main and paper-validated are not the same SHA" >&2', health)
        self.assertIn('echo "V7 health blocked: canonical refs do not match the requested deployed SHA" >&2', health)
        self.assertNotIn('reconciliation_event=true # workflow_dispatch', health)

    def test_health_uses_v7_monitoring_manifest_not_legacy_project_context(self) -> None:
        health = (ROOT / ".github/workflows/v7-paper-server-health.yml").read_text(encoding="utf-8")
        self.assertIn("monitoring/v7_monitoring_manifest.json", health)
        self.assertIn("polymarket_v7_monitoring_manifest_v1", health)
        self.assertIn("polymarket_v7_ledger_", health)
        self.assertNotIn("config/project_context.json", health)


if __name__ == "__main__":
    unittest.main()
