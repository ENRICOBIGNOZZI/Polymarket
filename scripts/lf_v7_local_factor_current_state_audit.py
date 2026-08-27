#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def residual_change(phi: float, current: float, mean: float, steps: float) -> float:
    if not 0.0 < float(phi) < 1.0:
        return 0.0
    return (float(phi) ** max(0.0, float(steps)) - 1.0) * (float(current) - float(mean))


def side_from_change(change: float) -> str:
    return "YES" if change > 0.0 else "NO" if change < 0.0 else "FLAT"


def counterexamples() -> dict[str, Any]:
    phi = 0.80
    steps = 6.0
    z_floor = 1.0

    historical_a, historical_b = 2.0, -2.0
    current_a, current_b = 0.10, -0.10
    stale_a = residual_change(phi, historical_a, 0.0, steps)
    stale_b = residual_change(phi, historical_b, 0.0, steps)
    current_move_a = residual_change(phi, current_a, 0.0, steps)
    current_move_b = residual_change(phi, current_b, 0.0, steps)

    reverted = {
        "historical_residual_z": [historical_a, historical_b],
        "current_book_residual_z": [current_a, current_b],
        "incumbent_historical_gate_passes": abs(historical_a) >= z_floor and abs(historical_b) >= z_floor,
        "current_state_gate_passes": abs(current_a) >= z_floor and abs(current_b) >= z_floor,
        "incumbent_forecast_residual_change": [stale_a, stale_b],
        "current_state_forecast_residual_change": [current_move_a, current_move_b],
        "forecast_magnitude_overstatement": abs(stale_a) / max(abs(current_move_a), 1e-12),
    }

    historical_a2, historical_b2 = 0.20, -0.20
    current_a2, current_b2 = 2.0, -2.0
    new_dislocation = {
        "historical_residual_z": [historical_a2, historical_b2],
        "current_book_residual_z": [current_a2, current_b2],
        "incumbent_historical_gate_passes": abs(historical_a2) >= z_floor and abs(historical_b2) >= z_floor,
        "current_state_gate_passes": abs(current_a2) >= z_floor and abs(current_b2) >= z_floor,
        "incumbent_forecast_residual_change": [
            residual_change(phi, historical_a2, 0.0, steps),
            residual_change(phi, historical_b2, 0.0, steps),
        ],
        "current_state_forecast_residual_change": [
            residual_change(phi, current_a2, 0.0, steps),
            residual_change(phi, current_b2, 0.0, steps),
        ],
    }

    historical_a3, historical_b3 = 2.0, -2.0
    current_a3, current_b3 = -2.0, 2.0
    stale_sign_a = residual_change(phi, historical_a3, 0.0, steps)
    stale_sign_b = residual_change(phi, historical_b3, 0.0, steps)
    current_sign_a = residual_change(phi, current_a3, 0.0, steps)
    current_sign_b = residual_change(phi, current_b3, 0.0, steps)
    sign_flip = {
        "historical_residual_z": [historical_a3, historical_b3],
        "current_book_residual_z": [current_a3, current_b3],
        "incumbent_sides": [side_from_change(stale_sign_a), side_from_change(stale_sign_b)],
        "current_state_sides": [side_from_change(current_sign_a), side_from_change(current_sign_b)],
        "direction_reversed": [side_from_change(stale_sign_a) != side_from_change(current_sign_a), side_from_change(stale_sign_b) != side_from_change(current_sign_b)],
    }

    return {
        "phi": phi,
        "horizon_steps": steps,
        "minimum_abs_residual_z": z_floor,
        "reverted_between_completed_bucket_and_current_book": reverted,
        "new_dislocation_after_completed_bucket": new_dislocation,
        "sign_flip_after_completed_bucket": sign_flip,
    }


def source_contract(root: Path) -> dict[str, Any]:
    core_base = (root / "scripts/v7_local_factor_core_base.py").read_text(encoding="utf-8")
    driver = (root / "scripts/v7_local_factor_research.py").read_text(encoding="utf-8")
    orientation = (root / "scripts/v7_local_factor_orientation.py").read_text(encoding="utf-8")

    stale_gate = "abs(fit.residual_z_a)<min_abs_z" in core_base and "abs(fit.residual_z_b)<min_abs_z" in core_base
    stale_forecast = "cur_a=fit.residual_a[-1]" in core_base and "cur_b=fit.residual_b[-1]" in core_base
    target_only_current_mid_map = (
        "{market_a_id: yes_a.mid, market_b_id: yes_b.mid}" in driver
        and "core.build_pair_signal(" in driver
    )
    frozen_factor_returns_scores_only = "return tuple(factor)" in orientation

    return {
        "historical_endpoint_used_for_post_multiplicity_z_gate": stale_gate,
        "historical_endpoint_used_for_n_step_signal_forecast": stale_forecast,
        "driver_passes_only_current_target_mids_to_pair_signal": target_only_current_mid_map,
        "orientation_pc1_returns_temporal_scores_without_current_projection_state": frozen_factor_returns_scores_only,
        "current_book_pair_residual_reconstruction_available": not (
            stale_gate and stale_forecast and target_only_current_mid_map and frozen_factor_returns_scores_only
        ),
    }


def build_report(root: Path) -> dict[str, Any]:
    contract = source_contract(root)
    cases = counterexamples()
    blocker = (
        contract["historical_endpoint_used_for_post_multiplicity_z_gate"]
        and contract["historical_endpoint_used_for_n_step_signal_forecast"]
        and contract["driver_passes_only_current_target_mids_to_pair_signal"]
    )
    return {
        "schema_version": 1,
        "research_only": True,
        "decision": "MORE_EVIDENCE_REQUIRED",
        "finding": "CURRENT_BOOK_RESIDUAL_STATE_NOT_RECONSTRUCTED" if blocker else "SOURCE_CONTRACT_CHANGED_REAUDIT_REQUIRED",
        "material_structural_blocker": blocker,
        "source_contract": contract,
        "deterministic_counterexamples": cases,
        "interpretation": (
            "The fitted residual endpoint can be fresh and regular yet differ materially from the current executable-book residual. "
            "Using the historical endpoint for the residual-z gate and n-step forecast can admit already-reverted states, miss new dislocations, or reverse trade direction."
        ),
        "required_successor_contract": [
            "Freeze the pair-excluded orientation-invariant PC1 scoring basis from training; do not refit it on the current observation.",
            "Persist enough control standardization and PC1 projection state to score the frozen factor at the current book.",
            "Require fresh causally coherent current books for both targets and every predeclared control; fail closed when any required state is missing or stale.",
            "Standardize current target/control logits using training-only means/scales and reconstruct both current residuals on the frozen pair-excluded factor basis.",
            "Apply the post-multiplicity residual-z floor to the reconstructed current residuals, keeping the historical endpoint only as a diagnostic.",
            "Use the reconstructed current residuals for the horizon-matched n-step forecast and retain the existing TTR-minus-buffer guard.",
            "Only after current-state validity attach executable depth, authoritative fees, slippage, empirical joint fill states, partial abort/unwind loss and fill-conditioned cost-stressed PnL.",
        ],
        "safety": {
            "paper_only": True,
            "authenticated_execution": False,
            "operator_authority_unchanged": True,
            "main_or_paper_validated_mutation": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V7 Local Factor current-book residual-state semantics")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()
    report = build_report(args.repo_root)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["material_structural_blocker"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
