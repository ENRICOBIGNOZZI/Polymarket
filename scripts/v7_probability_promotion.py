#!/usr/bin/env python3
"""Cold-plane PAPER admission proof. Missing evidence always closes activation.

This validator performs no fitting, network access, order submission or writes.
It binds the report, inference parity and all promotion checks to exact bytes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import re

REQUIRED_GATES = ("artifact_schema", "feature_contract", "causal_audit", "no_leakage",
    "market_support", "time_block_support", "calibration", "oos_probability", "executable_ev",
    "risk_ceilings", "all_contexts", "native_inference", "exact_sha", "paper_only", "diverse_economic_support")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_measurements(report, model):
    """Gate actual native-artifact audit statistics, not a selected offline proxy."""
    audit, baseline = report.get("native_audit", {}), report.get("native_audit_pm", {})
    for key in ("log_loss", "brier", "ece"):
        if not finite(audit.get(key)) or not finite(baseline.get(key)):
            raise ValueError("NATIVE_OOS_METRICS_MISSING")
    if audit["log_loss"] >= baseline["log_loss"] or audit["brier"] >= baseline["brier"]:
        raise ValueError("NO_INCREMENTAL_INFORMATION")
    if audit["ece"] > baseline["ece"]+.005:
        raise ValueError("CALIBRATION_WORSE")
    support = report.get("context_validation", {})
    contexts = {a+":"+h for a in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
                for h in ("M5", "M15", "H1", "H4", "D1")}
    if set(support) != contexts or any(v.get("passed") is not True
            or v.get("mode") not in {"DIRECT_OOS", "VALIDATED_PARTIAL_POOLING"}
            or not isinstance(v.get("evidence_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", v["evidence_sha256"]) for v in support.values()):
        raise ValueError("UNSTABLE_CONTEXTS")
    parity = report.get("native_runtime_validation", {})
    if (parity.get("feature_schema_sha256") != model.get("feature_schema_sha256")
            or not finite(parity.get("maximum_absolute_error")) or parity["maximum_absolute_error"] > 1e-9
            or parity.get("cases", 0) < 30 or parity.get("hot_allocations") != 0):
        raise ValueError("NATIVE_PARITY_UNVERIFIED")
    economic = report.get("economic_quality", {})
    conservative = economic.get("policies", {}).get("uncertainty_adjusted", {})
    if (not finite(conservative.get("net_pnl")) or conservative["net_pnl"] <= 0
            or not finite(conservative.get("net_pnl_day_block_lower_95"))
            or conservative["net_pnl_day_block_lower_95"] <= 0
            or conservative.get("markets", 0) < 100 or conservative.get("time_blocks", 0) < 14
            or economic.get("diverse_context_support_verified") is not True):
        raise ValueError("NO_EXECUTABLE_EDGE")


def checked(path, sha):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16*1024*1024:
        raise ValueError("UNSAFE_PROMOTION_EVIDENCE")
    raw = path.read_bytes()
    if not isinstance(sha, str) or hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("PROMOTION_EVIDENCE_SHA_MISMATCH")
    return json.loads(raw)


def validate(model_path, proof_path, code_sha):
    if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
        raise ValueError("EXACT_CODE_SHA_REQUIRED")
    proof_path = Path(proof_path)
    if proof_path.is_symlink() or not proof_path.is_file():
        raise ValueError("PROMOTION_EVIDENCE_REQUIRED")
    proof = json.loads(proof_path.read_bytes())
    if (proof.get("schema") != "v7_probability_promotion_proof_v1" or proof.get("code_sha") != code_sha
            or proof.get("state") != "PAPER_ELIGIBLE" or proof.get("paper_only") is not True
            or proof.get("authenticated_execution") is not False or proof.get("real_order_submission") is not False
            or proof.get("automatic_promotion") is not False):
        raise ValueError("PROMOTION_PROOF_CONTRACT")
    model = checked(model_path, proof.get("artifact_sha256"))
    if model.get("code_sha") != code_sha or model.get("training_data_sha256") != proof.get("dataset_sha256"):
        raise ValueError("PROMOTION_MODEL_IDENTITY")
    report_name = proof.get("report_file")
    if not isinstance(report_name, str) or Path(report_name).name != report_name:
        raise ValueError("PROMOTION_REPORT_MUST_BE_ADJACENT")
    report = checked(proof_path.parent/report_name, proof.get("report_sha256"))
    if (report.get("code_sha") != code_sha or report.get("dataset_sha256") != proof.get("dataset_sha256")
            or report.get("native_candidate_sha256") != proof.get("artifact_sha256")
            or report.get("state") != "PAPER_ELIGIBLE" or report.get("reasons") != []):
        raise ValueError("PROMOTION_REPORT_NOT_ELIGIBLE")
    if any(proof.get("gates", {}).get(k) is not True for k in REQUIRED_GATES):
        raise ValueError("PROMOTION_GATES_FAILED")
    data = report.get("data") or {}
    if data.get("markets", 0) < 100 or data.get("time_blocks", 0) < 14:
        raise ValueError("INSUFFICIENT_DATA")
    parity = report.get("native_runtime_validation") or {}
    if (parity.get("code_sha") != code_sha or parity.get("artifact_sha256") != proof.get("artifact_sha256")
            or parity.get("passed") is not True):
        raise ValueError("NATIVE_PARITY_UNVERIFIED")
    economic = report.get("economic_quality") or {}
    if (economic.get("portfolio_capital_replay_verified") is not True
            or economic.get("counterfactual_transportability_validated") is not True):
        raise ValueError("EXECUTABLE_POLICY_UNVERIFIED")
    validate_measurements(report, model)
    return {"state": "PAPER_ELIGIBLE", "artifact_sha256": proof["artifact_sha256"],
            "report_sha256": proof["report_sha256"], "code_sha": code_sha}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", type=Path, required=True); p.add_argument("--proof", type=Path, required=True)
    p.add_argument("--code-sha", required=True); a = p.parse_args()
    print(json.dumps(validate(a.model, a.proof, a.code_sha), sort_keys=True))
