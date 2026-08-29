#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_market_common import FeeDetails
from v7_market_maker_status import assess, executable_sell_mark


class MakerStatusTests(unittest.TestCase):
    def test_full_depth_walk_rejects_insufficient_liquidity(self) -> None:
        walked = executable_sell_mark([(0.50, 2.0), (0.49, 3.0)], 5.0)
        self.assertIsNotNone(walked)
        assert walked is not None
        self.assertAlmostEqual(walked[0], 0.494)
        self.assertAlmostEqual(walked[1], 2.47)
        self.assertIsNone(executable_sell_mark([(0.50, 2.0)], 5.0))

    def fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        state = root / "state.json"
        cfg = root / "config.json"
        selection = root / "selection.json"
        output = root / "status.json"
        state.write_text(json.dumps({
            "paper_only": True,
            "authenticated_execution": False,
            "model_sha": "a" * 40,
            # Ten YES shares around 0.50 imply about $5 of deployed capital;
            # keep the fixture economically coherent instead of fabricating a
            # 45% account loss that should correctly trigger the hard guard.
            "cash": 95.0,
            "starting_capital": 100.0,
            "realized_trading_pnl": 0.0,
            "inventory": {
                "market-1": {
                    "condition_id": "cond-1",
                    "yes_token": "yes-1",
                    "no_token": "no-1",
                    "yes_shares": 10.0,
                    "no_shares": 0.0,
                }
            },
        }))
        cfg.write_text(json.dumps({
            "paper_only": True,
            "starting_capital": 100.0,
            "slippage_bps": 10.0,
            "clob_url": "https://clob.invalid",
            "v7": {"authenticated_execution": False, "real_order_submission": False},
        }))
        selection.write_text(json.dumps({
            "model_sha": "a" * 40,
            "markets": [{"market_id": "market-1", "condition_id": "cond-1"}]
        }))
        return state, cfg, selection, output

    def test_bootstrap_status_inherits_exact_identity_from_pinned_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state, cfg, selection, output = self.fixture(Path(tmp))
            state.unlink()
            report = assess(state, cfg, output, selection_path=selection)
            self.assertEqual(report["source"], "not_started")
            self.assertEqual(report["model_sha"], "a" * 40)
            self.assertTrue(report["paper_only"])
            self.assertFalse(report["authenticated_execution"])
            self.assertFalse(report["real_order_submission"])
            self.assertFalse(report["killed"])

    def test_equity_is_net_of_fee_and_slippage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state, cfg, selection, output = self.fixture(Path(tmp))
            book = [{
                "asset_id": "yes-1",
                "bids": [{"price": "0.50", "size": "5"}, {"price": "0.49", "size": "5"}],
            }]
            fee = FeeDetails(rate=0.04, exponent=1.0, taker_only=True, verified=True, source="test")
            with patch("v7_market_maker_status.request_json", return_value=book), patch(
                "v7_market_maker_status.resolve_fee_details", return_value=fee
            ):
                report = assess(state, cfg, output, selection_path=selection)
            gross = 4.95
            vwap = 0.495
            expected_fee = 10.0 * 0.04 * vwap * (1.0 - vwap)
            expected_slippage = gross * 10.0 / 10_000.0
            self.assertAlmostEqual(report["executable_inventory_value"], gross - expected_fee - expected_slippage)
            self.assertAlmostEqual(report["equity"], 95.0 + gross - expected_fee - expected_slippage)
            self.assertFalse(report["killed"])
            self.assertEqual(report["source"], "full_visible_bid_depth_net_verified_fee_and_slippage")

    def test_held_inventory_survives_reward_selection_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state, cfg, selection, output = self.fixture(Path(tmp))
            selection.write_text(json.dumps({"markets": []}))
            book = [{
                "asset_id": "yes-1",
                "bids": [{"price": "0.50", "size": "10"}],
            }]
            fee = FeeDetails(rate=0.04, exponent=1.0, taker_only=True, verified=True, source="test")
            with patch("v7_market_maker_status.request_json", return_value=book), patch(
                "v7_market_maker_status.resolve_fee_details", return_value=fee
            ):
                report = assess(state, cfg, output, selection_path=selection)
            self.assertFalse(report["killed"])
            self.assertEqual(report["positions"][0]["condition_id"], "cond-1")
            self.assertEqual(report["unmarkable_tokens"], [])

    def test_unverified_fee_schedule_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state, cfg, selection, output = self.fixture(Path(tmp))
            book = [{"asset_id": "yes-1", "bids": [{"price": "0.50", "size": "10"}]}]
            fee = FeeDetails(rate=0.0, exponent=1.0, taker_only=True, verified=False, source="unknown")
            with patch("v7_market_maker_status.request_json", return_value=book), patch(
                "v7_market_maker_status.resolve_fee_details", return_value=fee
            ):
                report = assess(state, cfg, output, selection_path=selection)
            self.assertTrue(report["killed"])
            self.assertEqual(report["equity"], 0.0)
            self.assertEqual(report["unmarkable_tokens"][0]["reason"], "unverified_exit_fee_schedule")


if __name__ == "__main__":
    unittest.main()
