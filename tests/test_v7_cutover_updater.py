from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7CutoverUpdaterTest(unittest.TestCase):
    def test_deploy_lock_is_owned_and_only_expired_orphans_are_recovered(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        function = text[text.index("acquire_deploy_lock(){"):text.index("write_status(){")]
        self.assertIn('LOCK_STALE_SECONDS="${POLYMARKET_DEPLOY_LOCK_STALE_SECONDS:-7200}"', text)
        self.assertIn('kill -0 "$owner_pid"', function)
        self.assertIn('age_seconds < LOCK_STALE_SECONDS', function)
        self.assertIn('mv "$LOCK_DIR" "$quarantine"', function)
        self.assertIn('! -name owner_pid ! -name started_at ! -name nonce', function)
        self.assertIn('printf \'%s\\n\' "$$" > "$LOCK_DIR/owner_pid"', function)
        self.assertIn('"$(cat "$LOCK_DIR/nonce"', function)
        self.assertNotIn('rm -rf "$LOCK_DIR"', function)

    def test_live_lock_recovery_is_limited_to_verified_orphan_updater(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        function = text[text.index("recover_orphaned_live_deploy(){"):text.index("acquire_deploy_lock(){")]
        self.assertIn('POLYMARKET_DEPLOY_ORPHAN_GRACE_SECONDS:-300', text)
        self.assertIn('[[ "$nonce" =~ ^([0-9a-f]{40})', function)
        self.assertIn('nonce timestamp mismatch', function)
        self.assertIn('merge-base --is-ancestor "$owner_sha" "$EXPECTED_SHA"', function)
        self.assertIn('ps -p "$owner_pid" -o ppid=,command=', function)
        self.assertIn('[[ "$parent_pid" =~ ^[1-9][0-9]*$ && "$command_line" == *bash* ]]', function)
        self.assertIn("status.get('state') == 'running'", function)
        self.assertIn("status.get('expected_sha') == owner_sha", function)
        self.assertIn("status.get('server_head') == owner_sha", function)
        self.assertIn("exact V7 SHA started; monitoring health pending", function)
        self.assertIn("runtime.get('model_sha') == owner_sha", function)
        self.assertIn("runtime.get('paper_only') is True", function)
        self.assertIn("runtime.get('authenticated_execution') is False", function)
        self.assertIn("runtime.get('real_order_submission') is False", function)
        self.assertIn('runtime_pid != owner_pid', function)
        self.assertIn("if ! python3 -", function)
        self.assertIn("runtime/deploy status proof invalid", function)
        self.assertLess(function.index("if ! python3 -"), function.index("then\n    log"))
        self.assertIn('kill -TERM "$owner_pid"', function)
        self.assertIn('kill -KILL "$owner_pid"', function)
        self.assertNotIn("pkill", function)

    def test_external_ws_stop_token_has_explicit_apple_libcpp_fallback(self) -> None:
        header = (ROOT / "include/pm/v7_external_ws.hpp").read_text(encoding="utf-8")
        source = (ROOT / "src/v7_external_ws.cpp").read_text(encoding="utf-8")
        self.assertIn("!defined(__APPLE__)", header)
        self.assertIn("PM_V7_EXTERNAL_USE_STD_STOP_TOKEN", header)
        self.assertIn("class ExternalStopToken final", header)
        self.assertIn("const std::atomic<bool>* requested_", header)
        self.assertIn("void run(ExternalStopToken stop)", header)
        self.assertIn("void bounded_backoff(ExternalStopToken stop", source)
        self.assertNotIn("std::stop_token stop", source)

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

    def test_transient_monitoring_uses_the_same_canonical_state_as_launchd(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        function = text[text.index("start_monitoring(){"):text.index("runtime_health(){")]
        self.assertIn('monitoring_root="$APP_DIR/runs/monitoring"', function)
        self.assertIn('--storage.tsdb.path="$monitoring_root/prometheus"', function)
        self.assertIn('GF_PATHS_DATA="$monitoring_root/grafana/data"', function)
        self.assertIn('GF_PATHS_LOGS="$monitoring_root/grafana/logs"', function)
        self.assertIn('GF_PATHS_PLUGINS="$monitoring_root/grafana/plugins"', function)
        self.assertNotIn('$STATE_DIR/prometheus-data', function)
        self.assertNotIn('$STATE_DIR/grafana/data', function)
        self.assertNotIn('$STATE_DIR/grafana/log', function)
        self.assertNotIn('$STATE_DIR/grafana/plugins', function)

    def test_exact_deploy_receipt_is_written_before_monitoring_health_gate(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        start = text.rindex("start_production_runtime\n")
        receipt = text.index("record_deployed_sha\n", start)
        monitoring = text.index("start_monitoring\n", receipt)
        self.assertLess(start, receipt)
        self.assertLess(receipt, monitoring)
        self.assertIn('write_status running "exact V7 SHA started; monitoring health pending"', text)

    def test_exact_sha_transition_archives_prior_evidence_before_new_runtime(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        stop = text.rindex("stop_production_runtime\n")
        stop_monitoring = text.index("stop_owned_monitoring\n", stop)
        archive = text.index('python3 "$CUTOVER_ARCHIVER"', stop_monitoring)
        start = text.rindex("start_production_runtime\n")
        self.assertLess(stop, stop_monitoring)
        self.assertLess(stop_monitoring, archive)
        self.assertLess(archive, start)
        self.assertIn('paper_v7_archives', text)
        self.assertIn('POLYMARKET_CUTOVER_ARCHIVER', text)
        self.assertNotIn('rm -rf "$(production_run_root)"', text)

    def test_health_failure_emits_endpoint_and_monitoring_log_diagnostics(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn("=== V7 DEPLOY EPOCH expected_sha=%s started_at=%s ===", text)
        self.assertIn('tail -n "+$log_epoch_line" "$runtime_log"', text)
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

    def test_monitoring_cutover_cannot_reuse_stale_sha_listener(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn("stop_stale_monitoring_listener exporter 9108", text)
        self.assertIn("stop_stale_monitoring_listener prometheus 9090", text)
        self.assertIn("stop_stale_monitoring_listener grafana 3000", text)
        self.assertIn("refusing to replace unknown listener", text)
        self.assertIn("polymarket_external_fair_present 1", text)
        self.assertIn("http://127.0.0.1:9108/external-fair.json", text)
        self.assertIn("'exporter': {'@APP_DIR@':app", text)
        self.assertIn("'prometheus': {'@APP_DIR@':app", text)
        self.assertIn("'grafana': {'@APP_DIR@':app", text)
        self.assertIn("f'com.polymarket.v7.{name}.plist.in'", text)
        self.assertIn('launchctl bootout "$domain/com.polymarket.v7.$label"', text)
        self.assertIn('launchctl bootstrap "$domain" "$destination"', text)

    def test_updater_requires_exact_approved_main_sha(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn('[[ "$MAIN_SHA" == "$EXPECTED_SHA" ]]', text)
        self.assertIn('[[ "$(git rev-parse HEAD)" == "$EXPECTED_SHA" ]]', text)
        self.assertIn('[[ "$DEPLOY_REF" == "main" ]]', text)

    def test_updater_contains_no_retired_generation_or_fallback_logic(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8").lower()
        for forbidden in (
            "paper_v3",
            "paper_v4",
            "paper_v5",
            "paper_v6",
            "paper_latest",
            "polymarket_engine",
            "rollback_v6",
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
