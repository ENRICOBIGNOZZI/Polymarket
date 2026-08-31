#!/usr/bin/env python3
"""Bounded public Coinbase Exchange L2 snapshot collector for V7.

This is a polling-only fallback for the Coinbase Exchange websocket observer.
It must never be represented as real-time L2 continuity, an HFT trigger, or
an execution authority. The record retains request timing and a content hash
so every snapshot has independent provenance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.request
from pathlib import Path
from typing import Any


BASE = "https://api.exchange.coinbase.com"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class CoinbaseL2RestError(ValueError):
    pass


def _fetch(timeout_s: float) -> tuple[dict[str, Any], dict[str, Any]]:
    started_mono = time.monotonic_ns()
    request = urllib.request.Request(
        BASE + "/products/BTC-USD/book?level=2",
        headers={"User-Agent": "polymarket-v7-external/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # nosec B310: fixed public HTTPS origin
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        status = int(response.status)
    received_wall = time.time_ns()
    received_mono = time.monotonic_ns()
    if status != 200:
        raise CoinbaseL2RestError(f"http_status:{status}")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CoinbaseL2RestError("response_too_large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CoinbaseL2RestError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise CoinbaseL2RestError("response_not_object")
    return payload, {
        "local_receive_wall_ns": received_wall,
        "local_receive_monotonic_ns": received_mono,
        "request_duration_ns": received_mono - started_mono,
        "raw_payload_hash": hashlib.sha256(raw).hexdigest(),
        "raw_payload_bytes": len(raw),
    }


def _number(value: Any, name: str, *, allow_zero: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CoinbaseL2RestError(f"invalid_{name}") from exc
    if not math.isfinite(parsed) or (parsed < 0.0 if allow_zero else parsed <= 0.0):
        raise CoinbaseL2RestError(f"invalid_{name}")
    return parsed


def _levels(value: Any, side: str) -> list[list[float]]:
    if not isinstance(value, list) or not value:
        raise CoinbaseL2RestError(f"invalid_{side}_levels")
    output: list[list[float]] = []
    previous = math.inf if side == "bid" else 0.0
    for row in value[:20]:
        if not isinstance(row, list) or len(row) < 2:
            raise CoinbaseL2RestError(f"invalid_{side}_level")
        price = _number(row[0], f"{side}_price")
        size = _number(row[1], f"{side}_size")
        if (side == "bid" and price > previous) or (side == "ask" and price < previous):
            raise CoinbaseL2RestError(f"unordered_{side}_levels")
        previous = price
        output.append([price, size])
    return output


def collect_once(*, timeout_s: float = 10.0) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, request = _fetch(timeout_s)
    bids = _levels(payload.get("bids"), "bid")
    asks = _levels(payload.get("asks"), "ask")
    if bids[0][0] >= asks[0][0]:
        raise CoinbaseL2RestError("crossed_book")
    try:
        sequence = int(payload.get("sequence"))
    except (TypeError, ValueError) as exc:
        raise CoinbaseL2RestError("invalid_sequence") from exc
    if sequence <= 0:
        raise CoinbaseL2RestError("invalid_sequence")
    bid_l20 = sum(row[1] for row in bids)
    ask_l20 = sum(row[1] for row in asks)
    row = {
        "schema": "polymarket_v7_coinbase_l2_rest_observation_v1",
        "source_id": "coinbase_spot_btcusd_rest_snapshot", "provider": "Coinbase",
        "venue": "COINBASE_SPOT", "asset": "BTC", "instrument_id": "BTC-USD",
        "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
        "realtime_l2_continuity": False, "hft_trigger_eligible": False,
        "sequence": sequence, "exchange_time": payload.get("time"),
        "bids_l20": bids, "asks_l20": asks,
        "best_bid": bids[0][0], "best_ask": asks[0][0],
        "mid": 0.5 * (bids[0][0] + asks[0][0]),
        "bid_depth_l20": bid_l20, "ask_depth_l20": ask_l20,
        "imbalance_l20": (bid_l20 - ask_l20) / (bid_l20 + ask_l20) if bid_l20 + ask_l20 > 0.0 else 0.0,
        "request": request, "local_receive_monotonic_ns": request["local_receive_monotonic_ns"],
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "execution_authority": False, "capital_authority": False, "oms_authority": False,
        "ledger_writer_authority": False, "promotion_authority": False,
    }
    status = {
        "schema": "polymarket_v7_coinbase_l2_rest_status_v1", "state": "OPERATIONAL_POLLING",
        "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
        "realtime_l2_continuity": False, "hft_trigger_eligible": False,
        "latest": row, "blocker": "WEBSOCKET_REALTIME_CONTINUITY_NOT_ESTABLISHED",
    }
    return row, status


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            row, status = collect_once()
            args.tape.parent.mkdir(parents=True, exist_ok=True)
            with args.tape.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        except (CoinbaseL2RestError, OSError, urllib.error.URLError) as exc:
            status = {"schema": "polymarket_v7_coinbase_l2_rest_status_v1", "state": "DEGRADED",
                      "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
                      "realtime_l2_continuity": False, "hft_trigger_eligible": False,
                      "blocker": str(exc), "execution_authority": False}
        _write_json(args.status, status)
        if not args.loop:
            return 0 if status["state"] == "OPERATIONAL_POLLING" else 2
        time.sleep(max(2.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
