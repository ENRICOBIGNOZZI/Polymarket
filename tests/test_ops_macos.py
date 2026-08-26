from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MacOSOpsContractTest(unittest.TestCase):
    def test_autoupdate_launchd_exports_updater_environment(self):
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn('${POLYMARKET_APP_DIR:-$HOME/polymarket}', updater)
        self.assertIn('<key>EnvironmentVariables</key>', installer)
        self.assertIn('<key>HOME</key>', installer)
        self.assertIn('<key>PATH</key>', installer)
        self.assertIn('<key>POLYMARKET_APP_DIR</key>', installer)
        self.assertIn('$BREW_PREFIX/bin:$BREW_PREFIX/sbin', installer)

    def test_homebrew_discovery_survives_minimal_path(self):
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        for script in (installer, updater):
            self.assertIn('find_brew()', script)
            self.assertIn('/opt/homebrew/bin/brew', script)
            self.assertIn('/usr/local/bin/brew', script)
            self.assertIn('BREW_BIN="$(find_brew)"', script)
            self.assertIn('"$BREW_BIN" --prefix', script)

    def test_autoupdate_remains_paper_only(self):
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn('update_server_macos.sh', installer)
        self.assertNotIn('tiny_live_pilot.py', installer)
        self.assertNotIn('--execute', installer)
        self.assertNotIn('--execute', updater)
        self.assertNotIn('wallet', updater.lower())

    def test_grafana_tailnet_viewer_uses_multi_strategy_home(self):
        runtime = (ROOT / "ops" / "apply_runtime_config_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        finish = (ROOT / "ops" / "finish_bootstrap_macos.sh").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")

        self.assertIn('http_addr = 127.0.0.1', runtime)
        self.assertIn(
            'POLYMARKET_GRAFANA_URL="${POLYMARKET_GRAFANA_URL:-http://${TAILSCALE_FQDN}}"',
            runtime,
        )
        self.assertIn('actual_dns=', runtime)
        self.assertIn('tailscale DNS mismatch:', runtime)
        self.assertIn('root_url = ${POLYMARKET_GRAFANA_URL}/', runtime)
        self.assertNotIn('http_addr = 0.0.0.0', runtime)
        self.assertIn('serve --bg --http=80 localhost:3000', runtime)
        self.assertIn('[auth]\ndisable_login_form = true', runtime)
        self.assertIn('[auth.basic]\nenabled = false', runtime)
        self.assertIn('[auth.anonymous]\nenabled = true', runtime)
        self.assertIn('org_role = Viewer', runtime)
        self.assertNotIn('org_role = Admin', runtime)
        self.assertIn(
            'default_home_dashboard_path = $APP_DIR/monitoring/grafana/dashboards/polymarket-multi-strategy.json',
            runtime,
        )
        self.assertIn(
            'GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH: /var/lib/grafana/dashboards/polymarket-multi-strategy.json',
            compose,
        )
        self.assertIn('reporting_enabled = false', runtime)
        self.assertIn('apply_runtime_config_macos.sh', updater)
        self.assertIn('apply_runtime_config_macos.sh', finish)
        self.assertIn('http://127.0.0.1:3000/api/search', updater)

    def test_same_sha_repair_reapplies_runtime_config_before_restart(self):
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        marker = 'if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then'
        self.assertIn(marker, updater)
        repair = updater.split(marker, 1)[1].split('\nif git merge-base --is-ancestor', 1)[0]
        apply = 'bash "$APP_DIR/ops/apply_runtime_config_macos.sh"'
        restart = 'sudo -n /usr/local/sbin/polymarket-service-control restart'
        self.assertIn(apply, repair)
        self.assertIn(restart, repair)
        self.assertLess(repair.index(apply), repair.index(restart))
        self.assertIn('Runtime and Grafana configuration repaired', repair)

    def test_v5_cold_start_wait_uses_startup_aware_readiness(self):
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        readiness = (ROOT / "scripts" / "v5_runtime_readiness.py").read_text(encoding="utf-8")
        self.assertIn(
            'RUNTIME_HEALTH_ATTEMPTS="${POLYMARKET_RUNTIME_HEALTH_ATTEMPTS:-180}"',
            updater,
        )
        self.assertIn('local attempts="${1:-$RUNTIME_HEALTH_ATTEMPTS}"', updater)
        self.assertIn('POLYMARKET_RUNTIME_HEALTH_ATTEMPTS must be a positive integer', updater)
        self.assertIn('scripts/v5_runtime_readiness.py', updater)
        self.assertIn('--model-output-max-age 120', updater)
        self.assertIn('--startup-grace 600', updater)
        self.assertIn('model_output_max_age', readiness)
        self.assertIn('startup_grace', readiness)
        self.assertIn('allocator_events.csv', readiness)
        self.assertIn('models_alive', readiness)

    def test_deployment_uses_validated_ref_and_versioned_health(self):
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        linux_updater = (ROOT / "ops" / "update_server.sh").read_text(encoding="utf-8")
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")

        for script in (updater, linux_updater):
            self.assertIn('${POLYMARKET_DEPLOY_REF:-paper-validated}', script)
            self.assertIn('git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"', script)
            self.assertIn('origin/$DEPLOY_REF', script)
            self.assertIn('polymarket_runtime_pnl_usd', script)
            self.assertIn('polymarket_allocator_state_present', script)
            self.assertIn('polymarket_allocator_models_expected', script)
            self.assertIn('polymarket_model_info', script)
        self.assertIn('write_status awaiting_validation', updater)
        self.assertIn('paper_runtime_healthy()', updater)
        self.assertIn('full_runtime_healthy()', updater)
        self.assertIn('wait_for_runtime_health()', updater)
        self.assertIn('write_status repaired', updater)
        self.assertIn('polymarket-service-control restart', updater)
        self.assertIn('scripts/v5_runtime_readiness.py', updater)
        self.assertIn('polymarket_v6_exporter_info', updater)
        self.assertIn('hard_arb', updater)
        self.assertIn('paper_latest_loop.sh', linux_updater)
        self.assertIn('POLYMARKET_RUN_NAME=auto', linux_updater)

        self.assertIn('git fetch -q origin main paper-validated', health)
        self.assertIn('test "$head_sha" = "$validated_sha"', health)
        self.assertIn('polymarket_allocator_models_expected 5', health)
        self.assertIn('polymarket-multi-strategy-v5', health)
        self.assertIn('runs/paper_v5_live', health)
        self.assertIn('test "$allocator_alive" = "1"', health)
        self.assertIn('scripts/v5_runtime_readiness.py', health)
        self.assertIn('--startup-grace 600', health)
        self.assertIn('polymarket_v6_exporter_info', health)
        self.assertIn('hard_arb/status.json', health)

        self.assertIn('Advance paper validated ref', smoke)
        self.assertIn('git/refs/heads/paper-validated', smoke)
        self.assertIn("github.event_name != 'pull_request' && success()", smoke)
        self.assertIn('paper_v5_live', smoke)
        self.assertIn('adapter="v5"', smoke)

        self.assertIn('workflow_run:', deploy)
        self.assertIn('workflows: ["v4-live-paper-smoke"]', deploy)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", deploy)
        self.assertIn('EXPECTED_VALIDATED_SHA', deploy)
        self.assertIn('git show "$validated_sha:$updater_path"', deploy)
        self.assertIn('POLYMARKET_DEPLOY_REF=paper-validated bash "$updater"', deploy)
        self.assertIn('[[ "$version" == "5" || "$version" == "6" ]]', deploy)
        self.assertIn('adapter=\\"v${version}\\"', deploy)
        self.assertIn('polymarket_v6_exporter_info', deploy)
        self.assertIn('hard_arb/status.json', deploy)
        self.assertIn('polymarket-multi-strategy-v5', deploy)

    def test_deploy_verifier_reports_named_failures_without_weakening_checks(self):
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        self.assertIn("fail_verify()", deploy)
        self.assertIn("require_equal()", deploy)
        self.assertIn("require_metric()", deploy)
        self.assertIn("require_file()", deploy)
        for label in (
            "server_head_matches_validated",
            "validated_not_ancestor_of_main",
            "validated_matches_trigger_sha",
            "exporter_healthz",
            "runtime_info_schema",
            "runtime_pnl_metric",
            "allocator_state_metric",
            "allocator_models_expected_metric",
            "model_info_metric",
            "v6_exporter_info_metric",
            "v6_model_fills_metric",
            "v6_model_gross_exposure_metric",
            "v6_model_drawdown_metric",
            "v6_model_alive_metric",
            "v6_model_staleness_metric",
            "v6_hard_arb_status_file",
            "v6_local_factor_status_file",
            "v6_runtime_status_file",
            "prometheus_ready",
            "grafana_health",
            "grafana_dashboard_missing",
        ):
            self.assertIn(f'"{label}"', deploy)
        self.assertIn("verify_result=success", deploy)
        self.assertNotIn("|| true", deploy.split("- name: Verify production paper services", 1)[1])

    def test_remote_verifiers_are_compatible_with_macos_bash3(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        for workflow in (health, deploy):
            self.assertNotIn("readarray", workflow)
            self.assertNotIn("mapfile", workflow)
            self.assertIn("IFS=$'\\t' read -r", workflow)

    def test_server_health_serializes_with_paper_deploy(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        shared_lock = "concurrency:\n  group: polymarket-paper-server\n  cancel-in-progress: false"
        self.assertIn(shared_lock, health)
        self.assertIn(shared_lock, deploy)
        self.assertNotIn("group: polymarket-paper-server-health", health)
        self.assertIn(
            "/bin/launchctl print system/com.polymarket.autoupdate >/dev/null",
            health,
        )
        self.assertNotIn(
            "sudo -n launchctl print system/com.polymarket.autoupdate",
            health,
        )

    def test_service_status_redacts_launchd_environment(self):
        control = (ROOT / "ops" / "macos_service_control.sh").read_text(encoding="utf-8")
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        self.assertIn("print_safe_status()", control)
        self.assertIn("/usr/bin/awk", control)
        self.assertIn("active count|path|state|program|pid|last exit code", control)
        self.assertNotIn("sed -n '1,30p'", control)
        self.assertIn(
            'Darwin) bash ops/macos_service_control.sh status || fail_verify "macos_service_status" ;;',
            deploy,
        )
        self.assertNotIn("Darwin) sudo -n /usr/local/sbin/polymarket-service-control status ;;", deploy)

    def test_bootstrap_services_follow_the_champion_manifest(self):
        linux = (ROOT / "ops" / "bootstrap_server.sh").read_text(encoding="utf-8")
        mac = (ROOT / "ops" / "bootstrap_macos.sh").read_text(encoding="utf-8")
        latest = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
        self.assertIn('config/live_champion.json', linux)
        self.assertIn('paper_latest_loop.sh', linux)
        self.assertIn('POLYMARKET_RUN_NAME=auto', linux)
        self.assertIn('paper_latest_loop.sh', mac)
        self.assertIn('config/live_champion.json', latest)
        self.assertIn('automatic validated integration', latest)


if __name__ == "__main__":
    unittest.main()
