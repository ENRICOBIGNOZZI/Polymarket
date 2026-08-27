#!/usr/bin/env python3
"""Audit whether canonical V7 execution evidence can represent LF/PCA/ranking lanes.

Research-only: this script does not mutate model, execution, risk, operator authority,
or canonical refs.  It checks the current execution-evidence registry and its policy
normalizer against the V7 model/horizon contracts.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_SCRIPT = ROOT / "scripts" / "v7_execution_evidence.py"
POLICY_PATH = ROOT / "config" / "v7_execution_evidence.json"

spec = importlib.util.spec_from_file_location("v7_execution_evidence_horizon_audit", EVIDENCE_SCRIPT)
assert spec and spec.loader
execution_evidence = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = execution_evidence
spec.loader.exec_module(execution_evidence)

# Latest explicit user instruction requires frozen ranking evidence at 2h/6h.
# The authoritative operator contract requires PCA evidence at 30m/1h/2h/6h.
# Local Factor's hold horizon is candidate-specific, so the audit requires a family
# surface whose actual horizon can be retained rather than inventing one fixed bucket.
REQUIRED_MODEL_SURFACES: dict[str, dict[str, Any]] = {
    "local_factor": {
        "family": "local_factor",
        "target": "hedged_convergence",
        "horizon_seconds": 0,
        "horizon_semantics": "candidate_actual_hold_horizon_required",
    },
    "pca_30m": {
        "family": "pca",
        "target": "short_horizon_markout",
        "horizon_seconds": 1800,
        "horizon_semantics": "fixed_forecast_horizon",
    },
    "pca_1h": {
        "family": "pca",
        "target": "short_horizon_markout",
        "horizon_seconds": 3600,
        "horizon_semantics": "fixed_forecast_horizon",
    },
    "pca_2h": {
        "family": "pca",
        "target": "short_horizon_markout",
        "horizon_seconds": 7200,
        "horizon_semantics": "fixed_forecast_horizon",
    },
    "pca_6h": {
        "family": "pca",
        "target": "short_horizon_markout",
        "horizon_seconds": 21600,
        "horizon_semantics": "fixed_forecast_horizon",
    },
    "ranking_2h": {
        "family": "cross_sectional_ranking",
        "target": "hedged_convergence",
        "horizon_seconds": 7200,
        "horizon_semantics": "frozen_relative_pair_holdout",
    },
    "ranking_6h": {
        "family": "cross_sectional_ranking",
        "target": "hedged_convergence",
        "horizon_seconds": 21600,
        "horizon_semantics": "frozen_relative_pair_holdout",
    },
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def injected_policy() -> dict[str, Any]:
    policy = execution_evidence.default_policy()
    policy["models"] = dict(policy.get("models") or {})
    for name, contract in REQUIRED_MODEL_SURFACES.items():
        policy["models"][name] = {
            "target": contract["target"],
            "horizon_seconds": contract["horizon_seconds"],
            "min_fills": 1,
            "min_pnl_observations": 1,
            "min_markout_observations": 0,
            "min_fill_rate": 0.0,
            "min_net_pnl": 0.0,
            "min_stressed_net_pnl": 0.0,
            "max_bootstrap_pvalue": 0.10,
            "min_active_folds": 0,
            "min_positive_fold_fraction": 0.0,
        }
    return policy


def audit() -> dict[str, Any]:
    configured = read_json(POLICY_PATH)
    configured_models = configured.get("models") if isinstance(configured.get("models"), dict) else {}

    candidate = injected_policy()
    normalized = execution_evidence.normalize_policy(candidate)
    normalized_models = normalized.get("models") if isinstance(normalized.get("models"), dict) else {}

    required_names = sorted(REQUIRED_MODEL_SURFACES)
    dropped = sorted(name for name in required_names if name not in normalized_models)
    missing_from_config = sorted(name for name in required_names if name not in configured_models)
    unmapped = []
    for name in required_names:
        execution_paths, submission_paths = execution_evidence.strategy_paths(ROOT, name)
        if not execution_paths and not submission_paths:
            unmapped.append(name)

    return {
        "schema": "polymarket_lf_v7_model_evidence_horizon_audit_v1",
        "decision": "MORE_EVIDENCE_REQUIRED",
        "state": "MODEL_HORIZON_EXECUTION_EVIDENCE_BLOCKER",
        "paper_only": True,
        "authenticated_execution": False,
        "required_model_surfaces": REQUIRED_MODEL_SURFACES,
        "configured_model_names": sorted(configured_models),
        "normalized_default_model_names": sorted(normalized_models),
        "missing_from_config": missing_from_config,
        "dropped_after_normalization": dropped,
        "unmapped_strategy_paths": sorted(unmapped),
        "silent_drop_demonstrated": bool(dropped),
        "material": bool(dropped or missing_from_config or unmapped),
        "impact": (
            "Canonical V7 execution evidence cannot currently emit horizon-separated LF/PCA/ranking "
            "fill/completion/PnL evidence. Adding these model contracts to policy alone is insufficient "
            "because normalize_policy silently removes names outside its built-in five-model registry, "
            "and strategy_paths has no source mapping for the required lanes."
        ),
        "required_successor_contract": [
            "Represent Local Factor, PCA and cross-sectional ranking as first-class V7 evidence families without restoring V3-V6 runtime surfaces.",
            "Preserve PCA 30m/1h/2h/6h identities separately and frozen ranking 2h/6h identities separately; never pool inference across horizons.",
            "Retain Local Factor's actual candidate hold horizon/TTR provenance on every executable observation rather than inventing a fixed horizon.",
            "Unknown configured model/evidence families must fail closed with an explicit error instead of disappearing during policy normalization.",
            "Bind submissions, unique fills, joint completion/partial states, terminal PnL and cost-stress observations to exact model family, horizon and source SHA.",
            "Require the same frozen observations for 1x/1.5x/2x audited cost stress and preserve PAPER-only/authenticated-disabled safety.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = audit()
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 1 if not report["material"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
