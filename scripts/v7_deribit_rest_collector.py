#!/usr/bin/env python3
"""Bounded public Deribit futures and options-surface collector for V7.

The WebSocket runtime owns the low-latency BTC perpetual context.  This
collector deliberately covers discovery and slow-changing derivative fields
through public REST polling, and marks every observation as polling data.  It
has no execution, capital, OMS, ledger, or promotion authority.
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


BASE = "https://www.deribit.com/api/v2/public"


class DeribitRestError(ValueError):
    pass


def _fetch(path: str, timeout_s: float) -> tuple[Any, dict[str, Any]]:
    started_mono = time.monotonic_ns()
    request = urllib.request.Request(BASE + path, headers={"User-Agent": "polymarket-v7-external/1"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # nosec B310: fixed HTTPS origin
        raw = response.read()
        status = int(response.status)
    received_wall = time.time_ns()
    received_mono = time.monotonic_ns()
    if status != 200:
        raise DeribitRestError(f"http_status:{status}")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeribitRestError("invalid_json") from exc
    if not isinstance(envelope, dict) or "error" in envelope or "result" not in envelope:
        raise DeribitRestError("invalid_jsonrpc_result")
    return envelope["result"], {
        "local_receive_wall_ns": received_wall,
        "local_receive_monotonic_ns": received_mono,
        "request_duration_ns": received_mono - started_mono,
        "raw_payload_hash": hashlib.sha256(raw).hexdigest(),
    }


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DeribitRestError(f"invalid_{name}") from exc
    if not math.isfinite(parsed) or (positive and parsed <= 0.0):
        raise DeribitRestError(f"invalid_{name}")
    return parsed


def _active(instruments: Any, kind: str) -> list[dict[str, Any]]:
    if not isinstance(instruments, list):
        raise DeribitRestError("instruments_not_list")
    return [row for row in instruments if isinstance(row, dict) and row.get("kind") == kind
            and row.get("is_active") is not False]


def _next_future(instruments: list[dict[str, Any]], now_wall_ns: int) -> dict[str, Any]:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for row in instruments:
        name = row.get("instrument_name")
        delivery = row.get("expiration_timestamp")
        if not isinstance(name, str) or name == "BTC-PERPETUAL":
            continue
        try:
            delivery_ns = int(delivery) * 1_000_000
        except (TypeError, ValueError):
            continue
        if delivery_ns > now_wall_ns:
            candidates.append((delivery_ns, row))
    if not candidates:
        raise DeribitRestError("nearest_future_unavailable")
    return min(candidates, key=lambda item: item[0])[1]


def _option_surface(instruments: list[dict[str, Any]], summaries: Any) -> list[dict[str, Any]]:
    if not isinstance(summaries, list):
        raise DeribitRestError("option_summaries_not_list")
    metadata = {row.get("instrument_name"): row for row in instruments if isinstance(row.get("instrument_name"), str)}
    surface: list[dict[str, Any]] = []
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        name = summary.get("instrument_name")
        instrument = metadata.get(name)
        if instrument is None:
            continue
        try:
            mark_iv = _number(summary.get("mark_iv"), "option_mark_iv", positive=True)
            mark_price = _number(summary.get("mark_price"), "option_mark_price", positive=True)
            strike = _number(instrument.get("strike"), "option_strike", positive=True)
            expiry_ms = int(instrument.get("expiration_timestamp"))
        except DeribitRestError:
            continue
        option_type = instrument.get("option_type")
        if option_type not in {"call", "put"}:
            continue
        surface.append({
            "instrument_id": name, "expiry_ms": expiry_ms, "strike": strike,
            "option_type": option_type, "mark_iv": mark_iv, "mark_price": mark_price,
            "bid_iv": summary.get("bid_iv"), "ask_iv": summary.get("ask_iv"),
            "underlying_price": summary.get("underlying_price"),
            "open_interest": summary.get("open_interest"),
        })
    if not surface:
        raise DeribitRestError("option_surface_empty")
    return sorted(surface, key=lambda row: (row["expiry_ms"], row["strike"], row["option_type"]))


def collect_once(*, timeout_s: float = 10.0) -> tuple[dict[str, Any], dict[str, Any]]:
    query = urllib.parse.urlencode({"currency": "BTC", "expired": "false"})
    instruments, instruments_meta = _fetch(f"/get_instruments?{query}", timeout_s)
    futures = _active(instruments, "future")
    options = _active(instruments, "option")
    next_future = _next_future(futures, int(instruments_meta["local_receive_wall_ns"]))
    option_summary, option_summary_meta = _fetch(
        "/get_book_summary_by_currency?" + urllib.parse.urlencode({"currency": "BTC", "kind": "option"}), timeout_s)
    perpetual, perpetual_meta = _fetch("/ticker?" + urllib.parse.urlencode({"instrument_name": "BTC-PERPETUAL"}), timeout_s)
    future_name = next_future["instrument_name"]
    future, future_meta = _fetch("/ticker?" + urllib.parse.urlencode({"instrument_name": future_name}), timeout_s)
    historical_volatility, volatility_meta = _fetch("/get_historical_volatility?currency=BTC", timeout_s)
    if not isinstance(perpetual, dict) or not isinstance(future, dict) or not isinstance(historical_volatility, list):
        raise DeribitRestError("ticker_or_volatility_shape_invalid")
    surface = _option_surface(options, option_summary)
    mark = _number(perpetual.get("mark_price"), "perpetual_mark_price", positive=True)
    index = _number(perpetual.get("index_price"), "perpetual_index_price", positive=True)
    future_mark = _number(future.get("mark_price"), "future_mark_price", positive=True)
    receive_mono = max(int(meta["local_receive_monotonic_ns"]) for meta in
                       (instruments_meta, option_summary_meta, perpetual_meta, future_meta, volatility_meta))
    row = {
        "schema": "polymarket_v7_deribit_rest_observation_v1",
        "source_id": "deribit_btc", "provider": "Deribit", "venue": "DERIBIT",
        "asset": "BTC", "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
        "perpetual": {"instrument_id": "BTC-PERPETUAL", "mark_price": mark, "index_price": index,
                      "current_funding": perpetual.get("current_funding"), "funding_8h": perpetual.get("funding_8h"),
                      "open_interest": perpetual.get("open_interest")},
        "nearest_future": {"instrument_id": future_name, "expiry_ms": next_future.get("expiration_timestamp"),
                           "mark_price": future_mark, "open_interest": future.get("open_interest"),
                           "basis_bps_to_perpetual": (future_mark / mark - 1.0) * 10_000.0},
        "option_surface": surface,
        "historical_volatility": historical_volatility[-1] if historical_volatility else None,
        "instrument_request": instruments_meta, "option_summary_request": option_summary_meta,
        "perpetual_request": perpetual_meta, "future_request": future_meta, "volatility_request": volatility_meta,
        "local_receive_monotonic_ns": receive_mono,
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "execution_authority": False, "capital_authority": False, "oms_authority": False,
        "ledger_writer_authority": False, "promotion_authority": False,
    }
    status = {"schema": "polymarket_v7_deribit_rest_status_v1", "state": "OPERATIONAL",
              "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
              "option_surface_points": len(surface), "nearest_future": future_name, "latest": row, "blocker": None}
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
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            row, status = collect_once()
            args.tape.parent.mkdir(parents=True, exist_ok=True)
            with args.tape.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        except (DeribitRestError, OSError, urllib.error.URLError) as exc:
            status = {"schema": "polymarket_v7_deribit_rest_status_v1", "state": "DEGRADED",
                      "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
                      "blocker": str(exc), "execution_authority": False}
        _write_json(args.status, status)
        if not args.loop:
            return 0 if status["state"] == "OPERATIONAL" else 2
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
