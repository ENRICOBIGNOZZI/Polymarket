#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_learned_execution_evidence_gate import EvidenceGateError, evaluate

POLICY = json.loads((ROOT / "config" / "v7_learned_execution_validation.json").read_text())


def metric(*, folds=4, days=10, fraction=1.0, worst=0.02, ci=0.01, p=0.01):
    return {
        "state": "OOS_SCORED",
        "scored_folds": folds,
        "positive_brier_fold_fraction": fraction,
        "worst_brier_improvement": worst,
        "brier_day_block_bootstrap": {"state": "BOOTSTRAPPED", "days": days, "ci_lower": ci, "p_nonpositive": p},
    }


def mark_metric(**kw):
    x = metric(**kw)
    return {
        "state": x["state"], "scored_folds": x["scored_folds"],
        "positive_mse_fold_fraction": x["positive_brier_fold_fraction"],
        "worst_mse_improvement": x["worst_brier_improvement"],
        "mse_day_block_bootstrap": x["brier_day_block_bootstrap"],
    }


def joint_metric(**kw):
    x = metric(**kw)
    return {
        "state": x["state"], "scored_folds": x["scored_folds"],
        "positive_vs_marginal_fold_fraction": x["positive_brier_fold_fraction"],
        "worst_vs_marginal_improvement": x["worst_brier_improvement"],
        "vs_product_marginals_day_block_bootstrap": x["brier_day_block_bootstrap"],
    }


def report():
    return {
        "schema": "polymarket_v7_learned_execution_walkforward_v1",
        "model_sha": "a" * 40,
        "paper_only": True,
        "authenticated_execution": False,
        "read_only": True,
        "promotion_allowed": False,
        "validation_authority": "BLOCKED_WALK_FORWARD",
        "strategy_validation": {"GRAPH_RV": {"fill": metric(), "completion": metric(), "markouts": {"60s": mark_metric()}}},
        "joint_validation": {"GRAPH_RV::1|2": joint_metric()},
    }


def test_strong_oos_targets_are_supported_but_never_promoted():
    out = evaluate(report(), POLICY)
    assert len(out["statistically_supported_targets"]) == 4
    assert out["strategy_targets"]["GRAPH_RV"]["fill"]["statistically_supported"] is True
    assert out["joint_targets"]["GRAPH_RV::1|2"]["statistically_supported"] is True
    assert out["promotion_allowed"] is False and out["economic_pnl_gate_satisfied"] is False
    assert out["decision"] == "MORE_EVIDENCE_REQUIRED"


def test_fold_day_worst_and_bootstrap_fail_closed():
    r = report()
    r["strategy_validation"]["GRAPH_RV"]["fill"] = metric(folds=2, days=3, fraction=0.5, worst=-0.01, ci=-0.001, p=0.4)
    grade = evaluate(r, POLICY)["strategy_targets"]["GRAPH_RV"]["fill"]
    assert grade["statistically_supported"] is False
    assert set(grade["reasons"]) == {"insufficient_scored_folds", "fold_stability_gate", "worst_fold_gate", "insufficient_bootstrap_days", "bootstrap_ci_gate", "bootstrap_p_gate"}


def test_unsafe_or_wrong_schema_report_is_rejected():
    r = report(); r["authenticated_execution"] = True
    try:
        evaluate(r, POLICY)
    except EvidenceGateError as exc:
        assert "unsafe_walkforward_report" in str(exc)
    else:
        raise AssertionError("unsafe report accepted")
    r = report(); r["schema"] = "wrong"
    try:
        evaluate(r, POLICY)
    except EvidenceGateError as exc:
        assert "walkforward_schema_mismatch" in str(exc)
    else:
        raise AssertionError("wrong schema accepted")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for test in tests:
        test()
    print(f"ok {len(tests)} v7 learned execution evidence gate tests")
