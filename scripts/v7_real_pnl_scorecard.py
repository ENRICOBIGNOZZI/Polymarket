#!/usr/bin/env python3
"""Fail-closed economic proof scorecard for independently verified real PnL.

PAPER, counterfactual, quoted-edge, and unsigned/reconciled-only reports are
rejected.  This module is read-only: it cannot trade, attest, or amend source
evidence.  It only turns an immutable terminal-unit tape into conservative
cluster-aware statistics for manual review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator


SCHEMA = "polymarket_v7_real_pnl_economic_scorecard_v1"
SAMPLE_KIND = "REAL_PNL_ECONOMIC_SAMPLE"
GENESIS_HASH = "0" * 64
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ATTESTATION_SCHEMA = "polymarket_v7_real_pnl_attestation_v1"
POLICY = {
    "minimum_event_clusters": 30,
    "minimum_regimes": 3,
    "minimum_capacity_tiers": 3,
    "confidence_z": 1.96,
    "maximum_drawdown_ratio": 0.15,
    "cost_stress_multipliers": (1.0, 1.5, 2.0),
}


class ScorecardError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _integer(value: Any, field: str, *, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (nonnegative and value < 0):
        raise ScorecardError(f"{field}:invalid")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScorecardError(f"{field}:invalid")
    return value.strip()


def validate_verified_report(report: Any) -> dict[str, Any]:
    """Require an attestation bound to the exact unsigned verifier report.

    The HMAC itself is an operator attestation and is deliberately not exposed
    to this read-only scorecard.  The scorecard can nevertheless independently
    reject a report whose contents, report hash, or attestation identity were
    spliced together after verification.
    """
    if (not isinstance(report, dict) or report.get("state") != "REAL_PNL_VERIFIED"
            or report.get("real_pnl_verified") is not True
            or not SHA_RE.fullmatch(str(report.get("model_sha")))
            or not SHA256_RE.fullmatch(str(report.get("report_sha256")))):
        raise ScorecardError("verified_report:real_pnl_verified_required")
    attestation = report.get("attestation")
    required = {"schema", "operator_id", "report_sha256", "model_sha", "journal_head_hash", "algorithm", "signature"}
    if not isinstance(attestation, dict) or set(attestation) != required:
        raise ScorecardError("verified_report:attestation_shape")
    if (attestation.get("schema") != ATTESTATION_SCHEMA
            or not _text(attestation.get("operator_id"), "attestation_operator")
            or attestation.get("report_sha256") != report["report_sha256"]
            or attestation.get("model_sha") != report["model_sha"]
            or not SHA256_RE.fullmatch(str(attestation.get("journal_head_hash")))
            or attestation.get("algorithm") != "HMAC-SHA256"
            or not SHA256_RE.fullmatch(str(attestation.get("signature")))):
        raise ScorecardError("verified_report:attestation_identity")
    unsigned = dict(report)
    unsigned.pop("attestation")
    unsigned.pop("report_sha256")
    unsigned["state"] = "REAL_PNL_RECONCILED_UNSIGNED"
    unsigned["real_pnl_verified"] = False
    if digest(unsigned) != report["report_sha256"]:
        raise ScorecardError("verified_report:report_hash_mismatch")
    return report


def sample_hash_payload(raw: dict[str, Any]) -> dict[str, Any]:
    value = dict(raw)
    value.pop("record_hash", None)
    return value


def seal_sample(raw: dict[str, Any], previous_record_hash: str) -> dict[str, Any]:
    value = dict(raw)
    value["record_kind"] = SAMPLE_KIND
    value["previous_record_hash"] = previous_record_hash
    value["record_hash"] = None
    validate_sample(value, sealed=False)
    value["record_hash"] = digest(sample_hash_payload(value))
    validate_sample(value)
    return value


def validate_sample(raw: Any, *, sealed: bool = True) -> None:
    required = {
        "record_kind", "model_sha", "report_sha256", "sample_id", "event_cluster", "regime",
        "terminal_ts_ms", "gross_pnl_units", "fee_units", "slippage_units", "reward_units",
        "capital_units", "capacity_tier", "previous_record_hash", "record_hash",
    }
    if not isinstance(raw, dict) or set(raw) != required or raw.get("record_kind") != SAMPLE_KIND:
        raise ScorecardError("sample:shape")
    if not isinstance(raw["model_sha"], str) or not SHA_RE.fullmatch(raw["model_sha"]):
        raise ScorecardError("sample:model_sha")
    for name in ("report_sha256", "previous_record_hash"):
        if not isinstance(raw[name], str) or not SHA256_RE.fullmatch(raw[name]):
            raise ScorecardError(f"sample:{name}")
    for name in ("sample_id", "event_cluster", "regime"):
        _text(raw[name], name)
    _integer(raw["terminal_ts_ms"], "terminal_ts_ms", nonnegative=True)
    if raw["terminal_ts_ms"] <= 0:
        raise ScorecardError("sample:terminal_ts_ms")
    for name in ("gross_pnl_units", "reward_units"):
        _integer(raw[name], name)
    for name in ("fee_units", "slippage_units", "capital_units"):
        _integer(raw[name], name, nonnegative=True)
    if raw["capital_units"] <= 0:
        raise ScorecardError("sample:capital_units")
    _integer(raw["capacity_tier"], "capacity_tier", nonnegative=True)
    if raw["capacity_tier"] <= 0:
        raise ScorecardError("sample:capacity_tier")
    if sealed:
        if not isinstance(raw["record_hash"], str) or not SHA256_RE.fullmatch(raw["record_hash"]):
            raise ScorecardError("sample:record_hash")
        if raw["record_hash"] != digest(sample_hash_payload(raw)):
            raise ScorecardError("sample:record_hash_mismatch")
    elif raw["record_hash"] is not None:
        raise ScorecardError("sample:unsealed_hash")


def iter_samples(path: Path, *, model_sha: str, report_sha256: str) -> Iterator[dict[str, Any]]:
    tip = GENESIS_HASH
    sample_ids: set[str] = set()
    previous_terminal_ts = 0
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                validate_sample(raw)
            except (json.JSONDecodeError, ScorecardError) as exc:
                raise ScorecardError(f"line_{line_number}:{exc}") from exc
            if raw["model_sha"] != model_sha or raw["report_sha256"] != report_sha256:
                raise ScorecardError(f"line_{line_number}:report_identity")
            if raw["previous_record_hash"] != tip:
                raise ScorecardError(f"line_{line_number}:chain_break")
            if raw["sample_id"] in sample_ids:
                raise ScorecardError(f"line_{line_number}:duplicate_sample_id")
            if raw["terminal_ts_ms"] < previous_terminal_ts:
                raise ScorecardError(f"line_{line_number}:terminal_time_regression")
            sample_ids.add(raw["sample_id"])
            previous_terminal_ts = raw["terminal_ts_ms"]
            tip = raw["record_hash"]
            yield raw


def _lcb(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "lower": None, "n": 0}
    center = sum(values) / len(values)
    if len(values) < 2:
        return {"mean": center, "lower": None, "n": len(values)}
    variance = sum((value - center) ** 2 for value in values) / (len(values) - 1)
    return {"mean": center, "lower": center - POLICY["confidence_z"] * math.sqrt(variance / len(values)), "n": len(values)}


def _drawdown_and_es(values: list[int], capitals: list[int]) -> dict[str, float | int | None]:
    equity = peak = 0
    max_drawdown = 0
    returns: list[float] = []
    for value, capital in zip(values, capitals):
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        returns.append(value / capital)
    loss_count = max(1, math.ceil(len(returns) * 0.05)) if returns else 0
    expected_shortfall = sum(sorted(returns)[:loss_count]) / loss_count if loss_count else None
    return {
        "maximum_drawdown_units": max_drawdown,
        "maximum_drawdown_ratio": max_drawdown / peak if peak > 0 else None,
        "expected_shortfall_95": expected_shortfall,
    }


def scorecard(verified_report: dict[str, Any], samples_path: Path) -> dict[str, Any]:
    verified_report = validate_verified_report(verified_report)
    model_sha = str(verified_report["model_sha"])
    report_sha = str(verified_report["report_sha256"])
    report_pnl = _integer(verified_report.get("reconstructed_realized_pnl_units"), "report_pnl")
    rows = list(iter_samples(samples_path, model_sha=model_sha, report_sha256=report_sha))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["event_cluster"])].append(row)
    cost_results: dict[str, dict[str, Any]] = {}
    base_values: list[int] = []
    for multiplier in POLICY["cost_stress_multipliers"]:
        cluster_values = []
        reward_free_values = []
        raw_values = []
        for cluster_rows in grouped.values():
            pnl = [int(row["gross_pnl_units"]) - multiplier * (int(row["fee_units"]) + int(row["slippage_units"]))
                   + int(row["reward_units"]) for row in cluster_rows]
            reward_free = [int(row["gross_pnl_units"]) - multiplier * (int(row["fee_units"]) + int(row["slippage_units"]))
                           for row in cluster_rows]
            cluster_values.append(sum(pnl) / len(pnl))
            reward_free_values.append(sum(reward_free) / len(reward_free))
            raw_values.extend(pnl)
        label = f"{multiplier:.1f}x"
        cost_results[label] = {
            "cluster_equal_weighted": _lcb(cluster_values),
            "reward_free_cluster_equal_weighted": _lcb(reward_free_values),
            "total_net_pnl_units": sum(raw_values),
        }
    base_values = [int(row["gross_pnl_units"]) - int(row["fee_units"]) - int(row["slippage_units"])
                   + int(row["reward_units"]) for row in rows]
    capacity: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        capacity[int(row["capacity_tier"])].append(row)
    capacity_curve = [{
        "tier": tier,
        "samples": len(tier_rows),
        "capital_units": sum(int(row["capital_units"]) for row in tier_rows),
        "net_pnl_units": sum(int(row["gross_pnl_units"]) - int(row["fee_units"]) - int(row["slippage_units"])
                             + int(row["reward_units"]) for row in tier_rows),
    } for tier, tier_rows in sorted(capacity.items())]
    for row in capacity_curve:
        row["net_return"] = row["net_pnl_units"] / row["capital_units"] if row["capital_units"] else None
    risk = _drawdown_and_es(base_values, [int(row["capital_units"]) for row in rows])
    reconciled_total = sum(base_values)
    lcb_2x = cost_results["2.0x"]["cluster_equal_weighted"]["lower"]
    reward_free_lcb_2x = cost_results["2.0x"]["reward_free_cluster_equal_weighted"]["lower"]
    reasons: list[str] = []
    if not rows:
        reasons.append("no_terminal_real_pnl_samples")
    if reconciled_total != report_pnl:
        reasons.append("terminal_sample_pnl_does_not_match_verified_report")
    if len(grouped) < POLICY["minimum_event_clusters"]:
        reasons.append("insufficient_event_clusters")
    if len({str(row["regime"]) for row in rows}) < POLICY["minimum_regimes"]:
        reasons.append("insufficient_forward_regimes")
    if len(capacity_curve) < POLICY["minimum_capacity_tiers"]:
        reasons.append("insufficient_capacity_tiers")
    if lcb_2x is None or lcb_2x <= 0:
        reasons.append("nonpositive_2x_cost_lcb")
    if reward_free_lcb_2x is None or reward_free_lcb_2x <= 0:
        reasons.append("nonpositive_reward_free_2x_cost_lcb")
    if risk["maximum_drawdown_ratio"] is None or risk["maximum_drawdown_ratio"] > POLICY["maximum_drawdown_ratio"]:
        reasons.append("drawdown_limit_not_met")
    return {
        "schema": SCHEMA,
        "model_sha": model_sha,
        "verified_report_sha256": report_sha,
        "samples_path": str(samples_path),
        "samples_sha256": file_sha256(samples_path),
        "samples": len(rows),
        "event_clusters": len(grouped),
        "regimes": sorted({str(row["regime"]) for row in rows}),
        "cost_stress": cost_results,
        "capacity_curve": capacity_curve,
        "risk": risk,
        "verified_report_pnl_units": report_pnl,
        "terminal_sample_pnl_units": reconciled_total,
        "policy": POLICY,
        "reason_codes": sorted(reasons),
        "state": "REAL_PNL_ECONOMIC_PROOF" if not reasons else "MORE_EVIDENCE_REQUIRED",
        "automatic_promotion": False,
        "world_class_candidate": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verified-report", type=Path, required=True)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.loads(args.verified_report.read_text(encoding="utf-8"))
    result = scorecard(report, args.samples)
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    main()
