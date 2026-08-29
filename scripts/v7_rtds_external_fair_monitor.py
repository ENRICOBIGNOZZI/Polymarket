#!/usr/bin/env python3
"""Observe public Polymarket RTDS inputs for the V7 External Fair PAPER plane.

This zero-authority process consumes the public Chainlink and Binance topics,
writes an evidence tape, and reports real data-plane liveness. It deliberately
keeps contract binding, fair value and OMS routing fail-closed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import socket
import ssl
import struct
import time
from pathlib import Path
from typing import Any, Iterator

from v7_public_https_proxy import DEFAULT_DNS, PublicResolver

HOST = "ws-live-data.polymarket.com"
ORACLE_TOPIC = "crypto_prices_chainlink"
EXTERNAL_TOPIC = "crypto_prices"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
FRESH_NS = 3_000_000_000


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        output.flush()


def _recv_exact(stream: ssl.SSLSocket, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        chunk = stream.recv(size - len(output))
        if not chunk:
            raise OSError("websocket EOF")
        output.extend(chunk)
    return bytes(output)


def connect_websocket(resolver: PublicResolver) -> ssl.SSLSocket:
    last_error: Exception | None = None
    for address in resolver.resolve(HOST):
        raw: socket.socket | None = None
        try:
            raw = socket.create_connection((address, 443), timeout=8.0)
            stream = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET / HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\nOrigin: https://polymarket.com\r\n\r\n"
            )
            stream.sendall(request.encode("ascii"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                response.extend(stream.recv(4096))
                if len(response) > 65536:
                    raise OSError("oversized websocket handshake")
            header, _, remainder = bytes(response).partition(b"\r\n\r\n")
            if not header.startswith(b"HTTP/1.1 101 ") or remainder:
                raise OSError("websocket upgrade rejected")
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()).decode("ascii")
            headers = {}
            for line in header.decode("latin-1").split("\r\n")[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            if headers.get("sec-websocket-accept") != expected:
                raise OSError("websocket accept mismatch")
            stream.settimeout(1.0)
            return stream
        except Exception as exc:  # noqa: BLE001 - try all public addresses
            last_error = exc
            if raw is not None:
                try:
                    raw.close()
                except OSError:
                    pass
    raise OSError(f"RTDS connection failed: {last_error}")


def send_frame(stream: ssl.SSLSocket, opcode: int, payload: bytes) -> None:
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("websocket payload too large")
    first = 0x80 | (opcode & 0x0F)
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes((first, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
    else:
        header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    stream.sendall(header + mask + masked)


def send_json(stream: ssl.SSLSocket, payload: dict[str, Any]) -> None:
    send_frame(stream, 0x1, json.dumps(payload, separators=(",", ":")).encode())


def read_frame(stream: ssl.SSLSocket) -> tuple[bool, int, bytes]:
    first, second = _recv_exact(stream, 2)
    if first & 0x70:
        raise OSError("unsupported websocket extension")
    final, opcode = bool(first & 0x80), first & 0x0F
    masked, length = bool(second & 0x80), second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(stream, 8))[0]
    if length > MAX_MESSAGE_BYTES:
        raise OSError("websocket message exceeds bound")
    mask = _recv_exact(stream, 4) if masked else b""
    payload = _recv_exact(stream, length)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return final, opcode, payload


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_ms(raw: Any) -> int:
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return 0
    if value > 10**17:
        return value // 1_000_000
    if value > 10**14:
        return value // 1_000
    if value < 10**11:
        return value * 1_000
    return value


def observations(value: Any, inherited_topic: str = "") -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for row in value:
            yield from observations(row, inherited_topic)
        return
    if not isinstance(value, dict):
        return
    topic = str(value.get("topic") or inherited_topic)
    payload = value.get("payload")
    if isinstance(payload, (list, dict)):
        yield from observations(payload, topic)
        return
    if topic not in {ORACLE_TOPIC, EXTERNAL_TOPIC}:
        return
    price = _finite(value.get("value", value.get("price")))
    timestamp_ms = _timestamp_ms(value.get("timestamp", value.get("timestamp_ms")))
    symbol = str(value.get("symbol") or "").lower()
    if price is None or price <= 0.0 or timestamp_ms <= 0:
        return
    yield {"topic": topic, "symbol": symbol, "price": price, "timestamp_ms": timestamp_ms}


class Monitor:
    def __init__(self, root: Path, code_sha: str) -> None:
        self.root, self.code_sha = root, code_sha
        self.latest: dict[str, dict[str, Any]] = {}
        self.accepted = self.written = self.dropped = 0
        self.connection_epoch = self.reconnects = self.gaps = 0
        self.last_error = "starting"

    def ingest(self, row: dict[str, Any]) -> None:
        topic = str(row["topic"])
        previous = self.latest.get(topic)
        if previous and int(row["timestamp_ms"]) <= int(previous["timestamp_ms"]):
            self.dropped += 1
            return
        enriched = dict(row)
        enriched.update({
            "schema": "polymarket_v7_rtds_price_event_v1",
            "connection_epoch": self.connection_epoch,
            "receive_wall_ns": time.time_ns(),
            "receive_monotonic_ns": time.monotonic_ns(),
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
        })
        self.latest[topic] = enriched
        self.accepted += 1
        append_jsonl(self.root / "rtds_events.jsonl", enriched)
        self.written += 1

    def publish(self) -> None:
        now = time.time_ns()
        oracle, external = self.latest.get(ORACLE_TOPIC, {}), self.latest.get(EXTERNAL_TOPIC, {})
        oracle_age = max(0, now - int(oracle.get("receive_wall_ns") or 0)) if oracle else 0
        external_age = max(0, now - int(external.get("receive_wall_ns") or 0)) if external else 0
        oracle_healthy = bool(oracle and oracle_age <= FRESH_NS)
        external_fresh = bool(external and external_age <= FRESH_NS)
        continuity = "LIVE_CONTINUOUS" if oracle_healthy and self.accepted >= 2 else "CONTINUITY_UNKNOWN"
        common = {"paper_only": True, "authenticated_execution": False, "real_order_submission": False}
        atomic_json(self.root / "oracle_status.json", {
            "schema": "polymarket_v7_same_oracle_status_v1", "state": continuity,
            "reason": "" if oracle_healthy else self.last_error, "timestamp_ns": now,
            "healthy": oracle_healthy, "value": float(oracle.get("price") or 0.0),
            "age_ns": oracle_age, "source_sequence": int(oracle.get("timestamp_ms") or 0),
            "connection_epoch": self.connection_epoch, "reconnects": self.reconnects,
            "gaps": self.gaps, "transport": "POLYMARKET_PUBLIC_RTDS_CHAINLINK", **common,
        })
        status = {
            "schema": "polymarket_v7_external_fair_status_v1",
            "state": "DATA_PLANE_OPERATIONAL" if oracle_healthy else "DATA_PLANE_DEGRADED",
            "code_sha": self.code_sha, "execution_authority": "SHADOW_ZERO_AUTHORITY",
            "external_fair_required_markets": 0, **common,
            "contract": {"verified": False, "rules_hash_recognized": False, "oracle_window_seconds": 60},
            "settlement_reference": {"valid": False, "value": 0.0, "version": 0},
            "oracle": {"healthy": oracle_healthy, "value": float(oracle.get("price") or 0.0),
                       "age_ns": oracle_age, "continuity": continuity,
                       "connection_epoch": self.connection_epoch, "reconnects": self.reconnects, "gaps": self.gaps},
            "external": {"healthy": False, "fresh_venue_count": 1 if external_fresh else 0,
                         "dispersion_bps": 0.0, "age_ns": external_age, "venues": [{
                "venue": "BINANCE_SPOT", "healthy": external_fresh, "age_ns": external_age,
                "price": float(external.get("price") or 0.0), "microprice": float(external.get("price") or 0.0),
                "spread_bps": 0.0, "weight": 1.0 if external_fresh else 0.0,
                "basis_bps": ((float(external["price"]) / float(oracle["price"]) - 1.0) * 10_000.0
                              if external_fresh and oracle_healthy else 0.0), "disabled": not external_fresh}]},
            "fair": {"valid": False, "yes": 0.5, "lower": 0.0, "upper": 1.0},
            "model": {"mature": False, "coverage": 0.0},
            "tape": {"evidence_valid": True, "accepted": self.accepted,
                     "written": self.written, "dropped": self.dropped},
            "blockers": ["CONTRACT_BINDING_NOT_RUNNING", "SETTLEMENT_REFERENCE_NOT_CAPTURED",
                         "MULTI_VENUE_EXTERNAL_COMPOSITE_NOT_RUNNING", "FAIR_VALUE_RUNTIME_NOT_RUNNING",
                         "OMS_EXTERNAL_FAIR_ROUTING_NOT_RUNNING"],
        }
        atomic_json(self.root / "status.json", status)

    def run(self, resolver: PublicResolver) -> None:
        while True:
            stream: ssl.SSLSocket | None = None
            try:
                self.connection_epoch += 1
                if self.connection_epoch > 1:
                    self.reconnects += 1
                    self.gaps += 1
                stream = connect_websocket(resolver)
                send_json(stream, {"action": "subscribe", "subscriptions": [
                    {"topic": ORACLE_TOPIC, "type": "*", "filters": "{\"symbol\":\"btc/usd\"}"},
                    {"topic": EXTERNAL_TOPIC, "type": "*", "filters": "{\"symbol\":\"BTCUSDT\"}"},
                ]})
                fragments, fragment_opcode = bytearray(), 0
                self.last_error = "awaiting_public_rtds_observation"
                self.publish()
                while True:
                    try:
                        final, opcode, payload = read_frame(stream)
                    except socket.timeout:
                        self.publish()
                        continue
                    if opcode == 0x8:
                        raise OSError("RTDS websocket closed")
                    if opcode == 0x9:
                        send_frame(stream, 0xA, payload)
                        continue
                    if opcode == 0xA:
                        continue
                    if opcode in {0x1, 0x2}:
                        fragments, fragment_opcode = bytearray(payload), opcode
                    elif opcode == 0x0 and fragment_opcode:
                        fragments.extend(payload)
                    else:
                        raise OSError("unexpected websocket opcode")
                    if len(fragments) > MAX_MESSAGE_BYTES:
                        raise OSError("fragmented websocket message exceeds bound")
                    if not final:
                        continue
                    if fragment_opcode == 0x1:
                        try:
                            decoded = json.loads(fragments.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            self.dropped += 1
                        else:
                            for row in observations(decoded):
                                self.ingest(row)
                    fragments, fragment_opcode = bytearray(), 0
                    self.last_error = ""
                    self.publish()
            except Exception as exc:  # noqa: BLE001 - persistent reconnect loop
                self.last_error = f"rtds_transport:{type(exc).__name__}:{exc}"
                self.publish()
                time.sleep(min(5.0, 0.25 * (2 ** min(self.reconnects, 4))))
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--dns", action="append", default=[])
    args = parser.parse_args()
    if len(args.code_sha) != 40 or any(char not in "0123456789abcdef" for char in args.code_sha):
        raise SystemExit("--code-sha must be a lowercase 40-character Git SHA")
    Monitor(args.output_dir.resolve(), args.code_sha).run(PublicResolver(args.dns or list(DEFAULT_DNS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
