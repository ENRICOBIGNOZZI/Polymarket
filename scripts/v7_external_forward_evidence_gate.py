#!/usr/bin/env python3
"""Fail-closed forward-evidence gate for the V7 external information fabric.

It reports engineering health separately from sufficient forward duration. The
gate owns no execution authority and cannot promote or enable a strategy.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_external_forward_evidence_status_v1"
EVENT_SOURCES = ("binance_spot", "bybit_spot", "bybit_linear", "deribit", "binance_usdm_market")
RAW_SOURCES = ("binance_spot", "bybit_spot", "bybit_linear", "deribit", "binance_usdm_depth", "binance_usdm_market")


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _tape_ok(tapes: Any, source: str) -> tuple[bool, str | None, int]:
    value = tapes.get(source) if isinstance(tapes, dict) else None
    if not isinstance(value, dict):
        return False, f"TAPE_MISSING:{source}", 0
    accepted = int(value.get("accepted") or 0)
    written = int(value.get("written") or 0)
    if value.get("enabled") is not True or value.get("evidence_valid") is not True or value.get("writer_healthy") is not True:
        return False, f"TAPE_UNHEALTHY:{source}", accepted
    # The recorder publishes independent atomic counters. During an active,
    # high-rate session the writer can have popped a record between the
    # `queued` and `written` observations, so exact equality at a random gate
    # instant is not a valid durability test. Evidence loss remains fail-closed
    # through the recorder's sticky `evidence_valid` flag and a nonzero drop;
    # terminal tape replay verifies a fully drained artifact separately.
    if accepted <= 0 or written > accepted or int(value.get("dropped") or 0) != 0:
        return False, f"TAPE_INCOMPLETE:{source}", accepted
    return True, None, accepted


def evaluate(runtime: dict[str, Any], coinbase_rest: dict[str, Any], deribit_rest: dict[str, Any],
             *, min_duration_s: float) -> dict[str, Any]:
    failures: list[str] = []
    observations: dict[str, int] = {}
    if runtime.get("schema") != "polymarket_v7_external_venue_runtime_v1":
        failures.append("RUNTIME_STATUS_INVALID")
    if runtime.get("paper_only") is not True or runtime.get("authenticated_execution") is not False or runtime.get("real_order_submission") is not False:
        failures.append("RUNTIME_SAFETY_INVALID")
    uptime_ns = int(runtime.get("uptime_ns") or 0)
    duration_s = uptime_ns / 1_000_000_000.0
    if runtime.get("valid") is not True:
        failures.append("RUNTIME_COMPOSITE_NOT_VALID")
    for source in EVENT_SOURCES:
        ok, failure, count = _tape_ok(runtime.get("normalized_event_tapes"), source)
        observations[f"normalized_events_{source}"] = count
        if not ok and failure:
            failures.append(failure)
    for source in RAW_SOURCES:
        ok, failure, count = _tape_ok(runtime.get("raw_frame_tapes"), source)
        observations[f"raw_frames_{source}"] = count
        if not ok and failure:
            failures.append(failure)
    for field in ("binance_spot_l2", "bybit_spot_l2", "bybit_linear_l2", "bybit_linear", "deribit", "binance_usdm"):
        value = runtime.get(field)
        if not isinstance(value, dict) or value.get("valid") is not True or int(value.get("parse_failures") or 0) != 0:
            failures.append(f"LIVE_SOURCE_UNHEALTHY:{field}")
    if coinbase_rest.get("state") != "OPERATIONAL_POLLING" or coinbase_rest.get("hft_trigger_eligible") is not False:
        failures.append("COINBASE_POLLING_FALLBACK_UNHEALTHY")
    if deribit_rest.get("state") != "OPERATIONAL" or deribit_rest.get("option_surface_valid") is not True:
        failures.append("DERIBIT_REST_SURFACE_UNHEALTHY")
    engineering_valid = not failures
    state = "ENGINEERING_VALIDATED" if engineering_valid and duration_s >= min_duration_s else "FORWARD_EVIDENCE_INSUFFICIENT"
    return {
        "schema": SCHEMA, "state": state,
        "engineering_valid": engineering_valid,
        "forward_evidence_sufficient": state == "ENGINEERING_VALIDATED",
        "runtime_duration_seconds": duration_s, "minimum_duration_seconds": min_duration_s,
        "failures": sorted(set(failures)), "observations": observations,
        "coinbase_realtime_l2_continuity": False,
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "execution_authority": False, "capital_authority": False, "oms_authority": False,
        "ledger_writer_authority": False, "promotion_authority": False,
        "generated_at_ns": time.time_ns(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-status", type=Path, required=True)
    parser.add_argument("--coinbase-rest-status", type=Path, required=True)
    parser.add_argument("--deribit-rest-status", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-duration-seconds", type=float, default=86_400.0)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    while True:
        result = evaluate(_load(args.runtime_status), _load(args.coinbase_rest_status), _load(args.deribit_rest_status),
                          min_duration_s=max(0.0, args.min_duration_seconds))
        _atomic_json(args.output, result)
        if not args.loop:
            return 0 if result["engineering_valid"] else 2
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
