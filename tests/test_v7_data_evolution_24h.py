from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_data_evolution_24h import (  # noqa: E402
    policy_actions, safe_config, screening_state,
)
from v7_external_settlement_dataset import FEATURE_SCHEMA, MODEL_FEATURE_NAMES  # noqa: E402
from v7_fair_value_registry import FairModelArtifact  # noqa: E402


def artifact() -> FairModelArtifact:
    return FairModelArtifact.build(
        family="btc_5m_settlement_margin_linear_v1",
        model_version="24h-screen-test",
        feature_schema_version=FEATURE_SCHEMA,
        code_sha="a" * 40,
        policy_version="b" * 64,
        artifact_role="RESEARCH",
        training_start_ns=1,
        training_end_ns=2,
        training_contracts=100,
        training_days=2,
        assets=("BTC",),
        contract_templates=("BTC_USD_UPDOWN_5M",),
        rules_hashes=("c" * 64,),
        parameters={
            "feature_names": list(MODEL_FEATURE_NAMES),
            "feature_means": {name: 0.0 for name in MODEL_FEATURE_NAMES},
            "feature_scales": {name: 1.0 for name in MODEL_FEATURE_NAMES},
            "coefficients": {name: 0.0 for name in MODEL_FEATURE_NAMES},
            "intercept": 1.0,
            "default_residual_sigma_bps": 5.0,
            "residual_sigma_by_tte": [],
            "mean_uncertainty_bps": 10.0,
            "calibration": {"intercept": 0.0, "slope": 1.0},
        },
        hyperparameters={},
        oos_scores={},
        probability_interval_diagnostics={},
        economic_replay={},
        generated_timestamp_ns=3,
    )


def row() -> dict:
    features = {name: 0.0 for name in MODEL_FEATURE_NAMES}
    features["tte_seconds"] = 90.0
    features["market_yes"] = 0.50
    return {
        "market_id": "m1", "observed_ms": 1000, "actual_yes": 1.0,
        "features": features,
        "execution": {
            "yes_best_ask": 0.50, "no_best_ask": 0.50,
            "yes_best_ask_visible_size": 10.0, "no_best_ask_visible_size": 10.0,
            "yes_min_order_size": 5.0, "no_min_order_size": 5.0,
            "fee_schedule": {"rate": 0.0, "exponent": 1.0},
        },
        "causality_valid": True,
    }


def main() -> None:
    config = json.loads((ROOT / "config/v7_data_evolution_24h.json").read_text())
    safe_config(config)
    unsafe = copy.deepcopy(config)
    unsafe["automatic_promotion"] = True
    try:
        safe_config(unsafe)
        raise AssertionError("unsafe config was accepted")
    except ValueError:
        pass

    runtime = {
        "taker": {
            "minimum_robust_ev_per_share": 0.001,
            "base_execution_risk_per_share": 0.0005,
        }
    }
    point = policy_actions([row()], artifact(), runtime, 0.04, "point")
    robust = policy_actions([row()], artifact(), runtime, 0.04, "robust")
    assert len(point) == 1
    assert len(robust) == 0
    result = {
        "model_minus_market_brier": 0.01,
        "policy": {
            "actions": 25,
            "bootstrap95_conservative_pnl_per_market": [0.01, 0.10],
            "stress_bootstrap95_conservative_pnl_per_market": [0.005, 0.08],
            "positive_market_fraction": 0.64,
            "mean_conservative_pnl_per_market": 0.04,
            "stress_mean_conservative_pnl_per_market": 0.03,
        },
    }
    state, reasons = screening_state(
        result, contracts=150, rows=300, causality_failures=0,
        screening=config["screening"],
    )
    assert state == "PROMISING_FOR_PAPER_EXPLORATION_PROBE"
    assert reasons == []
    state, reasons = screening_state(
        result, contracts=20, rows=40, causality_failures=0,
        screening=config["screening"],
    )
    assert state == "INSUFFICIENT_24H_EVIDENCE"
    assert "INSUFFICIENT_FORWARD_CONTRACTS" in reasons


if __name__ == "__main__":
    main()
