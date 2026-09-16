#!/usr/bin/env python3
"""Read-only regional WS freshness/jitter report from existing V7 status surfaces."""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

PERCENTILES = (0.50, 0.90, 0.95, 0.99, 0.999)
DEFAULT_VENUES = ("BINANCE_SPOT", "COINBASE_SPOT")


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: object required")
    return value


def exact_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)


def distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "p90": None, "p95": None,
                "p99": None, "p99_9": None, "max": None}
    ordered = sorted(values)
    def q(p: float) -> float:
        index = int(p * (len(ordered) - 1))
        return ordered[index]
    return {
        "count": len(ordered), "p50": q(.50), "p90": q(.90),
        "p95": q(.95), "p99": q(.99), "p99_9": q(.999), "max": ordered[-1],
    }


def require_safety(value: dict[str, Any], sha: str, field: str) -> None:
    if value.get(field) != sha:
        raise ValueError(f"{field} mismatch")
    if value.get("paper_only") is not True:
        raise ValueError("paper_only required")
    if value.get("authenticated_execution") is not False:
        raise ValueError("authenticated execution present")
    if value.get("real_order_submission") is not False:
        raise ValueError("real order submission present")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-status", type=Path, required=True)
    parser.add_argument("--pm-status", type=Path, required=True)
    parser.add_argument("--exact-code-sha", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    parser.add_argument("--interval-ms", type=int, default=25)
    parser.add_argument("--venue", action="append", default=[])
    args = parser.parse_args()
    if not exact_sha(args.exact_code_sha):
        raise ValueError("exact lowercase SHA required")
    if args.duration_seconds <= 0 or args.interval_ms <= 0:
        raise ValueError("positive duration and interval required")
    venues = tuple(args.venue) or DEFAULT_VENUES
    if len(set(venues)) != len(venues):
        raise ValueError("duplicate venue")

    venue_stale: dict[str, list[float]] = {venue: [] for venue in venues}
    venue_receive: dict[str, list[int]] = {venue: [] for venue in venues}
    venue_first: dict[str, dict[str, int]] = {}
    venue_last: dict[str, dict[str, int]] = {}
    pm_stale: list[float] = []
    pm_receive: list[int] = []
    pm_first: dict[str, int] | None = None
    pm_last: dict[str, int] | None = None
    samples = 0
    deadline = time.monotonic() + args.duration_seconds
    started_ns = time.time_ns()

    while True:
        now_ns = time.time_ns()
        external = load(args.external_status)
        pm = load(args.pm_status)
        require_safety(external, args.exact_code_sha, "code_sha")
        require_safety(pm, args.exact_code_sha, "model_sha")
        rows = external.get("venues")
        if not isinstance(rows, list):
            raise ValueError("external venues missing")
        by_name = {str(row.get("venue")): row for row in rows if isinstance(row, dict)}
        for venue in venues:
            row = by_name.get(venue)
            if not isinstance(row, dict):
                raise ValueError(f"missing venue {venue}")
            receive_ns = int(row.get("last_receive_wall_ns") or 0)
            if receive_ns <= 0 or receive_ns > now_ns:
                raise ValueError(f"invalid receive clock {venue}")
            venue_stale[venue].append((now_ns - receive_ns) / 1e6)
            if not venue_receive[venue] or venue_receive[venue][-1] != receive_ns:
                venue_receive[venue].append(receive_ns)
            counters = {
                "frames": int(row.get("frames_received") or 0),
                "reconnects": max(0, int(row.get("connection_attempts") or 0) - 1),
                "transport_failures": int(row.get("transport_failures") or 0),
                "decode_failures": int(row.get("decode_failures") or 0),
            }
            venue_first.setdefault(venue, counters)
            venue_last[venue] = counters

        pm_receive_ms = int(pm.get("book_watermark_receive_wall_ms") or pm.get("last_receive_wall_ms") or 0)
        if pm_receive_ms <= 0 or pm_receive_ms * 1_000_000 > now_ns:
            raise ValueError("invalid PM receive clock")
        pm_stale.append((now_ns / 1e6) - pm_receive_ms)
        if not pm_receive or pm_receive[-1] != pm_receive_ms:
            pm_receive.append(pm_receive_ms)
        counters = {
            "messages": int(pm.get("feed_messages") or 0),
            "reconnects": int(pm.get("feed_reconnects") or pm.get("reconnects") or 0),
            "errors": int(pm.get("feed_errors") or 0),
            "decoder_failures": int(pm.get("decoder_failures") or 0),
        }
        if pm_first is None:
            pm_first = counters
        pm_last = counters
        samples += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(args.interval_ms / 1000.0, remaining))

    finished_ns = time.time_ns()
    elapsed_s = max((finished_ns - started_ns) / 1e9, 1e-9)
    sources: dict[str, Any] = {}
    for venue in venues:
        gaps = [(b - a) / 1e6 for a, b in zip(venue_receive[venue], venue_receive[venue][1:]) if b >= a]
        first, last = venue_first[venue], venue_last[venue]
        sources[venue] = {
            "staleness_ms": distribution(venue_stale[venue]),
            "observed_receive_gap_ms": distribution(gaps),
            "frames_delta": last["frames"] - first["frames"],
            "frames_per_second": max(0, last["frames"] - first["frames"]) / elapsed_s,
            "reconnects_delta": last["reconnects"] - first["reconnects"],
            "transport_failures_delta": last["transport_failures"] - first["transport_failures"],
            "decode_failures_delta": last["decode_failures"] - first["decode_failures"],
        }
    assert pm_first is not None and pm_last is not None
    pm_gaps = [float(b - a) for a, b in zip(pm_receive, pm_receive[1:]) if b >= a]
    sources["POLYMARKET_BOOK_WS"] = {
        "staleness_ms": distribution(pm_stale),
        "observed_receive_gap_ms": distribution(pm_gaps),
        "messages_delta": pm_last["messages"] - pm_first["messages"],
        "messages_per_second": max(0, pm_last["messages"] - pm_first["messages"]) / elapsed_s,
        "reconnects_delta": pm_last["reconnects"] - pm_first["reconnects"],
        "errors_delta": pm_last["errors"] - pm_first["errors"],
        "decoder_failures_delta": pm_last["decoder_failures"] - pm_first["decoder_failures"],
    }
    output = {
        "schema": "polymarket_v7_regional_ws_health_v1",
        "region": args.region,
        "exact_code_sha": args.exact_code_sha,
        "started_wall_ns": started_ns,
        "finished_wall_ns": finished_ns,
        "duration_seconds": elapsed_s,
        "samples": samples,
        "sources": sources,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "not_one_way_latency": True,
        "clock_claim": "same-host receive staleness and observed inter-arrival only",
        "authorizes_cutover": False,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
