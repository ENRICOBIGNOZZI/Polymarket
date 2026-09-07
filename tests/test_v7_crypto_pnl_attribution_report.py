from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.v7_crypto_pnl_attribution_report import build


class CryptoPnlAttributionReportTests(unittest.TestCase):
    @staticmethod
    def write_jsonl(path: Path, rows: list[dict]) -> None:
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    @staticmethod
    def decision(market: str, snapshot: str, *, action: str = "TAKE", wealth: float = 0.25) -> dict:
        attribution = {
            "settlement_alpha": wealth + 0.05,
            "spread_capture": -0.02,
            "rebate": 0.0,
            "fees": -0.01,
            "slippage": 0.0,
            "adverse_selection": 0.0,
            "latency": -0.02,
            "inventory": 0.0,
            "unwind": 0.0,
            "cancel": 0.0,
            "capital": 0.0,
        }
        return {
            "schema": "polymarket_v7_crypto_execution_alpha_decision_v1",
            "report": {
                "market_id": market,
                "source_snapshot_identity": snapshot,
                "selected_action": {
                    "action": action,
                    "conservative_expected_wealth_change": wealth,
                    "attribution": attribution,
                },
                "best_point_action": {"point_expected_wealth_change": wealth + 0.10},
                "maker_information_probe_recommended": action == "NOTHING",
            },
        }

    @staticmethod
    def fill(record: str, *, fee: float) -> dict:
        return {
            "paper_only": True,
            "strategy": "CRYPTO_INFORMED_TAKER",
            "event_type": "FILL",
            "record_id": record,
            "fee": fee,
        }

    @staticmethod
    def final(
        record: str,
        market: str,
        pnl: float,
        *,
        settlement_alpha: float | None,
        crossing: float = -0.05,
        fees: float = -0.01,
        latency: float = -0.02,
    ) -> dict:
        metadata = {
            "pnl_decomposition": {
                "trading_pnl": pnl,
                "spread_capture": 0.0,
                "adverse_markout": 0.0,
                "inventory_pnl": 0.0,
                "maker_rebates": 0.0,
                "liquidity_rewards": 0.0,
            }
        }
        if settlement_alpha is not None:
            expected = {
                "settlement_alpha": settlement_alpha,
                "crossing_and_spread": crossing,
                "fees": fees,
                "execution_latency_risk": latency,
            }
            expected["total_expected_wealth_change"] = sum(expected.values())
            expected["identity_verified"] = True
            point = dict(expected)
            point["settlement_alpha"] += 0.10
            point["total_expected_wealth_change"] += 0.10
            metadata["expected_pnl_attribution"] = expected
            metadata["point_pnl_attribution"] = point
        return {
            "paper_only": True,
            "strategy": "CRYPTO_INFORMED_TAKER",
            "event_type": "FINAL",
            "complete": True,
            "record_id": record,
            "market_id": market,
            "intended_action": "TAKE",
            "final_pnl": pnl,
            "metadata": metadata,
        }

    def test_forward_expected_and_realized_are_separate_and_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            decisions = root / "decisions.jsonl"
            ledger = root / "ledger.jsonl"
            # First snapshot is duplicated: report must treat the causal cut once.
            self.write_jsonl(decisions, [
                self.decision("m1", "s1", wealth=0.25),
                self.decision("m1", "s1", wealth=0.25),
                self.decision("m2", "s2", action="NOTHING", wealth=0.0),
            ])
            self.write_jsonl(ledger, [
                self.fill("f1", fee=0.03),
                self.fill("f2", fee=0.04),
                self.final("x1", "m1", 1.20, settlement_alpha=0.40),
                self.final("x2", "m2", -0.30, settlement_alpha=0.20),
            ])
            report = build(decisions, ledger)
            surface = report["decision_surface"]
            executed = report["executed_paper"]
            self.assertEqual(surface["raw_decision_rows"], 3)
            self.assertEqual(surface["unique_causal_snapshots"], 2)
            self.assertEqual(surface["selected_action_counts"], {"NOTHING": 1, "TAKE": 1})
            self.assertEqual(surface["maker_information_probe_recommendations"], 1)
            self.assertAlmostEqual(executed["realized_cash_pnl"], 0.90)
            self.assertEqual(executed["terminal_positions"], 2)
            self.assertEqual(executed["independent_markets"], 2)
            self.assertEqual(executed["terminal_positions_with_frozen_expected_attribution"], 2)
            self.assertEqual(executed["terminal_positions_without_frozen_expected_attribution"], 0)
            self.assertAlmostEqual(executed["observed_fees"], 0.07)
            expected = 0.40 + 0.20 + 2 * (-0.05 - 0.01 - 0.02)
            self.assertAlmostEqual(executed["frozen_conservative_expected_wealth_change"], expected)
            self.assertAlmostEqual(executed["realized_minus_frozen_conservative_expected"], 0.90 - expected)
            self.assertEqual(executed["expected_attribution_identity_failures"], [])
            self.assertFalse(report["separation_contract"]["frozen_expected_attribution_is_realized_pnl"])
            self.assertTrue(report["separation_contract"]["canonical_final_cash_pnl_is_realized_pnl"])

    def test_legacy_final_is_never_retroactively_component_imputed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            decisions = root / "decisions.jsonl"; decisions.write_text("")
            ledger = root / "ledger.jsonl"
            self.write_jsonl(ledger, [
                self.final("legacy", "m0", 2.0, settlement_alpha=None),
                self.final("forward", "m1", 1.0, settlement_alpha=0.30),
            ])
            executed = build(decisions, ledger)["executed_paper"]
            self.assertEqual(executed["terminal_positions"], 2)
            self.assertEqual(executed["terminal_positions_with_frozen_expected_attribution"], 1)
            self.assertEqual(executed["terminal_positions_without_frozen_expected_attribution"], 1)
            self.assertIsNone(executed["realized_minus_frozen_conservative_expected"])
            self.assertIn("do not infer", executed["historical_unattributed_residual_policy"])

    def test_broken_expected_attribution_identity_is_surfaced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); decisions = root / "decisions.jsonl"; decisions.write_text("")
            ledger = root / "ledger.jsonl"
            row = self.final("bad", "m1", 1.0, settlement_alpha=0.30)
            row["metadata"]["expected_pnl_attribution"]["total_expected_wealth_change"] += 1.0
            self.write_jsonl(ledger, [row])
            executed = build(decisions, ledger)["executed_paper"]
            self.assertEqual(executed["expected_attribution_identity_failures"], ["bad"])


if __name__ == "__main__":
    unittest.main()
