from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_v7_global_account_checkpoint import SHA as SOURCE_SHA, fixture
from test_v7_global_portfolio_coordinator import forward_envelope
from test_v7_opportunity import multi_forward_envelope
from v7_coordinator_reservations import ReservationLimits, ReservationProjection, ReservationRequest
from v7_execution_ledger import iter_records
from v7_fast_forward_ipc import FastForwardIpcBridge, request as ipc_request
from v7_global_account_checkpoint import build as build_checkpoint
from v7_global_portfolio_coordinator import process_fast_forward_ipc_reserved
from v7_ledger_spool import append_event_via_ledger_ipc, append_ledger_ipc_request

WRITER_SHA = "d" * 40
NOW_MS = 1_800_000_000_000
NOW_NS = NOW_MS * 1_000_000


def setup_chain(root: Path):
    allocation, ledger = fixture(root)
    checkpoint = build_checkpoint(run_root=root, allocation_manifest=allocation,
                                  ledger_snapshot=ledger, expected_sha=SOURCE_SHA)
    limits = ReservationLimits("USDC", *[D("100")] * 6)
    projection = ReservationProjection.from_checkpoint(
        checkpoint, writer_code_sha=WRITER_SHA, limits=limits,
    )
    raw = forward_envelope("multi-crypto-ipc-forward")
    raw["model_sha"] = WRITER_SHA
    raw["decision_receive_timestamp_ns"] = NOW_NS - 10_000_000
    raw["source_event_timestamps_ns"] = [NOW_NS - 20_000_000]
    raw["expires_at_ns"] = NOW_NS + 1_000_000_000
    leg = raw["execution_plan"]["legs"][0]
    reservation = ReservationRequest(
        "new-multi-crypto-cohort", raw["forward_test"]["protocol_hash"],
        raw["market_id"], leg["token_id"], "signal-new", "parent-shock-new",
        "BTC", "M5", raw["engine_id"], "USDC", D("20"),
        D(str(leg["target_quantity"])), D(str(leg["limit_price"])),
        NOW_MS + 1000, raw["deterministic_replay_key"],
    )
    return checkpoint, projection, raw, reservation


def setup_multi_chain(root: Path, *, asset: str = "ETH", horizon: str = "M5"):
    allocation, ledger = fixture(root)
    checkpoint = build_checkpoint(run_root=root, allocation_manifest=allocation,
                                  ledger_snapshot=ledger, expected_sha=SOURCE_SHA)
    limits = ReservationLimits("USDC", *[D("100")] * 6)
    projection = ReservationProjection.from_checkpoint(
        checkpoint, writer_code_sha=WRITER_SHA, limits=limits,
    )
    raw = multi_forward_envelope(asset=asset, horizon=horizon, key=f"{asset}-{horizon}-ipc")
    raw["model_sha"] = WRITER_SHA
    raw["decision_receive_timestamp_ns"] = NOW_NS - 10_000_000
    raw["source_event_timestamps_ns"] = [NOW_NS - 20_000_000]
    raw["expires_at_ns"] = NOW_NS + 1_000_000_000
    leg = raw["execution_plan"]["legs"][0]
    packet = raw["multi_crypto_forward"]
    reservation = ReservationRequest(
        packet["experiment_id"], packet["protocol_hash"], raw["market_id"], leg["token_id"],
        "signal-multi", "parent-shock-multi", asset, horizon, raw["engine_id"], "USDC", D("20"),
        D(str(leg["target_quantity"])), D(str(leg["limit_price"])),
        NOW_MS + 1000, raw["deterministic_replay_key"],
    )
    return checkpoint, projection, raw, reservation


class ReservedPaperChainTest(unittest.TestCase):
    def test_checkpoint_to_direct_ipc_to_same_coordinator_to_durable_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); checkpoint, projection, raw, reservation = setup_chain(root)
            socket_path = root / "coordinator.sock"; events = []; reply: dict = {}
            with FastForwardIpcBridge(socket_path) as bridge:
                client = threading.Thread(target=lambda: reply.update(ipc_request(socket_path, raw)))
                client.start(); deadline = time.monotonic() + 1.0
                while bridge.snapshot()["queued"] == 0 and time.monotonic() < deadline:
                    time.sleep(.001)
                bridge.drain(lambda envelope: process_fast_forward_ipc_reserved(
                    root, envelope, now_ns=NOW_NS, risk_preempt=False, drain_active=False,
                    reservation_projection=projection,
                    requests_by_replay_key={reservation.coordinator_replay_key: reservation},
                    append_event=events.append, entry_gate_open=True,
                ))
                client.join(1)
            self.assertEqual(reply["action"], "TAKE", reply)
            self.assertTrue(reply["reservation_durable"])
            self.assertFalse(reply["new_risk_authorized"])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].model_sha, WRITER_SHA)
            self.assertEqual(projection.snapshot()["source_checkpoint_code_sha"], SOURCE_SHA)
            self.assertEqual(checkpoint["available_cash"], "105.4")
            self.assertFalse((root / "opportunities/receipts").exists())

    def test_full_chain_uses_separate_single_writer_router_and_durable_ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, projection, raw, reservation = setup_chain(root)
            coordinator_socket = root / "coordinator.sock"
            ledger_socket = root / "ledger.sock"
            existing: set[str] = set(); hashes: dict[str, str] = {}
            stop = threading.Event(); reply: dict = {}
            with FastForwardIpcBridge(ledger_socket) as ledger_bridge, FastForwardIpcBridge(coordinator_socket) as coordinator_bridge:
                def ledger_router() -> None:
                    while not stop.is_set():
                        ledger_bridge.drain(lambda value: append_ledger_ipc_request(
                            root, value, model_sha=WRITER_SHA, existing=existing,
                            existing_hashes=hashes,
                        ))
                        time.sleep(.0005)
                writer_thread = threading.Thread(target=ledger_router)
                writer_thread.start()
                client = threading.Thread(target=lambda: reply.update(ipc_request(coordinator_socket, raw)))
                client.start(); deadline = time.monotonic() + 1.0
                while coordinator_bridge.snapshot()["queued"] == 0 and time.monotonic() < deadline:
                    time.sleep(.001)
                coordinator_bridge.drain(lambda envelope: process_fast_forward_ipc_reserved(
                    root, envelope, now_ns=NOW_NS, risk_preempt=False, drain_active=False,
                    reservation_projection=projection,
                    requests_by_replay_key={reservation.coordinator_replay_key: reservation},
                    append_event=lambda event: append_event_via_ledger_ipc(ledger_socket, event),
                    entry_gate_open=True,
                ))
                client.join(1); stop.set(); writer_thread.join(1)
            self.assertEqual(reply["action"], "TAKE", reply)
            self.assertTrue(reply["reservation_durable"])
            records = list(iter_records(root / "ledger/execution.jsonl"))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].event_type, "CAPITAL_RESERVE")
            self.assertEqual(records[0].model_sha, WRITER_SHA)
            self.assertFalse((root / "opportunities/receipts").exists())

    def test_eth_multi_forward_packet_binds_coordinator_reservation_and_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, projection, raw, reservation = setup_multi_chain(root, asset="ETH", horizon="M5")
            events = []
            decision = process_fast_forward_ipc_reserved(
                root, raw, now_ns=NOW_NS, risk_preempt=False, drain_active=False,
                reservation_projection=projection,
                requests_by_replay_key={reservation.coordinator_replay_key: reservation},
                append_event=events.append, entry_gate_open=True,
            )
            self.assertEqual(decision["action"], "TAKE", decision)
            self.assertTrue(decision["paper_multi_crypto_forward_authorized"])
            self.assertFalse(decision["new_risk_authorized"])
            self.assertTrue(decision["reservation_durable"])
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0].metadata["paper_multi_crypto_forward"])
            self.assertEqual(events[0].metadata["multi_crypto_forward"], raw["multi_crypto_forward"])
            self.assertEqual(events[0].metadata["coordinator_receipt"]["selected_replay_key"], raw["deterministic_replay_key"])

    def test_multi_forward_experiment_mismatch_fails_before_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, projection, raw, reservation = setup_multi_chain(root, asset="SOL", horizon="M15")
            wrong = ReservationRequest(
                "wrong-experiment", reservation.protocol_hash, reservation.market_id, reservation.token_id,
                reservation.signal_id, reservation.parent_shock_id, reservation.asset, reservation.horizon,
                reservation.strategy, reservation.currency, reservation.maximum_debit, reservation.quantity,
                reservation.limit_price, reservation.expires_wall_ms, reservation.coordinator_replay_key,
            )
            events = []
            decision = process_fast_forward_ipc_reserved(
                root, raw, now_ns=NOW_NS, risk_preempt=False, drain_active=False,
                reservation_projection=projection,
                requests_by_replay_key={wrong.coordinator_replay_key: wrong},
                append_event=events.append, entry_gate_open=True,
            )
            self.assertEqual(decision["action"], "NOTHING")
            self.assertEqual(events, [])

    def test_old_sha_candidate_cannot_be_relabelled_by_new_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, projection, raw, reservation = setup_chain(root)
            raw["model_sha"] = SOURCE_SHA
            events = []
            decision = process_fast_forward_ipc_reserved(
                root, raw, now_ns=NOW_NS, risk_preempt=False, drain_active=False,
                reservation_projection=projection,
                requests_by_replay_key={reservation.coordinator_replay_key: reservation},
                append_event=events.append, entry_gate_open=True,
            )
            self.assertEqual(decision["action"], "NOTHING")
            self.assertIn("RESERVATION_ENVELOPE_BINDING_MISMATCH", " ".join(decision["reasons"]))
            self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
