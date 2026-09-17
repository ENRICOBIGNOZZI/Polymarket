from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from v7_multi_crypto_performance import render_prometheus, summarize_multi_crypto

SHA = "a" * 40


def registry() -> dict:
    return {"contexts": [
        {"asset": asset, "horizon": horizon, "enabled": True,
         "research_only": not (asset == "BTC" and horizon == "M5"),
         "authority": "PAPER_EXPLORATION" if (asset, horizon) == ("BTC", "M5") else "SHADOW_ZERO_AUTHORITY"}
        for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
        for horizon in ("M5", "M15")
    ]}


def models() -> dict:
    return {"models": [
        {"asset": asset, "horizon": horizon, "new_risk_authorized": False}
        for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
        for horizon in ("M5", "M15")
    ]}


def portfolio() -> dict:
    return {"drawdown": 0.01, "engines": {
        "CRYPTO_SETTLEMENT_ENGINE": {"budget": 5000.0, "equity": 5002.0}
    }}


class MultiCryptoPerformanceTest(unittest.TestCase):
    def _summarize(self, root: Path, canonical_realized: float = 0.0, canonical_mtime_ms: float | None = None):
        return summarize_multi_crypto(
            root, expected_sha=SHA, portfolio=portfolio(),
            canonical={"strategy_net_pnl": {"CRYPTO_SETTLEMENT_ENGINE": canonical_realized}},
            global_coordinator={"crypto_correlation_risk": {
                "gross_crypto_exposure_usd": 10.0,
                "net_directional_crypto_exposure_usd": 4.0,
                "correlated_crypto_cluster_exposure_usd": 7.0,
                "per_asset_exposure_usd": {"BTC": 4.0, "ETH": 6.0},
                "per_horizon_exposure_usd": {"M5": 4.0, "M15": 6.0},
                "oracle_concentration_fraction": 0.7,
                "exchange_source_concentration_fraction": 0.6,
            }},
            crypto_registry=registry(), crypto_model_registry=models(), ledger_valid=True,
            canonical_mtime_ms=canonical_mtime_ms,
        )

    def test_no_economic_evidence_is_na_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "ledger").mkdir(); (root / "ledger/execution.jsonl").write_text("")
            summary = self._summarize(root)
            text = "\n".join(render_prometheus(summary))
            self.assertEqual(summary["registered_lanes"], 12)
            self.assertEqual(summary["known_economic_lanes"], 0)
            self.assertIn('polymarket_mc_lane_economic_evidence_present{asset="ETH",horizon="M15"} 0', text)
            self.assertNotIn('polymarket_mc_lane_realized_pnl_usd{asset="ETH",horizon="M15"}', text)


    def test_nested_coordinator_exposure_is_exported_without_zero_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "ledger").mkdir(); (root / "ledger/execution.jsonl").write_text("")
            summary = self._summarize(root)
            text = "\n".join(render_prometheus(summary))
            self.assertEqual(summary["portfolio"]["candidate_gross_exposure"], 10.0)
            self.assertEqual(summary["exposure"]["per_asset"]["ETH"], 6.0)
            self.assertIn('polymarket_mc_coordinator_candidate_asset_exposure_usd{asset="ETH"} 6', text)
            self.assertIn('polymarket_mc_coordinator_candidate_horizon_exposure_usd{horizon="M15"} 6', text)
            self.assertIn('polymarket_mc_coordinator_candidate_oracle_concentration_ratio 0.7', text)

    def test_frozen_lead_lag_is_attributed_to_btc_m5(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "ledger").mkdir()
            rows = [
                {"event_type": "FILL", "strategy": "CRYPTO_SETTLEMENT_ENGINE", "model_sha": SHA,
                 "paper_only": True, "authenticated_execution": False, "position_id": "p1",
                 "fill_price": 0.4, "filled_size": 10, "fee": 0.1,
                 "metadata": {"model_family": "lead_lag_taker_v1"}},
                {"event_type": "FINAL", "strategy": "CRYPTO_SETTLEMENT_ENGINE", "model_sha": SHA,
                 "paper_only": True, "authenticated_execution": False, "position_id": "p1",
                 "final_pnl": 1.5, "metadata": {"model_family": "lead_lag_taker_v1", "won": True}},
            ]
            (root / "ledger/execution.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
            summary = self._summarize(root, 1.5)
            lane = next(x for x in summary["lanes"] if x["asset"] == "BTC" and x["horizon"] == "M5")
            self.assertEqual(lane["realized_pnl"], 1.5)
            self.assertEqual(lane["fills"], 1); self.assertEqual(lane["finals"], 1)
            self.assertEqual(lane["open_positions"], 0)
            self.assertEqual(lane["open_cost_at_risk"], 0.0)
            self.assertTrue(summary["attribution"]["reconciled"])
            text = "\n".join(render_prometheus(summary))
            self.assertIn('polymarket_mc_strategy_realized_pnl_usd{asset="BTC",horizon="M5",strategy="LEAD_LAG_TAKER_V1"} 1.5', text)
            self.assertIn('polymarket_mc_lane_win_rate{asset="BTC",horizon="M5"} 1', text)

    def test_generic_context_supports_non_btc_lane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "ledger").mkdir()
            context = {"asset": "ETH", "horizon": "M15"}
            rows = [
                {"event_type": "FILL", "strategy": "CRYPTO_SETTLEMENT_ENGINE", "model_sha": SHA,
                 "paper_only": True, "authenticated_execution": False, "position_id": "p2",
                 "fill_price": 0.5, "filled_size": 4, "fee": 0.02,
                 "metadata": {"model_family": "shared_lead_lag_v2", "crypto_context": context}},
                {"event_type": "FINAL", "strategy": "CRYPTO_SETTLEMENT_ENGINE", "model_sha": SHA,
                 "paper_only": True, "authenticated_execution": False, "position_id": "p2",
                 "final_pnl": -0.5, "metadata": {"model_family": "shared_lead_lag_v2", "crypto_context": context, "won": False}},
            ]
            (root / "ledger/execution.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
            summary = self._summarize(root, -0.5)
            lane = next(x for x in summary["lanes"] if x["asset"] == "ETH" and x["horizon"] == "M15")
            self.assertEqual(lane["realized_pnl"], -0.5)
            self.assertTrue(summary["attribution"]["reconciled"])


    def test_shadow_runtime_is_separate_zero_authority_source(self) -> None:
        from v7_multi_crypto_performance import render_shadow_prometheus, summarize_shadow_runtime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "control").mkdir()
            status = {
                "schema": "polymarket_v7_multi_crypto_shadow_runtime_status_v1",
                "timestamp_ns": 1_000_000_000, "code_sha": "b" * 40, "state": "RUNNING_SHADOW",
                "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
                "real_capital_at_risk": False, "execution_authority": False, "automatic_promotion": False,
                "external_ready_assets": 6, "oracle_healthy_assets": 6,
                "contract_active_markets": 12, "contract_active_ready_markets": 12,
                "contract_all_active_ready": True, "book_evidence_complete": True,
                "label_evidence_complete": True, "feature_tape_emitted": 123,
                "children": {"a": {"alive": True}, "b": {"alive": True}},
            }
            (root / "control/runtime_status.json").write_text(json.dumps(status))
            summary = summarize_shadow_runtime(root, now_ns=2_000_000_000)
            self.assertTrue(summary["safe"]); self.assertTrue(summary["ready"])
            self.assertEqual(summary["age_seconds"], 1.0)
            text = "\n".join(render_shadow_prometheus(summary))
            self.assertIn("polymarket_mc_shadow_external_ready_assets 6", text)
            self.assertIn("polymarket_mc_shadow_contract_ready_markets 12", text)
            self.assertIn("polymarket_mc_shadow_children_alive 2", text)


    def test_stale_canonical_uses_complete_ledger_provisionally_without_false_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "ledger").mkdir()
            rows = [
                {"event_type": "FILL", "strategy": "CRYPTO_SETTLEMENT_ENGINE", "model_sha": SHA,
                 "paper_only": True, "authenticated_execution": False, "position_id": "p3",
                 "recorded_ts_ms": 1_900, "fill_price": 0.4, "filled_size": 10, "fee": 0.1,
                 "metadata": {"model_family": "lead_lag_taker_v1"}},
                {"event_type": "FINAL", "strategy": "CRYPTO_SETTLEMENT_ENGINE", "model_sha": SHA,
                 "paper_only": True, "authenticated_execution": False, "position_id": "p3",
                 "recorded_ts_ms": 2_000, "final_pnl": 1.5,
                 "metadata": {"model_family": "lead_lag_taker_v1", "won": True}},
            ]
            (root / "ledger/execution.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
            pending = self._summarize(root, 0.0, canonical_mtime_ms=1_500)
            self.assertEqual(pending["attribution"]["state"], "PENDING_CANONICAL_REFRESH")
            self.assertEqual(pending["attribution"]["status_code"], 1)
            self.assertTrue(pending["attribution"]["canonical_stale_vs_ledger"])
            self.assertEqual(pending["portfolio"]["realized_pnl"], 1.5)
            self.assertEqual(pending["attribution"]["display_realized_source"], "CANONICAL_LEDGER_PROVISIONAL")
            text = "\n".join(render_prometheus(pending))
            self.assertIn("polymarket_mc_attribution_status_code 1", text)
            self.assertIn("polymarket_mc_canonical_stale_vs_ledger 1", text)
            self.assertIn("polymarket_mc_realized_pnl_usd 1.5", text)

            diverged = self._summarize(root, 0.0, canonical_mtime_ms=2_500)
            self.assertEqual(diverged["attribution"]["state"], "DIVERGED")
            self.assertEqual(diverged["attribution"]["status_code"], 0)
            self.assertFalse(diverged["attribution"]["canonical_stale_vs_ledger"])
            self.assertEqual(diverged["portfolio"]["realized_pnl"], 0.0)

    def test_unattributed_final_fails_reconciliation_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "ledger").mkdir()
            row = {"event_type": "FINAL", "strategy": "CRYPTO_SETTLEMENT_ENGINE", "model_sha": SHA,
                   "paper_only": True, "authenticated_execution": False, "final_pnl": 2.0, "metadata": {}}
            (root / "ledger/execution.jsonl").write_text(json.dumps(row)+"\n")
            summary = self._summarize(root, 2.0)
            self.assertFalse(summary["attribution"]["reconciled"])
            self.assertEqual(summary["attribution"]["unattributed_final_rows"], 1)


if __name__ == "__main__":
    unittest.main()
