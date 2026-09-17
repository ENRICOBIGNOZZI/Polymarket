from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CryptoOnlyRuntimeGatesTest(unittest.TestCase):
    def test_live_paper_validation_uses_single_crypto_scope(self) -> None:
        text = (ROOT / ".github/workflows/v7-live-paper-validation.yml").read_text(encoding="utf-8")
        self.assertIn("m.get('version') == 8", text)
        self.assertIn("m.get('live_algorithm_count') == 1", text)
        self.assertIn("{'CRYPTO_SETTLEMENT_ENGINE'}", text)
        self.assertNotIn("m.get('live_algorithm_count') == 2", text)

    def test_server_health_uses_single_crypto_scope_everywhere(self) -> None:
        text = (ROOT / ".github/workflows/v7-paper-server-health.yml").read_text(encoding="utf-8")
        self.assertIn("Verify crypto-only PAPER runtime", text)
        self.assertIn("allocation.get('engine_count')==1", text)
        self.assertIn("scope.get('version')==8", text)
        self.assertIn("scope.get('live_algorithm_count')==1", text)
        self.assertIn("^polymarket_v7_live_algorithm_count 1$", text)
        self.assertNotIn("Verify two-engine PAPER runtime", text)
        self.assertNotIn("scope.get('live_algorithm_count')==2", text)
        self.assertNotIn("allocation.get('engine_count')==2", text)
        self.assertNotIn("^polymarket_v7_live_algorithm_count 2$", text)

    def test_legacy_server_updater_health_gate_is_crypto_only(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn("^polymarket_v7_live_algorithm_count 1$", text)
        self.assertNotIn("^polymarket_v7_live_algorithm_count 2$", text)

    def test_portfolio_coordinator_reports_one_economic_engine(self) -> None:
        text = (ROOT / "scripts/v7_global_portfolio_coordinator.py").read_text(encoding="utf-8")
        self.assertIn('"economic_engine_count": 1', text)
        for stale in ("both V7 economic-engine", "both engines", "Structural opportunities"):
            self.assertNotIn(stale, text)

    def test_runtime_execution_replay_docs_are_crypto_only(self) -> None:
        runtime = (ROOT / "docs/RUNTIME.md").read_text(encoding="utf-8")
        replay = (ROOT / "docs/REPLAY.md").read_text(encoding="utf-8")
        execution = (ROOT / "docs/EXECUTION.md").read_text(encoding="utf-8")
        self.assertIn("single canonical `CRYPTO_SETTLEMENT_ENGINE`", runtime)
        self.assertIn("canonical Crypto Settlement Engine", replay)
        self.assertIn("complete-set opportunity", execution)
        blob = "\n".join((runtime, replay, execution))
        for stale in ("Structural Arbitrage Engine", "two canonical algorithms", "two engine identities", "structural arbitrage"):
            self.assertNotIn(stale, blob)


if __name__ == "__main__":
    unittest.main()
