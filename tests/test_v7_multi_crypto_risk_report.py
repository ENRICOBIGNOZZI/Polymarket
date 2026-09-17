from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_lead_lag_replay import ReplayError
from v7_multi_crypto_risk_report import build

BASE_MS = 1_800_000_000_000


def report(asset: str, *, horizon: str = "M5", pending: bool = False, scale: float = 1.0) -> dict:
    rows = []
    values = [1.0, -2.0, 3.0, -1.0]
    for index, value in enumerate(values):
        rows.append({
            "market_id": f"{asset}-{index}", "asset": asset, "horizon": horizon,
            "reservation_id": f"r-{asset}-{index}", "parent_shock_id": f"shock-{index}",
            "authorized_ms": BASE_MS + index * 60_000,
            "submitted": True, "fill_records": 1, "filled_size": 5.0,
            "resolved": not (pending and index == 3),
            "pnl": None if (pending and index == 3) else value * scale,
            "status": "FILLED_PENDING_SETTLEMENT" if (pending and index == 3) else "RESOLVED",
        })
    return {
        "schema": "polymarket_v7_multi_crypto_forward_report_v1",
        "experiment_id": f"{asset}-{horizon}-cohort", "protocol_hash": "a" * 64,
        "code_sha": "b" * 40, "asset": asset, "horizon": horizon,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "automatic_promotion": False, "entry_authority": False,
        "market_rows": rows,
    }


class MultiCryptoRiskReportTest(unittest.TestCase):
    def test_common_block_correlation_and_explicit_shrinkage(self) -> None:
        result = build([report("BTC"), report("ETH", scale=2.0)],
                       block_ns=60_000_000_000, minimum_common_blocks=2, shrinkage=.25)
        self.assertEqual(result["status"], "ESTIMATED")
        pair = result["pairwise_block_correlations"][0]
        self.assertAlmostEqual(pair["raw_correlation"], 1.0)
        self.assertAlmostEqual(pair["shrunk_correlation"], .75)
        self.assertEqual(pair["shrinkage_to_zero"], .25)
        self.assertEqual(result["parent_shock_stress"]["maximum_simultaneous_cohorts"], 2)
        self.assertFalse(result["entry_authority"])

    def test_pending_settlement_forces_insufficient_evidence(self) -> None:
        result = build([report("BTC"), report("ETH", pending=True)],
                       block_ns=60_000_000_000, minimum_common_blocks=2, shrinkage=.5)
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["pending_markets"], 1)
        self.assertEqual(result["pending_market_counts"], {"ETH:M5": 1})

    def test_too_few_common_blocks_does_not_publish_correlation(self) -> None:
        result = build([report("BTC"), report("SOL")],
                       block_ns=60_000_000_000, minimum_common_blocks=10, shrinkage=.1)
        pair = result["pairwise_block_correlations"][0]
        self.assertIsNone(pair["raw_correlation"])
        self.assertIsNone(pair["shrunk_correlation"])
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")

    def test_duplicate_cohort_and_invalid_shrinkage_fail_closed(self) -> None:
        with self.assertRaisesRegex(ReplayError, "DUPLICATE_COHORT"):
            build([report("BTC"), report("BTC")], block_ns=1, minimum_common_blocks=2, shrinkage=.5)
        with self.assertRaisesRegex(ReplayError, "SHRINKAGE_INVALID"):
            build([report("BTC")], block_ns=1, minimum_common_blocks=2, shrinkage=1.5)


if __name__ == "__main__":
    unittest.main()
