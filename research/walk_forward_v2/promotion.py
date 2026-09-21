#!/usr/bin/env python3
"""Fail-closed automatic PAPER-research champion selection for nightly V2.

This script never submits orders, never edits the London runtime, and never
changes risk limits. It may only update a repository-side research champion
registry after preregistered OOS gates pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_executable_markout_research_champion_v1"
RECEIPT_SCHEMA = "polymarket_v7_executable_markout_promotion_receipt_v1"
ALLOWED_CANDIDATES = {
    "pooled_500ms", "pooled_1000ms", "pooled_2000ms",
    "asset_specific_500ms", "asset_specific_1000ms", "asset_specific_2000ms",
}
EXPECTED_MINIMUM_WALL_NS = 1_789_921_800_000_000_000
MIN_MARKED_FILLS = 50
MIN_MARKETS = 20
MIN_FILL_RATE = 0.10
MIN_CHALLENGER_IMPROVEMENT = 0.002  # 0.2c/share on OOS marked fills
REFERENCE_LATENCY_MS = 50


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"object required:{path}")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def candidate_surface(economics: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    family, horizon_text = candidate_id.rsplit("_", 1)
    horizon = horizon_text.removesuffix("ms")
    if family == "pooled":
        root = economics.get("live_parity_horizon_latency", {})
    elif family == "asset_specific":
        root = economics.get("live_parity_asset_horizon_latency", {})
    else:
        return {}
    return root.get(horizon, {}) if isinstance(root, dict) else {}


def assess_candidate(economics: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    candidate = economics.get("live_policy_promotion_candidates", {}).get(candidate_id)
    failures: list[str] = []
    if candidate_id not in ALLOWED_CANDIDATES:
        failures.append("CANDIDATE_NOT_PREREGISTERED")
    if not isinstance(candidate, dict):
        return {
            "candidate_id": candidate_id,
            "qualified": False,
            "failures": failures + ["CANDIDATE_METRICS_UNAVAILABLE"],
        }

    metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) else {}
    uncertainty = candidate.get("uncertainty") if isinstance(candidate.get("uncertainty"), dict) else {}
    interval = uncertainty.get("markout_per_fill_interval")
    marked = int(metrics.get("marked_fills") or 0)
    fills = int(metrics.get("fills") or 0)
    markout = metrics.get("markout_pnl")
    per_fill = metrics.get("markout_per_fill")
    fill_rate = metrics.get("fill_rate")
    markets = int((uncertainty.get("markout") or {}).get("markets") or 0)

    if candidate.get("reference_latency_ms") != REFERENCE_LATENCY_MS:
        failures.append("REFERENCE_LATENCY_MISMATCH")
    if marked < MIN_MARKED_FILLS:
        failures.append("INSUFFICIENT_MARKED_FILLS")
    if markets < MIN_MARKETS:
        failures.append("INSUFFICIENT_MARKET_BLOCKS")
    if not finite(markout) or markout <= 0:
        failures.append("NONPOSITIVE_TOTAL_MARKOUT")
    if not finite(per_fill) or per_fill <= 0:
        failures.append("NONPOSITIVE_MARKOUT_PER_FILL")
    if not finite(fill_rate) or fill_rate < MIN_FILL_RATE:
        failures.append("INSUFFICIENT_FILL_RATE")
    if (
        not isinstance(interval, list) or len(interval) != 2
        or not all(finite(value) for value in interval)
    ):
        failures.append("MARKET_BLOCK_INTERVAL_UNAVAILABLE")
        lower = None
    else:
        lower = float(interval[0])
        if lower < 0:
            failures.append("MARKET_BLOCK_LOWER_BOUND_NEGATIVE")

    surface = candidate_surface(economics, candidate_id)
    latency_checks = {}
    for latency in ("25", "50"):
        cell = surface.get(latency) if isinstance(surface, dict) else None
        state = cell.get("state") if isinstance(cell, dict) else None
        value = cell.get("markout_pnl") if isinstance(cell, dict) else None
        latency_checks[latency] = {
            "state": state,
            "markout_pnl": value,
            "marked_fills": cell.get("marked_fills") if isinstance(cell, dict) else None,
        }
        if state != "READY" or not finite(value) or value < 0:
            failures.append(f"NONROBUST_LATENCY_{latency}MS")

    score = lower if lower is not None else float("-inf")
    return {
        "candidate_id": candidate_id,
        "qualified": not failures,
        "failures": failures,
        "score": score,
        "metrics": {
            "fills": fills,
            "marked_fills": marked,
            "markets": markets,
            "fill_rate": fill_rate,
            "markout_pnl": markout,
            "markout_per_fill": per_fill,
            "markout_per_fill_interval": interval,
        },
        "latency_checks": latency_checks,
        "family": candidate.get("family"),
        "horizon_ms": candidate.get("horizon_ms"),
        "candidate_contract": candidate.get("candidate_contract"),
    }


def expanding_history_gate(
    manifest: dict[str, Any],
    artifact: dict[str, Any],
    previous: dict[str, Any] | None,
) -> tuple[bool, list[str], dict[str, Any]]:
    failures: list[str] = []
    configured_start = manifest.get("minimum_wall_ns")
    window = artifact.get("training_window") if isinstance(artifact.get("training_window"), dict) else {}
    rows = window.get("decision_rows")
    end = window.get("maximum_decision_ns")

    if configured_start != EXPECTED_MINIMUM_WALL_NS:
        failures.append("TRAINING_EPOCH_CHANGED")
    if window.get("mode") != "EXPANDING_ALL_CAUSAL_HISTORY":
        failures.append("TRAINING_WINDOW_NOT_EXPANDING")
    if not isinstance(rows, int) or rows <= 0:
        failures.append("TRAINING_ROWS_INVALID")
    if not isinstance(end, int) or end <= 0:
        failures.append("TRAINING_END_INVALID")

    if previous:
        prev_window = previous.get("training_window") if isinstance(previous.get("training_window"), dict) else {}
        prev_rows = prev_window.get("decision_rows")
        prev_end = prev_window.get("maximum_decision_ns")
        prev_epoch = previous.get("configured_minimum_wall_ns")
        if prev_epoch is not None and prev_epoch != configured_start:
            failures.append("PREVIOUS_CHAMPION_EPOCH_MISMATCH")
        if isinstance(prev_rows, int) and isinstance(rows, int) and rows < prev_rows:
            failures.append("TRAINING_ROWS_DECREASED")
        if isinstance(prev_end, int) and isinstance(end, int) and end < prev_end:
            failures.append("TRAINING_END_MOVED_BACKWARD")

    return not failures, failures, {
        "configured_minimum_wall_ns": configured_start,
        "mode": window.get("mode"),
        "decision_rows": rows,
        "maximum_decision_ns": end,
    }


def selected_model(artifact: dict[str, Any], assessment: dict[str, Any]) -> dict[str, Any]:
    horizon = str(int(assessment["horizon_ms"]))
    family = assessment["family"]
    if family == "POOLED":
        spec = artifact.get("executable_markout_models", {}).get(horizon)
        if not isinstance(spec, dict) or spec.get("state") != "READY":
            raise ValueError("pooled full-window model unavailable")
        return {"family": family, "horizon_ms": int(horizon), "model": spec}
    if family == "ASSET_SPECIFIC":
        specs = artifact.get("asset_executable_markout_models", {}).get(horizon)
        if not isinstance(specs, dict):
            raise ValueError("asset-specific full-window models unavailable")
        ready = {
            asset: spec for asset, spec in specs.items()
            if isinstance(spec, dict) and spec.get("state") == "READY"
        }
        if not ready:
            raise ValueError("no ready asset-specific full-window models")
        return {"family": family, "horizon_ms": int(horizon), "models_by_asset": ready}
    raise ValueError("unknown candidate family")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()

    economics = load(args.report_dir / "economic_metrics.json")
    manifest = load(args.report_dir / "data_manifest.json")
    results = load(args.report_dir / "results.json")
    artifact_path = args.report_dir / "full_window_repricing_models.json"
    artifact = load(artifact_path)

    for obj in (economics, manifest, results, artifact):
        if (
            obj.get("paper_only") is not True
            or obj.get("authenticated_execution") is not False
            or obj.get("real_order_submission") is not False
            or obj.get("real_capital_at_risk") is not False
        ):
            raise ValueError("PAPER-only promotion boundary violated")

    previous = load(args.registry) if args.registry.exists() else None
    history_ok, history_failures, training_window = expanding_history_gate(
        manifest, artifact, previous)

    assessments = [
        assess_candidate(economics, candidate_id)
        for candidate_id in sorted(ALLOWED_CANDIDATES)
    ]
    qualified = [value for value in assessments if value["qualified"]]
    qualified.sort(
        key=lambda value: (
            float(value.get("score", float("-inf"))),
            int((value.get("metrics") or {}).get("marked_fills") or 0),
            value["candidate_id"],
        ),
        reverse=True,
    )
    best = qualified[0] if qualified else None

    previous_id = previous.get("candidate_id") if previous else None
    previous_assessment = next(
        (value for value in assessments if value["candidate_id"] == previous_id),
        None,
    )
    action = "HOLD"
    reason = "NO_QUALIFIED_CANDIDATE"
    promoted = None
    selected_assessment = None

    if not history_ok:
        reason = "EXPANDING_HISTORY_GATE_FAILED"
    elif best is not None:
        if previous is None:
            action = "PROMOTE"
            reason = "INITIAL_CHAMPION_PASSED_ALL_GATES"
            selected_assessment = best
        elif previous_assessment is None:
            reason = "PREVIOUS_CHAMPION_NOT_IN_PREREGISTERED_SET"
        elif not previous_assessment.get("qualified"):
            action = "PROMOTE"
            reason = "CURRENT_CHAMPION_FAILED_CURRENT_GATES"
            selected_assessment = best
        else:
            improvement = float(best["score"]) - float(previous_assessment["score"])
            if best["candidate_id"] == previous_id:
                action = "REFIT_CHAMPION"
                reason = "CURRENT_CHAMPION_REMAINS_BEST_REFIT_ON_EXPANDING_HISTORY"
                selected_assessment = previous_assessment
            elif improvement >= MIN_CHALLENGER_IMPROVEMENT:
                action = "PROMOTE"
                reason = "CHALLENGER_IMPROVES_MARKET_BLOCK_LOWER_BOUND"
                selected_assessment = best
            else:
                action = "REFIT_CHAMPION"
                reason = "CHALLENGER_BELOW_MARGIN_REFIT_CURRENT_CHAMPION"
                selected_assessment = previous_assessment

    artifact_hash = sha256(artifact_path)
    if action in {"PROMOTE", "REFIT_CHAMPION"} and selected_assessment is not None:
        model = selected_model(artifact, selected_assessment)
        promoted = {
            "schema": SCHEMA,
            "version": 1,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": False,
            "automatic_promotion": True,
            "promotion_scope": "PAPER_RESEARCH_CHAMPION_ONLY",
            "hot_path_mutation": False,
            "trader_restart_required": False,
            "candidate_id": selected_assessment["candidate_id"],
            "family": selected_assessment["family"],
            "horizon_ms": selected_assessment["horizon_ms"],
            "reference_latency_ms": REFERENCE_LATENCY_MS,
            "source_code_sha": results.get("start_sha"),
            "source_data_sha256": manifest.get("data_sha256"),
            "source_full_window_artifact_sha256": artifact_hash,
            "configured_minimum_wall_ns": manifest.get("minimum_wall_ns"),
            "training_window": training_window,
            "oos_gate_snapshot": selected_assessment,
            "selection_action": action,
            "selected_model": model,
        }
        atomic_json(args.registry, promoted)

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "version": 1,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": False,
        "automatic_promotion_scope": "PAPER_RESEARCH_CHAMPION_ONLY",
        "hot_path_mutation": False,
        "action": action,
        "reason": reason,
        "history_gate": {
            "passed": history_ok,
            "failures": history_failures,
            "training_window": training_window,
        },
        "policy": {
            "allowed_candidates": sorted(ALLOWED_CANDIDATES),
            "minimum_marked_fills": MIN_MARKED_FILLS,
            "minimum_market_blocks": MIN_MARKETS,
            "minimum_fill_rate": MIN_FILL_RATE,
            "reference_latency_ms": REFERENCE_LATENCY_MS,
            "minimum_challenger_improvement": MIN_CHALLENGER_IMPROVEMENT,
            "require_nonnegative_25ms_and_50ms_markout": True,
            "require_market_block_lower_bound_nonnegative": True,
        },
        "previous_candidate_id": previous_id,
        "selected_candidate_id": (
            selected_assessment["candidate_id"] if selected_assessment is not None
            else best["candidate_id"] if best else None
        ),
        "candidates": assessments,
        "registry_updated": action in {"PROMOTE", "REFIT_CHAMPION"},
        "registry_path": str(args.registry),
        "artifact_sha256": artifact_hash,
    }
    atomic_json(args.receipt, receipt)
    print(json.dumps({
        "action": action,
        "reason": reason,
        "selected_candidate_id": receipt["selected_candidate_id"],
        "history_gate_passed": history_ok,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
