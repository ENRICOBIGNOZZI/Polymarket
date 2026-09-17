#!/usr/bin/env python3
"""Read-only public WebSocket feed timing probe for external venues."""
from __future__ import annotations

import argparse
import json
import math
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SPECS = (
    ("binance_spot", "wss://stream.binance.com:9443/ws",
     {"method":"SUBSCRIBE","params":["btcusdt@bookTicker","btcusdt@aggTrade"],"id":1}),
    ("binance_usdm", "wss://fstream.binance.com/public/ws",
     {"method":"SUBSCRIBE","params":["btcusdt@depth20@100ms","btcusdt@aggTrade"],"id":1}),
    ("coinbase_spot", "wss://ws-feed.exchange.coinbase.com/",
     {"type":"subscribe","product_ids":["BTC-USD"],"channels":["level2_batch"]}),
    ("bybit_spot", "wss://stream.bybit.com/v5/public/spot",
     {"op":"subscribe","args":["orderbook.50.BTCUSDT","publicTrade.BTCUSDT"]}),
    ("deribit", "wss://www.deribit.com/ws/api/v2",
     {"jsonrpc":"2.0","method":"public/subscribe","id":1,
      "params":{"channels":["ticker.BTC-PERPETUAL.100ms","trades.BTC-PERPETUAL.100ms"]}}),
)

def exact_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def distribution(values: list[float]) -> dict[str, float]:
    values = sorted(values)
    if not values:
        return {}
    def q(probability: float) -> float:
        index = max(0, min(len(values)-1, math.ceil(probability*len(values))-1))
        return values[index]
    return {name: round(q(p), 3) for name, p in (
        ("p50", .5), ("p90", .9), ("p95", .95), ("p99", .99), ("max", 1.0)
    )}


def iso_ms(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() * 1000 if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def timestamps(value: object) -> list[float]:
    out: list[float] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"E", "T", "ts", "timestamp"} and isinstance(item, (int, float)):
                if 1e12 < item < 1e14:
                    out.append(float(item))
            elif key == "time" and isinstance(item, str):
                parsed = iso_ms(item)
                if parsed is not None:
                    out.append(parsed)
            elif isinstance(item, (dict, list)):
                out.extend(timestamps(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(timestamps(item))
    return out


def current_sha(app: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(app), "rev-parse", "HEAD"], text=True,
        stderr=subprocess.DEVNULL, timeout=5,
    ).strip()


def run_feed(websocket_cls, url: str, subscription: dict[str, object],
             duration_s: float, maximum_messages: int) -> dict[str, object]:
    start = time.monotonic_ns()
    errors: list[str] = []
    ages: list[float] = []
    interarrival: list[float] = []
    count = 0
    previous_ns: int | None = None
    first_ms: float | None = None
    websocket = None
    try:
        websocket = websocket_cls(url, 8.0)
        handshake_ms = (time.monotonic_ns() - start) / 1e6
        sent_ns = time.monotonic_ns()
        websocket.send_text(json.dumps(subscription, separators=(",", ":")))
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline and count < maximum_messages:
            try:
                message = websocket.recv_message()
            except socket.timeout:
                continue
            except Exception as exc:
                errors.append(type(exc).__name__ + ":" + str(exc))
                break
            if message is None:
                continue
            now_ns = time.monotonic_ns()
            now_ms = time.time_ns() / 1e6
            if first_ms is None:
                first_ms = (now_ns - sent_ns) / 1e6
            if previous_ns is not None:
                interarrival.append((now_ns - previous_ns) / 1e6)
            previous_ns = now_ns
            try:
                parsed = json.loads(message)
            except json.JSONDecodeError:
                continue
            observed = timestamps(parsed)
            if observed:
                age = now_ms - max(observed)
                if -10_000 < age < 10_000:
                    ages.append(age)
            count += 1
        return {
            "handshake_ms": round(handshake_ms, 3),
            "first_message_ms": None if first_ms is None else round(first_ms, 3),
            "messages": count, "message_interarrival_ms": distribution(interarrival),
            "timestamp_age_ms": distribution(ages), "timestamp_samples": len(ages),
            "negative_age_count": sum(value < 0 for value in ages), "errors": errors[:10],
        }
    except Exception as exc:
        errors.append(type(exc).__name__ + ":" + str(exc))
        return {
            "handshake_ms": None, "first_message_ms": None, "messages": count,
            "message_interarrival_ms": {}, "timestamp_age_ms": {},
            "timestamp_samples": 0, "negative_age_count": 0, "errors": errors[:10],
        }
    finally:
        if websocket is not None:
            try:
                websocket.close()
            except Exception:
                pass

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--maximum-messages", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not exact_sha(args.expected_sha):
        raise SystemExit("exact lowercase SHA required")
    if not 1 <= args.duration <= 3600 or not 1 <= args.maximum_messages <= 100_000:
        raise SystemExit("probe bounds invalid")
    observed_sha = current_sha(args.app)
    if observed_sha != args.expected_sha:
        raise SystemExit(f"exact SHA mismatch: {observed_sha} != {args.expected_sha}")
    sys.path.insert(0, str(args.app / "scripts"))
    from v7_public_book_wire_probe import WebSocket
    rows = {
        name: run_feed(WebSocket, url, subscription, args.duration, args.maximum_messages)
        for name, url, subscription in SPECS
    }
    result = {
        "schema": "polymarket_v7_external_ws_age_probe_v1",
        "region": args.region, "exact_code_sha": args.expected_sha,
        "targets": rows, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "authorizes_live_execution": False,
        "measurement_scope": "PUBLIC_WEBSOCKET_CONNECTIVITY_AND_FEED_TIMING_ONLY",
        "clock_caveat": (
            "timestamp_age compares venue wall-clock fields with local wall clock; "
            "it is not one-way latency proof without bounded clock offset"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
