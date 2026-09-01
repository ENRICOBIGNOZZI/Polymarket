#!/usr/bin/env python3
"""Typed multi-asset crypto-settlement contexts and fail-closed bindings."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


REGISTRY_SCHEMA = "polymarket_v7_crypto_settlement_market_registry_v1"
MODEL_SCHEMA = "polymarket_v7_crypto_settlement_model_v1"
MODEL_REGISTRY_SCHEMA = "polymarket_v7_crypto_settlement_model_registry_v1"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
HASH64 = re.compile(r"^[0-9a-f]{64}$")


class CryptoSettlementError(ValueError):
    pass


class CryptoAsset(str, Enum):
    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    XRP = "XRP"


class CryptoHorizon(str, Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"


HORIZON_SECONDS = {
    CryptoHorizon.M1: 60,
    CryptoHorizon.M5: 300,
    CryptoHorizon.M15: 900,
    CryptoHorizon.H1: 3_600,
    CryptoHorizon.H4: 14_400,
}


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def settlement_semantic_payload(row: dict[str, Any]) -> dict[str, Any]:
    settlement = row.get("settlement") or {}
    return {
        "asset": row.get("asset"),
        "reference_pair": settlement.get("reference_pair"),
        "oracle_source": settlement.get("oracle_source"),
        "settlement_window_seconds": settlement.get("settlement_window_seconds"),
        "timestamp_semantics": settlement.get("timestamp_semantics"),
        "comparison_operator": settlement.get("comparison_operator"),
        "rounding_rules": settlement.get("rounding_rules"),
        "boundary_behavior": settlement.get("boundary_behavior"),
        "fallback_resolution": settlement.get("fallback_resolution"),
    }


@dataclass(frozen=True)
class CryptoSettlementContext:
    asset: CryptoAsset
    horizon: CryptoHorizon
    horizon_seconds: int
    contract_family: str
    slug_template: str
    settlement_semantic_hash: str
    authority: str
    research_only: bool
    enabled: bool
    raw: dict[str, Any]

    @property
    def context_id(self) -> str:
        return f"{self.asset.value}_{self.horizon.value}"


def validate_registry(value: dict[str, Any]) -> dict[tuple[CryptoAsset, CryptoHorizon], CryptoSettlementContext]:
    if (
        value.get("schema") != REGISTRY_SCHEMA
        or value.get("version") != 1
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("real_capital_at_risk") is not False
        or value.get("automatic_discovery_execution") is not False
        or value.get("decision_owner") != "CRYPTO_SETTLEMENT_ENGINE"
    ):
        raise CryptoSettlementError("registry_identity_or_safety")
    supported_assets = set(value.get("supported_assets") or [])
    supported_horizons = set(value.get("supported_horizons") or [])
    if supported_assets != {asset.value for asset in CryptoAsset}:
        raise CryptoSettlementError("supported_assets")
    if supported_horizons != {horizon.value for horizon in CryptoHorizon}:
        raise CryptoSettlementError("supported_horizons")
    rows = value.get("contexts")
    if not isinstance(rows, list) or not rows:
        raise CryptoSettlementError("contexts")
    contexts: dict[tuple[CryptoAsset, CryptoHorizon], CryptoSettlementContext] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CryptoSettlementError("context_row")
        try:
            asset = CryptoAsset(row["asset"])
            horizon = CryptoHorizon(row["horizon"])
        except (KeyError, ValueError) as exc:
            raise CryptoSettlementError("context_identity") from exc
        key = (asset, horizon)
        if key in contexts or int(row.get("horizon_seconds") or 0) != HORIZON_SECONDS[horizon]:
            raise CryptoSettlementError("context_unique_or_duration")
        settlement = row.get("settlement") or {}
        external = row.get("external_symbols") or {}
        mapping = row.get("polymarket") or {}
        horizon_slug = {
            CryptoHorizon.M1: "1m", CryptoHorizon.M5: "5m",
            CryptoHorizon.M15: "15m", CryptoHorizon.H1: "1h", CryptoHorizon.H4: "4h",
        }[horizon]
        observed_slug = str(mapping.get("observed_live_slug") or "")
        maker_window = row.get("maker_tte_window_seconds")
        taker_window = row.get("taker_tte_window_seconds")
        if (
            row.get("market_mapping_verified") is not True
            or row.get("settlement_mapping_verified") is not True
            or settlement.get("comparison_operator") != "GREATER_THAN_OR_EQUAL"
            or settlement.get("boundary_behavior") != "EQUAL_IS_UP"
            or settlement.get("fallback_resolution") != "NONE_FAIL_CLOSED"
            or settlement.get("settlement_window_seconds") != 60
            or settlement.get("oracle_source") != "CHAINLINK_DATA_STREAM"
            or settlement.get("reference_pair") != f"{asset.value}/USD"
            or not str(settlement.get("stream_url") or "").startswith("https://data.chain.link/streams/")
            or mapping.get("slug_template") != f"{asset.value.lower()}-updown-{{horizon_slug}}-{{window_start_unix}}"
            or mapping.get("horizon_slug") != horizon_slug
            or not re.fullmatch(
                rf"{asset.value.lower()}-updown-{re.escape(horizon_slug)}-[0-9]+", observed_slug,
            )
            or mapping.get("verification_source") != "https://gamma-api.polymarket.com"
            or not str(mapping.get("verified_at_utc") or "").endswith("Z")
            or not isinstance(maker_window, list) or len(maker_window) != 2
            or not isinstance(taker_window, list) or len(taker_window) != 2
            or not 0 <= int(maker_window[0]) <= int(maker_window[1]) <= HORIZON_SECONDS[horizon]
            or not 0 <= int(taker_window[0]) <= int(taker_window[1]) <= HORIZON_SECONDS[horizon]
            or not all(external.get(name) for name in ("binance_spot", "coinbase_spot", "bybit_spot", "binance_perp", "bybit_perp"))
        ):
            raise CryptoSettlementError(f"context_mapping:{asset.value}:{horizon.value}")
        semantic_hash = canonical_hash(settlement_semantic_payload(row))
        if row.get("settlement_semantic_hash") != semantic_hash:
            raise CryptoSettlementError(f"settlement_semantic_hash:{asset.value}:{horizon.value}")
        research_only = row.get("research_only") is True
        authority = row.get("authority")
        if asset is not CryptoAsset.BTC and (
            not research_only or authority != "SHADOW_ZERO_AUTHORITY"
        ):
            raise CryptoSettlementError("non_btc_must_start_shadow_zero_authority")
        contexts[key] = CryptoSettlementContext(
            asset=asset, horizon=horizon, horizon_seconds=HORIZON_SECONDS[horizon],
            contract_family=str(row.get("contract_family") or ""),
            slug_template=str(mapping["slug_template"]),
            settlement_semantic_hash=semantic_hash, authority=str(authority),
            research_only=research_only, enabled=row.get("enabled") is True,
            raw=json.loads(json.dumps(row, sort_keys=True)),
        )
    return contexts


def load_registry(path: Path) -> dict[tuple[CryptoAsset, CryptoHorizon], CryptoSettlementContext]:
    return validate_registry(json.loads(path.read_text(encoding="utf-8")))


def require_context(
    contexts: dict[tuple[CryptoAsset, CryptoHorizon], CryptoSettlementContext],
    asset: str, horizon: str,
) -> CryptoSettlementContext:
    try:
        context = contexts[(CryptoAsset(asset), CryptoHorizon(horizon))]
    except (KeyError, ValueError) as exc:
        raise CryptoSettlementError("unknown_or_unverified_crypto_context") from exc
    if not context.enabled:
        raise CryptoSettlementError("crypto_context_disabled")
    return context


def validate_model_artifact(model: dict[str, Any], context: CryptoSettlementContext) -> dict[str, Any]:
    required_hashes = (
        "feature_schema_hash", "settlement_semantic_hash", "source_registry_hash",
        "latency_profile_hash",
    )
    if model.get("schema") != MODEL_SCHEMA or model.get("immutable") is not True:
        raise CryptoSettlementError("model_identity")
    if model.get("asset") != context.asset.value or model.get("horizon") != context.horizon.value:
        raise CryptoSettlementError("model_context_mismatch")
    if model.get("settlement_semantic_hash") != context.settlement_semantic_hash:
        raise CryptoSettlementError("model_settlement_semantics_mismatch")
    if not SHA40.fullmatch(str(model.get("model_sha") or "")):
        raise CryptoSettlementError("model_sha")
    if any(not HASH64.fullmatch(str(model.get(name) or "")) for name in required_hashes):
        raise CryptoSettlementError("model_hash_binding")
    if not all(str(model.get(name) or "") for name in (
        "model_sha", "training_cut", "training_period", "validation_period",
        "calibration_artifact", "forward_shadow_start",
    )):
        raise CryptoSettlementError("model_provenance")
    return json.loads(json.dumps(model, sort_keys=True))


def validate_model_registry(
    value: dict[str, Any],
    contexts: dict[tuple[CryptoAsset, CryptoHorizon], CryptoSettlementContext],
) -> dict[tuple[CryptoAsset, CryptoHorizon], dict[str, Any] | None]:
    if (
        value.get("schema") != MODEL_REGISTRY_SCHEMA
        or value.get("version") != 1
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("automatic_promotion") is not False
    ):
        raise CryptoSettlementError("model_registry_identity_or_safety")
    rows = value.get("models")
    if not isinstance(rows, list) or len(rows) != len(contexts):
        raise CryptoSettlementError("model_registry_partition")
    output: dict[tuple[CryptoAsset, CryptoHorizon], dict[str, Any] | None] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CryptoSettlementError("model_registry_row")
        try:
            key = (CryptoAsset(row["asset"]), CryptoHorizon(row["horizon"]))
            context = contexts[key]
        except (KeyError, ValueError) as exc:
            raise CryptoSettlementError("model_registry_context") from exc
        if key in output or row.get("settlement_semantic_hash") != context.settlement_semantic_hash:
            raise CryptoSettlementError("model_registry_context_binding")
        artifact = row.get("artifact")
        if artifact is None:
            if row.get("status") != "UNREGISTERED_SHADOW" or row.get("new_risk_authorized") is not False:
                raise CryptoSettlementError("unregistered_model_authority")
            output[key] = None
            continue
        validated = validate_model_artifact(artifact, context)
        if row.get("status") != "FROZEN" or row.get("new_risk_authorized") is not False:
            raise CryptoSettlementError("model_registry_promotion_forbidden")
        output[key] = validated
    if set(output) != set(contexts):
        raise CryptoSettlementError("model_registry_partition")
    return output


def _causal_frame(
    value: dict[str, Any] | None, *, expected_asset: str,
    decision_receive_timestamp_ns: int, group: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "asset", "source_id", "receive_timestamp_ns", "features",
    }:
        raise CryptoSettlementError(f"feature_frame:{group}")
    receive_ns = value.get("receive_timestamp_ns")
    if (
        value.get("asset") != expected_asset
        or isinstance(receive_ns, bool)
        or not isinstance(receive_ns, int)
        or receive_ns <= 0
        or receive_ns > decision_receive_timestamp_ns
        or not isinstance(value.get("source_id"), str)
        or not value["source_id"]
        or not isinstance(value.get("features"), dict)
    ):
        raise CryptoSettlementError(f"feature_identity_or_causality:{group}")
    for name, raw in value["features"].items():
        if not isinstance(name, str) or not name or isinstance(raw, bool):
            raise CryptoSettlementError(f"feature_value:{group}")
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CryptoSettlementError(f"feature_value:{group}") from exc
        if not math.isfinite(number):
            raise CryptoSettlementError(f"feature_value:{group}")
    return json.loads(json.dumps(value, sort_keys=True))


def assemble_causal_feature_groups(
    context: CryptoSettlementContext, *, decision_receive_timestamp_ns: int,
    local: dict[str, Any], oracle: dict[str, Any], polymarket: dict[str, Any],
    btc_cross: dict[str, Any] | None = None,
    crypto_wide: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one receive-time-causal feature cut without asset leakage."""
    if isinstance(decision_receive_timestamp_ns, bool) or decision_receive_timestamp_ns <= 0:
        raise CryptoSettlementError("decision_receive_timestamp")
    groups = {
        "local": _causal_frame(
            local, expected_asset=context.asset.value,
            decision_receive_timestamp_ns=decision_receive_timestamp_ns, group="local",
        ),
        "oracle": _causal_frame(
            oracle, expected_asset=context.asset.value,
            decision_receive_timestamp_ns=decision_receive_timestamp_ns, group="oracle",
        ),
        "polymarket": _causal_frame(
            polymarket, expected_asset=context.asset.value,
            decision_receive_timestamp_ns=decision_receive_timestamp_ns, group="polymarket",
        ),
        "btc_cross": _causal_frame(
            btc_cross, expected_asset="BTC",
            decision_receive_timestamp_ns=decision_receive_timestamp_ns, group="btc_cross",
        ),
        "crypto_wide": _causal_frame(
            crypto_wide, expected_asset="CRYPTO_WIDE",
            decision_receive_timestamp_ns=decision_receive_timestamp_ns, group="crypto_wide",
        ),
    }
    return {
        "asset": context.asset.value,
        "horizon": context.horizon.value,
        "settlement_semantic_hash": context.settlement_semantic_hash,
        "decision_receive_timestamp_ns": decision_receive_timestamp_ns,
        "groups": groups,
    }


def healthy_source_composite(
    context: CryptoSettlementContext, sources: list[dict[str, Any]], *,
    decision_receive_timestamp_ns: int, maximum_age_ns: int,
) -> dict[str, Any]:
    """Drop stale/unhealthy local venues and renormalize context-specific weights."""
    if maximum_age_ns <= 0 or decision_receive_timestamp_ns <= 0:
        raise CryptoSettlementError("composite_clock")
    healthy: list[tuple[str, float, float, int]] = []
    dropped: dict[str, str] = {}
    for row in sources:
        if not isinstance(row, dict):
            raise CryptoSettlementError("composite_source")
        source_id = str(row.get("source_id") or "")
        if not source_id or source_id in dropped or any(source_id == old[0] for old in healthy):
            raise CryptoSettlementError("composite_source_identity")
        if row.get("asset") != context.asset.value:
            raise CryptoSettlementError("composite_asset_mismatch")
        receive_ns = row.get("receive_timestamp_ns")
        if isinstance(receive_ns, bool) or not isinstance(receive_ns, int) or receive_ns <= 0 \
                or receive_ns > decision_receive_timestamp_ns:
            raise CryptoSettlementError("composite_causality")
        if row.get("healthy") is not True:
            dropped[source_id] = "UNHEALTHY"
            continue
        if decision_receive_timestamp_ns - receive_ns > maximum_age_ns:
            dropped[source_id] = "STALE"
            continue
        try:
            price, weight = float(row["price"]), float(row["weight"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise CryptoSettlementError("composite_price_or_weight") from exc
        if not math.isfinite(price) or price <= 0.0 or not math.isfinite(weight) or weight < 0.0:
            raise CryptoSettlementError("composite_price_or_weight")
        healthy.append((source_id, price, weight, receive_ns))
    total_weight = sum(row[2] for row in healthy)
    if not healthy or total_weight <= 0.0:
        raise CryptoSettlementError("no_healthy_crypto_sources")
    normalized = {source_id: weight / total_weight for source_id, _, weight, _ in healthy}
    price = sum(price * normalized[source_id] for source_id, price, _, _ in healthy)
    return {
        "asset": context.asset.value,
        "horizon": context.horizon.value,
        "price": price,
        "weights": dict(sorted(normalized.items())),
        "healthy_source_ids": sorted(normalized),
        "dropped_sources": dict(sorted(dropped.items())),
        "decision_receive_timestamp_ns": decision_receive_timestamp_ns,
    }


def aggregate_correlated_crypto_risk(exposures: list[dict[str, Any]]) -> dict[str, Any]:
    per_asset: dict[str, float] = {}
    per_horizon: dict[str, float] = {}
    gross = 0.0
    net = 0.0
    oracle_gross: dict[str, float] = {}
    venue_gross: dict[str, float] = {}
    for row in exposures:
        asset = CryptoAsset(str(row.get("asset"))).value
        horizon = CryptoHorizon(str(row.get("horizon"))).value
        signed = float(row.get("signed_exposure_usd") or 0.0)
        gross += abs(signed)
        net += signed
        per_asset[asset] = per_asset.get(asset, 0.0) + signed
        per_horizon[horizon] = per_horizon.get(horizon, 0.0) + signed
        oracle = str(row.get("oracle_source") or "UNKNOWN")
        venue = str(row.get("exchange_source") or "UNKNOWN")
        oracle_gross[oracle] = oracle_gross.get(oracle, 0.0) + abs(signed)
        venue_gross[venue] = venue_gross.get(venue, 0.0) + abs(signed)
    return {
        "gross_crypto_exposure_usd": gross,
        "net_directional_crypto_exposure_usd": net,
        "correlated_crypto_cluster_exposure_usd": abs(net),
        "per_asset_exposure_usd": dict(sorted(per_asset.items())),
        "per_horizon_exposure_usd": dict(sorted(per_horizon.items())),
        "oracle_gross_exposure_usd": dict(sorted(oracle_gross.items())),
        "exchange_source_gross_exposure_usd": dict(sorted(venue_gross.items())),
        "oracle_concentration_fraction": max(oracle_gross.values(), default=0.0) / gross if gross else 0.0,
        "exchange_source_concentration_fraction": max(venue_gross.values(), default=0.0) / gross if gross else 0.0,
        "independent_asset_diversification_credit_usd": 0.0,
    }
