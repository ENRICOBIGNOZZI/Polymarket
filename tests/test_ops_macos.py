from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MacOSOpsContractTest(unittest.TestCase):
    def test_autoupdate_exports_required_environment(self) -> None:
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn('${POLYMARKET_APP_DIR:-$HOME/polymarket}', updater)
        self.assertIn('<key>EnvironmentVariables</key>', installer)
        self.assertIn('<key>HOME</key>', installer)
        self.assertIn('<key>PATH</key>', installer)
        self.assertIn('<key>POLYMARKET_APP_DIR</key>', installer)

    def test_homebrew_discovery_survives_minimal_path(self) -> None:
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        for script in (installer, updater):
            self.assertIn('find_brew()', script)
            self.assertIn('/opt/homebrew/bin/brew', script)
            self.assertIn('/usr/local/bin/brew', script)
            self.assertIn('BREW_BIN="$(find_brew)"', script)

    def test_autoupdate_remains_paper_only(self) -> None:
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn('update_server_macos.sh', installer)
        self.assertNotIn('--execute', installer)
        self.assertNotIn('--execute', updater)
        self.assertNotIn('wallet', updater.lower())
        self.assertIn("authenticated_execution'] is False", updater)

    def test_grafana_tailnet_viewer_uses_v7_home(self) -> None:
        runtime = (ROOT / "ops" / "apply_runtime_config_macos.sh").read_text(encoding="utf-8")
        bootstrap = (ROOT / "ops" / "bootstrap_macos.sh").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
        self.assertIn('http_addr = 127.0.0.1', runtime)
        self.assertIn('root_url = ${POLYMARKET_GRAFANA_URL}/', runtime)
        self.assertIn('org_role = Viewer', runtime)
        self.assertIn('default_home_dashboard_path = $APP_DIR/monitoring/grafana/dashboards/polymarket-multi-strategy.json', runtime)
        self.assertIn('/app/exporter_latest_v7.py', compose)
        self.assertIn('paper_v7_loop.sh', bootstrap)
        self.assertIn('exporter_latest_v7.py', bootstrap)

    def test_same_sha_repair_reapplies_runtime_config_before_restart(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        marker = 'if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then'
        repair = updater.split(marker, 1)[1].split('\nif git merge-base --is-ancestor', 1)[0]
        apply = 'bash "$APP_DIR/ops/apply_runtime_config_macos.sh"'
        restart = 'sudo -n /usr/local/sbin/polymarket-service-control restart'
        self.assertIn(apply, repair)
        self.assertIn(restart, repair)
        self.assertLess(repair.index(apply), repair.index(restart))

    def test_deployment_is_v7_only(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        linux = (ROOT / "ops" / "update_server.sh").read_text(encoding="utf-8")
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        smoke = (ROOT / ".github" / "workflows" / "v7-live-paper-smoke.yml").read_text(encoding="utf-8")
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        for text in (updater, linux, health, smoke, deploy):
            self.assertIn('paper_v7', text)
            for token in ('paper_v4', 'paper_v5', 'paper_v6', 'exporter_v4', 'exporter_v5', 'exporter_v6', 'v4-live-paper-smoke', 'polymarket_v6_'):
                self.assertNotIn(token, text)
        self.assertIn('${POLYMARKET_DEPLOY_REF:-paper-validated}', updater)
        self.assertIn('${POLYMARKET_DEPLOY_REF:-paper-validated}', linux)
        self.assertIn('write_status awaiting_validation', updater)
        self.assertIn('paper_runtime_healthy()', updater)
        self.assertIn('polymarket_v7_runtime_info', updater)
        self.assertIn('workflows: ["V7 live PAPER smoke"]', deploy)
        self.assertIn('champion_version=7', deploy)
        self.assertIn('- name: Advance paper validated ref', smoke)

    def test_failed_candidate_health_is_captured_before_rollback(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        diagnostics = (ROOT / "ops" / "capture_runtime_health_macos.sh").read_text(encoding="utf-8")
        self.assertIn('capture_runtime_health_diagnostics "$NEW_SHA"', updater)
        self.assertIn('rollback "post-deploy V7 PAPER runtime health checks failed"', updater)
        self.assertIn('candidate_health_diagnostics_begin', diagnostics)
        self.assertNotIn('printenv', diagnostics)
        self.assertNotIn('set -x', diagnostics)

    def test_bootstrap_services_start_v7_directly(self) -> None:
        linux = (ROOT / "ops" / "bootstrap_server.sh").read_text(encoding="utf-8")
        mac = (ROOT / "ops" / "bootstrap_macos.sh").read_text(encoding="utf-8")
        for text in (linux, mac):
            self.assertIn('paper_v7_loop.sh', text)
            self.assertIn('config/paper_v7.json', text)
            self.assertIn('runs/paper_v7_live', text)
            self.assertNotIn('paper_latest_loop.sh', text)


if __name__ == "__main__":
    unittest.main()
