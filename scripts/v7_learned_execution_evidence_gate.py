#!/usr/bin/env python3
"""Fail-closed statistical evidence gate for V7 learned execution.

The gate grades OOS execution-model evidence only. It never authorizes capital,
orders, champion promotion or authenticated execution; economic realized-PnL
promotion remains a separate V7 contract.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

POLICY_SCHEMA = "polymarket_v7_learned_execution_validation_policy_v1"
REPORT_SCHEMA = "polymarket_v7_learned_execution_walkforward_v1"
OUTPUT_SCHEMA = "polymarket_v7_learned_execution_evidence_gate_v1"


class EvidenceGateError(ValueError):
    pass


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceGateError(f"json_unreadable:{path}") from exc
    if not isinstance(value, dict):
        raise EvidenceGateError(f"json_not_object:{path}")
    return value


def validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema") != POLICY_SCHEMA:
        raise EvidenceGateError("policy_schema_mismatch")
    if policy.get("paper_only") is not True or policy.get("promotion_allowed") is not False:
        raise EvidenceGateError("unsafe_policy")
    minimums = policy.get("minimums")
    metrics = policy.get("metrics")
    if not isinstance(minimums, dict) or not isinstance(metrics, dict):
        raise EvidenceGateError("policy_shape_invalid")
    for key in ("scored_folds", "bootstrap_days"):
        value = minimums.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EvidenceGateError(f"policy_minimum_invalid:{key}")
    for key in ("positive_fold_fraction", "max_p_nonpositive"):
        value = _number(minimums.get(key))
        if value is None or not 0.0 <= value <= 1.0:
            raise EvidenceGateError(f"policy_minimum_invalid:{key}")
    for key in ("worst_improvement", "bootstrap_ci_lower"):
        if _number(minimums.get(key)) is None:
            raise EvidenceGateError(f"policy_minimum_invalid:{key}")
    for name in ("fill", "completion", "markout", "joint"):
        spec = metrics.get(name)
        if not isinstance(spec, dict) or not all(
            isinstance(spec.get(field), str) and spec.get(field)
            for field in ("positive_fraction_field", "worst_field", "bootstrap_field")
        ):
            raise EvidenceGateError(f"metric_policy_invalid:{name}")


def validate_report(report: dict[str, Any]) -> None:
    if report.get("schema") != REPORT_SCHEMA:
        raise EvidenceGateError("walkforward_schema_mismatch")
    if report.get("paper_only") is not True or report.get("authenticated_execution") is not False:
        raise EvidenceGateError("unsafe_walkforward_report")
    if report.get("read_only") is not True or report.get("promotion_allowed") is not False:
        raise EvidenceGateError("walkforward_authority_violation")
    if report.get("validation_authority") != "BLOCKED_WALK_FORWARD":
        raise EvidenceGateError("walkforward_authority_missing")


def grade_metric(result: dict[str, Any], spec: dict[str, str], minimums: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    scored_folds = result.get("scored_folds")
    if isinstance(scored_folds, bool) or not isinstance(scored_folds, int) or scored_folds < minimums["scored_folds"]:
        reasons.append("insufficient_scored_folds")
    fraction = _number(result.get(spec["positive_fraction_field"]))
    if fraction is None or fraction < float(minimums["positive_fold_fraction"]):
        reasons.append("fold_stability_gate")
    worst = _number(result.get(spec["worst_field"]))
    if worst is None or worst <= float(minimums["worst_improvement"]):
        reasons.append("worst_fold_gate")
    bootstrap = result.get(spec["bootstrap_field"])
    if not isinstance(bootstrap, dict) or bootstrap.get("state") != "BOOTSTRAPPED":
        reasons.append("bootstrap_unverifiable")
        days = None
        ci_lower = None
        p_nonpositive = None
    else:
        days = bootstrap.get("days")
        ci_lower = _number(bootstrap.get("ci_lower"))
        p_nonpositive = _number(bootstrap.get("p_nonpositive"))
        if isinstance(days, bool) or not isinstance(days, int) or days < minimums["bootstrap_days"]:
            reasons.append("insufficient_bootstrap_days")
        if ci_lower is None or ci_lower <= float(minimums["bootstrap_ci_lower"]):
            reasons.append("bootstrap_ci_gate")
        if p_nonpositive is None or p_nonpositive > float(minimums["max_p_nonpositive"]):
            reasons.append("bootstrap_p_gate")
    return {
        "state": "STATISTICALLY_SUPPORTED" if not reasons else "MORE_EVIDENCE_REQUIRED",
        "statistically_supported": not reasons,
        "reasons": reasons,
        "scored_folds": scored_folds,
        "positive_fold_fraction": fraction,
        "worst_improvement": worst,
        "bootstrap_days": days,
        "bootstrap_ci_lower": ci_lower,
        "bootstrap_p_nonpositive": p_nonpositive,
    }


def evaluate(report: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    validate_policy(policy)
    validate_report(report)
    minimums = policy["minimums"]
    metrics = policy["metrics"]
    strategies: dict[str, Any] = {}
    supported: list[str] = []
    for strategy, payload in sorted((report.get("strategy_validation") or {}).items()):
        if not isinstance(payload, dict):
            continue
        out = {}
        for name in ("fill", "completion"):
            result = payload.get(name)
            grade = grade_metric(result if isinstance(result, dict) else {}, metrics[name], minimums)
            out[name] = grade
            if grade["statistically_supported"]:
                supported.append(f"strategy:{strategy}:{name}")
        marks = {}
        for horizon, result in sorted((payload.get("markouts") or {}).items()):
            grade = grade_metric(result if isinstance(result, dict) else {}, metrics["markout"], minimums)
            marks[horizon] = grade
            if grade["statistically_supported"]:
                supported.append(f"strategy:{strategy}:markout:{horizon}")
        out["markouts"] = marks
        strategies[strategy] = out
    joints = {}
    for key, result in sorted((report.get("joint_validation") or {}).items()):
        grade = grade_metric(result if isinstance(result, dict) else {}, metrics["joint"], minimums)
        joints[key] = grade
        if grade["statistically_supported"]:
            supported.append(f"joint:{key}")
    return {
        "schema": OUTPUT_SCHEMA,
        "model_sha": report.get("model_sha"),
        "paper_only": True,
        "authenticated_execution": False,
        "read_only": True,
        "promotion_allowed": False,
        "economic_pnl_gate_satisfied": False,
        "decision": "MORE_EVIDENCE_REQUIRED",
        "statistically_supported_targets": supported,
        "strategy_targets": strategies,
        "joint_targets": joints,
        "policy_minimums": minimums,
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = evaluate(read_json(args.report), read_json(args.policy))
    atomic_json(args.output, result)
    print(json.dumps({"output": str(args.output), "decision": result["decision"], "supported_targets": len(result["statistically_supported_targets"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
