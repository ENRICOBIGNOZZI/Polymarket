from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MacOSDeployMutexContractTest(unittest.TestCase):
    def test_updater_serializes_checkout_mutation(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn("POLYMARKET_DEPLOY_LOCK_V1=1", updater)
        self.assertIn('DEPLOY_LOCK_DIR="${POLYMARKET_DEPLOY_LOCK_DIR:-$CACHE_DIR/update.lock}"', updater)
        self.assertIn('DEPLOY_LOCK_WAIT_SECONDS="${POLYMARKET_DEPLOY_LOCK_WAIT_SECONDS:-900}"', updater)
        self.assertIn('DEPLOY_LOCK_STALE_SECONDS="${POLYMARKET_DEPLOY_LOCK_STALE_SECONDS:-3600}"', updater)
        self.assertIn('while ! mkdir "$DEPLOY_LOCK_DIR" 2>/dev/null; do', updater)
        self.assertIn('Reclaiming stale deployment mutex', updater)
        self.assertIn('deployment mutex busy', updater)
        self.assertIn('trap release_deploy_lock EXIT', updater)
        self.assertNotIn('wait_for_legacy_updater', updater)
        self.assertNotIn('pre-mutex', updater)

    def test_updater_fails_if_checkout_moves_despite_serialization(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn('FINAL_SHA="$(git -C "$APP_DIR" rev-parse HEAD)"', updater)
        self.assertIn('[[ "$FINAL_SHA" == "$NEW_SHA" ]] || rollback', updater)
        self.assertIn('checkout moved during serialized deployment', updater)

    def test_mutex_does_not_reintroduce_old_version_paths(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        for token in ('paper_v4', 'paper_v5', 'paper_v6', 'scripts/v4_', 'scripts/v5_', 'scripts/v6_', 'exporter_v4', 'exporter_v5', 'exporter_v6'):
            self.assertNotIn(token, updater)


if __name__ == "__main__":
    unittest.main()
