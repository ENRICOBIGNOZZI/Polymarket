#!/usr/bin/env python3
"""Zero-authority unified feature plane for six-asset lead/lag research."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
SCHEMA = "polymarket_v7_multi_crypto_feature_snapshot_v1"
FEATURE_SCHEMA_VERSION = "multi-crypto-causal-features-v1"
FEATURE_SCHEMA = {
    "version": FEATURE_SCHEMA_VERSION,
    "units": {"returns": "bp", "time": "ms_or_seconds_as_named", "probability": "unit_interval"},
    "groups": ["pm_book", "oracle", "settlement_reference", "external_spot", "derivatives", "cross_crypto"],
    "missing_value_policy": "NULL_NEVER_ZERO_IMPUTATION",
    "availability_semantics": "SOURCE_AVAILABLE_AT_OR_BEFORE_DECISION_ONLY",
}
STOP = False


def stop_handler(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


FEATURE_SCHEMA_HASH = canonical_hash(FEATURE_SCHEMA)


def parse_utc_ns(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1e9)
    except ValueError:
        return 0


def validate_policy(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema") != "polymarket_v7_multi_crypto_feature_policy_v1" \
            or value.get("paper_only") is not True \
            or value.get("authenticated_execution") is not False \
            or value.get("real_order_submission") is not False \
            or value.get("execution_authority") is not False \
            or value.get("research_only") is not True \
            or value.get("mode") != "SHADOW_COLLECTION_ONLY_UNCALIBRATED" \
            or value.get("preserve_btc_frozen") is not True:
        raise ValueError("feature policy authority/identity invalid")
    if int(value.get("shock_window_ms") or 0) != 100:
        raise ValueError("only preregistered 100ms research shock is supported")
    half_life = finite(value.get("ewma_half_life_seconds"))
    minimum = int(value.get("minimum_shock_observations") or 0)
    if half_life is None or half_life <= 0 or minimum < 2:
        raise ValueError("invalid causal volatility policy")
    if value.get("sigma_floor_bp") is not None or value.get("trigger_threshold_z") is not None:
        raise ValueError("uncalibrated shadow policy cannot invent floor/threshold")
    return value


def safe_source(value: dict[str, Any]) -> bool:
    return value.get("paper_only") is True \
        and value.get("authenticated_execution") is False \
        and value.get("real_order_submission") is False


def source_age_ms(timestamp_ns: Any, now_ns: int) -> float | None:
    try:
        timestamp = int(timestamp_ns)
    except (TypeError, ValueError, OverflowError):
        return None
    if timestamp <= 0 or timestamp > now_ns:
        return None
    return (now_ns - timestamp) / 1_000_000.0


def bp_field(value: dict[str, Any], key: str) -> float | None:
    number = finite(value.get(key))
    return 10_000.0 * number if number is not None else None


class ShockTracker:
    def __init__(self, *, half_life_seconds: float, minimum_observations: int):
        self.half_life_ns = half_life_seconds * 1e9
        self.minimum_observations = minimum_observations
        self.last_state_version = 0
        self.last_receive_ns = 0
        self.last_return_bp: float | None = None
        self.last_output: dict[str, Any] | None = None
        self.variance_bp2 = 0.0
        self.observations = 0

    def update(self, external: dict[str, Any]) -> dict[str, Any]:
        version = int(external.get("state_version") or 0)
        receive_ns = int(external.get("latest_input_receive_monotonic_ns") or 0)
        return_fraction = finite(external.get("return_100ms"))
        if version <= 0 or receive_ns <= 0 or return_fraction is None:
            return self.snapshot(None)
        return_bp = 10_000.0 * return_fraction
        if version == self.last_state_version:
            if receive_ns != self.last_receive_ns or self.last_return_bp != return_bp:
                return self.snapshot(None)
            return dict(self.last_output) if self.last_output is not None else self.snapshot(None)
        if self.last_state_version > 0 and (version < self.last_state_version or receive_ns <= self.last_receive_ns):
            return self.snapshot(None)
        prior_sigma = math.sqrt(self.variance_bp2) if self.observations >= self.minimum_observations \
            and self.variance_bp2 > 0 else None
        if self.observations == 0:
            self.variance_bp2 = return_bp * return_bp
        else:
            dt_ns = max(1, receive_ns - self.last_receive_ns)
            alpha = 1.0 - math.exp(-math.log(2.0) * dt_ns / self.half_life_ns)
            alpha = max(1e-9, min(1.0, alpha))
            self.variance_bp2 = (1.0 - alpha) * self.variance_bp2 + alpha * return_bp * return_bp
        self.observations += 1
        self.last_state_version = version
        self.last_receive_ns = receive_ns
        self.last_return_bp = return_bp
        shock = return_bp / prior_sigma if prior_sigma and prior_sigma > 0 else None
        output = {
            "return_100ms_bp": return_bp,
            "sigma_100ms_bp_prior": prior_sigma,
            "shock_z_unfloored": shock,
            "observations": self.observations,
            "calibrated": False,
            "signal_eligible": False,
        }
        self.last_output = dict(output)
        return output

    def snapshot(self, return_bp: float | None) -> dict[str, Any]:
        sigma = math.sqrt(self.variance_bp2) if self.observations >= self.minimum_observations \
            and self.variance_bp2 > 0 else None
        return {
            "return_100ms_bp": return_bp,
            "sigma_100ms_bp_prior": sigma,
            "shock_z_unfloored": return_bp / sigma if return_bp is not None and sigma else None,
            "observations": self.observations,
            "calibrated": False,
            "signal_eligible": False,
        }


class FeatureEngine:
    def __init__(self, policy: dict[str, Any]):
        self.policy = validate_policy(policy)
        self.policy_hash = canonical_hash(self.policy)
        self.shocks = {asset: ShockTracker(
            half_life_seconds=float(policy["ewma_half_life_seconds"]),
            minimum_observations=int(policy["minimum_shock_observations"]),
        ) for asset in ASSETS}

    def build(
        self, *, external: dict[str, dict[str, Any]], oracle: dict[str, Any],
        selection: dict[str, Any], book_dir: Path, model_sha: str, now_ns: int,
    ) -> dict[str, Any]:
        if set(external) != set(ASSETS):
            raise ValueError("all six external asset states are required")
        if (not safe_source(oracle) or oracle.get("execution_authority") is not False
                or oracle.get("model_sha") != model_sha):
            raise ValueError("oracle source authority/identity invalid")
        if (selection.get("schema") != "polymarket_v7_multi_crypto_book_selection_v1"
                or not safe_source(selection) or selection.get("execution_authority") is not False
                or selection.get("model_sha") != model_sha):
            raise ValueError("book selection authority/identity invalid")
        maximum_external_age_ms = int(self.policy["maximum_external_age_ms"])
        maximum_oracle_age_ms = int(self.policy["maximum_oracle_age_ms"])
        maximum_book_age_ms = int(self.policy["maximum_book_age_ms"])
        external_features: dict[str, dict[str, Any]] = {}
        for asset in ASSETS:
            value = external[asset]
            if not safe_source(value):
                raise ValueError(f"{asset}: external authority invalid")
            age_ms = source_age_ms(value.get("timestamp_ns"), now_ns)
            source_identity_ok = value.get("code_sha") == model_sha and value.get("asset") == asset
            fresh = bool(source_identity_ok and value.get("valid") is True and age_ms is not None
                         and age_ms <= maximum_external_age_ms)
            shock = self.shocks[asset].update(value) if fresh else self.shocks[asset].snapshot(None)
            external_features[asset] = {
                "source_code_sha": str(value.get("code_sha") or ""),
                "state_version": int(value.get("state_version") or 0),
                "valid": value.get("valid") is True,
                "fresh": fresh,
                "source_age_ms": age_ms,
                "composite_price": finite(value.get("composite_price")) if fresh else None,
                "composite_microprice": finite(value.get("composite_microprice")) if fresh else None,
                "return_50ms_bp": bp_field(value, "return_50ms") if fresh else None,
                "return_100ms_bp": bp_field(value, "return_100ms") if fresh else None,
                "return_250ms_bp": bp_field(value, "return_250ms") if fresh else None,
                "return_1s_bp": bp_field(value, "return_1s") if fresh else None,
                "dispersion_bps": finite(value.get("dispersion_bps")) if fresh else None,
                "aggregate_ofi": finite(value.get("aggregate_ofi")) if fresh else None,
                "aggregate_trade_imbalance": finite(value.get("aggregate_trade_imbalance")) if fresh else None,
                "fresh_venue_count": int(value.get("fresh_venue_count") or 0) if fresh else 0,
                "shock": shock,
                "derivatives": value.get("derivative_contexts") if fresh and isinstance(value.get("derivative_contexts"), list) else [],
            }
        oracle_assets = oracle.get("assets") if isinstance(oracle.get("assets"), dict) else {}
        references = oracle.get("settlement_references") if isinstance(oracle.get("settlement_references"), dict) else {}
        markets = selection.get("markets") if isinstance(selection.get("markets"), list) else []
        rows: list[dict[str, Any]] = []
        for market in markets:
            if not isinstance(market, dict):
                continue
            asset = str(market.get("asset") or "")
            if asset not in external_features:
                continue
            yes_token = str(market.get("yes_token") or "")
            no_token = str(market.get("no_token") or "")
            yes = load(book_dir / f"{yes_token}.json")
            no = load(book_dir / f"{no_token}.json")
            yes_age_ms = source_age_ms(int(yes.get("receive_wall_ms") or 0) * 1_000_000, now_ns)
            no_age_ms = source_age_ms(int(no.get("receive_wall_ms") or 0) * 1_000_000, now_ns)

            def book_identity_valid(book: dict[str, Any], token: str, age_ms: float | None) -> bool:
                return (safe_source(book) and book.get("model_sha") == model_sha
                        and book.get("market_id") == str(market.get("market_id") or "")
                        and book.get("token_id") == token and book.get("valid") is True
                        and book.get("lineage_continuous") is True
                        and book.get("execution_authority") == "ZERO_AUTHORITY_RESEARCH_ONLY"
                        and age_ms is not None and age_ms <= maximum_book_age_ms)

            book_valid = book_identity_valid(yes, yes_token, yes_age_ms) \
                and book_identity_valid(no, no_token, no_age_ms)
            yes_bid, yes_ask = finite(yes.get("best_bid")), finite(yes.get("best_ask"))
            no_bid, no_ask = finite(no.get("best_bid")), finite(no.get("best_ask"))
            yes_mid = (yes_bid + yes_ask) / 2.0 if book_valid and yes_bid is not None and yes_ask is not None else None
            no_mid = (no_bid + no_ask) / 2.0 if book_valid and no_bid is not None and no_ask is not None else None
            oracle_row = oracle_assets.get(asset) if isinstance(oracle_assets.get(asset), dict) else {}
            oracle_age_ms = finite(oracle_row.get("receive_age_ms"))
            oracle_fresh = bool(oracle_row.get("fresh") is True and oracle_age_ms is not None
                                and 0 <= oracle_age_ms <= maximum_oracle_age_ms)
            oracle_price = finite(oracle_row.get("price")) if oracle_fresh else None
            market_id = str(market.get("market_id") or "")
            reference = references.get(market_id) if isinstance(references.get(market_id), dict) else {}
            reference_capture_ms = int(reference.get("captured_at_ms") or 0)
            reference_capture_age_ms = source_age_ms(reference_capture_ms * 1_000_000, now_ns)
            reference_valid = bool(reference.get("valid") is True
                                   and reference.get("market_id") == market_id
                                   and reference.get("asset") == asset
                                   and reference.get("horizon") == str(market.get("horizon") or "")
                                   and reference.get("normalized_rules_hash") == str(market.get("normalized_rules_hash") or "")
                                   and reference_capture_age_ms is not None)
            reference_price = finite(reference.get("price")) if reference_valid else None
            spot = external_features[asset]["composite_price"]
            spot_minus_oracle = 10_000.0 * (spot / oracle_price - 1.0) if spot and oracle_price else None
            distance_reference = 10_000.0 * (oracle_price / reference_price - 1.0) if oracle_price and reference_price else None
            start_ns = parse_utc_ns(market.get("start_timestamp"))
            end_ns = parse_utc_ns(market.get("end_timestamp"))
            tte = max(0.0, (end_ns - now_ns) / 1e9) if end_ns > 0 else None
            active_now = bool(start_ns > 0 and end_ns > start_ns and start_ns <= now_ns < end_ns)
            leader_features: dict[str, Any] = {}
            for leader, follower in self.policy.get("cross_crypto_graph") or []:
                if follower == asset and leader in external_features:
                    leader_features[leader] = {
                        "return_100ms_bp": external_features[leader]["return_100ms_bp"],
                        "shock_z_unfloored": external_features[leader]["shock"]["shock_z_unfloored"],
                        "calibrated": False,
                    }
            derivative_features: list[dict[str, Any]] = []
            for derivative in external_features[asset]["derivatives"]:
                if not isinstance(derivative, dict):
                    continue
                age_ns = int(derivative.get("age_ns") or 0)
                usable = bool(derivative.get("healthy") is True and 0 <= age_ns
                              <= maximum_external_age_ms * 1_000_000
                              and int(derivative.get("valid_mask") or 0) > 0)
                mark = finite(derivative.get("mark_price")) if usable else None
                derivative_features.append({
                    "venue": str(derivative.get("venue") or ""),
                    "healthy": derivative.get("healthy") is True,
                    "usable": usable,
                    "valid_mask": int(derivative.get("valid_mask") or 0),
                    "age_ns": age_ns,
                    "mark_price": mark,
                    "index_price": finite(derivative.get("index_price")) if usable else None,
                    "funding_rate": finite(derivative.get("funding_rate")) if usable else None,
                    "open_interest_native": finite(derivative.get("open_interest_native")) if usable else None,
                    "basis_to_spot_bp": 10_000.0 * (mark / spot - 1.0) if mark and spot else None,
                })
            blockers: list[str] = ["UNCALIBRATED_SHADOW"]
            if not external_features[asset]["fresh"]:
                blockers.append("EXTERNAL_FEED_STALE_OR_IDENTITY_INVALID")
            if not oracle_fresh:
                blockers.append("ORACLE_STALE")
            if not book_valid:
                blockers.append("PM_BOOK_INVALID_OR_STALE")
            if not reference_valid:
                blockers.append("MISSING_OR_MISMATCHED_REFERENCE")
            source_versions = {
                "external_state_version": int(external_features[asset]["state_version"]),
                "external_timestamp_ns": int(external[asset].get("timestamp_ns") or 0),
                "oracle_version": int(oracle_row.get("version") or 0),
                "oracle_source_timestamp_ms": int(oracle_row.get("source_timestamp_ms") or 0),
                "oracle_receive_wall_ns": int(oracle_row.get("receive_wall_ns") or 0),
                "pm_yes_state_version": int(yes.get("state_version") or 0),
                "pm_no_state_version": int(no.get("state_version") or 0),
                "pm_yes_connection_epoch": int(yes.get("connection_epoch") or 0),
                "pm_no_connection_epoch": int(no.get("connection_epoch") or 0),
                "reference_source_timestamp_ms": int(reference.get("source_timestamp_ms") or 0),
                "reference_captured_at_ms": reference_capture_ms,
                "selection_generated_at_ms": int(selection.get("generated_at_ms") or 0),
            }
            availability_candidates = [
                source_versions["external_timestamp_ns"], source_versions["oracle_receive_wall_ns"],
                int(yes.get("receive_wall_ms") or 0) * 1_000_000,
                int(no.get("receive_wall_ms") or 0) * 1_000_000,
                source_versions["reference_captured_at_ms"] * 1_000_000,
                source_versions["selection_generated_at_ms"] * 1_000_000,
            ]
            available_at_ns = max(availability_candidates) if availability_candidates else 0
            if available_at_ns <= 0 or available_at_ns > now_ns:
                blockers.append("FUTURE_OR_UNKNOWN_INPUT_AVAILABILITY")
            source_identity_hash = canonical_hash({
                "model_sha": model_sha, "market_id": market_id,
                "rules_hash": str(market.get("normalized_rules_hash") or ""),
                "source_versions": source_versions,
            })
            rows.append({
                "asset": asset,
                "horizon": str(market.get("horizon") or ""),
                "market_id": str(market.get("market_id") or ""),
                "event_id": str(market.get("event_id") or ""),
                "yes_token": yes_token,
                "no_token": no_token,
                "start_timestamp": str(market.get("start_timestamp") or ""),
                "end_timestamp": str(market.get("end_timestamp") or ""),
                "active_now": active_now,
                "tte_seconds": tte,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "feature_schema_hash": FEATURE_SCHEMA_HASH,
                "source_versions": source_versions,
                "source_identity_hash": source_identity_hash,
                "available_at_ns": available_at_ns,
                "pm_book_valid": book_valid,
                "pm_yes_age_ms": yes_age_ms,
                "pm_no_age_ms": no_age_ms,
                "pm_yes_mid": yes_mid,
                "pm_no_mid": no_mid,
                "pm_complete_set_gap": yes_mid + no_mid - 1.0 if yes_mid is not None and no_mid is not None else None,
                "pm_yes_spread": yes_ask - yes_bid if yes_bid is not None and yes_ask is not None else None,
                "pm_yes_imbalance": finite((yes.get("placement_features") or {}).get("imbalance")) if isinstance(yes.get("placement_features"), dict) else None,
                "oracle_fresh": oracle_fresh,
                "oracle_age_ms": oracle_age_ms,
                "oracle_price": oracle_price,
                "reference_valid": reference_valid,
                "reference_capture_age_ms": reference_capture_age_ms,
                "reference_price": reference_price,
                "distance_to_reference_bp": distance_reference,
                "spot_minus_oracle_bp": spot_minus_oracle,
                "external": external_features[asset],
                "leader_features": leader_features,
                "derivatives": derivative_features,
                "calibration_status": "UNCALIBRATED_SHADOW",
                "signal_eligible": False,
                "blockers": blockers,
            })
        return {
            "schema": SCHEMA,
            "version": 1,
            "timestamp_ns": now_ns,
            "model_sha": model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": False,
            "research_only": True,
            "policy_mode": self.policy["mode"],
            "policy_hash": self.policy_hash,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "feature_schema_hash": FEATURE_SCHEMA_HASH,
            "market_count": len(rows),
            "fresh_external_assets": sum(int(external_features[a]["fresh"]) for a in ASSETS),
            "ready_for_calibration_markets": sum(
                int(row["blockers"] == ["UNCALIBRATED_SHADOW"]) for row in rows
            ),
            "markets": rows,
        }


def parse_external(values: list[str]) -> dict[str, Path]:
    output: dict[str, Path] = {}
    for raw in values:
        asset, sep, path = raw.partition("=")
        if not sep or asset not in ASSETS or not path or asset in output:
            raise ValueError("--external requires unique ASSET=/path for six assets")
        output[asset] = Path(path)
    if set(output) != set(ASSETS):
        raise ValueError("all six --external sources are required")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=Path("config/v7_multi_crypto_feature_policy.json"))
    parser.add_argument("--external", action="append", default=[])
    parser.add_argument("--oracle-status", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--book-features-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-ms", type=int, default=50)
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise ValueError("exact model SHA required")
    if not 10 <= args.interval_ms <= 5000:
        raise ValueError("interval-ms out of range")
    paths = parse_external(args.external)
    engine = FeatureEngine(validate_policy(load(args.policy)))
    signal.signal(signal.SIGINT, stop_handler); signal.signal(signal.SIGTERM, stop_handler)
    while True:
        value = engine.build(
            external={asset: load(path) for asset, path in paths.items()},
            oracle=load(args.oracle_status), selection=load(args.selection),
            book_dir=args.book_features_dir, model_sha=args.model_sha, now_ns=time.time_ns())
        atomic_json(args.output, value)
        if not args.loop or STOP:
            break
        time.sleep(args.interval_ms / 1000.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
