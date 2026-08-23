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


if __name__ == "__main__":
    unittest.main()
