from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import v6_queue_filter as qf


class V6ExecutionMeasurementTest(unittest.TestCase):
    def test_taker_fee_is_level_specific_and_maker_fee_is_zero(self) -> None:
        details = qf.FeeDetails(True, 0.25, 1.0, True, "test")
        self.assertEqual(qf.fee_amount(10.0, 0.50, details, maker=True), 0.0)
        fee_mid = qf.fee_amount(10.0, 0.50, details)
        fee_tail = qf.fee_amount(10.0, 0.05, details)
        self.assertGreater(fee_mid, fee_tail)
        self.assertGreater(fee_tail, 0.0)

    def test_explicit_fee_disabled_never_falls_back(self) -> None:
        raw = {"feesEnabled": False}
        details = qf.resolve_fee_details(raw, "https://clob.polymarket.com", "condition", "token")
        self.assertTrue(details.verified)
        self.assertFalse(details.enabled)
        self.assertEqual(details.source, "market:feesEnabled=false")

    def test_clob_fee_descriptor_beats_conservative_fallback(self) -> None:
        raw = {"feesEnabled": True, "feeRateBps": 25}
        details = qf.resolve_fee_details(raw, "https://clob.polymarket.com", "condition", "token")
        self.assertTrue(details.verified)
        self.assertTrue(details.enabled)
        self.assertEqual(details.fee_rate_bps, 25.0)
        self.assertNotIn("fallback", details.source)

    def test_depth_walk_charges_each_level_at_its_own_price(self) -> None:
        details = qf.FeeDetails(True, 25.0, 1.0, True, "test")
        fill = qf.walk_book_for_shares(
            [(0.50, 5.0), (0.60, 5.0)],
            8.0,
            details,
            buy=True,
            slippage_bps=0.0,
            require_full=True,
        )
        self.assertIsNotNone(fill)
        assert fill is not None
        expected = qf.fee_amount(5.0, 0.50, details) + qf.fee_amount(3.0, 0.60, details)
        self.assertAlmostEqual(fill.fee, expected)
        self.assertGreater(fill.raw_vwap, 0.50)

    def test_depth_walk_rejects_fake_full_fill_and_supports_partial_exit(self) -> None:
        details = qf.FeeDetails(False, 0.0, 1.0, True, "test")
        self.assertIsNone(qf.walk_book_for_shares([(0.50, 2.0)], 5.0, details, buy=True, require_full=True))
        partial = qf.walk_book_for_shares([(0.49, 2.0)], 5.0, details, buy=False, require_full=False)
        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertEqual(partial.filled_shares, 2.0)
        self.assertFalse(partial.complete)

    def test_measurement_branch_does_not_route_live_v6(self) -> None:
        loop = (SCRIPTS / "paper_v6_loop.sh").read_text()
        self.assertNotIn("scripts/v6_queue_filter.py", loop)
        self.assertNotIn("scripts/v6_hard_arb_guard.py", loop)
        self.assertIn("scripts/v6_micro_taker.py", loop)
        self.assertIn("scripts/v6_hard_arb_paper.py", loop)
        self.assertIn("--completion-threshold 0.75", loop)

    def test_registered_smoke_uses_realistic_execution_probe(self) -> None:
        workflow = (ROOT / ".github/workflows/v6-research-smoke.yml").read_text()
        self.assertIn("scripts/v6_queue_filter.py self-test", workflow)
        self.assertIn("scripts/v6_hard_arb_guard.py self-test", workflow)
        self.assertIn("scripts/v6_hard_arb_guard.py --config", workflow)
        self.assertIn("scripts/v6_queue_filter.py micro", workflow)
        self.assertIn("--leg-latency-ms 100", workflow)
        self.assertIn("--max-leg-age-ms 2000", workflow)
        self.assertIn("--max-cross-leg-skew-ms 1000", workflow)


if __name__ == "__main__":
    unittest.main()
