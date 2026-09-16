#!/usr/bin/env python3
"""Causal pooled residual benchmark for the six-asset SHADOW feature tape.

Consumes only hashed multi-crypto labeled rows. It performs no network access,
no execution, no threshold selection and no automatic model promotion. All
ablations use the same frozen chronological split and fixed ridge penalty.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from v7_lead_lag_replay import ASSETS, HORIZONS, ReplayError, digest, primitive
from v7_lead_lag_research import (
    ResearchRow, chronological_split, cluster_mean_ci, connected_clusters,
    fit_residual_model,
)

INPUT_SCHEMA = "polymarket_v7_multi_crypto_repricing_labeled_row_v1"
OUTPUT_SCHEMA = "polymarket_v7_multi_crypto_residual_benchmark_v1"
VENUES = ("BINANCE_USDM", "BYBIT_LINEAR", "DERIBIT")

PM_FEATURES = (
    ("pm_yes_mid", "probability"),
    ("pm_complete_set_gap", "probability"),
    ("pm_yes_spread", "probability"),
    ("pm_yes_imbalance", "unitless"),
    ("tte_seconds", "seconds"),
)
OWN_FEATURES = (
    ("return_50ms_bp", "bp"), ("return_100ms_bp", "bp"),
    ("return_250ms_bp", "bp"), ("return_1s_bp", "bp"),
    ("dispersion_bps", "bp"), ("aggregate_ofi", "native_normalized_source"),
    ("aggregate_trade_imbalance", "unitless"),
    ("shock_z_unfloored", "sigma"), ("sigma_100ms_bp_prior", "bp"),
)
ORACLE_FEATURES = (
    ("distance_to_reference_bp", "bp"), ("spot_minus_oracle_bp", "bp"),
)
ASSET_FEATURES = tuple((f"asset_{asset}", "indicator") for asset in ASSETS if asset != "BTC")
CONTRACT_FEATURES = (("contract_M15", "indicator"),)
LEADER_FEATURES = tuple(
    feature
    for asset in ASSETS
    for feature in ((f"leader_{asset}_return_100ms_bp", "bp"), (f"leader_{asset}_shock_z", "sigma"))
)
DERIVATIVE_FEATURES = tuple(
    feature
    for venue in VENUES
    for feature in ((f"deriv_{venue}_basis_to_spot_bp", "bp"), (f"deriv_{venue}_funding_rate", "rate"))
)

GROUPS = {
    "PM_ONLY": PM_FEATURES + ASSET_FEATURES + CONTRACT_FEATURES,
    "OWN_EXTERNAL": PM_FEATURES + OWN_FEATURES + ASSET_FEATURES + CONTRACT_FEATURES,
    "ORACLE": PM_FEATURES + OWN_FEATURES + ORACLE_FEATURES + ASSET_FEATURES + CONTRACT_FEATURES,
    "LEADERS": PM_FEATURES + OWN_FEATURES + ORACLE_FEATURES + LEADER_FEATURES + ASSET_FEATURES + CONTRACT_FEATURES,
    "DERIVATIVES": PM_FEATURES + OWN_FEATURES + ORACLE_FEATURES + LEADER_FEATURES + DERIVATIVE_FEATURES + ASSET_FEATURES + CONTRACT_FEATURES,
}


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def canonical_hash(value: Any) -> str:
    return digest(value)


def validate_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema") != INPUT_SCHEMA:
        raise ReplayError("MULTI_CRYPTO_LABEL_SCHEMA_INVALID")
    if (raw.get("paper_only") is not True or raw.get("authenticated_execution") is not False
            or raw.get("real_order_submission") is not False or raw.get("execution_authority") is not False):
        raise ReplayError("MULTI_CRYPTO_LABEL_AUTHORITY_INVALID")
    base = dict(raw); row_hash = str(base.pop("row_hash", ""))
    if len(row_hash) != 64 or row_hash != canonical_hash(base):
        raise ReplayError("MULTI_CRYPTO_LABEL_HASH_INVALID")
    asset, horizon = str(raw.get("asset") or ""), str(raw.get("horizon") or "")
    if asset not in ASSETS or horizon not in HORIZONS:
        raise ReplayError("MULTI_CRYPTO_LABEL_SCOPE_INVALID")
    decision, available = int(raw.get("decision_wall_ns") or 0), int(raw.get("available_at_ns") or 0)
    if decision <= 0 or available <= 0 or available > decision:
        raise ReplayError("MULTI_CRYPTO_LABEL_FEATURE_AVAILABILITY_INVALID")
    if not isinstance(raw.get("features"), dict) or not isinstance(raw.get("labels"), dict):
        raise ReplayError("MULTI_CRYPTO_LABEL_CONTENT_MISSING")
    for name in ("model_sha", "policy_hash", "feature_schema_hash", "source_identity_hash", "origin_record_hash"):
        value = str(raw.get(name) or "")
        expected = 40 if name == "model_sha" else 64
        if len(value) != expected or any(ch not in "0123456789abcdef" for ch in value):
            raise ReplayError("MULTI_CRYPTO_LABEL_IDENTITY_INVALID:" + name)
    return dict(raw)


def feature_map(raw: Mapping[str, Any]) -> dict[str, float | None]:
    features = raw["features"]
    external = features.get("external") if isinstance(features.get("external"), dict) else {}
    shock = external.get("shock") if isinstance(external.get("shock"), dict) else {}
    output: dict[str, float | None] = {
        "pm_yes_mid": finite(features.get("pm_yes_mid")),
        "pm_complete_set_gap": finite(features.get("pm_complete_set_gap")),
        "pm_yes_spread": finite(features.get("pm_yes_spread")),
        "pm_yes_imbalance": finite(features.get("pm_yes_imbalance")),
        "tte_seconds": finite(features.get("tte_seconds")),
        "distance_to_reference_bp": finite(features.get("distance_to_reference_bp")),
        "spot_minus_oracle_bp": finite(features.get("spot_minus_oracle_bp")),
        "return_50ms_bp": finite(external.get("return_50ms_bp")),
        "return_100ms_bp": finite(external.get("return_100ms_bp")),
        "return_250ms_bp": finite(external.get("return_250ms_bp")),
        "return_1s_bp": finite(external.get("return_1s_bp")),
        "dispersion_bps": finite(external.get("dispersion_bps")),
        "aggregate_ofi": finite(external.get("aggregate_ofi")),
        "aggregate_trade_imbalance": finite(external.get("aggregate_trade_imbalance")),
        "shock_z_unfloored": finite(shock.get("shock_z_unfloored")),
        "sigma_100ms_bp_prior": finite(shock.get("sigma_100ms_bp_prior")),
    }
    asset = str(raw["asset"]); horizon = str(raw["horizon"])
    for name, _ in ASSET_FEATURES:
        output[name] = float(asset == name.removeprefix("asset_"))
    output["contract_M15"] = float(horizon == "M15")
    leaders = features.get("leader_features") if isinstance(features.get("leader_features"), dict) else {}
    for leader in ASSETS:
        row = leaders.get(leader) if isinstance(leaders.get(leader), dict) else {}
        output[f"leader_{leader}_return_100ms_bp"] = finite(row.get("return_100ms_bp"))
        output[f"leader_{leader}_shock_z"] = finite(row.get("shock_z_unfloored"))
    derivatives = features.get("derivatives") if isinstance(features.get("derivatives"), list) else []
    by_venue = {str(row.get("venue") or "").upper(): row for row in derivatives if isinstance(row, dict)}
    for venue in VENUES:
        row = by_venue.get(venue, {})
        usable = row.get("usable") is True
        output[f"deriv_{venue}_basis_to_spot_bp"] = finite(row.get("basis_to_spot_bp")) if usable else None
        output[f"deriv_{venue}_funding_rate"] = finite(row.get("funding_rate")) if usable else None
    return output


def research_row(raw: Mapping[str, Any], *, label_horizon_ms: int,
                 feature_names: tuple[str, ...]) -> ResearchRow | None:
    validated = validate_row(raw)
    label = validated["labels"].get(str(label_horizon_ms))
    if not isinstance(label, dict) or label.get("status") != "LABELED":
        return None
    delta = finite(label.get("delta_pm_yes")); p0 = finite(validated["features"].get("pm_yes_mid"))
    if delta is None or p0 is None or not 0 <= p0 <= 1 or not 0 <= p0 + delta <= 1:
        raise ReplayError("MULTI_CRYPTO_LABEL_VALUE_INVALID")
    decision = int(validated["decision_wall_ns"]); target = int(label.get("target_wall_ns") or 0)
    available = int(label.get("label_available_wall_ns") or 0)
    if target - decision != label_horizon_ms * 1_000_000 or available < target:
        raise ReplayError("MULTI_CRYPTO_LABEL_CLOCK_INVALID")
    values = feature_map(validated)
    feature_available = int(validated["available_at_ns"])
    source = str(validated["source_identity_hash"])
    return ResearchRow(
        row_id=f"{validated['origin_record_hash']}:{label_horizon_ms}",
        asset=str(validated["asset"]), horizon=str(validated["horizon"]),
        decision_ns=decision, label_end_ns=target, label_available_ns=available,
        features=tuple(values.get(name) for name in feature_names),
        pm_mid=p0, future_mid=p0 + delta, market_id=str(validated["market_id"]),
        parent_shock_id=source, feature_available_ns=tuple(feature_available for _ in feature_names),
        feature_source_ids=tuple(source + ":" + name for name in feature_names),
    )


def read_rows(paths: Iterable[Path]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for path in paths:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = validate_row(json.loads(line))
                key = str(row["row_hash"])
                if key in unique and unique[key] != row:
                    raise ReplayError("MULTI_CRYPTO_LABEL_HASH_REDEFINED")
                unique[key] = row
    return sorted(unique.values(), key=lambda row: (int(row["decision_wall_ns"]), str(row["market_id"]), str(row["row_hash"])))


def metrics(rows: list[ResearchRow], model: Any, *, names: tuple[str, ...], units: tuple[str, ...],
            label_horizon_ns: int, block_ns: int, minimum_clusters: int, bootstrap_draws: int) -> dict[str, Any]:
    errors: list[float] = []; signs: list[float] = []; biases: list[float] = []; cluster_rows: list[dict[str, Any]] = []
    for row in rows:
        prediction = model.predict(row.features, feature_names=names, units=units,
                                   decision_ns=row.decision_ns, label_horizon_ns=label_horizon_ns)
        target = float(row.target); errors.append((prediction - target) ** 2); biases.append(prediction - target)
        if abs(target) > 1e-15:
            signs.append(float((prediction > 0) == (target > 0)))
        cluster_rows.append({"decision_ns": row.decision_ns, "market_id": row.market_id,
                             "parent_shock_id": row.parent_shock_id})
    clusters = connected_clusters(cluster_rows, block_ns=block_ns) if rows else ()
    return {
        "rows": len(rows), "markets": len({r.market_id for r in rows}), "assets": sorted({r.asset for r in rows}),
        "mse": sum(errors) / len(errors) if errors else None,
        "bias": sum(biases) / len(biases) if biases else None,
        "sign_accuracy_nonzero": sum(signs) / len(signs) if signs else None,
        "nonzero_targets": len(signs),
        "mse_cluster_ci": cluster_mean_ci(errors, clusters, draws=bootstrap_draws,
                                           minimum_clusters=minimum_clusters) if rows else None,
    }


def build(rows: list[dict[str, Any]], *, label_horizon_ms: int, train_end_ns: int,
          validation_end_ns: int, embargo_ns: int, ridge: float, block_ns: int,
          minimum_clusters: int, bootstrap_draws: int) -> dict[str, Any]:
    if label_horizon_ms <= 0 or train_end_ns <= 0 or validation_end_ns <= train_end_ns:
        raise ReplayError("MULTI_CRYPTO_BENCHMARK_POLICY_INVALID")
    identities = {(row["model_sha"], row["policy_hash"], row["feature_schema_hash"]) for row in rows}
    if len(identities) != 1:
        raise ReplayError("MULTI_CRYPTO_MIXED_DATASET_IDENTITY")
    result: dict[str, Any] = {
        "schema": OUTPUT_SCHEMA, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False, "automatic_promotion": False,
        "economic_evidence": "NOT_PROVEN", "label_horizon_ms": label_horizon_ms,
        "split": {"train_end_ns": train_end_ns, "validation_end_ns": validation_end_ns, "embargo_ns": embargo_ns},
        "ridge_penalty": ridge, "block_ns": block_ns, "minimum_clusters": minimum_clusters,
        "bootstrap_draws": bootstrap_draws, "bootstrap_seed": 17,
        "input_identity": dict(zip(("model_sha", "policy_hash", "feature_schema_hash"), next(iter(identities)))),
        "input_rows": len(rows), "ablations": {},
        "excluded_features": {"open_interest_native": "NOT_POOLED_UNTIL_ASSET/VENUE_UNITS_ARE_NORMALIZED"},
    }
    for group, spec in GROUPS.items():
        names = tuple(name for name, _ in spec); units = tuple(unit for _, unit in spec)
        converted = [r for raw in rows if (r := research_row(raw, label_horizon_ms=label_horizon_ms, feature_names=names)) is not None]
        split = chronological_split(converted, train_end_ns=train_end_ns,
                                    validation_end_ns=validation_end_ns, embargo_ns=embargo_ns)
        raw_train = list(split["train"]); raw_validation = list(split["validation"]); raw_test = list(split["test"])
        if not raw_train:
            raise ReplayError("MULTI_CRYPTO_EMPTY_TRAINING_SPLIT:" + group)
        active = tuple(index for index in range(len(names)) if any(row.features[index] is not None for row in raw_train))
        if not active:
            raise ReplayError("MULTI_CRYPTO_NO_TRAINING_FEATURES:" + group)
        active_names = tuple(names[index] for index in active); active_units = tuple(units[index] for index in active)
        def project(row: ResearchRow) -> ResearchRow:
            return replace(row, features=tuple(row.features[index] for index in active),
                           feature_available_ns=tuple(row.feature_available_ns[index] for index in active),
                           feature_source_ids=tuple(row.feature_source_ids[index] for index in active))
        train = [project(row) for row in raw_train]
        validation = [project(row) for row in raw_validation]
        test = [project(row) for row in raw_test]
        model = fit_residual_model(train, feature_names=active_names, units=active_units,
                                   training_cutoff_ns=train_end_ns, ridge_penalty=ridge,
                                   label_horizon_ns=label_horizon_ms * 1_000_000)
        result["ablations"][group] = {
            "model": primitive(model),
            "feature_selection": {"training_only": True, "active": list(active_names),
                                  "dropped_all_missing_in_training": [name for i, name in enumerate(names) if i not in active]},
            "counts": {name: len(split[name]) for name in ("train", "validation", "test", "purged")},
            "validation": metrics(validation, model, names=active_names, units=active_units,
                                  label_horizon_ns=label_horizon_ms * 1_000_000,
                                  block_ns=block_ns, minimum_clusters=minimum_clusters,
                                  bootstrap_draws=bootstrap_draws),
            "test": metrics(test, model, names=active_names, units=active_units,
                            label_horizon_ns=label_horizon_ms * 1_000_000,
                            block_ns=block_ns, minimum_clusters=minimum_clusters,
                            bootstrap_draws=bootstrap_draws),
        }
    result["report_hash"] = digest(result)
    return result


def source_manifest(paths: Iterable[Path]) -> list[dict[str, Any]]:
    output = []
    for path in paths:
        h = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                h.update(chunk)
        output.append({"path": str(path), "sha256": h.hexdigest(), "bytes": Path(path).stat().st_size})
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label-horizon-ms", type=int, required=True)
    parser.add_argument("--train-end-ns", type=int, required=True)
    parser.add_argument("--validation-end-ns", type=int, required=True)
    parser.add_argument("--embargo-ns", type=int, required=True)
    parser.add_argument("--ridge", type=float, required=True)
    parser.add_argument("--block-ns", type=int, default=60_000_000_000)
    parser.add_argument("--minimum-clusters", type=int, default=20)
    parser.add_argument("--bootstrap-draws", type=int, default=2000)
    args = parser.parse_args()
    if args.output.exists() or "ledger" in args.output.parts:
        parser.exit(2, "refusing overwrite or canonical ledger destination\n")
    try:
        report = build(read_rows(args.input), label_horizon_ms=args.label_horizon_ms,
                       train_end_ns=args.train_end_ns, validation_end_ns=args.validation_end_ns,
                       embargo_ns=args.embargo_ns, ridge=args.ridge, block_ns=args.block_ns,
                       minimum_clusters=args.minimum_clusters, bootstrap_draws=args.bootstrap_draws)
        report.pop("report_hash", None)
        report["source_files"] = source_manifest(args.input)
        report["report_hash"] = digest(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True, indent=2, allow_nan=False); handle.write("\n")
        print(json.dumps({"report_hash": report["report_hash"], "economic_evidence": "NOT_PROVEN",
                          "input_rows": report["input_rows"], "groups": list(report["ablations"])}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"multi-crypto residual benchmark failed closed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
