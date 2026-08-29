from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_finalize_maker_cutover as cutover


SHA = "a" * 40
NONCE = f"{'b' * 40}.123.456"


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


class MakerCutoverFinalizerTest(unittest.TestCase):
    def fixture(self, root: Path, now: int) -> None:
        write(root / "control/CUTOVER_DRAIN", {
            "schema": "polymarket_v7_cutover_drain_v1", "nonce": NONCE,
            "current_sha": SHA, "target_sha": "b" * 40, "paper_only": True,
        })
        write(root / "micro_maker/state.json", {
            "paper_only": True, "authenticated_execution": False, "model_sha": SHA,
            "starting_capital": 100.0, "cash": 95.0, "realized_trading_pnl": 0.0,
            "inventory": {"m1": {
                "condition_id": "c1", "yes_token": "yes", "no_token": "no",
                "yes_shares": 10.0, "no_shares": 0.0,
                "yes_cost": 5.0, "no_cost": 0.0,
            }},
        })
        write(root / "micro_maker/status.json", {
            "schema": "polymarket_v7_professional_maker_status_v1",
            "timestamp_ms": now - 1, "paper_only": True, "authenticated_execution": False,
            "model_sha": SHA, "marking_complete": True, "killed": False,
            "drain_requested": True, "new_risk_frozen": True,
            "cash": 95.0, "equity": 99.79,
            "positions": [{
                "market_id": "m1", "condition_id": "c1", "token_id": "yes",
                "shares": 10.0, "full_depth_vwap": 0.49,
                "gross_executable_liquidation_value": 4.9,
                "exit_fee": 0.1, "exit_fee_source": "test-authoritative",
                "slippage_haircut": 0.01, "net_executable_liquidation_value": 4.79,
                "exchange_ts_ms": now - 3, "receive_ts_ms": now - 2,
                "book_snapshot_id": "snapshot-1",
            }],
        })

    def test_verified_inventory_is_flattened_with_terminal_ledger_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = 1_788_040_000_000
            self.fixture(root, now)
            receipt = cutover.finalize(root, SHA, NONCE, now_ms=now)
            self.assertEqual(receipt["state"], "MAKER_FLAT")
            self.assertEqual(receipt["positions_liquidated"], 1)
            self.assertAlmostEqual(receipt["final_pnl"], -0.21)
            state = json.loads((root / "micro_maker/state.json").read_text())
            self.assertEqual(state["inventory"]["m1"]["yes_shares"], 0.0)
            self.assertAlmostEqual(state["cash"], 99.79)
            status = json.loads((root / "micro_maker/status.json").read_text())
            self.assertEqual(status["positions"], [])
            self.assertTrue(status["drain_complete"])
            events = [json.loads(line) for line in (root / "ledger/execution.jsonl").read_text().splitlines()]
            self.assertEqual([row["event_type"] for row in events], ["FILL", "FINAL"])
            self.assertEqual(events[0]["side"], "SELL")
            self.assertEqual(events[0]["metadata"]["purpose"], "LIQUIDATION")
            self.assertAlmostEqual(events[1]["final_pnl"], -0.21)
            # The receipt makes a completed retry idempotent.
            self.assertEqual(cutover.finalize(root, SHA, NONCE, now_ms=now), receipt)

    def test_crash_after_state_commit_resumes_with_stale_mark_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = 1_788_040_000_000
            self.fixture(root, now)
            original_atomic = cutover.atomic_json
            crashed = False

            def crash_before_status(path: Path, value: dict) -> None:
                nonlocal crashed
                if path == root / "micro_maker/status.json" and not crashed:
                    crashed = True
                    raise OSError("simulated crash")
                original_atomic(path, value)

            with patch.object(cutover, "atomic_json", side_effect=crash_before_status):
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    cutover.finalize(root, SHA, NONCE, now_ms=now)
            pending = json.loads((root / "control/maker_cutover_liquidation.json").read_text())
            self.assertEqual(pending["state"], "LIQUIDATION_PENDING")
            # Recovery is driven by the journal, so an expired market mark does
            # not strand a transaction that already committed its ledger/state.
            receipt = cutover.finalize(root, SHA, NONCE, now_ms=now + 60_000)
            self.assertEqual(receipt["state"], "MAKER_FLAT")
            events = (root / "ledger/execution.jsonl").read_text().splitlines()
            self.assertEqual(len(events), 2)
            state = json.loads((root / "micro_maker/state.json").read_text())
            self.assertEqual(state["inventory"]["m1"]["yes_shares"], 0.0)

    def test_complement_buy_and_merge_is_audited_as_the_actual_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = 1_788_040_000_000
            self.fixture(root, now)
            status_path = root / "micro_maker/status.json"
            status = json.loads(status_path.read_text())
            mark = status["positions"][0]
            mark.update({
                "liquidation_method": "COMPLEMENT_BUY_AND_MERGE",
                "execution_token_id": "no", "execution_side": "BUY",
                "full_depth_vwap": 0.51,
            })
            write(status_path, status)
            receipt = cutover.finalize(root, SHA, NONCE, now_ms=now)
            self.assertEqual(receipt["liquidations"][0]["liquidation_method"], "COMPLEMENT_BUY_AND_MERGE")
            events = [json.loads(line) for line in (root / "ledger/execution.jsonl").read_text().splitlines()]
            self.assertEqual(events[0]["side"], "BUY")
            self.assertEqual(events[0]["token_id"], "no")
            self.assertTrue(events[0]["metadata"]["complete_set_merge"])
            self.assertEqual(events[0]["metadata"]["inventory_token_id"], "yes")

    def test_stale_or_incomplete_mark_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = 1_788_040_000_000
            self.fixture(root, now)
            status = json.loads((root / "micro_maker/status.json").read_text())
            status["marking_complete"] = False
            write(root / "micro_maker/status.json", status)
            with self.assertRaisesRegex(cutover.MakerCutoverError, "maker_not_safely_marked"):
                cutover.finalize(root, SHA, NONCE, now_ms=now)


if __name__ == "__main__":
    unittest.main()
