#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from v7_fast_joint_execution_evidence import SOURCE, aggregate_events
from v7_fast_structural_gate import gate_candidate

SHA = "a" * 40


def theory(ready: bool = True) -> dict:
    return {
        "mode": "research_only",
        "real_order_submission": False,
        "research_ready": True,
        "promotion_ready": ready,
        "candidate_policy": {
            "real_order_submission": False,
            "promotion_ready": ready,
            "min_net_edge": 0.0005,
        },
    }


def good_execution() -> dict:
    return {
        "schema": "polymarket_v7_fast_joint_execution_v1",
        "source": SOURCE,
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "point_in_time": True,
        "authoritative_fees": True,
        "depth_executable": True,
        "partial_unwind_accounted": True,
        "joint_state_observations": 30,
        "realized_pnl_observations": 30,
        "completed_baskets": 25,
        "fill_conditioned_net_pnl": 1.0,
        "cost_stress_1_5x_net_pnl": 0.5,
        "cost_stress_2x_net_pnl": 0.2,
    }


def test_empty_canonical_ledger_is_fail_closed() -> None:
    report = aggregate_events([], expected_sha=SHA, ledger_files=0)
    assert report["source"] == SOURCE
    assert report["ledger_files"] == 0
    assert report["joint_state_observations"] == 0
    assert report["realized_pnl_observations"] == 0
    assert report["point_in_time"] is False
    assert report["authoritative_fees"] is False
    assert report["depth_executable"] is False


def test_quoted_theory_cannot_promote_without_canonical_execution() -> None:
    gated = gate_candidate(theory(True), {}, expected_sha=SHA)
    assert gated["quoted_theory_promotion_ready"] is True
    assert gated["promotion_ready"] is False
    assert "canonical_joint_execution_evidence_missing" in gated["promotion_gate"]["reasons"]


def test_noncanonical_or_mixed_sha_evidence_is_rejected() -> None:
    evidence = good_execution()
    evidence["source"] = "side_file"
    gated = gate_candidate(theory(True), evidence, expected_sha=SHA)
    assert gated["promotion_ready"] is False
    assert "noncanonical_execution_source" in gated["promotion_gate"]["reasons"]

    evidence = good_execution()
    evidence["model_sha"] = "b" * 40
    gated = gate_candidate(theory(True), evidence, expected_sha=SHA)
    assert gated["promotion_ready"] is False
    assert "mixed_or_wrong_sha_execution_evidence" in gated["promotion_gate"]["reasons"]


def test_both_theory_and_execution_contract_are_required() -> None:
    assert gate_candidate(theory(True), good_execution(), expected_sha=SHA)["promotion_ready"] is True
    assert gate_candidate(theory(False), good_execution(), expected_sha=SHA)["promotion_ready"] is False


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} V7 Fast economic-gate contract tests")
