#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "v7_pca_stat_arb_research.py"


def historical_endpoint_gate(residual_last: float, residual_mean: float, residual_sd: float, minimum_abs_z: float) -> bool:
    if residual_sd <= 0.0:
        return False
    return abs((residual_last - residual_mean) / residual_sd) >= minimum_abs_z


def current_state_gate(current_residual_z: float, minimum_abs_z: float) -> bool:
    return abs(float(current_residual_z)) >= float(minimum_abs_z)


def deterministic_counterexamples() -> dict[str, Any]:
    minimum_abs_z = 1.0
    stale_pass = {
        "training_endpoint_z": 2.0,
        "current_book_z": 0.20,
        "incumbent_passes": historical_endpoint_gate(2.0, 0.0, 1.0, minimum_abs_z),
        "current_state_should_pass": current_state_gate(0.20, minimum_abs_z),
    }
    stale_reject = {
        "training_endpoint_z": 0.20,
        "current_book_z": 2.0,
        "incumbent_passes": historical_endpoint_gate(0.20, 0.0, 1.0, minimum_abs_z),
        "current_state_should_pass": current_state_gate(2.0, minimum_abs_z),
    }
    return {
        "minimum_abs_z": minimum_abs_z,
        "historical_extreme_currently_mean_reverted": stale_pass,
        "historical_mild_currently_extreme": stale_reject,
    }


def source_contract(path: Path = DRIVER) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    historical_expression = "residual_z = abs((model.residual_last - model.residual_mean) / model.residual_sd)"
    current_gate_expression = "abs(score.residual_z)"
    return {
        "driver": str(path.relative_to(ROOT)),
        "uses_training_endpoint_for_post_multiplicity_z_gate": historical_expression in source,
        "uses_current_scored_residual_z_for_post_multiplicity_gate": current_gate_expression in source,
        "score_current_is_computed_after_training_endpoint_gate": (
            source.find(historical_expression) >= 0
            and source.find("score = inference.score_with_total_single_leg_risk") > source.find(historical_expression)
        ),
    }


def audit() -> dict[str, Any]:
    counters = deterministic_counterexamples()
    contract = source_contract()
    stale_pass = counters["historical_extreme_currently_mean_reverted"]
    stale_reject = counters["historical_mild_currently_extreme"]
    defect_reproduced = bool(
        contract["uses_training_endpoint_for_post_multiplicity_z_gate"]
        and contract["score_current_is_computed_after_training_endpoint_gate"]
        and stale_pass["incumbent_passes"]
        and not stale_pass["current_state_should_pass"]
        and not stale_reject["incumbent_passes"]
        and stale_reject["current_state_should_pass"]
    )
    return {
        "status": "STRUCTURAL_BLOCKER" if defect_reproduced else "NOT_REPRODUCED",
        "finding": "V7 PCA applies the post-multiplicity residual-z threshold to the last historical training residual rather than the current residual recomputed from the executable book.",
        "source_contract": contract,
        "counterexamples": counters,
        "impact": [
            "A hypothesis can pass the z gate after its residual has already mean-reverted at the current book.",
            "A currently extreme residual can be rejected because the historical training endpoint was mild.",
            "The post-multiplicity opportunity filter is therefore not aligned with the state used to price the single-leg candidate.",
        ],
        "required_successor": [
            "Compute score_current / score_with_total_single_leg_risk first.",
            "Apply minimum_abs_residual_z_after_multiplicity to abs(score.residual_z), not model.residual_last.",
            "Record both historical endpoint z and current-book z separately for diagnostics.",
            "Re-run each 30m/1h/2h/6h horizon on identical chronological data after the gate repair; do not pool horizons.",
            "Keep BY/null-bootstrap inference, TTR, fees, slippage, depth and PAPER-only boundaries unchanged.",
        ],
        "decision": "MORE_EVIDENCE_REQUIRED",
    }


def main() -> int:
    report = audit()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "STRUCTURAL_BLOCKER" else 1


if __name__ == "__main__":
    raise SystemExit(main())
