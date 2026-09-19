from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from v7_coordinator_reservations import ReservationLimits, ReservationProjection, ReservationRequest
from v7_fast_forward_ipc import FastForwardIpcBridge, FastForwardIpcError, request
from v7_historical_coordinator_fixtures import forward_envelope
from decimal import Decimal as D
from unittest.mock import patch


class FastForwardIpcTest(unittest.TestCase):
    def test_transport_thread_never_runs_economic_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ipc.sock"
            owner_thread = threading.get_ident()
            seen: list[int] = []
            result: dict = {}
            with FastForwardIpcBridge(path) as bridge:
                def client() -> None:
                    result.update(request(path, {"candidate": 1}))
                thread = threading.Thread(target=client); thread.start()
                deadline = time.monotonic() + 1
                while bridge.snapshot()["queued"] == 0 and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertEqual(bridge.drain(lambda value: seen.append(threading.get_ident()) or {
                    "action": "NOTHING", "paper_only": True, "new_risk_authorized": False,
                }), 1)
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(seen, [owner_thread])
                self.assertEqual(result["action"], "NOTHING")
                self.assertFalse(result["new_risk_authorized"])
                self.assertGreaterEqual(result["ipc_queue_wait_ns"], 0)


    def test_bounded_queue_fails_closed_instead_of_dropping_silently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ipc.sock"
            replies: list[dict] = []
            with FastForwardIpcBridge(path, capacity=1) as bridge:
                def client(i: int) -> None:
                    replies.append(request(path, {"candidate": i}))
                first = threading.Thread(target=client, args=(1,)); first.start()
                deadline = time.monotonic() + 1
                while bridge.snapshot()["queued"] == 0 and time.monotonic() < deadline:
                    time.sleep(.001)
                second = threading.Thread(target=client, args=(2,)); second.start()
                deadline = time.monotonic() + 1
                while bridge.snapshot()["rejected"] == 0 and time.monotonic() < deadline:
                    time.sleep(.001)
                bridge.drain(lambda _: {"action": "NOTHING", "new_risk_authorized": False})
                first.join(1); second.join(1)
                self.assertEqual(len(replies), 2)
                self.assertTrue(any(reply.get("reason") == "CRITICAL_QUEUE_FULL" for reply in replies))
                self.assertEqual(bridge.snapshot()["rejected"], 1)

    def test_existing_socket_path_is_never_stolen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ipc.sock"; path.write_text("not a socket")
            with self.assertRaisesRegex(FastForwardIpcError, "already_exists"):
                FastForwardIpcBridge(path)
            self.assertEqual(path.read_text(), "not a socket")

    def test_close_removes_only_owned_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ipc.sock"
            bridge = FastForwardIpcBridge(path)
            self.assertTrue(path.exists()); bridge.close(); self.assertFalse(path.exists())

    def test_invalid_json_frame_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ipc.sock"
            with FastForwardIpcBridge(path) as bridge:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.settimeout(1); sock.connect(str(path))
                sock.sendall((3).to_bytes(4, "big") + b"bad")
                header = sock.recv(4); size = int.from_bytes(header, "big"); reply = json.loads(sock.recv(size))
                sock.close()
                self.assertEqual(reply["action"], "NOTHING")
                self.assertIn("IPC_REQUEST_INVALID", reply["reason"])
                self.assertFalse(reply["new_risk_authorized"])


if __name__ == "__main__":
    unittest.main()
