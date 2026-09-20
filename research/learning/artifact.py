"""Small native-compatible probability artifacts; fitting conveys no authority."""
from __future__ import annotations
import re
import numpy as np
from .common import SAFETY, CONTEXTS, canonical, digest, publish
from .models import offset_logistic, calibrate
from .validation import weights, sigmoid, DAY
from v7_fit_probability_candidate import FEATURES, ASSETS, HORIZONS, feature_vector, scales_for


def export_native(train, calibration, *, dataset, output, fit_cutoff_ns, calibration_end_ns,
                  ridge=8., seed=20260920):
    if not re.fullmatch(r"[0-9a-f]{40}", dataset["code_sha"]):
        raise ValueError("EXACT_CODE_SHA_REQUIRED")
    rows = train+calibration
    if (not train or not calibration or any(r["stratum"] != "native_causal_features_v1" for r in rows)
            or {r["market_id"] for r in train} & {r["market_id"] for r in calibration}):
        raise ValueError("NATIVE_FEATURE_CONTRACT_OR_CALIBRATION_OVERLAP")
    if any(not r["feature_information_ns"] <= r["decision_ns"] < r["label_information_ns"] < fit_cutoff_ns for r in train):
        raise ValueError("LEAKAGE_DETECTED")
    if any(not fit_cutoff_ns <= r["decision_ns"] < r["label_information_ns"] < calibration_end_ns for r in calibration):
        raise ValueError("CALIBRATION_TIMING")
    convert = lambda r: {"features": r["native_input"], "asset": r["asset"], "horizon": r["horizon"]}
    legacy = [convert(r) for r in train]
    scales = scales_for(legacy)
    if any(not np.isfinite(s) or s <= 0 for s in scales):
        raise ValueError("INSUFFICIENT_SHOCK_VARIATION")
    X = np.vstack([feature_vector(convert(r), scales) for r in train])
    C = np.vstack([feature_vector(convert(r), scales) for r in calibration])
    indices = [i for i in range(len(FEATURES)) if i != 1]
    w = weights(train); y = np.array([r["outcome"] for r in train])
    b = np.zeros(len(FEATURES)); b[1] = 1.
    b[indices] = offset_logistic(X[:, indices], y, w, X[:, 1], ridge)
    params = calibrate(sigmoid(C@b), calibration, "platt")
    # Fold calibration algebra into the native dot product. No Python inference.
    calibrated_b = b*params["slope"]; calibrated_b[0] += params["intercept"]
    blocks = np.array([r["information_end_ns"]//DAY for r in train]); unique = np.unique(blocks)
    if len(unique) < 4:
        raise ValueError("INSUFFICIENT_UNCERTAINTY_BLOCKS")
    rng = np.random.default_rng(seed); draws = []
    for _ in range(64):
        selected = rng.choice(unique, len(unique), replace=True)
        bw = w*np.array([np.sum(selected == block) for block in blocks])
        beta = b.copy(); beta[indices] = offset_logistic(X[:, indices], y, bw, X[:, 1], ridge)
        draws.append(beta*params["slope"])
    q = sigmoid(X@b)
    curvature = np.linalg.inv(X.T@((w*q*(1-q))[:, None]*X)+np.eye(len(FEATURES))*ridge)
    covariance = np.cov(draws, rowvar=False)+curvature
    artifact = {"schema": "v7_probability_logit_candidate_v1", **SAFETY,
        "code_sha": dataset["code_sha"], "training_cutoff_ns": calibration_end_ns,
        "training_data_sha256": dataset["dataset_sha256"], "feature_schema": list(FEATURES),
        "feature_schema_sha256": digest(canonical(list(FEATURES))), "coefficients": calibrated_b.tolist(),
        "covariance": covariance.tolist(), "shock_scales": scales, "asset_order": list(ASSETS),
        "horizon_order": list(HORIZONS), "calibration": params,
        "context_encoding": "SHARED_ASSET_AND_HORIZON_EFFECTS_WITH_RIDGE_SHRINKAGE",
        "training_context_counts": {c: sum(r["asset"]+":"+r["horizon"] == c for r in train) for c in CONTEXTS},
        "parameters_empirically_fitted": True, "forward_calibrated": False,
        "uncertainty_z": 1.96, "explicit_logit_reserve": .25,
        "uncertainty_semantics": "DAY_BLOCK_BOOTSTRAP_PLUS_PENALIZED_CURVATURE_NOT_CONDITIONAL_COVERAGE",
        "test_duration_seconds": 7200, "excluded_assets": [], "asset_shadow_overrides": [],
        "maximum_order_cost_microdollars": 3750000, "maximum_quantity_microunits": 20000000,
        "execution_reserve_per_share": .005, "minimum_net_edge": .02, "fractional_kelly": .25,
        "maximum_chase_ticks": 2, "candidate_state": "TRAINED",
        "execution_policy_semantics": "ARRIVAL_TOP_FAK_ACTUAL_PRICE_PARTIAL_FILL_V2",
        "tte_policy_seconds": [105, 120], "promotion_evidence_required": True,
        "proposal_population": "ALL_CAUSALLY_VALID_SIGNAL_EVENTS_INCLUDING_REJECTS",
        "seed": seed}
    validate(artifact, dataset["code_sha"])
    return publish(output, "native_candidates", artifact), artifact


def validate(a, expected_code_sha):
    if (not re.fullmatch(r"[0-9a-f]{40}", expected_code_sha) or a.get("code_sha") != expected_code_sha
            or a.get("schema") != "v7_probability_logit_candidate_v1"
            or any(a.get(k) != v for k, v in SAFETY.items())
            or a.get("feature_schema") != list(FEATURES) or a.get("asset_order") != list(ASSETS)
            or a.get("horizon_order") != list(HORIZONS)
            or a.get("test_duration_seconds") != 7200 or a.get("tte_policy_seconds") != [105, 120]
            or a.get("excluded_assets") != [] or a.get("asset_shadow_overrides") != []):
        raise ValueError("ARTIFACT_IDENTITY_FEATURE_OR_SAFETY")
    b = np.asarray(a.get("coefficients")); cov = np.asarray(a.get("covariance")); scales = np.asarray(a.get("shock_scales"))
    if (b.shape != (18,) or cov.shape != (18, 18) or scales.shape != (6,)
            or not all(np.isfinite(x).all() for x in (b, cov, scales)) or (abs(b) > 100).any()
            or (scales <= 0).any() or not np.allclose(cov, cov.T, atol=1e-8)
            or np.linalg.eigvalsh(cov).min() < -1e-10):
        raise ValueError("ARTIFACT_PARAMETERS")
    if (not 10 < a["maximum_order_cost_microdollars"] <= 3750000
            or not 0 < a["maximum_quantity_microunits"] <= 20000000
            or not 0 < a["fractional_kelly"] <= .25 or not .02 <= a["minimum_net_edge"] <= .25
            or not 0 <= a["maximum_chase_ticks"] <= 2 or not .005 <= a["execution_reserve_per_share"] < 1):
        raise ValueError("ARTIFACT_RISK_CEILINGS")
    return digest(canonical(a))
