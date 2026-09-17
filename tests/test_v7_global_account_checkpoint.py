from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_coordinator_reservations import ReservationLimits, ReservationProjection
from v7_execution_ledger import LedgerEvent
from v7_global_account_checkpoint import build
from v7_lead_lag_replay import ReplayError

SHA = "a" * 40
PROTOCOL = "b" * 64


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))


def fixture(root: Path) -> tuple[Path, Path]:
    allocation = root / "control/allocations/manifest.json"
    write(allocation, {
        "schema": "polymarket_v7_capital_allocation_v3", "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "real_capital_at_risk": False, "capital_authority_owner": "V7_CANONICAL_ALLOCATOR",
        "capital_authority_owner_count": 1, "account_starting_capital": 100.0,
        "engine_budgets": {"CRYPTO_SETTLEMENT_ENGINE": 60.0, "STRUCTURAL_ARB_ENGINE": 20.0},
        "reserve_budget": 20.0,
    })
    write(root / "external_fair/paper_router_status.json", {
        "schema": "polymarket_v7_crypto_settlement_engine_status_v1", "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False, "real_capital_at_risk": False,
        "paper_exploration_account": {
            "schema": "polymarket_v7_paper_exploration_account_v1", "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False, "real_capital_at_risk": False,
            "complete": True, "model_sha": SHA, "starting_capital": 60.0, "cash": 63.4,
            "realized_pnl": 3.4, "open_positions": 0, "pending_maker_orders": 0,
            "issues": [], "invalid_spool_records": [],
        },
    })
    write(root / "research/lead_lag_taker_v1/status.json", {
        "schema": "polymarket_v7_lead_lag_taker_v1_status", "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False, "real_capital_at_risk": False,
        "model_sha": SHA, "protocol_hash": PROTOCOL, "entries": 1, "settled": 1,
        "open_positions": 0, "realized_pnl": 2.0,
    })
    write(root / "research/lead_lag_taker_v1/state.json", {
        "model_sha": SHA, "protocol_hash": PROTOCOL, "realized_pnl": 2.0,
        "positions": {"p1": {"settled": True, "protocol_hash": PROTOCOL, "final_pnl": 2.0}},
    })
    write(root / "canonical_economics.json", {
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "net_pnl": 5.4,
    })
    ledger = root / "snapshot/execution.jsonl"; ledger.parent.mkdir(parents=True, exist_ok=True)
    events = [
        LedgerEvent(event_type="FINAL", strategy="CRYPTO_SETTLEMENT_ENGINE", model_sha=SHA,
                    record_id="final-ext", final_pnl=3.4),
        LedgerEvent(event_type="FINAL", strategy="CRYPTO_SETTLEMENT_ENGINE", model_sha=SHA,
                    record_id="final-lead", final_pnl=2.0),
    ]
    ledger.write_text("\n".join(json.dumps(event.to_dict(), sort_keys=True) for event in events) + "\n")
    return allocation, ledger


class GlobalAccountCheckpointTest(unittest.TestCase):
    def test_flat_account_reconciles_one_shared_cash_balance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); allocation, ledger = fixture(root)
            result = build(run_root=root, allocation_manifest=allocation, ledger_snapshot=ledger, expected_sha=SHA)
            self.assertTrue(result["whole_portfolio_reconciled"])
            self.assertTrue(result["all_preexisting_lanes_flat"])
            self.assertEqual(result["available_cash"], "105.4")
            self.assertEqual(result["canonical_terminal_pnl"], "5.4")
            self.assertEqual(result["external_exposures"], [])
            self.assertFalse(result["entry_authority"])

    def test_open_lead_lag_position_blocks_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); allocation, ledger = fixture(root)
            status_path = root / "research/lead_lag_taker_v1/status.json"
            state_path = root / "research/lead_lag_taker_v1/state.json"
            status = json.loads(status_path.read_text()); status.update(entries=2, settled=1, open_positions=1)
            write(status_path, status)
            state = json.loads(state_path.read_text()); state["positions"]["p2"] = {
                "settled": False, "protocol_hash": PROTOCOL, "entry_cost": 1.0, "entry_fee": .01,
            }; write(state_path, state)
            with self.assertRaisesRegex(ReplayError, "LEAD_LAG_NOT_FLAT"):
                build(run_root=root, allocation_manifest=allocation, ledger_snapshot=ledger, expected_sha=SHA)

    def test_open_external_or_pending_order_blocks_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); allocation, ledger = fixture(root)
            path = root / "external_fair/paper_router_status.json"; status = json.loads(path.read_text())
            status["paper_exploration_account"]["pending_maker_orders"] = 1; write(path, status)
            with self.assertRaisesRegex(ReplayError, "EXTERNAL_FAIR_NOT_FLAT"):
                build(run_root=root, allocation_manifest=allocation, ledger_snapshot=ledger, expected_sha=SHA)

    def test_canonical_or_ledger_pnl_mismatch_blocks_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); allocation, ledger = fixture(root)
            path = root / "canonical_economics.json"; value = json.loads(path.read_text()); value["net_pnl"] = 9.0; write(path, value)
            with self.assertRaisesRegex(ReplayError, "TERMINAL_PNL_RECONCILIATION_FAILED"):
                build(run_root=root, allocation_manifest=allocation, ledger_snapshot=ledger, expected_sha=SHA)

    def test_mixed_sha_ledger_blocks_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); allocation, ledger = fixture(root)
            other = LedgerEvent(event_type="FINAL", strategy="CRYPTO_SETTLEMENT_ENGINE", model_sha="c"*40,
                                record_id="foreign", final_pnl=0.0)
            with ledger.open("a") as handle: handle.write(json.dumps(other.to_dict()) + "\n")
            with self.assertRaisesRegex(ReplayError, "LEDGER_MIXED_SHA"):
                build(run_root=root, allocation_manifest=allocation, ledger_snapshot=ledger, expected_sha=SHA)

    def test_checkpoint_hands_off_cash_to_new_writer_sha_without_relabeling_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); allocation, ledger = fixture(root)
            checkpoint = build(run_root=root, allocation_manifest=allocation, ledger_snapshot=ledger, expected_sha=SHA)
            limits = ReservationLimits("USDC", *[__import__("decimal").Decimal("100")] * 6)
            writer_sha = "d" * 40
            projection = ReservationProjection.from_checkpoint(checkpoint, writer_code_sha=writer_sha, limits=limits)
            self.assertEqual(projection.code_sha, writer_sha)
            self.assertEqual(projection.snapshot()["cash"], "105.4")
            self.assertEqual(projection.snapshot()["source_checkpoint_code_sha"], SHA)
            self.assertFalse(projection.snapshot()["entry_authority"])

    def test_tampered_checkpoint_cannot_initialize_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); allocation, ledger = fixture(root)
            checkpoint = build(run_root=root, allocation_manifest=allocation, ledger_snapshot=ledger, expected_sha=SHA)
            checkpoint["available_cash"] = "999"
            limits = ReservationLimits("USDC", *[__import__("decimal").Decimal("100")] * 6)
            with self.assertRaisesRegex(ReplayError, "HASH_MISMATCH"):
                ReservationProjection.from_checkpoint(checkpoint, writer_code_sha="d"*40, limits=limits)

    def test_cli_refuses_overwrite_and_is_not_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); allocation, ledger = fixture(root); output = root / "checkpoint.json"
            command = [sys.executable, str(ROOT / "scripts/v7_global_account_checkpoint.py"),
                       "--run-root", str(root), "--allocation-manifest", str(allocation),
                       "--ledger-snapshot", str(ledger), "--expected-sha", SHA, "--output", str(output)]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            result = json.loads(output.read_text()); self.assertFalse(result["entry_authority"])
            original = output.read_bytes(); second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0); self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
