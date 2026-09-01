#!/usr/bin/env python3
"""Read-only authenticated Kalshi WebSocket collector for the V7 cross sleeve.

It only reads market data.  Credentials are read from a locally protected key
file at connection time and are never written to the tape, status, or logs.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import stat
import struct
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Mapping, Sequence

from v7_cross_platform_collector import atomic_json, git_head


STATUS_SCHEMA = "polymarket_v7_kalshi_authenticated_ws_status_v1"
TAPE_SCHEMA = "polymarket_v7_kalshi_authenticated_ws_tape_v1"
STATE_SCHEMA = "polymarket_v7_kalshi_authenticated_ws_state_v1"
MAX_MESSAGE_BYTES = 1 << 20
CANONICAL_PATH = "/trade-api/ws/v2"


class KalshiWsError(ValueError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _state(path: Path) -> dict[str, Any]:
    value = _load(path)
    if value.get("schema") in (None, STATE_SCHEMA):
        return {"schema": STATE_SCHEMA, "last_hash": str(value.get("last_hash") or "0" * 64),
                "connection_epoch": int(value.get("connection_epoch") or 0)}
    raise KalshiWsError("ws_state_invalid")


def _append(path: Path, state_path: Path, state: dict[str, Any], payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema": TAPE_SCHEMA, "previous_hash": state["last_hash"], **dict(payload)}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**body, "record_hash": digest}, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush(); os.fsync(handle.fileno())
    state["last_hash"] = digest
    # Persist the hash cursor with every fsynced record.  Otherwise an abrupt
    # process exit can restart the next session from a stale cursor and break
    # the append-only evidence chain even though the tape itself survived.
    atomic_json(state_path, state)


def credentials(venue: Mapping[str, Any]) -> tuple[str, Path]:
    key_id = os.environ.get(str(venue.get("key_id_env") or ""), "").strip()
    key_path = Path(os.environ.get(str(venue.get("private_key_path_env") or ""), "").strip())
    if not key_id:
        raise KalshiWsError("BLOCKED_KALSHI_WS_KEY_ID_MISSING")
    try:
        info = key_path.stat()
    except OSError as exc:
        raise KalshiWsError("BLOCKED_KALSHI_WS_PRIVATE_KEY_PATH_MISSING") from exc
    if not stat.S_ISREG(info.st_mode):
        raise KalshiWsError("BLOCKED_KALSHI_WS_PRIVATE_KEY_NOT_REGULAR")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise KalshiWsError("BLOCKED_KALSHI_WS_PRIVATE_KEY_PERMISSIONS_UNSAFE")
    return key_id, key_path


def sign(key_path: Path, message: bytes) -> str:
    """RSA-PSS/SHA-256 signature required by Kalshi; key bytes never enter Python."""
    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key_path),
             "-sigopt", "rsa_padding_mode:pss", "-sigopt", "rsa_pss_saltlen:-1"],
            input=message, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KalshiWsError("kalshi_rsa_pss_signing_failed") from exc
    if not result.stdout:
        raise KalshiWsError("kalshi_rsa_pss_signature_empty")
    return base64.b64encode(result.stdout).decode("ascii")


def auth_headers(key_id: str, key_path: Path, timestamp_ms: int) -> dict[str, str]:
    timestamp = str(int(timestamp_ms))
    signature = sign(key_path, (timestamp + "GET" + CANONICAL_PATH).encode("ascii"))
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature}


def subscription(tickers: Sequence[str], *, ticker_all_markets: bool) -> dict[str, Any]:
    params: dict[str, Any] = {"channels": ["ticker"] if ticker_all_markets else []}
    clean = list(dict.fromkeys(str(x).strip() for x in tickers if str(x).strip()))
    if clean:
        params["channels"].append("orderbook_delta")
        params["market_tickers"] = clean
    if not params["channels"]:
        raise KalshiWsError("BLOCKED_KALSHI_WS_NO_CHANNELS_CONFIGURED")
    return {"id": 1, "cmd": "subscribe", "params": params}


class Wire:
    def __init__(self, stream: ssl.SSLSocket, buffered: bytes = b""):
        self.stream, self.buffered = stream, bytearray(buffered)

    def exact(self, size: int) -> bytes:
        while len(self.buffered) < size:
            chunk = self.stream.recv(max(4096, size - len(self.buffered)))
            if not chunk:
                raise OSError("kalshi websocket EOF")
            self.buffered.extend(chunk)
        output = bytes(self.buffered[:size]); del self.buffered[:size]
        return output

    def send(self, opcode: int, payload: bytes) -> None:
        if len(payload) > MAX_MESSAGE_BYTES:
            raise KalshiWsError("kalshi_ws_outbound_message_too_large")
        mask = os.urandom(4); size = len(payload)
        if size < 126:
            header = bytes((0x80 | opcode, 0x80 | size))
        elif size <= 0xffff:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", size)
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", size)
        self.stream.sendall(header + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(payload)))

    def receive(self) -> tuple[int, bytes]:
        first, second = self.exact(2)
        if first & 0x70 or not (first & 0x80):
            raise KalshiWsError("kalshi_ws_extensions_or_fragments_unsupported")
        opcode, size = first & 0x0f, second & 0x7f
        if size == 126: size = struct.unpack("!H", self.exact(2))[0]
        elif size == 127: size = struct.unpack("!Q", self.exact(8))[0]
        if size > MAX_MESSAGE_BYTES:
            raise KalshiWsError("kalshi_ws_message_too_large")
        masked = bool(second & 0x80); mask = self.exact(4) if masked else b""
        payload = self.exact(size)
        return opcode, bytes(x ^ mask[i % 4] for i, x in enumerate(payload)) if mask else payload


def connect(endpoint: str, headers: Mapping[str, str]) -> Wire:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password or parsed.path != CANONICAL_PATH:
        raise KalshiWsError("kalshi_ws_endpoint_invalid")
    raw = socket.create_connection((parsed.hostname, parsed.port or 443), timeout=10)
    try:
        stream = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [f"GET {CANONICAL_PATH} HTTP/1.1", f"Host: {parsed.hostname}", "Upgrade: websocket",
                 "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines.extend(f"{name}: {value}" for name, value in headers.items())
        stream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(stream.recv(4096))
            if len(response) > 65536: raise KalshiWsError("kalshi_ws_handshake_too_large")
        header, _, remainder = bytes(response).partition(b"\r\n\r\n")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        parsed_headers = {line.partition(":")[0].strip().lower(): line.partition(":")[2].strip()
                          for line in header.decode("latin-1").split("\r\n")[1:]}
        if not header.startswith(b"HTTP/1.1 101 ") or parsed_headers.get("sec-websocket-accept") != expected:
            raise KalshiWsError("kalshi_ws_upgrade_rejected")
        stream.settimeout(2.0)
        return Wire(stream, remainder)
    except BaseException:
        raw.close(); raise


def run_session(*, repository_root: Path, config_path: Path, tape_path: Path, state_path: Path,
                status_path: Path, max_messages: int | None = None) -> dict[str, Any]:
    config = _load(config_path)
    if (config.get("schema") != "polymarket_v7_external_inputs_v1" or config.get("version") != 7
            or config.get("paper_only") is not True or config.get("authenticated_execution") is not False
            or config.get("real_order_submission") is not False):
        raise KalshiWsError("kalshi_ws_external_input_config_invalid")
    section = config.get("cross_platform") if isinstance(config.get("cross_platform"), dict) else {}
    venue = next((x for x in section.get("venues", []) if isinstance(x, dict) and x.get("venue") == "kalshi"), {})
    policy = section.get("authenticated_websocket") if isinstance(section.get("authenticated_websocket"), dict) else {}
    now = int(time.time() * 1000); state = _state(state_path); messages = snapshots = deltas = tickers = 0
    last_message_at_ms: int | None = None
    base = {"schema": STATUS_SCHEMA, "version": 7, "family": "cross_platform", "authority": "RESEARCH",
            "model_sha": git_head(repository_root), "timestamp_ms": now, "paper_only": True, "research_only": True,
            "authenticated_execution": False, "real_order_submission": False, "execution_authority": False,
            "capital_authority": False, "oms_authority": False, "ledger_write_authority": False, "promotion_authority": False}
    def publish(feed_status: str, blocker: str) -> dict[str, Any]:
        timestamp_ms = int(time.time() * 1000)
        status = {
            **base,
            "timestamp_ms": timestamp_ms,
            "timestamp": timestamp_ms // 1000,
            "process_state": "RUNNING",
            "implementation_complete": True,
            "feed_operational": feed_status == "OPERATIONAL",
            "feed_status": feed_status,
            "transport": "AUTHENTICATED_WEBSOCKET",
            "websocket_attempted": feed_status != "BLOCKED",
            "messages": messages,
            "orderbook_snapshots": snapshots,
            "orderbook_deltas": deltas,
            "ticker_updates": tickers,
            "last_message_at_ms": last_message_at_ms,
            "feed_age_ms": timestamp_ms - last_message_at_ms if last_message_at_ms is not None else None,
            "connection_epoch": state["connection_epoch"],
            "last_attempt_ts": timestamp_ms // 1000,
            "last_success_ts": timestamp_ms // 1000 if feed_status == "OPERATIONAL" else 0,
            "blocker": blocker,
            "reason_codes": [blocker] if blocker else [],
        }
        atomic_json(status_path, status)
        return status

    wire: Wire | None = None
    status: dict[str, Any]
    try:
        if policy.get("enabled") is not True: raise KalshiWsError("BLOCKED_KALSHI_WS_DISABLED_BY_POLICY")
        key_id, key_path = credentials(venue)
        wire = connect(str(venue.get("websocket_endpoint") or ""), auth_headers(key_id, key_path, now))
        state["connection_epoch"] += 1
        atomic_json(state_path, state)
        wire.send(1, json.dumps(subscription(policy.get("market_tickers") or [],
                                            ticker_all_markets=policy.get("ticker_stream_all_markets") is True),
                               separators=(",", ":")).encode())
        # A connected, subscribed socket is operational even before its first
        # market update.  Publish immediately and refresh on idle timeouts so
        # the canonical supervisor never sees a healthy long-lived session as
        # missing or stale.
        status = publish("OPERATIONAL", "")
        while max_messages is None or messages < max_messages:
            try: opcode, payload = wire.receive()
            except socket.timeout:
                status = publish("OPERATIONAL", "")
                continue
            if opcode == 0x9: wire.send(0xA, payload); continue
            if opcode == 0x8: raise KalshiWsError("kalshi_ws_closed")
            if opcode != 0x1: continue
            value = json.loads(payload); kind = str(value.get("type") or "") if isinstance(value, dict) else ""
            last_message_at_ms = int(time.time() * 1000)
            _append(tape_path, state_path, state, {"received_at_ms": last_message_at_ms, "transport": "AUTHENTICATED_WEBSOCKET",
                                                   "connection_epoch": state["connection_epoch"], "message": value})
            messages += 1; snapshots += kind == "orderbook_snapshot"; deltas += kind == "orderbook_delta"; tickers += kind == "ticker"
            status = publish("OPERATIONAL", "")
        status = publish("OPERATIONAL", "")
    except Exception as exc:  # credentials and transport fail locally, without leaking detail
        feed_status, blocker = "BLOCKED" if str(exc).startswith("BLOCKED_") else "DOWN", str(exc)
        status = publish(feed_status, blocker)
    finally:
        if wire is not None:
            try:
                wire.stream.close()
            except OSError:
                pass
        atomic_json(state_path, state)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, default=Path("config/v7_external_inputs.json")); parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True); parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--loop", action="store_true"); args = parser.parse_args(argv)
    if args.max_messages is not None and args.max_messages < 1:
        parser.error("--max-messages must be positive")
    while True:
        result = run_session(repository_root=args.repository_root, config_path=args.config, tape_path=args.tape,
                             state_path=args.state, status_path=args.status, max_messages=args.max_messages)
        print(json.dumps(result, sort_keys=True), flush=True)
        if not args.loop: return 0
        time.sleep(5 if result["feed_status"] == "OPERATIONAL" else 15)


if __name__ == "__main__":
    raise SystemExit(main())
