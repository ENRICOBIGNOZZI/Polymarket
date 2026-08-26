from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MacOSDeployMutexContractTest(unittest.TestCase):
    def test_v7_updater_serializes_checkout_mutation(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn("POLYMARKET_DEPLOY_LOCK_V1=1", updater)
        self.assertIn('DEPLOY_LOCK_DIR="${POLYMARKET_DEPLOY_LOCK_DIR:-$CACHE_DIR/update.lock}"', updater)
        self.assertIn('DEPLOY_LOCK_WAIT_SECONDS="${POLYMARKET_DEPLOY_LOCK_WAIT_SECONDS:-900}"', updater)
        self.assertIn('DEPLOY_LOCK_STALE_SECONDS="${POLYMARKET_DEPLOY_LOCK_STALE_SECONDS:-3600}"', updater)
        self.assertIn('while ! mkdir "$DEPLOY_LOCK_DIR" 2>/dev/null; do', updater)
        self.assertIn("Reclaiming stale deployment mutex", updater)
        self.assertIn("deployment mutex busy", updater)
        self.assertIn("trap release_deploy_lock EXIT", updater)
        acquire = updater.index("acquire_deploy_lock\n")
        fetch = updater.index('git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"')
        reset = updater.index('git reset --hard "$NEW_SHA"')
        self.assertLess(acquire, fetch)
        self.assertLess(fetch, reset)

    def test_candidate_runtime_is_stopped_before_checkout_mutation_and_rollback_is_bounded(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        stop = updater.index('polymarket-service-control stop')
        checkout = updater.index('git checkout "$LOCAL_BRANCH"')
        reset = updater.index('git reset --hard "$NEW_SHA"')
        self.assertLess(stop, checkout)
        self.assertLess(checkout, reset)
        rollback = updater.split("rollback() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('git reset --hard "$OLD_SHA"', rollback)
        self.assertIn("apply_runtime_config_macos.sh", rollback)
        self.assertIn("polymarket-service-control restart", rollback)
        self.assertIn("wait_for_runtime_health 90", rollback)

    def test_mutex_updater_has_no_legacy_runtime_compatibility(self) -> None:
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn("require_v7_manifest()", updater)
        self.assertIn("paper_runtime_healthy()", updater)
        self.assertIn("full_runtime_healthy()", updater)
        self.assertIn("paper_v7_live", updater)
        for forbidden in ("v5_runtime_readiness", "paper_v5", "paper_v6", "polymarket_v6_", "paper_latest_loop"):
            self.assertNotIn(forbidden, updater)


if __name__ == "__main__":
    unittest.main()
