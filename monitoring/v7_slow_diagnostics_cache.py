#!/usr/bin/env python3
"""Compute heavy PAPER monitoring diagnostics out-of-process.

The core exporter reads only the atomically published cache. This worker has no
execution authority and never mutates trading state.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from exporter_v7 import _fillability, _runtime_latency, _json

STOP = False
SCHEMA = "polymarket_v7_slow_monitoring_diagnostics_v1"


def _stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_runtime(run_root: Path) -> dict[str, Any]:
    runtime = _json(run_root / "control/runtime_status.json")
    if (
        runtime.get("paper_only") is not True
        or runtime.get("authenticated_execution") is not False
        or runtime.get("real_order_submission") is not False
        or runtime.get("real_capital_at_risk") is not False
    ):
        raise ValueError("unsafe runtime authority for monitoring diagnostics")
    sha = str(runtime.get("model_sha") or "")
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise ValueError("invalid runtime sha")
    return runtime


def compute(run_root: Path, repository_root: Path) -> dict[str, Any]:
    started = time.monotonic()
    now = int(time.time())
    runtime = _safe_runtime(run_root)
    sha = str(runtime["model_sha"])
    fillability = _fillability(run_root, repository_root, sha, now)
    latency = _runtime_latency(run_root)
    return {
        "schema": SCHEMA,
        "timestamp": now,
        "model_sha": sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": False,
        "maker_fillability": fillability,
        "maker_latency": latency,
        "refresh_duration_seconds": time.monotonic() - started,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    args = parser.parse_args()
    interval = max(10.0, float(args.interval_seconds))
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    errors = 0
    while not STOP:
        started = time.monotonic()
        try:
            value = compute(args.run_root.resolve(), args.repository_root.resolve())
            _atomic_json(args.output.resolve(), value)
            _atomic_json(args.status.resolve(), {
                "schema": "polymarket_v7_slow_monitoring_diagnostics_status_v1",
                "timestamp": int(time.time()),
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "state": "RUNNING",
                "last_error": "",
                "errors": errors,
                "last_refresh_duration_seconds": value["refresh_duration_seconds"],
            })
        except Exception as exc:
            errors += 1
            _atomic_json(args.status.resolve(), {
                "schema": "polymarket_v7_slow_monitoring_diagnostics_status_v1",
                "timestamp": int(time.time()),
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "state": "DEGRADED",
                "last_error": f"{type(exc).__name__}:{exc}",
                "errors": errors,
            })
        remaining = interval - (time.monotonic() - started)
        deadline = time.monotonic() + max(0.1, remaining)
        while not STOP and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
