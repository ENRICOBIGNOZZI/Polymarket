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

    def test_autoupdate_heartbeat_proves_main_is_deployed(self):
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")

        self.assertIn('autoupdate_status.env', updater)
        self.assertIn('checked_ts=', updater)
        self.assertIn('status=%s', updater)
        self.assertIn('write_status up_to_date', updater)
        self.assertIn('write_status deployed', updater)
        self.assertIn('write_status rollback', updater)
        self.assertIn('git fetch -q origin main', health)
        self.assertIn('test "$head_sha" = "$origin_sha"', health)
        self.assertIn('system/com.polymarket.autoupdate', health)
        self.assertIn('autoupdate_status.env', health)
        self.assertIn('now - checked_ts', health)
        self.assertIn('up_to_date|deployed', health)


if __name__ == "__main__":
    unittest.main()
