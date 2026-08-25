#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "polymarket_fees", ROOT / "scripts" / "polymarket_fees.py"
)
assert SPEC and SPEC.loader
fees = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fees)


class PolymarketFeeTests(unittest.TestCase):
    def test_gamma_fee_schedule_is_authoritative(self) -> None:
        details = fees.resolve_fee_details(
            {
                "id": "m1",
                "conditionId": "c1",
                "feesEnabled": True,
                "feeSchedule": {"rate": 0.04, "exponent": 1.0, "takerOnly": True},
            },
            "https://clob.invalid",
            lambda *_args: self.fail("CLOB fallback should not be queried"),
        )
        self.assertTrue(details.enabled)
        self.assertAlmostEqual(details.rate, 0.04)
        self.assertEqual(details.source, "market:fee_schedule")
        self.assertEqual(fees.fee_per_share(0.5, details), 0.01)
        self.assertEqual(fees.fee_per_share(0.5, details, taker=False), 0.0)

    def test_explicit_fee_disabled_is_terminal_zero(self) -> None:
        details = fees.resolve_fee_details(
            {"id": "m2", "conditionId": "c2", "feesEnabled": False},
            "https://clob.invalid",
            lambda *_args: self.fail("disabled market must not fall through"),
        )
        self.assertFalse(details.enabled)
        self.assertEqual(details.rate, 0.0)
        self.assertEqual(fees.fee_per_share(0.4, details), 0.0)

    def test_clob_fd_is_used_when_gamma_has_no_schedule(self) -> None:
        seen = []

        def request(url: str, *_args):
            seen.append(url)
            return {"fd": {"r": 0.05, "e": 2.0, "to": True}}

        details = fees.resolve_fee_details(
            {"id": "m3", "conditionId": "condition 3"},
            "https://clob.example",
            request,
        )
        self.assertEqual(len(seen), 1)
        self.assertIn("condition%203", seen[0])
        self.assertEqual(details.source, "clob:fee_schedule")
        self.assertAlmostEqual(details.rate, 0.05)
        self.assertAlmostEqual(details.exponent, 2.0)
        self.assertAlmostEqual(fees.fee_per_share(0.5, details), 0.003125)

    def test_missing_fee_fails_closed_instead_of_using_point_zero_seven(self) -> None:
        with self.assertRaises(fees.FeeScheduleUnavailable):
            fees.resolve_fee_details(
                {"id": "m4", "conditionId": "c4"},
                "https://clob.example",
                lambda *_args: {},
            )

    def test_nonfinite_values_are_not_accepted(self) -> None:
        self.assertIsNone(fees.parse_fee_details({"feeSchedule": {"rate": math.nan}}))


if __name__ == "__main__":
    unittest.main()
