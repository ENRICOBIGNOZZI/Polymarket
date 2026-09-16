#!/usr/bin/env python3
"""Zero-authority six-asset Chainlink TWAP observation hub over public RTDS."""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from v7_public_https_proxy import DEFAULT_DNS, PublicResolver
from v7_rtds_external_fair_monitor import (
    APPLICATION_HEARTBEAT_SECONDS, MAX_MESSAGE_BYTES, ORACLE_TOPIC,
    connect_websocket, observations, read_frame, send_frame, send_json,
)

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
SCHEMA = "polymarket_v7_multi_crypto_oracle_hub_v2"
STOP = False


def stop_handler(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("settlement registry must be an object")
    return value


def bindings_from_registry(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if value.get("paper_only") is not True or value.get("authenticated_execution") is not False \
            or value.get("real_order_submission") is not False:
        raise ValueError("settlement registry is not fail-closed PAPER")
    contexts = value.get("contexts")
    if not isinstance(contexts, list):
        raise ValueError("settlement registry contexts missing")
    by_asset: dict[str, list[dict[str, Any]]] = {asset: [] for asset in ASSETS}
    for row in contexts:
        if not isinstance(row, dict) or row.get("asset") not in by_asset \
                or row.get("horizon") not in {"M5", "M15"}:
            continue
        by_asset[str(row["asset"])].append(row)
    output: dict[str, dict[str, Any]] = {}
    for asset in ASSETS:
        rows = by_asset[asset]
        if {str(row.get("horizon") or "") for row in rows} != {"M5", "M15"}:
            raise ValueError(f"{asset}: verified M5/M15 settlement contexts required")
        semantics = []
        for row in rows:
            settlement = row.get("settlement") if isinstance(row.get("settlement"), dict) else {}
            semantics.append((
                settlement.get("oracle_source"), settlement.get("reference_pair"),
                settlement.get("stream_url"), settlement.get("settlement_window_seconds"),
                settlement.get("comparison_operator"), row.get("settlement_semantic_hash"),
            ))
            if row.get("settlement_mapping_verified") is not True:
                raise ValueError(f"{asset}: settlement mapping unverified")
        first = semantics[0]
        if any(item[:5] != first[:5] for item in semantics[1:]):
            raise ValueError(f"{asset}: M5/M15 oracle semantics disagree")
        source, pair, stream_url, window_seconds, comparator, _ = first
        if source != "CHAINLINK_DATA_STREAM" or int(window_seconds or 0) != 60 \
                or pair != f"{asset}/USD" or not str(stream_url or "").startswith("https://") \
                or comparator != "GREATER_THAN_OR_EQUAL":
            raise ValueError(f"{asset}: unsupported oracle semantics")
        output[asset] = {
            "asset": asset,
            "symbol": str(pair).lower(),
            "reference_pair": pair,
            "stream_url": stream_url,
            "window_seconds": 60,
            "comparison_operator": comparator,
            "settlement_semantic_hashes": sorted({str(item[5]) for item in semantics}),
        }
    return output


def empty_state(bindings: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {asset: {
        **binding,
        "valid": False,
        "version": 0,
        "price": None,
        "price_decimal": None,
        "source_timestamp_ms": 0,
        "receive_wall_ns": 0,
        "receive_monotonic_ns": 0,
        "duplicates": 0,
        "out_of_order": 0,
        "future_clock_rejections": 0,
    } for asset, binding in bindings.items()}


def parse_utc_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def load_contract_selection(value: dict[str, Any]) -> list[dict[str, Any]]:
    if value.get("schema") != "polymarket_v7_multi_crypto_book_selection_v1" \
            or value.get("paper_only") is not True \
            or value.get("authenticated_execution") is not False \
            or value.get("real_order_submission") is not False \
            or value.get("execution_authority") is not False:
        raise ValueError("oracle selection is not zero-authority multi-crypto selection")
    markets = value.get("markets")
    if not isinstance(markets, list):
        raise ValueError("oracle selection markets missing")
    rows: list[dict[str, Any]] = []
    for row in markets:
        if not isinstance(row, dict):
            continue
        asset = str(row.get("asset") or "")
        market_id = str(row.get("market_id") or "")
        start_ms = parse_utc_ms(row.get("start_timestamp"))
        end_ms = parse_utc_ms(row.get("end_timestamp"))
        if asset not in ASSETS or not market_id or start_ms <= 0 or end_ms <= start_ms:
            raise ValueError("invalid market in oracle selection")
        rows.append({
            "asset": asset,
            "horizon": str(row.get("horizon") or ""),
            "market_id": market_id,
            "start_timestamp_ms": start_ms,
            "end_timestamp_ms": end_ms,
            "normalized_rules_hash": str(row.get("normalized_rules_hash") or ""),
        })
    if not rows:
        raise ValueError("oracle selection has no markets")
    return rows


def update_references(
    contracts: list[dict[str, Any]], history: dict[str, dict[int, dict[str, Any]]],
    references: dict[str, dict[str, Any]], *, now_ms: int, maximum_gap_ms: int,
) -> None:
    live_ids = {row["market_id"] for row in contracts}
    for market_id in list(references):
        if market_id not in live_ids:
            references.pop(market_id, None)
    for contract in contracts:
        market_id = contract["market_id"]
        if references.get(market_id, {}).get("valid") is True:
            continue
        boundary = int(contract["start_timestamp_ms"])
        base = {
            "asset": contract["asset"],
            "horizon": contract["horizon"],
            "market_id": market_id,
            "boundary_timestamp_ms": boundary,
            "normalized_rules_hash": contract["normalized_rules_hash"],
            "valid": False,
            "price": None,
            "price_decimal": None,
            "source_timestamp_ms": 0,
            "gap_ms": None,
            "status": "AWAITING_BOUNDARY" if now_ms < boundary else "MISSING_REFERENCE",
        }
        if now_ms < boundary:
            references[market_id] = base
            continue
        candidates = [timestamp for timestamp in history.get(contract["asset"], {}) if timestamp <= boundary]
        if not candidates:
            references[market_id] = base
            continue
        timestamp = max(candidates)
        gap = boundary - timestamp
        if gap < 0 or gap > maximum_gap_ms:
            base["gap_ms"] = gap
            references[market_id] = base
            continue
        observation = history[contract["asset"]][timestamp]
        available = observation.get("available_wall_ns")
        if type(available) is not int or not 0 < available <= now_ms * 1_000_000:
            base["status"] = "MISSING_OR_FUTURE_RECEIVE_PROVENANCE"
            references[market_id] = base
            continue
        base.update({
            "valid": gap == 0,
            "is_proxy": gap != 0,
            "available_wall_ns": max(available, now_ms * 1_000_000),
            "observation_received_wall_ns": available,
            "source": "POLYMARKET_PUBLIC_RTDS_CHAINLINK",
            "price": observation["price"],
            "price_decimal": observation["price_decimal"],
            "source_timestamp_ms": timestamp,
            "gap_ms": gap,
            "status": "REFERENCE_CAPTURED" if gap == 0 else "PROXY_NOT_EXACT_BOUNDARY",
        })
        references[market_id] = base


def apply_observation(
    state: dict[str, dict[str, Any]], row: dict[str, Any], *,
    receive_wall_ns: int, receive_monotonic_ns: int,
) -> bool:
    if row.get("topic") != ORACLE_TOPIC or int(row.get("window_seconds") or 0) != 60:
        return False
    symbol = str(row.get("symbol") or "").lower()
    asset = next((name for name, value in state.items() if value["symbol"] == symbol), None)
    if asset is None:
        return False
    value = state[asset]
    timestamp_ms = int(row.get("timestamp_ms") or 0)
    price = float(row.get("price") or 0.0)
    decimal = str(row.get("price_decimal") or "")
    if timestamp_ms <= 0 or not math.isfinite(price) or price <= 0 or not decimal:
        return False
    receive_wall_ms = receive_wall_ns // 1_000_000
    if timestamp_ms > receive_wall_ms + 5_000:
        value["future_clock_rejections"] += 1
        return False
    previous = int(value["source_timestamp_ms"] or 0)
    if timestamp_ms < previous:
        value["out_of_order"] += 1
        return False
    if timestamp_ms == previous and decimal == value.get("price_decimal"):
        value["duplicates"] += 1
        value["receive_wall_ns"] = max(int(value["receive_wall_ns"]), receive_wall_ns)
        value["receive_monotonic_ns"] = max(int(value["receive_monotonic_ns"]), receive_monotonic_ns)
        return True
    value.update({
        "valid": True,
        "version": int(value["version"]) + 1,
        "price": price,
        "price_decimal": decimal,
        "source_timestamp_ms": timestamp_ms,
        "receive_wall_ns": receive_wall_ns,
        "receive_monotonic_ns": receive_monotonic_ns,
    })
    return True


def snapshot(
    state: dict[str, dict[str, Any]], *, model_sha: str,
    transport_by_asset: dict[str, dict[str, Any]], maximum_receive_age_ms: int,
    running: bool, references: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now_ns = time.time_ns()
    assets: dict[str, Any] = {}
    healthy = 0
    for asset, value in state.items():
        receive_ns = int(value.get("receive_wall_ns") or 0)
        receive_age_ms = (now_ns - receive_ns) / 1_000_000.0 if 0 < receive_ns <= now_ns else None
        fresh = bool(value.get("valid")) and receive_age_ms is not None \
            and 0 <= receive_age_ms <= maximum_receive_age_ms
        healthy += int(fresh)
        assets[asset] = {
            **value,
            "fresh": fresh,
            "receive_age_ms": receive_age_ms,
            "transport": dict(transport_by_asset.get(asset) or {}),
        }
    return {
        "schema": SCHEMA,
        "version": 1,
        "timestamp_ns": now_ns,
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": False,
        "research_only": True,
        "transport": "POLYMARKET_PUBLIC_RTDS_CHAINLINK",
        "oracle_topic": ORACLE_TOPIC,
        "state": "RUNNING" if running else "STOPPED",
        "reconnects": sum(int(row.get("reconnects") or 0) for row in transport_by_asset.values()),
        "gaps": sum(int(row.get("gaps") or 0) for row in transport_by_asset.values()),
        "maximum_receive_age_ms": maximum_receive_age_ms,
        "healthy_assets": healthy,
        "required_assets": len(ASSETS),
        "all_assets_fresh": healthy == len(ASSETS),
        "one_way_latency_identified": False,
        "assets": assets,
        "settlement_references": dict(sorted((references or {}).items())),
    }


def oracle_worker(
    asset: str, binding: dict[str, Any], state: dict[str, dict[str, Any]],
    transport: dict[str, dict[str, Any]], history: dict[str, dict[int, dict[str, Any]]],
    lock: threading.Lock, dns: list[str], silence_reconnect_seconds: float,
) -> None:
    resolver = PublicResolver(dns)
    while not STOP:
        stream = None
        with lock:
            status = transport[asset]
            status["connection_epoch"] = int(status["connection_epoch"]) + 1
            if status["connection_epoch"] > 1:
                status["reconnects"] = int(status["reconnects"]) + 1
                status["gaps"] = int(status["gaps"]) + 1
            epoch = int(status["connection_epoch"])
            status["connected"] = False
            status["last_error"] = "connecting"
        try:
            stream = connect_websocket(resolver)
            send_json(stream, {"action": "subscribe", "subscriptions": [{
                "topic": ORACLE_TOPIC,
                "type": "update",
                "filters": json.dumps({"symbol": binding["symbol"]}, separators=(",", ":")),
            }]})
            with lock:
                status = transport[asset]
                status["connected"] = True
                status["last_error"] = ""
            fragments = bytearray(); fragment_opcode = 0
            last_heartbeat = time.monotonic(); last_observation = last_heartbeat
            while not STOP:
                now = time.monotonic()
                if now - last_observation >= silence_reconnect_seconds:
                    raise TimeoutError(f"{asset} RTDS oracle silence exceeded reconnect threshold")
                if now - last_heartbeat >= APPLICATION_HEARTBEAT_SECONDS:
                    send_frame(stream, 0x1, b"PING"); last_heartbeat = now
                try:
                    final, opcode, payload = read_frame(stream)
                except socket.timeout:
                    continue
                if opcode == 0x8:
                    raise OSError(f"{asset} RTDS websocket closed")
                if opcode == 0x9:
                    send_frame(stream, 0xA, payload); continue
                if opcode == 0xA:
                    continue
                if opcode in {0x1, 0x2}:
                    fragments, fragment_opcode = bytearray(payload), opcode
                elif opcode == 0x0 and fragment_opcode:
                    fragments.extend(payload)
                else:
                    raise OSError(f"{asset} unexpected websocket opcode")
                if len(fragments) > MAX_MESSAGE_BYTES:
                    raise OSError(f"{asset} fragmented websocket message exceeds bound")
                if not final:
                    continue
                accepted = False
                if fragment_opcode == 0x1 and fragments:
                    decoded = json.loads(fragments.decode("utf-8"))
                    receive_wall_ns = time.time_ns()
                    receive_monotonic_ns = time.monotonic_ns()
                    for row in observations(decoded):
                        if str(row.get("symbol") or "").lower() != binding["symbol"]:
                            continue
                        with lock:
                            row_accepted = apply_observation(
                                state, row, receive_wall_ns=receive_wall_ns,
                                receive_monotonic_ns=receive_monotonic_ns)
                            accepted = row_accepted or accepted
                            if row_accepted:
                                timestamp_ms = int(row["timestamp_ms"])
                                previous_observation = history[asset].get(timestamp_ms)
                                if (previous_observation is None or
                                        previous_observation["price_decimal"] != str(row["price_decimal"])):
                                    history[asset][timestamp_ms] = {
                                        "price": float(row["price"]),
                                        "price_decimal": str(row["price_decimal"]),
                                        "available_wall_ns": receive_wall_ns,
                                    }
                                cutoff = timestamp_ms - 30 * 60 * 1000
                                for old_timestamp in [t for t in history[asset] if t < cutoff]:
                                    history[asset].pop(old_timestamp, None)
                                transport[asset]["observations_accepted"] = int(
                                    transport[asset]["observations_accepted"]
                                ) + 1
                    if accepted:
                        last_observation = time.monotonic()
                fragments, fragment_opcode = bytearray(), 0
        except Exception as exc:  # noqa: BLE001 - public source worker reconnects closed
            with lock:
                status = transport[asset]
                status["connected"] = False
                status["last_error"] = type(exc).__name__ + ":" + str(exc)
            if not STOP:
                deadline = time.monotonic() + 1.0
                while not STOP and time.monotonic() < deadline:
                    time.sleep(0.05)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    with lock:
        transport[asset]["connected"] = False


def run(args: argparse.Namespace) -> None:
    bindings = bindings_from_registry(load_json(args.settlement_registry))
    state = empty_state(bindings)
    history: dict[str, dict[int, dict[str, Any]]] = {asset: {} for asset in ASSETS}
    references: dict[str, dict[str, Any]] = {}
    contracts = load_contract_selection(load_json(args.selection)) if args.selection else []
    selection_mtime_ns = args.selection.stat().st_mtime_ns if args.selection else 0
    transport = {asset: {
        "connection_epoch": 0,
        "reconnects": 0,
        "gaps": 0,
        "connected": False,
        "last_error": "not_started",
        "observations_accepted": 0,
    } for asset in ASSETS}
    lock = threading.Lock()
    dns = args.dns or list(DEFAULT_DNS)
    threads = [threading.Thread(
        target=oracle_worker,
        args=(asset, bindings[asset], state, transport, history, lock, dns, args.silence_reconnect_seconds),
        name=f"oracle-{asset.lower()}", daemon=True,
    ) for asset in ASSETS]
    for thread in threads:
        thread.start()
    try:
        last_selection_check = 0.0
        selection_error = ""
        while not STOP:
            if args.selection and time.monotonic() - last_selection_check >= 1.0:
                last_selection_check = time.monotonic()
                try:
                    mtime_ns = args.selection.stat().st_mtime_ns
                    if mtime_ns != selection_mtime_ns:
                        contracts = load_contract_selection(load_json(args.selection))
                        selection_mtime_ns = mtime_ns
                    selection_error = ""
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    selection_error = type(exc).__name__ + ":" + str(exc)
            with lock:
                if contracts:
                    update_references(
                        contracts, history, references, now_ms=time.time_ns() // 1_000_000,
                        maximum_gap_ms=args.reference_max_gap_ms)
                value = snapshot(
                    state, model_sha=args.model_sha, transport_by_asset=transport,
                    maximum_receive_age_ms=args.maximum_receive_age_ms, running=True,
                    references=references)
                value["selection_error"] = selection_error
                value["selection_market_count"] = len(contracts)
            atomic_json(args.output, value)
            time.sleep(0.25)
    finally:
        for thread in threads:
            thread.join(timeout=2.5)
        with lock:
            value = snapshot(
                state, model_sha=args.model_sha, transport_by_asset=transport,
                maximum_receive_age_ms=args.maximum_receive_age_ms, running=False,
                references=references)
        atomic_json(args.output, value)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--settlement-registry", type=Path, default=Path("config/v7_crypto_settlement_markets.json"))
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--reference-max-gap-ms", type=int, default=2000)
    parser.add_argument("--maximum-receive-age-ms", type=int, default=3000)
    parser.add_argument("--silence-reconnect-seconds", type=float, default=10.0)
    parser.add_argument("--dns", action="append", default=[])
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise ValueError("exact 40-hex model SHA required")
    if not 250 <= args.maximum_receive_age_ms <= 60_000 or args.silence_reconnect_seconds < 3:
        raise ValueError("invalid oracle freshness/reconnect bounds")
    if not 100 <= args.reference_max_gap_ms <= 10_000:
        raise ValueError("invalid reference maximum gap")
    signal.signal(signal.SIGINT, stop_handler); signal.signal(signal.SIGTERM, stop_handler)
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
