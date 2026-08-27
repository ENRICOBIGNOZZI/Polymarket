from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MacOSDeployMutexContractTest(unittest.TestCase):
    def test_updater_serializes_checkout_mutation_and_migrates_legacy_launchd(self):
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")

        self.assertIn("POLYMARKET_DEPLOY_LOCK_V7=1", updater)
        self.assertIn('DEPLOY_LOCK_DIR="${POLYMARKET_DEPLOY_LOCK_DIR:-$CACHE_DIR/update.lock}"', updater)
        self.assertIn('DEPLOY_LOCK_WAIT_SECONDS="${POLYMARKET_DEPLOY_LOCK_WAIT_SECONDS:-900}"', updater)
        self.assertIn('DEPLOY_LOCK_STALE_SECONDS="${POLYMARKET_DEPLOY_LOCK_STALE_SECONDS:-3600}"', updater)
        self.assertIn('while ! mkdir "$DEPLOY_LOCK_DIR" 2>/dev/null; do', updater)
        self.assertIn('Reclaiming stale deployment mutex', updater)
        self.assertIn('deployment mutex busy', updater)
        self.assertIn('trap release_deploy_lock EXIT', updater)
        self.assertIn('/usr/bin/pgrep -f "$APP_DIR/ops/update_server_macos.sh"', updater)
        self.assertIn('legacy pre-mutex updater still running', updater)

        first_wait = updater.index("wait_for_legacy_updater\n")
        fetch = updater.index('git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"')
        self.assertLess(first_wait, fetch)

        validation = updater.index('log "Candidate validation passed; staging production build"')
        second_wait = updater.index("wait_for_legacy_updater\n", validation)
        checkout = updater.index('git checkout "$LOCAL_BRANCH"', validation)
        reset = updater.index('git reset --hard "$NEW_SHA"', validation)
        self.assertLess(second_wait, checkout)
        self.assertLess(checkout, reset)

        cleanup = updater.split("cleanup() {", 1)[1].split("}\ntrap cleanup EXIT", 1)[0]
        self.assertIn("release_deploy_lock", cleanup)

    def test_updater_fails_if_checkout_moves_despite_serialization(self):
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn('FINAL_SHA="$(git -C "$APP_DIR" rev-parse HEAD)"', updater)
        self.assertIn('if [[ "$FINAL_SHA" != "$NEW_SHA" ]]; then', updater)
        self.assertIn('checkout moved during serialized deployment', updater)
        self.assertIn('rollback', updater.split('FINAL_SHA="$(git -C "$APP_DIR" rev-parse HEAD)"', 1)[1])


if __name__ == "__main__":
    unittest.main()
