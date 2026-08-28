from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7CutoverUpdaterTest(unittest.TestCase):
    def test_updater_prevalidates_exact_candidate_before_checkout_mutation(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn('log "Validating exact V7 candidate $EXPECTED_SHA before active-checkout mutation"', text)
        self.assertIn('git -C "$APP_DIR" worktree add --detach "$candidate" "$EXPECTED_SHA"', text)
        self.assertIn("scripts/v7_cutover_contract.py", text)
        self.assertIn("ctest --test-dir build --output-on-failure", text)
        self.assertLess(text.index("prevalidate_candidate"), text.rindex('git checkout --detach "$EXPECTED_SHA"'))

    def test_updater_recreates_active_cmake_build_in_final_path(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        function = text[text.index("build_current_checkout(){"):text.index("start_production_runtime(){")]
        self.assertIn('rm -rf "$APP_DIR/build"', function)
        self.assertIn('cmake -S "$APP_DIR" -B "$APP_DIR/build"', function)
        self.assertLess(function.index('rm -rf "$APP_DIR/build"'), function.index('cmake -S "$APP_DIR" -B "$APP_DIR/build"'))
        self.assertNotIn('-B "$APP_DIR/build.next"', function)
        self.assertNotIn('mv "$APP_DIR/build.next"', function)

    def test_updater_stops_only_registered_v7_runtime_owner(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn("control/runtime_status.json", text)
        self.assertIn('Stopping production V7 pid=$pid only', text)
        self.assertIn('force_stop_production_tree "$pid"', text)
        self.assertIn('["ps", "-axo", "pid=,ppid="]', text)
        self.assertNotIn("pkill -f", text)
        self.assertNotIn("pgrep -af", text)

    def test_runtime_drain_is_bounded_and_escalates_only_owned_tree(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        function = text[text.index("stop_production_runtime(){"):text.index("monitoring_contract(){")]
        self.assertIn('for _ in $(seq 1 "$DRAIN_ATTEMPTS")', function)
        self.assertIn('if kill -0 "$pid" 2>/dev/null; then', function)
        self.assertIn('force_stop_production_tree "$pid"', function)
        self.assertIn('survived bounded owned-tree termination', function)
        self.assertIn("return 0", function)
        self.assertNotIn('for _ in $(seq 1 300)', function)

    def test_monitoring_processes_are_bounded_and_force_stopped_if_needed(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        function = text[text.index("stop_owned_monitoring(){"):text.index("start_monitoring(){")]
        self.assertIn('kill -TERM "$pid"', function)
        self.assertIn('for _ in $(seq 1 50)', function)
        self.assertIn('kill -KILL "$pid"', function)
        self.assertIn('survived bounded shutdown', function)

    def test_exact_deploy_receipt_is_written_before_monitoring_health_gate(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        start = text.rindex("start_production_runtime\n")
        receipt = text.index("record_deployed_sha\n", start)
        monitoring = text.index("start_monitoring\n", receipt)
        self.assertLess(start, receipt)
        self.assertLess(receipt, monitoring)
        self.assertIn('write_status running "exact V7 SHA started; monitoring health pending"', text)

    def test_health_failure_emits_endpoint_and_monitoring_log_diagnostics(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn("runtime_health_diagnostics(){", text)
        for required in (
            "exporter_health",
            "exporter_metrics",
            "prometheus_ready",
            "grafana_health",
            "grafana_search",
            "grafana_dashboard",
            "grafana-v7.log",
            "prometheus-v7.log",
            "monitoring-exporter.log",
        ):
            self.assertIn(required, text)

    def test_updater_deploys_canonical_v7_loop_recorder_and_health_surfaces(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        for required in (
            "scripts/paper_v7_execution_loop.sh",
            "config/paper_v7.json",
            "polymarket_v7_trade_recorder",
            "control/runtime_status.json",
            "control/portfolio_state.json",
            "control/allocations/manifest.json",
            "control/research_sleeves_manifest.json",
            "scripts/v7_research_shadow_supervisor.py",
            "config/v7_live_model_scope.json",
            "graph_rv/status.json",
            "canonical_economics.json",
            "ledger/execution.jsonl",
            "trade_tape.csv",
            "monitoring/exporter_v7.py",
            "polymarket_v7_ledger_valid 1",
            "api/dashboards/uid",
            "control/deployed_sha",
        ):
            self.assertIn(required, text)

    def test_updater_requires_main_and_paper_validated_same_exact_sha(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn('[[ "$MAIN_SHA" == "$EXPECTED_SHA" ]]', text)
        self.assertIn('[[ "$VALIDATED_SHA" == "$EXPECTED_SHA" ]]', text)
        self.assertIn('[[ "$(git rev-parse HEAD)" == "$EXPECTED_SHA" ]]', text)

    def test_updater_contains_no_legacy_generation_or_fallback_logic(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8").lower()
        for forbidden in (
            "paper_v3",
            "paper_v4",
            "paper_v5",
            "paper_v6",
            "paper_latest",
            "polymarket_engine",
            "legacy_compatibility",
            "start_legacy",
            "rollback_v6",
            "paper-validated~",
        ):
            self.assertNotIn(forbidden, text)

    def test_monitoring_manifest_is_v2_and_canonical(self) -> None:
        manifest = json.loads((ROOT / "monitoring/v7_monitoring_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "polymarket_v7_monitoring_manifest_v2")
        self.assertEqual(manifest["version"], 7)
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertIn("control/runtime_status.json", manifest["required_surfaces"])
        self.assertIn("control/research_sleeves_manifest.json", manifest["required_surfaces"])
        self.assertIn("canonical_economics.json", manifest["required_surfaces"])
        self.assertIn("ledger/execution.jsonl", manifest["required_surfaces"])


if __name__ == "__main__":
    unittest.main()
