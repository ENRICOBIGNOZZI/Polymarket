from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MacOSOpsContractTest(unittest.TestCase):
    def test_autoupdate_launchd_exports_updater_environment(self):
        installer = (ROOT / "ops" / "install_autoupdate_macos.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")

        # update_server_macos.sh resolves APP_DIR/CACHE_DIR from HOME before it
        # discovers Homebrew, so a system LaunchDaemon must explicitly provide
        # HOME and a PATH containing the Homebrew prefix.
        self.assertIn('${POLYMARKET_APP_DIR:-$HOME/polymarket}', updater)
        self.assertIn('command -v brew', updater)
        self.assertIn('<key>EnvironmentVariables</key>', installer)
        self.assertIn('<key>HOME</key>', installer)
        self.assertIn('<key>PATH</key>', installer)
        self.assertIn('<key>POLYMARKET_APP_DIR</key>', installer)
        self.assertIn('BREW_PREFIX="$(brew --prefix)"', installer)
        self.assertIn('$BREW_PREFIX/bin:$BREW_PREFIX/sbin', installer)

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


if __name__ == "__main__":
    unittest.main()
