from __future__ import annotations

from decimal import Decimal as D
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_lead_lag_replay import (
    ArrivalCut, BookCut, Clock, DepthDepletion, FeeTerms,
    LatencyScenario, ReplayIntent, simulate_arrival,
)

FIXTURE = ROOT / "tests/fixtures/v7_taker_parity.json"
T = 1_000_000_000
W = 1_800_000_000_000_000_000


class TakerParityPythonTest(unittest.TestCase):
    def test_decimal_reference_matches_frozen_parity_fixture(self) -> None:
        fixture = json.loads(FIXTURE.read_text())
        self.assertEqual(fixture["schema"], "polymarket_v7_taker_parity_fixture_v1")
        for index, case in enumerate(fixture["cases"]):
            with self.subTest(case=case["name"]):
                market = f"market-{index}"; token = f"token-{index}"
                clock = Clock("parity-host", "parity-boot", T, W)
                fee = FeeTerms(
                    market, "fee-v1", D(str(fixture["fee_rate"])), int(fixture["fee_exponent"]),
                    "USDC", "CASH", D("0.000000000001"), "HALF_EVEN",
                    T - 1, T + 10_000_000_000, "SYNTHETIC_PARITY", True,
                )
                asks = tuple((D(str(price)), D(str(quantity))) for price, quantity in case["asks"])
                book = BookCut(
                    market, token, f"book-{index}", "epoch", 1, "a" * 64, "fee-v1",
                    D(str(fixture["tick_size"])), D(str(fixture["minimum_order"])),
                    clock, T, ((D("0.40"), D("100")),), asks, True,
                )
                cut = ArrivalCut(
                    book, fee, T, W + 60_000_000_000, True, True, True, True, True,
                    False, False, True, 0,
                )
                intent = ReplayIntent(
                    f"trace-{index}", "parity", "b" * 64, f"signal-{index}", "shock",
                    "BTC", "M5", market, token, "BUY", D(str(case["quantity"])),
                    D(str(case["limit_price"])), clock, T, T + 2_000_000_000,
                    T + 2_000_000_000, 1_000_000_000, 120_000_000_000,
                    10_000_000, 1, "epoch", "a" * 64, "fee-v1",
                    D(str(fixture["tick_size"])), 0, f"book-{index}",
                )
                result = simulate_arrival(
                    intent, cut, LatencyScenario("parity", 0, 1_000_000, 0, D(1)),
                    DepthDepletion(),
                )
                filled = result.filled
                average = (sum((price * quantity for price, quantity in result.levels), D(0)) / filled
                           if filled > 0 else D(0))
                self.assertAlmostEqual(float(filled), case["expected_filled"], places=12)
                self.assertAlmostEqual(float(average), case["expected_average_price"], places=12)
                self.assertAlmostEqual(float(-result.gross_cash), case["expected_gross_cost"], places=12)
                self.assertAlmostEqual(float(result.cash_fee), case["expected_fee"], places=12)


if __name__ == "__main__":
    unittest.main()
