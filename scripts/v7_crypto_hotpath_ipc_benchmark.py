#!/usr/bin/env python3
"""Synthetic Unix-socket wakeup/round-trip benchmark; no exchange or order I/O."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import tempfile
import threading
import time

from v7_fast_forward_ipc import FastForwardIpcBridge, request


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * (len(ordered) - 1)))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=2000)
    args = parser.parse_args()
    if args.samples < 16 or args.samples > 100000:
        raise SystemExit("samples must be in [16,100000]")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "hotpath.sock"
        latencies: list[float] = []
        stop = threading.Event()
        with FastForwardIpcBridge(path, capacity=128) as bridge:
            def owner() -> None:
                while not stop.is_set() or bridge.snapshot()["queued"]:
                    bridge.wait(.1)
                    bridge.drain(lambda value: {
                        "paper_only": True, "authenticated_execution": False,
                        "real_order_submission": False, "new_risk_authorized": False,
                        "action": "NOTHING", "sequence": value.get("sequence"),
                    })
            thread = threading.Thread(target=owner, name="benchmark-owner")
            thread.start()
            for sequence in range(args.samples):
                started = time.perf_counter_ns()
                reply = request(path, {"sequence": sequence}, timeout_seconds=1.0)
                ended = time.perf_counter_ns()
                if reply.get("sequence") != sequence:
                    raise RuntimeError("reply sequence mismatch")
                latencies.append((ended - started) / 1000.0)
            stop.set(); bridge._ready.set(); thread.join(2.0)
            if thread.is_alive():
                raise RuntimeError("owner thread did not stop")
            snapshot = bridge.snapshot()
    result = {
        "schema": "polymarket_v7_crypto_hotpath_ipc_benchmark_v1",
        "scope": "SYNTHETIC_LOCAL_UNIX_SOCKET_ROUND_TRIP_ONLY",
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "samples": len(latencies),
        "p50_us": percentile(latencies, .50), "p95_us": percentile(latencies, .95),
        "p99_us": percentile(latencies, .99), "max_us": max(latencies),
        "mean_us": statistics.fmean(latencies), "bridge": snapshot,
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
