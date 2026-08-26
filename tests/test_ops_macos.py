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

    def test_grafana_tailnet_viewer_snapshots_exact_assets_outside_checkout(self):
        runtime = (ROOT / "ops" / "apply_runtime_config_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        finish = (ROOT / "ops" / "finish_bootstrap_macos.sh").read_text(encoding="utf-8")
        access = (ROOT / ".github" / "workflows" / "grafana-access.yml").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")

        self.assertIn('http_addr = 127.0.0.1', runtime)
        self.assertIn(
            'POLYMARKET_GRAFANA_URL="${POLYMARKET_GRAFANA_URL:-http://${TAILSCALE_FQDN}}"',
            runtime,
        )
        self.assertIn('POLYMARKET_GRAFANA_ASSET_DIR', runtime)
        self.assertIn('GRAFANA_STATE_DASHBOARD_DIR="$STATE_DIR/grafana/dashboards"', runtime)
        self.assertIn('dashboard_sha256=', runtime)
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
            'default_home_dashboard_path = $GRAFANA_STATE_DASHBOARD_DIR/$CANONICAL_DASHBOARD',
            runtime,
        )
        self.assertIn('path: "$GRAFANA_STATE_DASHBOARD_DIR"', runtime)
        self.assertIn('polymarket-grafana-assets.tgz', access)
        self.assertIn('DASHBOARD_SHA256', access)
        self.assertIn('/api/dashboards/uid/$DASHBOARD_UID', access)
        self.assertIn(
            'GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH: /var/lib/grafana/dashboards/polymarket-multi-strategy.json',
            compose,
        )
        self.assertIn('reporting_enabled = false', runtime)
        self.assertIn('apply_runtime_config_macos.sh', updater)
        self.assertIn('apply_runtime_config_macos.sh', finish)

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

    def test_v5_is_explicit_compatibility_not_future_version_policy(self):
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        readiness = (ROOT / "scripts" / "v5_runtime_readiness.py").read_text(encoding="utf-8")
        self.assertIn(
            'RUNTIME_HEALTH_ATTEMPTS="${POLYMARKET_RUNTIME_HEALTH_ATTEMPTS:-180}"',
            updater,
        )
        self.assertIn('local attempts="${1:-$RUNTIME_HEALTH_ATTEMPTS}"', updater)
        self.assertIn('POLYMARKET_RUNTIME_HEALTH_ATTEMPTS must be a positive integer', updater)
        self.assertIn('if (( version == 5 )); then', updater)
        self.assertIn('scripts/v5_runtime_readiness.py', updater)
        self.assertIn('scripts/runtime_contract_health.py', updater)
        self.assertIn('--model-output-max-age 120', updater)
        self.assertIn('--startup-grace 600', updater)
        self.assertIn('model_output_max_age', readiness)
        self.assertIn('startup_grace', readiness)

    def test_deployment_uses_validated_ref_and_version_neutral_health(self):
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
            self.assertIn('polymarket_runtime_equity_usd', script)
            self.assertIn('scripts/runtime_contract_health.py', script)
            self.assertIn('polymarket_runtime_contract_present', script)
            self.assertIn('/api/dashboards/uid/', script)

        self.assertIn('write_status awaiting_validation', updater)
        self.assertIn('paper_runtime_healthy()', updater)
        self.assertIn('full_runtime_healthy()', updater)
        self.assertIn('wait_for_runtime_health()', updater)
        self.assertIn('write_status repaired', updater)
        self.assertIn('polymarket-service-control restart', updater)

        self.assertIn('git fetch -q origin main paper-validated', health)
        self.assertIn('test "$head_sha" = "$validated_sha"', health)
        self.assertIn('scripts/runtime_contract_health.py', health)
        self.assertIn('polymarket_runtime_contract_present 1', health)
        self.assertIn('/api/dashboards/uid/$GRAFANA_DASHBOARD_UID', health)

        self.assertIn('Advance paper validated ref', smoke)
        self.assertIn('git/refs/heads/paper-validated', smoke)
        self.assertIn("github.event_name != 'pull_request' && success()", smoke)
        self.assertIn('Version-neutral V7+ PAPER runtime smoke', smoke)
        self.assertIn('scripts/runtime_contract_health.py', smoke)
        self.assertIn('polymarket_runtime_contract_present 1', smoke)

        self.assertIn('workflow_run:', deploy)
        self.assertIn('workflows: ["v4-live-paper-smoke"]', deploy)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", deploy)
        self.assertIn('EXPECTED_VALIDATED_SHA', deploy)
        self.assertIn('git show "$validated_sha:$updater_path"', deploy)
        self.assertIn('POLYMARKET_DEPLOY_REF=paper-validated bash "$updater"', deploy)
        self.assertIn('scripts/runtime_contract_health.py', deploy)
        self.assertIn('runtime_contract_metric', deploy)
        self.assertIn('/api/dashboards/uid/$dashboard_uid', deploy)

    def test_generic_operational_surfaces_do_not_cap_future_versions(self):
        paths = (
            ROOT / "monitoring" / "exporter_latest.py",
            ROOT / "ops" / "update_server_macos.sh",
            ROOT / "ops" / "update_server.sh",
            ROOT / ".github" / "workflows" / "deploy-paper-server.yml",
            ROOT / ".github" / "workflows" / "server-health.yml",
            ROOT / ".github" / "workflows" / "v4-live-smoke.yml",
        )
        forbidden = (
            'unsupported champion version',
            'unsupported paper champion',
            '[[ "$version" == "5" || "$version" == "6" ]]',
            'if version not in (5,6)',
        )
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{path} reintroduced a fixed version ceiling: {token}")
        self.assertIn('import_module(f"exporter_v{major}")', paths[0].read_text(encoding="utf-8"))
        self.assertNotIn('for v in range(major', paths[0].read_text(encoding="utf-8"))

    def test_deploy_verifier_reports_generic_named_failures_without_weakening_checks(self):
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        self.assertIn("fail_verify()", deploy)
        self.assertIn("require_equal()", deploy)
        self.assertIn("require_metric()", deploy)
        for label in (
            "server_head_matches_validated",
            "validated_not_ancestor_of_main",
            "validated_matches_trigger_sha",
            "runtime_contract_health",
            "exporter_healthz",
            "runtime_info_schema",
            "runtime_pnl_metric",
            "runtime_equity_metric",
            "runtime_contract_metric",
            "prometheus_ready",
            "grafana_health",
            "grafana_dashboard_missing",
        ):
            self.assertIn(f'"{label}"', deploy)
        self.assertIn("verify_result=success", deploy)
        verifier = deploy.split("- name: Verify production PAPER contract", 1)[1]
        self.assertNotIn('curl -fsS http://127.0.0.1:9108/healthz >/dev/null || true', verifier)
        self.assertNotIn('curl -fsS http://127.0.0.1:3000/api/health >/dev/null || true', verifier)

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

    def test_failed_candidate_health_is_captured_before_rollback(self):
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        diagnostics = (ROOT / "ops" / "capture_runtime_health_macos.sh").read_text(encoding="utf-8")
        failure = (
            'if ! wait_for_runtime_health; then\n'
            '  capture_runtime_health_diagnostics "$NEW_SHA"\n'
            '  rollback "post-deploy runtime contract health checks failed"'
        )
        self.assertIn(failure, updater)
        self.assertIn('ops/capture_runtime_health_macos.sh', updater)
        self.assertIn('candidate_health_diagnostics_begin', diagnostics)
        self.assertIn('candidate_expected_sha=', diagnostics)
        self.assertIn('candidate_actual_sha=', diagnostics)
        self.assertIn('exporter_healthz', diagnostics)
        self.assertIn('prometheus_ready', diagnostics)
        self.assertIn('grafana_health', diagnostics)
        self.assertNotIn('printenv', diagnostics)
        self.assertNotIn('set -x', diagnostics)

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
