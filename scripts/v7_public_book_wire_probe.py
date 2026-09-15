#!/usr/bin/env python3
"""Capture a few public Polymarket BTC-5m book frames for decoder diagnostics.

Zero authority: public Gamma/CLOB/WebSocket data only. The probe never reads
credentials, never signs requests, never writes orders, and never feeds the
runtime. It exists only to explain why a canonical L2 snapshot was rejected.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_RAW_BYTES = 2 * 1024 * 1024


def _get_json(url: str, timeout: float = 10.0) -> Any:
    request = Request(url, headers={"User-Agent": "Polymarket-V7-public-diagnostic/1"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def discover_market(now_s: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now_s is None else int(now_s)
    boundary = now // 300 * 300
    candidates: list[tuple[int, dict[str, Any]]] = []
    for start in (boundary, boundary + 300, boundary - 300):
        slug = f"btc-updown-5m-{start}"
        try:
            value = _get_json(f"{GAMMA}/markets/slug/{slug}")
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        tokens = [str(item) for item in _as_list(value.get("clobTokenIds"))]
        outcomes = [str(item) for item in _as_list(value.get("outcomes"))]
        if len(tokens) != 2 or tokens[0] == tokens[1]:
            continue
        value = dict(value)
        value["_interval_start"] = start
        value["_tokens"] = tokens
        value["_outcomes"] = outcomes
        distance = 0 if start <= now < start + 300 else abs(start - now) + 10_000
        candidates.append((distance, value))
    if not candidates:
        raise RuntimeError("no BTC 5m Gamma market resolved around current boundary")
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def fetch_tick(token_id: str) -> Decimal:
    value = _get_json(f"{CLOB}/tick-size?token_id={quote(token_id, safe='')}")
    if not isinstance(value, dict) or "minimum_tick_size" not in value:
        raise RuntimeError("CLOB tick-size response missing minimum_tick_size")
    try:
        tick = Decimal(str(value["minimum_tick_size"]))
    except InvalidOperation as exc:
        raise RuntimeError("invalid CLOB tick size") from exc
    if tick <= 0 or tick >= 1:
        raise RuntimeError("CLOB tick size outside (0,1)")
    return tick


def _client_frame(opcode: int, payload: bytes) -> bytes:
    first = 0x80 | (opcode & 0x0F)
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes((first, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
    else:
        header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
    masked = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
    return header + mask + masked


class WebSocket:
    def __init__(self, url: str, timeout: float) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "wss" or not parsed.hostname:
            raise ValueError("probe requires wss:// URL")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.target = parsed.path or "/"
        if parsed.query:
            self.target += "?" + parsed.query
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        raw.settimeout(timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(raw, server_hostname=self.host)
        self.sock.settimeout(timeout)
        self.buffer = bytearray()
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.target} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        header = self._read_http_header()
        status = header.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise RuntimeError(f"websocket upgrade rejected: {status.decode('latin1', 'replace')}")
        fields: dict[str, str] = {}
        for line in header.split(b"\r\n")[1:]:
            if b":" not in line:
                continue
            name, value = line.split(b":", 1)
            fields[name.decode("latin1").strip().lower()] = value.decode("latin1").strip()
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        if fields.get("sec-websocket-accept") != expected:
            raise RuntimeError("websocket accept hash mismatch")

    def _read_http_header(self) -> bytes:
        marker = b"\r\n\r\n"
        while marker not in self.buffer:
            block = self.sock.recv(4096)
            if not block:
                raise RuntimeError("EOF during websocket handshake")
            self.buffer.extend(block)
            if len(self.buffer) > 64 * 1024:
                raise RuntimeError("oversized websocket handshake")
        index = self.buffer.index(marker) + len(marker)
        header = bytes(self.buffer[:index])
        del self.buffer[:index]
        return header

    def _need(self, count: int) -> bytes:
        while len(self.buffer) < count:
            block = self.sock.recv(max(4096, count - len(self.buffer)))
            if not block:
                raise EOFError("websocket EOF")
            self.buffer.extend(block)
        out = bytes(self.buffer[:count])
        del self.buffer[:count]
        return out

    def send_text(self, text: str) -> None:
        self.sock.sendall(_client_frame(0x1, text.encode("utf-8")))

    def recv_message(self) -> str | None:
        fragments = bytearray()
        collecting = False
        while True:
            head = self._need(2)
            first, second = head[0], head[1]
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._need(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._need(8))[0]
            if length > MAX_RAW_BYTES:
                raise RuntimeError("websocket diagnostic frame exceeds 2 MiB")
            mask = self._need(4) if masked else b""
            payload = self._need(length)
            if masked:
                payload = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self.sock.sendall(_client_frame(0xA, payload))
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                fragments = bytearray(payload)
                collecting = not fin
            elif opcode == 0x0 and collecting:
                fragments.extend(payload)
                collecting = not fin
            else:
                continue
            if fin and not collecting:
                if opcode == 0x2:
                    return None
                return bytes(fragments).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.sendall(_client_frame(0x8, b""))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def _events(value: Any) -> Iterable[tuple[dict[str, Any], dict[str, Any], str]]:
    rows = value if isinstance(value, list) else [value]
    for row in rows:
        if not isinstance(row, dict):
            continue
        outer_type = str(row.get("event_type") or row.get("type") or "")
        inner = row.get("payload") if isinstance(row.get("payload"), dict) else row
        inner_type = str(inner.get("event_type") or inner.get("type") or "")
        event_type = outer_type or inner_type
        if event_type.lower() == "book":
            yield row, inner, "outer" if outer_type.lower() == "book" else "inner"


def _level(raw: Any) -> tuple[Decimal, Decimal] | None:
    try:
        if isinstance(raw, dict):
            price, size = raw.get("price"), raw.get("size")
        elif isinstance(raw, list) and len(raw) >= 2:
            price, size = raw[0], raw[1]
        else:
            return None
        return Decimal(str(price)), Decimal(str(size))
    except (InvalidOperation, TypeError, ValueError):
        return None


def diagnose_book(outer: dict[str, Any], book: dict[str, Any], tick: Decimal | None) -> dict[str, Any]:
    bids_raw = book.get("bids") if isinstance(book.get("bids"), list) else []
    asks_raw = book.get("asks") if isinstance(book.get("asks"), list) else []
    bids = [value for raw in bids_raw if (value := _level(raw)) is not None and value[1] > 0]
    asks = [value for raw in asks_raw if (value := _level(raw)) is not None and value[1] > 0]
    prices = [price for price, _ in bids + asks]
    aligned = None
    if tick is not None and prices:
        aligned = all((price / tick) == (price / tick).to_integral_value() for price in prices)
    best_bid = max((price for price, _ in bids), default=None)
    best_ask = min((price for price, _ in asks), default=None)
    return {
        "asset_id": str(book.get("asset_id") or book.get("assetId") or book.get("token_id") or book.get("tokenId") or ""),
        "outer_timestamp": outer.get("timestamp"),
        "payload_timestamp": book.get("timestamp"),
        "outer_keys": sorted(str(key) for key in outer.keys()),
        "payload_keys": sorted(str(key) for key in book.keys()),
        "bid_levels_raw": len(bids_raw),
        "ask_levels_raw": len(asks_raw),
        "bid_levels_positive_parseable": len(bids),
        "ask_levels_positive_parseable": len(asks),
        "best_bid": str(best_bid) if best_bid is not None else None,
        "best_ask": str(best_ask) if best_ask is not None else None,
        "crossed_or_locked": bool(best_bid is not None and best_ask is not None and best_bid >= best_ask),
        "tick_size": str(tick) if tick is not None else None,
        "all_positive_levels_tick_aligned": aligned,
    }


def run(expected_sha: str, timeout_seconds: float, max_book_events: int) -> dict[str, Any]:
    market = discover_market()
    tokens = [str(value) for value in market["_tokens"]]
    ticks = {token: fetch_tick(token) for token in tokens}
    subscription = json.dumps({
        "assets_ids": tokens,
        "type": "market",
        "custom_feature_enabled": True,
    }, separators=(",", ":"))
    result: dict[str, Any] = {
        "schema": "polymarket_v7_public_book_wire_probe_v1",
        "generated_at_ms": time.time_ns() // 1_000_000,
        "code_sha": expected_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": "ZERO_AUTHORITY_PUBLIC_DIAGNOSTIC",
        "market": {
            "market_id": str(market.get("id") or ""),
            "condition_id": str(market.get("conditionId") or ""),
            "slug": str(market.get("slug") or ""),
            "interval_start": int(market["_interval_start"]),
            "tokens": tokens,
            "outcomes": market.get("_outcomes"),
            "ticks": {key: str(value) for key, value in ticks.items()},
        },
        "subscription": json.loads(subscription),
        "messages": [],
        "books": [],
    }
    ws = WebSocket(WS_URL, timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    raw_bytes = 0
    try:
        ws.send_text(subscription)
        while time.monotonic() < deadline and len(result["books"]) < max_book_events:
            try:
                text = ws.recv_message()
            except socket.timeout:
                break
            if text is None:
                continue
            encoded = text.encode("utf-8", "replace")
            raw_bytes += len(encoded)
            if raw_bytes > MAX_RAW_BYTES:
                result["bounded_raw_limit_reached"] = True
                break
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            found = list(_events(value))
            if not found:
                continue
            result["messages"].append({
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "bytes": len(encoded),
                "raw_payload": text,
            })
            for outer, book, _ in found:
                token = str(book.get("asset_id") or book.get("assetId") or book.get("token_id") or book.get("tokenId") or "")
                result["books"].append(diagnose_book(outer, book, ticks.get(token)))
                if len(result["books"]) >= max_book_events:
                    break
    finally:
        ws.close()
    result["captured_book_events"] = len(result["books"])
    result["captured_raw_bytes"] = raw_bytes
    result["state"] = "CAPTURED" if result["books"] else "NO_BOOK_FRAME_CAPTURED"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-book-events", type=int, default=4)
    args = parser.parse_args()
    if len(args.expected_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.expected_sha):
        raise SystemExit("--expected-sha must be exact lower-case 40-hex")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run(args.expected_sha, max(1.0, args.timeout_seconds), max(1, min(8, args.max_book_events)))
        code = 0 if result.get("state") == "CAPTURED" else 2
    except Exception as exc:
        result = {
            "schema": "polymarket_v7_public_book_wire_probe_v1",
            "generated_at_ms": time.time_ns() // 1_000_000,
            "code_sha": args.expected_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_PUBLIC_DIAGNOSTIC",
            "state": "PROBE_FAILED",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        code = 2
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: result.get(key) for key in ("state", "code_sha", "captured_book_events", "error")}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
