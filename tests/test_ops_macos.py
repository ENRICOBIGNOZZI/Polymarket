from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MacOSOpsContractTest(unittest.TestCase):
    def test_homebrew_discovery_survives_minimal_path(self) -> None:
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        for script in (installer, updater):
            self.assertIn("find_brew()", script)
            self.assertIn("/opt/homebrew/bin/brew", script)
            self.assertIn("/usr/local/bin/brew", script)
        self.assertIn('BREW_BIN="$(find_brew)"', updater)
        self.assertIn('"$BREW_BIN" --prefix', updater)

    def test_autoupdate_remains_paper_only(self) -> None:
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn("update_server_macos.sh", installer)
        self.assertNotIn("--execute", installer + updater)
        self.assertNotIn("wallet", updater.lower())
        self.assertIn("authenticated_execution", updater)
        self.assertIn('[[ "$paper" == "true" && "$auth" == "false" ]]', updater)

    def test_grafana_tailnet_viewer_is_v7_only(self) -> None:
        runtime = (ROOT / "ops" / "apply_runtime_config_macos.sh").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
        self.assertIn("http_addr = 127.0.0.1", runtime)
        self.assertIn("serve --bg --http=80 localhost:3000", runtime)
        self.assertIn("[auth.anonymous]\nenabled = true", runtime)
        self.assertIn("org_role = Viewer", runtime)
        self.assertNotIn("org_role = Admin", runtime)
        self.assertIn("polymarket-v7.json", runtime)
        self.assertIn("dashboard=polymarket-v7-paper", runtime)
        self.assertIn("/app/exporter_v7.py", compose)
        self.assertIn("polymarket-v7.json", compose)
        for forbidden in ("polymarket-multi-strategy-v5", "paper_v6", "exporter_latest"):
            self.assertNotIn(forbidden, runtime + compose)

    def test_same_sha_repair_reapplies_runtime_config_before_restart(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        marker = 'if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then'
        self.assertIn(marker, updater)
        repair = updater.split(marker, 1)[1].split('\nif git merge-base --is-ancestor', 1)[0]
        apply = 'bash "$APP_DIR/ops/apply_runtime_config_macos.sh"'
        restart = 'sudo -n /usr/local/sbin/polymarket-service-control restart'
        self.assertIn(apply, repair)
        self.assertIn(restart, repair)
        self.assertLess(repair.index(apply), repair.index(restart))
        self.assertIn("Runtime and Grafana configuration repaired", repair)

    def test_deployment_uses_validated_ref_and_v7_health(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        linux = (ROOT / "ops" / "update_server.sh").read_text(encoding="utf-8")
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        validator = (ROOT / ".github" / "workflows" / "v7-live-paper-validation.yml").read_text(encoding="utf-8")
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        for script in (updater, linux):
            self.assertIn('${POLYMARKET_DEPLOY_REF:-paper-validated}', script)
            self.assertIn('git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"', script)
            self.assertIn("paper_v7_live", script)
            self.assertIn("polymarket_runtime_pnl_usd", script)
            self.assertIn("polymarket_allocator_models_expected", script)
            self.assertNotIn("paper_latest_loop", script)
            self.assertNotIn("paper_v6", script)
        self.assertIn("write_status awaiting_validation", updater)
        self.assertIn("paper_runtime_healthy()", updater)
        self.assertIn("full_runtime_healthy()", updater)
        self.assertIn("wait_for_runtime_health()", updater)
        self.assertIn("POLYMARKET_DEPLOY_LOCK_V1=1", updater)
        self.assertIn("polymarket-v7-paper", health)
        self.assertIn("legacy_runtime_process_present", health)
        self.assertIn("Advance paper validated ref", validator)
        self.assertIn("git/refs/heads/paper-validated", validator)
        self.assertIn('workflows: ["v7-live-paper-validation"]', deploy)
        self.assertIn("champion_not_v7", deploy)
        self.assertIn("v7_hard_arb_status_file", deploy)
        for forbidden in ("v4-live-paper-smoke", "paper_v5", "paper_v6", "polymarket_v6_"):
            self.assertNotIn(forbidden, updater + linux + health + validator + deploy)

    def test_remote_verifiers_are_compatible_with_macos_bash3(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        for workflow in (health, deploy):
            self.assertNotIn("readarray", workflow)
            self.assertNotIn("mapfile", workflow)
            self.assertIn("IFS=$'\\t' read -r", workflow)

    def test_server_health_serializes_with_paper_deploy(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        shared_lock = "concurrency:\n  group: polymarket-paper-server\n  cancel-in-progress: false"
        self.assertIn(shared_lock, health)
        self.assertIn(shared_lock, deploy)

    def test_service_status_redacts_launchd_environment(self) -> None:
        control = (ROOT / "ops" / "macos_service_control.sh").read_text(encoding="utf-8")
        self.assertIn("print_safe_status()", control)
        self.assertIn("/usr/bin/awk", control)
        self.assertIn("active count|path|state|program|pid|last exit code", control)

    def test_failed_candidate_health_is_captured_before_rollback(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        diagnostics = (ROOT / "ops" / "capture_runtime_health_macos.sh").read_text(encoding="utf-8")
        failure = 'if ! wait_for_runtime_health; then\n  capture_runtime_health_diagnostics "$NEW_SHA"\n  rollback "post-deploy V7 runtime health checks failed"'
        self.assertIn(failure, updater)
        for token in ("candidate_health_diagnostics_begin", "candidate_expected_sha=", "exporter_healthz", "prometheus_ready", "grafana_health", "v7_supervisor.json", "execution/runtime_status.json", "v7_market_proxy"):
            self.assertIn(token, diagnostics)
        for forbidden in ("runtime_supervisor.csv", "polymarket_v6_", "local_factor_status.json"):
            self.assertNotIn(forbidden, diagnostics)

    def test_bootstrap_services_start_v7_directly(self) -> None:
        linux = (ROOT / "ops" / "bootstrap_server.sh").read_text(encoding="utf-8")
        mac = (ROOT / "ops" / "bootstrap_macos.sh").read_text(encoding="utf-8")
        for script in (linux, mac):
            self.assertIn("config/live_champion.json", script)
            self.assertIn("scripts/paper_v7_loop.sh", script)
            self.assertIn("config/paper_v7.json", script)
            self.assertIn("paper_v7_live", script)
            self.assertNotIn("paper_latest_loop", script)
            self.assertNotIn("exporter_latest", script)


if __name__ == "__main__":
    unittest.main()
