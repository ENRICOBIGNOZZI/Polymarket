#!/usr/bin/env python3
"""Fail-closed evaluator for read-only regional latency probes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_regional_shootout_v1"
PROBE_SCHEMA = "polymarket_v7_regional_latency_probe_v1"
PERCENTILES = ("p50", "p90", "p95", "p99", "p99_9", "max")
TIMING_SERIES = ("dns", "tcp_connect", "tls_connect", "first_byte", "total")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON object required")
    return value


def nonnegative_integer(value: Any) -> int:
    # JSON booleans and fractional floats are not counters or nanoseconds.
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("nonnegative signed-64-bit integer required")
    return value


def safe_quantile(value: dict[str, Any], name: str) -> int | None:
    try:
        return nonnegative_integer(value[name])
    except (KeyError, ValueError):
        return None


def monotone_distribution(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        points = [nonnegative_integer(value[name]) for name in PERCENTILES]
    except (KeyError, TypeError, ValueError):
        return False
    return all(point >= 0 for point in points) and points == sorted(points)


def evaluate_probe(
    probe: dict[str, Any], *, endpoint: str, min_duration_s: int,
    min_samples: int, max_failure_rate: float, max_reconnect_rate: float,
) -> dict[str, Any]:
    reasons: list[str] = []
    if probe.get("schema") != PROBE_SCHEMA:
        reasons.append("SCHEMA_MISMATCH")
    if probe.get("endpoint") != endpoint:
        reasons.append("ENDPOINT_MISMATCH")
    if probe.get("paper_only") is not True:
        reasons.append("NOT_PAPER_ONLY")
    if probe.get("authenticated_execution") is not False:
        reasons.append("AUTHENTICATED_EXECUTION_PRESENT")
    if probe.get("real_order_submission") is not False:
        reasons.append("REAL_ORDER_SUBMISSION_PRESENT")
    if probe.get("measures_order_or_cancel_ack") is not False:
        reasons.append("CLAIM_BOUNDARY_INVALID")

    try:
        started = nonnegative_integer(probe["started_wall_ms"])
        finished = nonnegative_integer(probe["finished_wall_ms"])
        requested = nonnegative_integer(probe["samples"])
        successful = nonnegative_integer(probe["successful_samples"])
        failed = nonnegative_integer(probe["failed_samples"])
        reconnects = nonnegative_integer(probe["reconnect_count"])
    except (KeyError, TypeError, ValueError):
        reasons.append("COUNTERS_MALFORMED")
        started = finished = requested = successful = failed = reconnects = 0

    duration_s = max(0.0, (finished - started) / 1000.0)
    duration_clock = "LEGACY_WALL_CLOCK_UNVERIFIED"
    if "measured_elapsed_monotonic_ns" in probe:
        try:
            measured_ns = nonnegative_integer(probe["measured_elapsed_monotonic_ns"])
            duration_s = measured_ns / 1_000_000_000.0
            duration_clock = "SAME_PROCESS_MONOTONIC"
            if abs(duration_s - (finished - started) / 1000.0) > 1.0:
                reasons.append("WALL_CLOCK_STEP_OR_DURATION_MISMATCH")
        except ValueError:
            reasons.append("MONOTONIC_DURATION_MALFORMED")
    denominator = successful + failed
    failure_rate = failed / denominator if denominator > 0 else 1.0
    reconnect_rate = reconnects / successful if successful > 0 else 1.0
    if duration_s < min_duration_s:
        reasons.append("DURATION_TOO_SHORT")
    if successful < min_samples:
        reasons.append("INSUFFICIENT_SUCCESSFUL_SAMPLES")
    if requested != successful + failed or requested <= 0 or finished < started:
        reasons.append("COUNTERS_INCONSISTENT")
    if failure_rate > max_failure_rate:
        reasons.append("FAILURE_RATE_TOO_HIGH")
    if reconnect_rate > max_reconnect_rate:
        reasons.append("RECONNECT_RATE_TOO_HIGH")

    timings = probe.get("timings_ns")
    if not isinstance(timings, dict):
        reasons.append("TIMINGS_MISSING")
        timings = {}
    for series in TIMING_SERIES:
        if not monotone_distribution(timings.get(series)):
            reasons.append(f"MALFORMED_PERCENTILES:{series}")

    total = timings.get("total") if isinstance(timings.get("total"), dict) else {}
    return {
        "region": str(probe.get("region") or ""),
        "exact_code_sha": str(probe.get("exact_code_sha") or ""),
        "duration_seconds": duration_s,
        "duration_clock": duration_clock,
        "successful_samples": successful,
        "failed_samples": failed,
        "failure_rate": failure_rate,
        "reconnect_count": reconnects,
        "reconnect_rate": reconnect_rate,
        "total_p99_ns": safe_quantile(total, "p99"),
        "total_p99_9_ns": safe_quantile(total, "p99_9"),
        "total_p50_ns": safe_quantile(total, "p50"),
        "healthy": not reasons,
        "reasons": sorted(set(reasons)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--probe", type=Path, action="append", required=True)
    parser.add_argument(
        "--candidate-region", action="append", default=[],
        help="Override policy candidate set; repeat for an AZ shootout.",
    )
    args = parser.parse_args()

    policy = load_json(args.policy)
    shootout = policy.get("regional_shootout")
    safety = policy.get("safety")
    if not isinstance(shootout, dict) or not isinstance(safety, dict):
        raise ValueError("latency policy missing regional_shootout/safety")
    if safety.get("paper_only") is not True or safety.get("authenticated_execution") is not False \
            or safety.get("real_order_submission") is not False \
            or safety.get("automatic_cutover") is not False:
        raise ValueError("latency policy is not fail-closed PAPER")

    expected = list(args.candidate_region) or [str(x) for x in shootout.get("candidate_regions", [])]
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("candidate regions must be non-empty and unique")

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    sha_set: set[str] = set()
    for path in args.probe:
        probe = load_json(path)
        result = evaluate_probe(
            probe,
            endpoint=str(shootout["public_probe_endpoint"]),
            min_duration_s=int(shootout["minimum_duration_seconds"]),
            min_samples=int(shootout["minimum_successful_samples"]),
            max_failure_rate=float(shootout["maximum_failure_rate"]),
            max_reconnect_rate=float(shootout["maximum_reconnect_rate"]),
        )
        region = result["region"]
        if not region:
            result["healthy"] = False
            result["reasons"] = sorted(set(result["reasons"] + ["REGION_MISSING"]))
        elif region in seen:
            result["healthy"] = False
            result["reasons"] = sorted(set(result["reasons"] + ["DUPLICATE_REGION"]))
        seen.add(region)
        sha = result["exact_code_sha"]
        if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
            result["healthy"] = False
            result["reasons"] = sorted(set(result["reasons"] + ["INVALID_EXACT_SHA"]))
        else:
            sha_set.add(sha)
        results.append(result)

    global_reasons: list[str] = []
    missing = sorted(set(expected) - seen)
    unexpected = sorted(seen - set(expected))
    if missing:
        global_reasons.append("MISSING_CANDIDATE_REGIONS")
    if unexpected:
        global_reasons.append("UNEXPECTED_REGIONS")
    if len(sha_set) != 1:
        global_reasons.append("MIXED_OR_MISSING_SHA")
    if any(not row["healthy"] for row in results):
        global_reasons.append("UNHEALTHY_PROBE")

    ranking = sorted(
        (row for row in results if row["healthy"] and row["region"] in set(expected)),
        key=lambda row: (
            row["total_p99_9_ns"], row["total_p99_ns"], row["failure_rate"],
            row["reconnect_rate"], row["total_p50_ns"], row["region"],
        ),
    )
    passed = not global_reasons and len(ranking) == len(expected)
    output = {
        "schema": SCHEMA,
        "passed": passed,
        "exact_code_sha": next(iter(sha_set)) if len(sha_set) == 1 else None,
        "expected_regions": expected,
        "missing_regions": missing,
        "unexpected_regions": unexpected,
        "global_reasons": sorted(set(global_reasons)),
        "ranking": [row["region"] for row in ranking] if passed else [],
        "selected_region": ranking[0]["region"] if passed else None,
        "results": sorted(results, key=lambda row: row["region"]),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "automatic_cutover": False,
        "network_probe_only": True,
        "selection_scope": "PUBLIC_HTTPS_PROBE_ONLY",
        "end_to_end_region_selection_ready": False,
        "authenticated_order_latency_observed": False,
        "host_hardware_and_load_parity_verified": False,
        "authorizes_live_execution": False,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
