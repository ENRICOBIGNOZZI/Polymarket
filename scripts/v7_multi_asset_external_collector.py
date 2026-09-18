#!/usr/bin/env python3
"""Bounded zero-authority external-market-data fanout for all live crypto assets."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
SCHEMA = "polymarket_v7_multi_asset_external_collector_v1"
CHILD_SCHEMA = "polymarket_v7_external_venue_runtime_v1"
STOP = False


def stop_handler(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def exact_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def canonical_symbols(registry: dict[str, Any]) -> dict[str, dict[str, str]]:
    if (
        registry.get("schema") != "polymarket_v7_crypto_settlement_market_registry_v1"
        or registry.get("paper_only") is not True
        or registry.get("authenticated_execution") is not False
        or registry.get("real_order_submission") is not False
    ):
        raise ValueError("unsafe market registry")
    rows = registry.get("contexts")
    if not isinstance(rows, list):
        raise ValueError("market registry contexts missing")
    output: dict[str, dict[str, str]] = {}
    for asset in ASSETS:
        candidates = []
        for row in rows:
            if not isinstance(row, dict) or row.get("enabled") is not True:
                continue
            if str(row.get("asset") or "") != asset:
                continue
            symbols = row.get("external_symbols")
            if not isinstance(symbols, dict):
                raise ValueError(f"{asset}: external symbols missing")
            normalized = {
                "binance_spot": str(symbols.get("binance_spot") or ""),
                "coinbase_spot": str(symbols.get("coinbase_spot") or ""),
                "bybit_spot": str(symbols.get("bybit_spot") or ""),
                "binance_perp": str(symbols.get("binance_perp") or ""),
                "bybit_perp": str(symbols.get("bybit_perp") or ""),
                "deribit": str(symbols.get("deribit") or ""),
            }
            candidates.append(normalized)
        if not candidates or any(row != candidates[0] for row in candidates[1:]):
            raise ValueError(f"{asset}: inconsistent external symbols across horizons")
        if not candidates[0]["binance_spot"]:
            raise ValueError(f"{asset}: Binance spot missing")
        secondary = sum(bool(candidates[0][key])
                        for key in ("coinbase_spot", "bybit_spot"))
        if secondary < 1:
            raise ValueError(f"{asset}: secondary spot venue missing")
        output[asset] = candidates[0]
    return output


@dataclass
class Child:
    asset: str
    process: subprocess.Popen[bytes]
    log_handle: Any
    status_path: Path


def child_paths(run_root: Path, asset: str) -> dict[str, Path]:
    base = run_root / "external_fair"
    if asset == "BTC":
        return {
            "base": base,
            "status": base / "external_venues.json",
            "tape": base / "tapes" / f"external_venues.{asset.lower()}.{os.getpid()}.bin",
            "raw": base / "raw",
            "normalized": base / "normalized_events",
            "log": base / "external_venues.log",
        }
    asset_root = base / "assets" / asset.lower()
    return {
        "base": asset_root,
        "status": asset_root / "external_venues.json",
        "tape": asset_root / "tapes" / f"external_venues.{asset.lower()}.{os.getpid()}.bin",
        "raw": asset_root / "raw",
        "normalized": asset_root / "normalized_events",
        "log": asset_root / "external_venues.log",
    }


def child_command(args: argparse.Namespace, asset: str,
                  symbols: dict[str, str]) -> tuple[list[str], dict[str, Path]]:
    paths = child_paths(args.run_root, asset)
    for key in ("base", "raw", "normalized"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["tape"].parent.mkdir(parents=True, exist_ok=True)

    command = [
        str(args.engine),
        "--output", str(paths["status"]),
        "--tape", str(paths["tape"]),
        "--raw-tape-dir", str(paths["raw"]),
        "--normalized-event-tape-dir", str(paths["normalized"]),
        "--disk-pressure-marker", str(args.disk_pressure_marker),
        "--disk-pressure-min-free-bytes", str(args.disk_pressure_min_free_bytes),
        "--model-sha", args.model_sha,
        "--asset", asset,
        "--binance-spot-symbol", symbols["binance_spot"] or "NONE",
        "--coinbase-spot-symbol", symbols["coinbase_spot"] or "NONE",
        "--bybit-spot-symbol", symbols["bybit_spot"] or "NONE",
        "--binance-usdm-symbol", symbols["binance_perp"] or "NONE",
        "--bybit-linear-symbol", symbols["bybit_perp"] or "NONE",
        "--deribit-symbol", symbols["deribit"] or "NONE",
        "--event-driven-ingress",
    ]
    if asset == "BTC":
        command.extend([
            "--external-cancel-signal", str(args.external_cancel_signal),
            "--external-cancel-rule-sha256", args.external_cancel_rule_sha256,
        ])
    return command, paths


def data_ready(status: dict[str, Any], *, asset: str, sha: str,
               now_ns: int) -> tuple[bool, str]:
    if (
        status.get("schema") != CHILD_SCHEMA
        or status.get("asset") != asset
        or status.get("code_sha") != sha
        or status.get("paper_only") is not True
        or status.get("authenticated_execution") is not False
        or status.get("real_order_submission") is not False
        or status.get("state") != "OPERATIONAL"
        or status.get("valid") is not True
    ):
        return False, "STATUS_CONTRACT"
    try:
        timestamp_ns = int(status.get("timestamp_ns") or 0)
    except (TypeError, ValueError, OverflowError):
        return False, "TIMESTAMP"
    if timestamp_ns <= 0 or now_ns - timestamp_ns > 15_000_000_000 or timestamp_ns > now_ns + 5_000_000_000:
        return False, "STALE"
    tapes = status.get("raw_frame_tapes")
    if not isinstance(tapes, dict):
        return False, "RAW_TAPES"
    primary = tapes.get("binance_spot")
    if not isinstance(primary, dict):
        return False, "BINANCE_TAPE"
    if (
        primary.get("enabled") is not True
        or primary.get("writer_healthy") is not True
        or primary.get("evidence_valid") is not True
        or int(primary.get("written") or 0) <= 0
        or int(primary.get("dropped") or 0) != 0
    ):
        return False, "BINANCE_TAPE_NOT_READY"
    secondary_ready = False
    for venue in ("coinbase_spot", "bybit_spot"):
        row = tapes.get(venue)
        if not isinstance(row, dict) or row.get("enabled") is not True:
            continue
        if (
            row.get("writer_healthy") is True
            and row.get("evidence_valid") is True
            and int(row.get("written") or 0) > 0
            and int(row.get("dropped") or 0) == 0
        ):
            secondary_ready = True
            break
    if not secondary_ready:
        return False, "SECONDARY_SPOT_TAPE_NOT_READY"
    return True, ""


def terminate(children: list[Child]) -> None:
    for child in children:
        if child.process.poll() is None:
            child.process.terminate()
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if all(child.process.poll() is not None for child in children):
            break
        time.sleep(0.05)
    for child in children:
        if child.process.poll() is None:
            child.process.kill()
    for child in children:
        try:
            child.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            child.log_handle.close()
        except Exception:
            pass


def write_status(path: Path, *, args: argparse.Namespace, children: list[Child],
                 state: str, blockers: dict[str, str]) -> None:
    now_ns = time.time_ns()
    rows = []
    ready = 0
    for child in children:
        value = load_json(child.status_path)
        is_ready, reason = data_ready(value, asset=child.asset,
                                      sha=args.model_sha, now_ns=now_ns)
        if is_ready:
            ready += 1
        rows.append({
            "asset": child.asset,
            "pid": child.process.pid,
            "alive": child.process.poll() is None,
            "returncode": child.process.poll(),
            "status_path": str(child.status_path),
            "data_ready": is_ready,
            "reason": blockers.get(child.asset) or reason,
            "timestamp_ns": value.get("timestamp_ns"),
            "fresh_venue_count": value.get("fresh_venue_count"),
        })
    atomic_json(path, {
        "schema": SCHEMA,
        "timestamp_ns": now_ns,
        "timestamp_ms": now_ns // 1_000_000,
        "model_sha": args.model_sha,
        "state": state,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": False,
        "asset_count": len(ASSETS),
        "ready_assets": ready,
        "missing_assets": [row["asset"] for row in rows if not row["data_ready"]],
        "assets": rows,
    })


def run(args: argparse.Namespace) -> int:
    registry = load_json(args.market_registry)
    symbols = canonical_symbols(registry)
    if args.validate_only:
        print(json.dumps({
            "schema": SCHEMA,
            "paper_only": True,
            "execution_authority": False,
            "asset_count": len(symbols),
            "assets": sorted(symbols),
        }, sort_keys=True))
        return 0

    args.run_root.mkdir(parents=True, exist_ok=True)
    status_path = args.run_root / "external_fair" / "all_assets_status.json"
    children: list[Child] = []
    try:
        for asset in ASSETS:
            command, paths = child_command(args, asset, symbols[asset])
            paths["log"].parent.mkdir(parents=True, exist_ok=True)
            handle = paths["log"].open("ab", buffering=0)
            process = subprocess.Popen(
                command, cwd=args.repository_root, stdout=handle,
                stderr=subprocess.STDOUT, env=os.environ.copy())
            children.append(Child(asset, process, handle, paths["status"]))

        started = time.monotonic()
        while not STOP:
            blockers: dict[str, str] = {}
            for child in children:
                rc = child.process.poll()
                if rc is not None:
                    blockers[child.asset] = f"CHILD_EXIT_{rc}"
            if blockers:
                write_status(status_path, args=args, children=children,
                             state="BLOCKED_CHILD_EXIT", blockers=blockers)
                return 70

            now_ns = time.time_ns()
            ready = 0
            for child in children:
                value = load_json(child.status_path)
                ok, reason = data_ready(value, asset=child.asset,
                                        sha=args.model_sha, now_ns=now_ns)
                if ok:
                    ready += 1
                elif reason:
                    blockers[child.asset] = reason
            state = "OPERATIONAL" if ready == len(ASSETS) else "WARMING"
            write_status(status_path, args=args, children=children,
                         state=state, blockers=blockers)
            if state != "OPERATIONAL" and time.monotonic() - started > args.startup_timeout_seconds:
                return 77
            time.sleep(1.0)
        return 0
    finally:
        terminate(children)
        if children:
            write_status(status_path, args=args, children=children,
                         state="STOPPED", blockers={})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--market-registry", type=Path, required=True)
    parser.add_argument("--disk-pressure-marker", type=Path, required=True)
    parser.add_argument("--disk-pressure-min-free-bytes", type=int, required=True)
    parser.add_argument("--external-cancel-signal", type=Path, required=True)
    parser.add_argument("--external-cancel-rule-sha256", required=True)
    parser.add_argument("--startup-timeout-seconds", type=int, default=60)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if not exact_sha(args.model_sha):
        parser.error("--model-sha must be exact lowercase 40-hex SHA")
    if args.disk_pressure_min_free_bytes <= 0:
        parser.error("disk pressure threshold must be positive")
    if len(args.external_cancel_rule_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in args.external_cancel_rule_sha256):
        parser.error("external cancel rule SHA must be exact lowercase 64-hex")
    if not 5 <= args.startup_timeout_seconds <= 300:
        parser.error("startup timeout must be in [5,300]")
    return args


def main() -> int:
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
