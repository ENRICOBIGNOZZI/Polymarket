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

    def test_autoupdate_remains_paper_deploy_only(self):
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        self.assertIn('update_server_macos.sh', installer)
        self.assertNotIn('tiny_live_pilot.py', installer)
        self.assertNotIn('--execute', installer)

    def test_grafana_passwordless_access_is_loopback_viewer_only(self):
        runtime = (ROOT / "ops" / "apply_runtime_config_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        finish = (ROOT / "ops" / "finish_bootstrap_macos.sh").read_text(encoding="utf-8")

        self.assertIn('http_addr = 127.0.0.1', runtime)
        self.assertIn('enabled = true', runtime)
        self.assertIn('org_role = Viewer', runtime)
        self.assertNotIn('org_role = Admin', runtime)
        self.assertIn('apply_runtime_config_macos.sh', updater)
        self.assertIn('apply_runtime_config_macos.sh', finish)
        self.assertIn('http://127.0.0.1:3000/api/search', updater)
        self.assertIn('http://127.0.0.1:3000/api/search', finish)

    def test_autoupdate_deploys_only_live_smoke_validated_ref(self):
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")

        self.assertIn('${POLYMARKET_DEPLOY_REF:-paper-validated}', updater)
        self.assertIn('git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"', updater)
        self.assertIn('origin/$DEPLOY_REF', updater)
        self.assertIn('write_status awaiting_validation', updater)
        self.assertIn('paper_runtime_healthy()', updater)
        self.assertIn('recorder_alive', updater)
        self.assertIn('broker_alive', updater)
        self.assertIn('deploy_ref=%s', updater)
        self.assertIn('validated=%s', updater)
        self.assertIn('origin_main=%s', updater)

        self.assertIn('git fetch -q origin main paper-validated', health)
        self.assertIn('origin/paper-validated', health)
        self.assertIn('test "$head_sha" = "$validated_sha"', health)
        self.assertIn('test "$status_ref" = "paper-validated"', health)
        self.assertIn('test "$status_validated" = "$validated_sha"', health)

        self.assertIn('Advance paper validated ref', smoke)
        self.assertIn('git/refs/heads/paper-validated', smoke)
        self.assertIn('github.event_name != \'pull_request\'', smoke)
        self.assertNotIn("github.head_ref == 'implement/paper-live-oos-pilot-v4'", smoke)


if __name__ == "__main__":
    unittest.main()
