#!/usr/bin/env python3
"""Validate and rank redacted, read-only regional latency probe reports."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SCHEMA = "polymarket_v7_regional_latency_probe_v1"
REPORT_SCHEMA = "polymarket_v7_regional_shootout_report_v1"
PERCENTILES = ("p50", "p90", "p95", "p99", "p99_9", "max")
TIMINGS = ("dns", "tcp_connect", "tls_connect", "first_byte", "total")


class RegionalShootoutError(ValueError):
    pass


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RegionalShootoutError(f"{field}:invalid")
    return value


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegionalShootoutError(f"probe:unreadable:{path}") from exc
    if not isinstance(value, dict):
        raise RegionalShootoutError("probe:not_object")
    return value


def validate_probe(value: Any, *, policy: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "endpoint", "region", "exact_code_sha", "started_wall_ms", "finished_wall_ms", "samples",
                "warmup", "successful_samples", "failed_samples", "warmup_failed_samples", "primary_ip",
                "connection_reused_samples", "new_connections", "reconnect_count", "timings_ns", "paper_only",
                "authenticated_execution", "real_order_submission", "measures_order_or_cancel_ack"}
    if not isinstance(value, dict) or set(value) != required:
        raise RegionalShootoutError("probe:shape")
    if (value["schema"] != SCHEMA or value["endpoint"] != policy["public_probe_endpoint"]
            or value["region"] not in policy["candidate_regions"] or not SHA_RE.fullmatch(str(value["exact_code_sha"]))
            or value["paper_only"] is not True or value["authenticated_execution"] is not False
            or value["real_order_submission"] is not False or value["measures_order_or_cancel_ack"] is not False):
        raise RegionalShootoutError("probe:identity")
    for field in ("started_wall_ms", "finished_wall_ms", "samples", "warmup", "successful_samples", "failed_samples",
                  "warmup_failed_samples", "connection_reused_samples", "new_connections", "reconnect_count"):
        _integer(value[field], f"probe:{field}")
    if (value["finished_wall_ms"] < value["started_wall_ms"] or value["samples"] == 0
            or value["successful_samples"] == 0 or value["successful_samples"] + value["failed_samples"] != value["samples"]
            or value["connection_reused_samples"] > value["successful_samples"]
            or value["reconnect_count"] > value["new_connections"]):
        raise RegionalShootoutError("probe:counts")
    if not isinstance(value["primary_ip"], str):
        raise RegionalShootoutError("probe:primary_ip")
    timings = value["timings_ns"]
    if not isinstance(timings, dict) or set(timings) != set(TIMINGS):
        raise RegionalShootoutError("probe:timings")
    for stage in TIMINGS:
        distribution = timings[stage]
        if not isinstance(distribution, dict) or set(distribution) != set(PERCENTILES):
            raise RegionalShootoutError("probe:timings")
        values = [_integer(distribution[item], f"probe:{stage}:{item}") for item in PERCENTILES]
        if values != sorted(values):
            raise RegionalShootoutError("probe:percentile_order")
    return value


def assess(policy: Any, probes: list[Any]) -> dict[str, Any]:
    if not isinstance(policy, dict) or policy.get("schema") != "polymarket_v7_latency_slo_v1":
        raise RegionalShootoutError("policy:shape")
    shootout = policy.get("regional_shootout")
    required_policy = {"duration_hours", "minimum_duration_seconds", "minimum_successful_samples", "maximum_failure_rate",
                       "maximum_reconnect_rate", "candidate_regions", "public_probe_endpoint", "same_binary_config_and_sha_required",
                       "winner_selected_by", "authenticated_order_cancel_probe"}
    if not isinstance(shootout, dict) or set(shootout) != required_policy or shootout["same_binary_config_and_sha_required"] is not True:
        raise RegionalShootoutError("policy:regional_shootout")
    for field in ("minimum_duration_seconds", "minimum_successful_samples"):
        if _integer(shootout[field], f"policy:{field}") <= 0:
            raise RegionalShootoutError("policy:regional_shootout")
    for field in ("maximum_failure_rate", "maximum_reconnect_rate"):
        if isinstance(shootout[field], bool) or not isinstance(shootout[field], (int, float)) or not 0 <= shootout[field] <= 1:
            raise RegionalShootoutError("policy:regional_shootout")
    if not isinstance(probes, list):
        raise RegionalShootoutError("probes:invalid")
    validated = [validate_probe(value, policy=shootout) for value in probes]
    regions = [value["region"] for value in validated]
    if len(set(regions)) != len(regions):
        raise RegionalShootoutError("probes:duplicate_region")
    shas = {value["exact_code_sha"] for value in validated}
    rows = []
    for value in validated:
        duration_seconds = (value["finished_wall_ms"] - value["started_wall_ms"]) / 1000.0
        failure_rate = value["failed_samples"] / value["samples"]
        reconnect_rate = value["reconnect_count"] / value["successful_samples"]
        reasons = []
        if duration_seconds < shootout["minimum_duration_seconds"]:
            reasons.append("duration_below_required_window")
        if value["successful_samples"] < shootout["minimum_successful_samples"]:
            reasons.append("sample_count_below_minimum")
        if failure_rate > shootout["maximum_failure_rate"]:
            reasons.append("failure_rate_exceeds_limit")
        if reconnect_rate > shootout["maximum_reconnect_rate"]:
            reasons.append("reconnect_rate_exceeds_limit")
        rows.append({"region": value["region"], "exact_code_sha": value["exact_code_sha"], "duration_seconds": duration_seconds,
                     "successful_samples": value["successful_samples"], "failure_rate": failure_rate,
                     "reconnect_rate": reconnect_rate, "total_p99_ns": value["timings_ns"]["total"]["p99"],
                     "total_p99_9_ns": value["timings_ns"]["total"]["p99_9"], "eligible": not reasons,
                     "reason_codes": reasons})
    supplied = {row["region"] for row in rows}
    missing = sorted(set(shootout["candidate_regions"]) - supplied)
    global_reasons: list[str] = []
    if missing:
        global_reasons.append("candidate_regions_missing")
    if len(shas) != 1:
        global_reasons.append("exact_code_sha_mismatch")
    eligible = [row for row in rows if row["eligible"]]
    winner = None
    if not global_reasons and len(eligible) == len(shootout["candidate_regions"]):
        winner = min(eligible, key=lambda row: (row["total_p99_9_ns"], row["total_p99_ns"], row["failure_rate"], row["reconnect_rate"], row["region"]))["region"]
    else:
        global_reasons.append("complete_healthy_shootout_required")
    return {"schema": REPORT_SCHEMA, "state": "REGIONAL_SHOOTOUT_READY_FOR_MANUAL_SELECTION" if winner else "MORE_EVIDENCE_REQUIRED",
            "live_execution_authorized": False, "network_or_clob_proven": False, "candidate_regions": shootout["candidate_regions"],
            "missing_regions": missing, "regional_results": sorted(rows, key=lambda row: row["region"]),
            "selected_region": winner, "selection_method": shootout["winner_selected_by"], "reason_codes": global_reasons}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--probe", action="append", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = assess(_load(args.policy), [_load(path) for path in args.probe])
        print(json.dumps(report, sort_keys=True))
        return 0 if report["selected_region"] else 1
    except RegionalShootoutError as exc:
        print(f"v7_regional_shootout: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
