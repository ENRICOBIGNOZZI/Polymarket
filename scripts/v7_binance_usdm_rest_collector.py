#!/usr/bin/env python3
"""Bounded public Binance USD-M slow-state collector for V7.

This intentionally labels REST observations as polling observations. It owns no
execution, capital, OMS, ledger, or promotion authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE = "https://fapi.binance.com"


class UsdmRestError(ValueError):
    pass


def _fetch(path: str, timeout_s: float) -> tuple[dict[str, Any], dict[str, Any]]:
    started_mono = time.monotonic_ns()
    request = urllib.request.Request(BASE + path, headers={"User-Agent": "polymarket-v7-external/1"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # nosec B310: fixed HTTPS origin
        raw = response.read()
        status = int(response.status)
    received_wall = time.time_ns()
    received_mono = time.monotonic_ns()
    if status != 200:
        raise UsdmRestError(f"http_status:{status}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsdmRestError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise UsdmRestError("response_not_object")
    return payload, {
        "local_receive_wall_ns": received_wall,
        "local_receive_monotonic_ns": received_mono,
        "request_duration_ns": received_mono - started_mono,
        "raw_payload_hash": hashlib.sha256(raw).hexdigest(),
    }


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise UsdmRestError(f"invalid_{name}") from exc
    if not math.isfinite(parsed) or (positive and parsed <= 0.0):
        raise UsdmRestError(f"invalid_{name}")
    return parsed


def collect_once(previous: dict[str, Any] | None = None, *, timeout_s: float = 5.0) -> tuple[dict[str, Any], dict[str, Any]]:
    previous = previous or {}
    premium, premium_meta = _fetch("/fapi/v1/premiumIndex?symbol=BTCUSDT", timeout_s)
    interest, interest_meta = _fetch("/fapi/v1/openInterest?symbol=BTCUSDT", timeout_s)
    mark = _number(premium.get("markPrice"), "mark_price", positive=True)
    index = _number(premium.get("indexPrice"), "index_price", positive=True)
    funding = _number(premium.get("lastFundingRate"), "funding_rate")
    open_interest = _number(interest.get("openInterest"), "open_interest", positive=True)
    previous_interest = float(previous.get("open_interest", open_interest))
    previous_receive = int(previous.get("local_receive_monotonic_ns", 0))
    now_receive = max(int(premium_meta["local_receive_monotonic_ns"]), int(interest_meta["local_receive_monotonic_ns"]))
    elapsed = now_receive - previous_receive
    delta = open_interest - previous_interest if previous_receive > 0 and elapsed > 0 else None
    velocity = delta / (elapsed / 1_000_000_000) if delta is not None and elapsed > 0 else None
    row = {
        "schema": "polymarket_v7_binance_usdm_rest_observation_v1",
        "source_id": "binance_usdm_btcusdt", "provider": "Binance", "venue": "BINANCE_USDM",
        "instrument_id": "BTCUSDT", "transport": "PUBLIC_REST_POLLING",
        "polling_latency_not_event_latency": True,
        "mark_price": mark, "index_price": index, "mark_index_basis_bps": (mark / index - 1.0) * 10_000.0,
        "funding_rate": funding, "next_funding_time_ms": premium.get("nextFundingTime"),
        "open_interest": open_interest, "delta_open_interest": delta, "open_interest_velocity": velocity,
        "premium_request": premium_meta, "open_interest_request": interest_meta,
        "local_receive_monotonic_ns": now_receive, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False, "execution_authority": False,
    }
    status = {"schema": "polymarket_v7_binance_usdm_rest_status_v1", "state": "OPERATIONAL",
              "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
              "latest": row, "blocker": None}
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
    previous: dict[str, Any] = {}
    while True:
        try:
            row, status = collect_once(previous)
            previous = row
            args.tape.parent.mkdir(parents=True, exist_ok=True)
            with args.tape.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        except (UsdmRestError, OSError, urllib.error.URLError) as exc:
            status = {"schema": "polymarket_v7_binance_usdm_rest_status_v1", "state": "DEGRADED",
                      "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
                      "blocker": str(exc), "execution_authority": False}
        _write_json(args.status, status)
        if not args.loop:
            return 0 if status["state"] == "OPERATIONAL" else 2
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
