from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_v7_coordinator_reservations import SHA, projection, receipt, request
from v7_execution_ledger import LedgerContractError, LedgerEvent, iter_records
from v7_fast_forward_ipc import BoundedUnixRequestBridge
from v7_ledger_spool import (
    _authority_route, append_event_via_ledger_ipc,
    append_ledger_ipc_request, ledger_ipc_request,
)


def reservation_event():
    req = request(asset="BTC")
    events = []
    p = projection()
    event = p.reserve(
        req, now_ms=1_800_000_000_000,
        receipt=receipt(req), append=events.append,
        entry_gate_open=True,
    )
    assert events == [event]
    return req, event


class LedgerIpcTest(unittest.TestCase):
    def test_reservation_event_passes_real_authority_firewall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, event = reservation_event()
            self.assertEqual(_authority_route(root, event), "APPEND")
            self.assertFalse((root / "opportunities/quarantine").exists())

    def test_direct_append_ack_is_durable_and_duplicate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, event = reservation_event()
            existing: set[str] = set(); hashes: dict[str, str] = {}
            first = append_ledger_ipc_request(
                root, ledger_ipc_request(event), model_sha=SHA,
                existing=existing, existing_hashes=hashes,
            )
            self.assertTrue(first["durable"])
            self.assertFalse(first["duplicate"])
            records = list(iter_records(root / "ledger/execution.jsonl"))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].record_id, event.record_id)
            second = append_ledger_ipc_request(
                root, ledger_ipc_request(event), model_sha=SHA,
                existing=existing, existing_hashes=hashes,
            )
            self.assertTrue(second["duplicate"])
            self.assertEqual(len(list(iter_records(root / "ledger/execution.jsonl"))), 1)

    def test_same_record_id_with_different_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, event = reservation_event()
            existing: set[str] = set(); hashes: dict[str, str] = {}
            append_ledger_ipc_request(root, ledger_ipc_request(event), model_sha=SHA,
                                      existing=existing, existing_hashes=hashes)
            conflict = replace(event, recorded_ts_ms=event.recorded_ts_ms + 1)
            with self.assertRaisesRegex(LedgerContractError, "record_id_conflict"):
                append_ledger_ipc_request(root, ledger_ipc_request(conflict), model_sha=SHA,
                                          existing=existing, existing_hashes=hashes)

    def test_unix_request_returns_only_after_canonical_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); socket_path = root / "ledger.sock"
            _, event = reservation_event(); existing: set[str] = set(); hashes: dict[str, str] = {}
            result: dict = {}
            with BoundedUnixRequestBridge(socket_path) as bridge:
                client = threading.Thread(target=lambda: result.update(
                    append_event_via_ledger_ipc(socket_path, event)))
                client.start(); deadline = time.monotonic() + 1.0
                while bridge.snapshot()["queued"] == 0 and time.monotonic() < deadline:
                    time.sleep(.001)
                bridge.drain(lambda raw: append_ledger_ipc_request(
                    root, raw, model_sha=SHA, existing=existing,
                    existing_hashes=hashes,
                ))
                client.join(1)
            self.assertFalse(client.is_alive())
            self.assertTrue(result["durable"])
            self.assertTrue((root / "ledger/execution.jsonl").exists())
            self.assertEqual(len(list(iter_records(root / "ledger/execution.jsonl"))), 1)

    def test_multi_crypto_risk_event_requires_same_frozen_packet_as_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); req = request(asset="SOL"); decision = receipt(req)
            packet = decision["multi_crypto_forward"]
            event = LedgerEvent(
                event_type="FILL", strategy=req.strategy, model_sha=SHA,
                record_id="multi-fill-record", recorded_ts_ms=1_800_000_000_100,
                opportunity_id=req.coordinator_replay_key, candidate_id=req.coordinator_replay_key,
                order_id=req.key, fill_id="multi-fill", position_id="multi-position",
                market_id=req.market_id, token_id=req.token_id,
                decision_ts_ms=1_800_000_000_090, exchange_ts_ms=1_800_000_000_080,
                receive_ts_ms=1_800_000_000_085, book_snapshot_id="multi-book",
                side="BUY", fill_price=.5, filled_size=2.0, complete=True,
                fee=.01, fee_source="SYNTHETIC_TEST_FEE",
                metadata={"paper_exploration": True, "economic_authority": "PAPER_EXPLORATION",
                          "paper_multi_crypto_forward": True, "multi_crypto_forward": packet,
                          "coordinator_receipt": decision},
            )
            self.assertEqual(_authority_route(root, event), "APPEND")
            metadata = dict(event.metadata); metadata["multi_crypto_forward"] = dict(packet, protocol_hash="9" * 64)
            tampered = replace(event, record_id="multi-fill-tampered", metadata=metadata)
            self.assertEqual(_authority_route(root, tampered), "QUARANTINED")

    def test_invalid_receipt_is_quarantined_not_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, event = reservation_event()
            metadata = dict(event.metadata)
            bad = dict(metadata["coordinator_receipt"]); bad["selected_replay_key"] = "wrong"
            metadata["coordinator_receipt"] = bad
            event = replace(event, record_id="bad-reservation", metadata=metadata)
            with self.assertRaisesRegex(LedgerContractError, "authority_route:QUARANTINED"):
                append_ledger_ipc_request(
                    root, ledger_ipc_request(event), model_sha=SHA,
                    existing=set(), existing_hashes={},
                )
            self.assertFalse((root / "ledger/execution.jsonl").exists())
            self.assertEqual(len(list((root / "opportunities/quarantine").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
