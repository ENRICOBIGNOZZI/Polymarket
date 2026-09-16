#!/usr/bin/env python3
"""Bounded Unix-stream transport for the existing V7 coordinator.

The accept thread performs framing only. It never calls economic logic. The
single coordinator owner must drain the bounded queue and provide each reply.
This removes candidate/receipt file polling for opt-in new lanes while keeping
PAPER authority and durability as separate concerns.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SCHEMA = "polymarket_v7_fast_forward_ipc_v1"
MAX_PAYLOAD_BYTES = 256 * 1024
_HEADER = struct.Struct("!I")


class FastForwardIpcError(RuntimeError):
    pass


def _encode(value: dict[str, Any], maximum: int = MAX_PAYLOAD_BYTES) -> bytes:
    if not isinstance(value, dict):
        raise FastForwardIpcError("payload_not_object")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if not payload or len(payload) > maximum:
        raise FastForwardIpcError("payload_size_invalid")
    return _HEADER.pack(len(payload)) + payload


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise FastForwardIpcError("peer_closed_before_frame_complete")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv(sock: socket.socket, maximum: int = MAX_PAYLOAD_BYTES) -> dict[str, Any]:
    header = _recv_exact(sock, _HEADER.size)
    (size,) = _HEADER.unpack(header)
    if size <= 0 or size > maximum:
        raise FastForwardIpcError("frame_size_invalid")
    try:
        value = json.loads(_recv_exact(sock, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FastForwardIpcError("frame_json_invalid") from exc
    if not isinstance(value, dict):
        raise FastForwardIpcError("frame_not_object")
    return value


@dataclass
class _Pending:
    connection: socket.socket
    request: dict[str, Any]
    accepted_monotonic_ns: int


class FastForwardIpcBridge:
    """Transport-only acceptor; coordinator thread owns `drain`."""

    def __init__(self, path: Path, *, capacity: int = 128,
                 socket_timeout_seconds: float = 1.0,
                 maximum_payload_bytes: int = MAX_PAYLOAD_BYTES):
        if capacity < 1 or socket_timeout_seconds <= 0 or maximum_payload_bytes < 1024:
            raise ValueError("invalid_ipc_limits")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FastForwardIpcError("ipc_socket_path_already_exists")
        if len(os.fsencode(str(self.path))) >= 100:
            raise FastForwardIpcError("ipc_socket_path_too_long")
        self.capacity = capacity
        self.timeout = socket_timeout_seconds
        self.maximum = maximum_payload_bytes
        self.pending: queue.Queue[_Pending] = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self._identity = (self.path.stat().st_dev, self.path.stat().st_ino)
        self._server.listen(min(capacity, 128))
        self._server.settimeout(0.1)
        self._thread = threading.Thread(target=self._accept_loop, name="v7-fast-forward-ipc", daemon=True)
        self._thread.start()
        self.accepted = 0
        self.rejected = 0
        self.replied = 0
        self.handler_errors = 0

    def _fail_reply(self, conn: socket.socket, reason: str) -> None:
        reply = {
            "schema": SCHEMA, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "real_capital_at_risk": False, "action": "NOTHING",
            "new_risk_authorized": False, "reason": reason,
        }
        try:
            conn.sendall(_encode(reply, self.maximum))
        except OSError:
            pass
        finally:
            try: conn.close()
            except OSError: pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            conn.settimeout(self.timeout)
            try:
                request = _recv(conn, self.maximum)
                pending = _Pending(conn, request, time.monotonic_ns())
                self.pending.put_nowait(pending)
                self.accepted += 1
            except queue.Full:
                self.rejected += 1
                self._fail_reply(conn, "CRITICAL_QUEUE_FULL")
            except (OSError, FastForwardIpcError) as exc:
                self.rejected += 1
                self._fail_reply(conn, f"IPC_REQUEST_INVALID:{type(exc).__name__}:{exc}")

    def drain(self, handler: Callable[[dict[str, Any]], dict[str, Any]], *, max_messages: int = 64) -> int:
        if max_messages < 1:
            raise ValueError("max_messages_must_be_positive")
        handled = 0
        for _ in range(max_messages):
            try:
                pending = self.pending.get_nowait()
            except queue.Empty:
                break
            try:
                response = handler(pending.request)
                if not isinstance(response, dict):
                    raise FastForwardIpcError("handler_response_not_object")
                response = dict(response)
                response.setdefault("ipc_schema", SCHEMA)
                response.setdefault("ipc_queue_wait_ns", max(0, time.monotonic_ns() - pending.accepted_monotonic_ns))
                pending.connection.sendall(_encode(response, self.maximum))
                self.replied += 1
            except Exception as exc:
                self.handler_errors += 1
                self._fail_reply(pending.connection, f"IPC_HANDLER_FAIL_CLOSED:{type(exc).__name__}:{exc}")
            else:
                pending.connection.close()
            finally:
                self.pending.task_done()
            handled += 1
        return handled

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA, "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "real_capital_at_risk": False,
            "execution_authority": False, "transport_only": True,
            "capacity": self.capacity, "queued": self.pending.qsize(),
            "accepted": self.accepted, "rejected": self.rejected,
            "replied": self.replied, "handler_errors": self.handler_errors,
        }

    def close(self) -> None:
        self._stop.set()
        try: self._server.close()
        except OSError: pass
        self._thread.join(timeout=1.0)
        while True:
            try: pending = self.pending.get_nowait()
            except queue.Empty: break
            self._fail_reply(pending.connection, "IPC_BRIDGE_CLOSED")
            self.pending.task_done()
        try:
            stat = self.path.stat()
            if (stat.st_dev, stat.st_ino) == self._identity:
                self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "FastForwardIpcBridge":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# Generic name used by other single-owner request/reply paths. The historical
# alias remains for the fast-forward coordinator call graph.
BoundedUnixRequestBridge = FastForwardIpcBridge


def request(path: Path, value: dict[str, Any], *, timeout_seconds: float = 1.0,
            maximum_payload_bytes: int = MAX_PAYLOAD_BYTES) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_must_be_positive")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_seconds)
    try:
        sock.connect(str(path))
        sock.sendall(_encode(value, maximum_payload_bytes))
        return _recv(sock, maximum_payload_bytes)
    finally:
        sock.close()
