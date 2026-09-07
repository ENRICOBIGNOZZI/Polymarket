#!/usr/bin/env python3
"""Build typed CRYPTO_SETTLEMENT_ENGINE MAKE proposals from canonical maker evidence.

This is a proposal bridge, not an OMS.  It reads the current reward/flow
selection, the durable maker execution model, the exact runtime identity and the
same settlement-aware fair snapshot used by the taker.  It emits only typed
OpportunityEnvelope dictionaries.  The global coordinator remains the sole
decision owner and the canonical execution chain remains the sole state owner.

No ordinary MAKE proposal is emitted from a cold-start point estimate alone.
The conservative EV uses a Wilson lower bound on fill-event probability, the
settlement fair lower/upper bound for the quoted outcome, and explicit adverse
selection/latency/unwind buffers.  Immature evidence can still be reported in
diagnostics but cannot manufacture positive conservative EV.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from v7_crypto_settlement import load_registry as load_crypto_registry, require_context
from v7_opportunity import OpportunityEnvelope, OpportunityError


BRIDGE_SCHEMA = "polymarket_v7_maker_opportunity_bridge_v1"
MODEL_SCHEMA = "polymarket_v7_maker_execution_model_v1"
STATUS_SCHEMA = "polymarket_v7_external_fair_status_v1"
RUNTIME_SCHEMA = "polymarket_v7_runtime_status_v3"


class MakerBridgeError(ValueError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _finite(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def wilson_lower(successes: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0 or successes <= 0:
        return 0.0
    n = float(trials)
    p = min(1.0, max(0.0, float(min(successes, trials)) / n))
    z2 = z * z
    center = p + z2 / (2.0 * n)
    radius = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    return _clamp((center - radius) / (1.0 + z2 / n))


def _group_key(action: str, outcome: str, side: str) -> str:
    return f"{action.upper()}:{outcome.upper()}:{side.upper()}"


def _quote_price(opportunity: dict[str, Any], action: str) -> float | None:
    bid = _finite(opportunity.get("best_bid"))
    ask = _finite(opportunity.get("best_ask"))
    tick = max(1e-6, _finite(opportunity.get("tick_size"), 0.01) or 0.01)
    side = str(opportunity.get("quote_side") or "").upper()
    if bid is None or ask is None or not 0.0 < bid < ask < 1.0:
        return None
    if side == "BUY":
        if action == "JOIN":
            return bid
        if action == "IMPROVE1" and ask - bid >= 2.0 * tick - 1e-12:
            return min(ask - tick, bid + tick)
    elif side == "SELL":
        if action == "JOIN":
            return ask
        if action == "IMPROVE1" and ask - bid >= 2.0 * tick - 1e-12:
            return max(bid + tick, ask - tick)
    return None


def _outcome_fair(fair: dict[str, Any], outcome: str) -> tuple[float, float, float] | None:
    yes = _finite(fair.get("yes"))
    lower = _finite(fair.get("lower"))
    upper = _finite(fair.get("upper"))
    if yes is None or lower is None or upper is None or not 0.0 <= lower <= yes <= upper <= 1.0:
        return None
    if outcome == "YES":
        return lower, yes, upper
    if outcome == "NO":
        return 1.0 - upper, 1.0 - yes, 1.0 - lower
    return None


def _edge_bounds(side: str, price: float, fair: tuple[float, float, float]) -> tuple[float, float, float]:
    lower, point, upper = fair
    if side == "BUY":
        return lower - price, point - price, upper - price
    if side == "SELL":
        return price - upper, price - point, price - lower
    raise MakerBridgeError("quote_side")


def _selection_lookup(selection: dict[str, Any], yes_token: str, no_token: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for market in selection.get("markets") if isinstance(selection.get("markets"), list) else []:
        if not isinstance(market, dict):
            continue
        tokens = {str(market.get("yes_token") or ""), str(market.get("no_token") or "")}
        if not ({yes_token, no_token} & tokens):
            continue
        by_identity = {
            (str(row.get("token_id") or ""), str(row.get("outcome") or "").upper(), str(row.get("quote_side") or "").upper()): row
            for row in market.get("quote_opportunities") if isinstance(market.get("quote_opportunities"), list) and isinstance(row, dict)
        }
        for cell in market.get("authorized_execution_cells") if isinstance(market.get("authorized_execution_cells"), list) else []:
            if not isinstance(cell, dict):
                continue
            key = (
                str(cell.get("token_id") or ""),
                str(cell.get("outcome") or "").upper(),
                str(cell.get("quote_side") or "").upper(),
            )
            opportunity = by_identity.get(key)
            if opportunity is not None:
                rows.append((market, cell | {"quote_opportunity": opportunity}))
    return rows


def _model_group(model: dict[str, Any], action: str, outcome: str, side: str) -> dict[str, Any]:
    groups = model.get("groups") if isinstance(model.get("groups"), dict) else {}
    value = groups.get(_group_key(action, outcome, side))
    if isinstance(value, dict):
        return value
    global_group = groups.get("GLOBAL")
    return global_group if isinstance(global_group, dict) else {}


def _fill_band(cell: dict[str, Any], opportunity: dict[str, Any], group: dict[str, Any]) -> tuple[float, float, float, str]:
    action = str(cell.get("action") or "").upper()
    projected_key = (
        "projected_improve1_fill_probability" if action == "IMPROVE1"
        else "projected_join_fill_probability"
    )
    projected = _clamp(_finite(opportunity.get(projected_key), 0.0) or 0.0)
    learned = _clamp(_finite(group.get("fill_probability"), projected) or projected)
    point = min(projected, learned) if projected > 0.0 and learned > 0.0 else max(projected, learned)
    orders = int(_finite(group.get("orders"), 0.0) or 0.0)
    filled_orders = int(_finite(group.get("filled_orders"), _finite(group.get("fills"), 0.0)) or 0.0)
    mature = group.get("mature") is True and orders >= 50 and filled_orders >= 20
    lower = min(point, wilson_lower(filled_orders, orders)) if mature else 0.0
    upper = _clamp(max(point, projected, learned))
    return lower, point, upper, "MATURE" if mature else "IMMATURE"


def _maker_cost_buffers(policy: dict[str, Any], group: dict[str, Any]) -> tuple[float, float, float]:
    execution = policy.get("execution_model") if isinstance(policy.get("execution_model"), dict) else {}
    cold_markout = max(0.0, _finite(execution.get("cold_start_adverse_markout_per_share"), 0.002) or 0.002)
    learned_markout = max(0.0, _finite(group.get("adverse_markout_per_share"), cold_markout) or cold_markout)
    adverse = max(cold_markout, learned_markout)
    latency = max(0.00025, 0.5 * cold_markout)
    unwind = max(0.0005, cold_markout)
    return adverse, latency, unwind


def _feature_packet(
    *, opportunity: dict[str, Any], fair_status: dict[str, Any], fill_band: tuple[float, float, float, str],
    group: dict[str, Any], action_ev_point: float, action_ev_conservative: float,
    decision_ns: int, feature_receive_ns: int, model_hash: str,
) -> dict[str, Any]:
    lower_fill, point_fill, upper_fill, evidence_status = fill_band
    external = fair_status.get("external") if isinstance(fair_status.get("external"), dict) else {}
    fair = fair_status.get("fair") if isinstance(fair_status.get("fair"), dict) else {}
    adverse = max(0.0, _finite(group.get("adverse_markout_per_share"), 0.002) or 0.002)
    spread = max(0.0, _finite(opportunity.get("best_ask"), 0.0) - _finite(opportunity.get("best_bid"), 0.0))
    aggressive = _finite(opportunity.get("opposite_flow_shares_per_second"))
    tte = _finite(fair.get("tte_seconds"))
    volatility = _finite(external.get("realized_vol_fast"))
    return {
        "schema": "polymarket_v7_execution_alpha_packet_v1",
        "model_id": "maker_execution_alpha_bridge_v1",
        "model_hash": model_hash,
        "feature_receive_timestamp_ns": feature_receive_ns,
        "features": {
            "queue_ahead": _finite(opportunity.get("queue_ahead_shares")),
            "spread": spread,
            "book_imbalance": None,
            "recent_aggressive_flow": aggressive,
            "quote_lifetime_ms": 0.0,
            "tte_seconds": tte,
            "binance_shock_bp": None,
            "coinbase_shock_bp": None,
            "bybit_shock_bp": None,
            "cross_venue_disagreement_bp": _finite(external.get("dispersion_bps")),
            "oracle_distance_bp": None,
            "volatility_bp": volatility,
            "latency_ms": _finite(opportunity.get("last_opposite_flow_age_ms"), 0.0),
        },
        "fill_probability": {"lower": lower_fill, "point": point_fill, "upper": upper_fill},
        "markout_per_share": {"lower": -max(0.01, 2.0 * adverse), "point": -adverse, "upper": 0.0},
        "toxic_fill_probability": {
            "lower": 0.0,
            "point": _clamp(adverse / max(0.001, spread + adverse)),
            "upper": 1.0,
        },
        "action_ev": {
            "MAKE": {"point": action_ev_point, "conservative": action_ev_conservative},
            "TAKE": {"point": 0.0, "conservative": 0.0},
            "CANCEL": {"point": 0.0, "conservative": 0.0},
            "NOTHING": {"point": 0.0, "conservative": 0.0},
        },
        "selected_action": "MAKE",
        "evidence_status": evidence_status,
    }


def build_maker_opportunities(
    run_root: Path,
    *,
    now_ns: int | None = None,
    repository_root: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = Path(run_root)
    repo = Path(repository_root) if repository_root is not None else Path(__file__).resolve().parents[1]
    decision_ns = int(now_ns if now_ns is not None else time.time_ns())
    runtime = _load(root / "control" / "runtime_status.json")
    selection = _load(root / "micro_maker" / "reward_selection.json")
    model = _load(root / "micro_maker" / "execution_model.json")
    fair_status = _load(root / "external_fair" / "status.json")
    policy = _load(repo / "config" / "v7_professional_market_maker.json")
    registry = load_crypto_registry(repo / "config" / "v7_crypto_settlement_markets.json")

    reasons: list[str] = []
    model_sha = str(runtime.get("model_sha") or "")
    if (
        runtime.get("schema") != RUNTIME_SCHEMA
        or runtime.get("paper_only") is not True
        or runtime.get("authenticated_execution") is not False
        or runtime.get("real_order_submission") is not False
        or len(model_sha) != 40
    ):
        reasons.append("RUNTIME_IDENTITY_NOT_READY")
    if (
        selection.get("paper_only") is not True
        or selection.get("authenticated_execution") is not False
        or selection.get("real_order_submission") is not False
        or selection.get("model_sha") != model_sha
        or not isinstance(selection.get("markets"), list)
    ):
        reasons.append("MAKER_SELECTION_NOT_READY")
    selection_ts_ms = int(_finite(selection.get("timestamp_ms"), 0.0) or 0.0)
    decision_ms = decision_ns // 1_000_000
    refresh_seconds = max(1.0, _finite(
        ((policy.get("market_selection") or {}).get("recent_flow") or {}).get(
            "selector_refresh_seconds"), 5.0
    ) or 5.0)
    selection_max_age_ms = int(max(5_000.0, 3_000.0 * refresh_seconds))
    if (
        selection_ts_ms <= 0
        or selection_ts_ms > decision_ms
        or decision_ms - selection_ts_ms > selection_max_age_ms
    ):
        reasons.append("MAKER_SELECTION_STALE_OR_NONCAUSAL")
    if (
        model.get("schema") != MODEL_SCHEMA
        or model.get("paper_only") is not True
        or model.get("authenticated_execution") is not False
        or model.get("real_order_submission") is not False
        or model.get("model_sha") != model_sha
    ):
        reasons.append("MAKER_EXECUTION_MODEL_NOT_READY")
    contract = fair_status.get("contract") if isinstance(fair_status.get("contract"), dict) else {}
    reference = fair_status.get("settlement_reference") if isinstance(fair_status.get("settlement_reference"), dict) else {}
    oracle = fair_status.get("oracle") if isinstance(fair_status.get("oracle"), dict) else {}
    external = fair_status.get("external") if isinstance(fair_status.get("external"), dict) else {}
    fair = fair_status.get("fair") if isinstance(fair_status.get("fair"), dict) else {}
    market_status = fair_status.get("market") if isinstance(fair_status.get("market"), dict) else {}
    if (
        fair_status.get("schema") != STATUS_SCHEMA
        or fair_status.get("paper_only") is not True
        or fair_status.get("authenticated_execution") is not False
        or fair_status.get("real_order_submission") is not False
        or fair_status.get("code_sha") != model_sha
        or fair_status.get("state") != "FULL_FAIR_SHADOW_OPERATIONAL"
        or contract.get("verified") is not True
        or contract.get("rules_hash_recognized") is not True
        or reference.get("valid") is not True
        or oracle.get("healthy") is not True
        or external.get("healthy") is not True
        or fair.get("valid") is not True
        or fair.get("explicit_champion_applied") is not True
    ):
        reasons.append("SETTLEMENT_FAIR_NOT_MATURE_OR_VERIFIED")
    if reasons:
        return [], {
            "schema": BRIDGE_SCHEMA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "state": "FAIL_CLOSED",
            "reasons": reasons,
            "candidate_cells": 0,
            "typed_make_opportunities": 0,
        }

    try:
        context = require_context(
            asset="BTC", horizon="M5", at_unix_ms=decision_ns // 1_000_000,
            registry=registry,
        )
    except Exception as exc:
        return [], {
            "schema": BRIDGE_SCHEMA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "state": "FAIL_CLOSED",
            "reasons": [f"CRYPTO_SETTLEMENT_CONTEXT_INVALID:{type(exc).__name__}:{exc}"],
            "candidate_cells": 0,
            "typed_make_opportunities": 0,
        }
    context = dict(context)
    context["authority"] = "PAPER_EXPLORATION"
    context["research_only"] = False

    semantic_hash = str(context.get("settlement_semantic_hash") or "")
    fair_semantic = str(fair.get("settlement_semantic_hash") or semantic_hash)
    if fair_semantic != semantic_hash:
        return [], {
            "schema": BRIDGE_SCHEMA, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "state": "FAIL_CLOSED", "reasons": ["SETTLEMENT_SEMANTIC_HASH_MISMATCH"],
            "candidate_cells": 0, "typed_make_opportunities": 0,
        }

    yes_token = str(market_status.get("yes_token") or "")
    no_token = str(market_status.get("no_token") or "")
    cells = _selection_lookup(selection, yes_token, no_token)
    model_hash = _sha256(model)
    output: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}

    for market, cell in cells:
        opportunity = cell.get("quote_opportunity") if isinstance(cell.get("quote_opportunity"), dict) else {}
        action = str(cell.get("action") or "").upper()
        outcome = str(cell.get("outcome") or "").upper()
        side = str(cell.get("quote_side") or "").upper()
        token = str(cell.get("token_id") or "")
        if side != "BUY":
            rejected["SELL_REQUIRES_CANONICAL_INVENTORY_BRIDGE"] = rejected.get(
                "SELL_REQUIRES_CANONICAL_INVENTORY_BRIDGE", 0
            ) + 1
            continue
        price = _quote_price(opportunity, action)
        fair_triplet = _outcome_fair(fair, outcome)
        if action not in {"JOIN", "IMPROVE1"} or side != "BUY" or price is None or fair_triplet is None:
            rejected["INVALID_QUOTE_CELL"] = rejected.get("INVALID_QUOTE_CELL", 0) + 1
            continue
        group = _model_group(model, action, outcome, side)
        fill_band = _fill_band(cell, opportunity, group)
        fill_lower, fill_point, _fill_upper, evidence_status = fill_band
        edge_lower, edge_point, _edge_upper = _edge_bounds(side, price, fair_triplet)
        adverse, latency_buffer, unwind_buffer = _maker_cost_buffers(policy, group)
        point_per_fill = edge_point - adverse
        conservative_per_fill = edge_lower - adverse - latency_buffer - unwind_buffer

        max_size = max(0.0, _finite(cell.get("maximum_quote_shares"), 0.0) or 0.0)
        configured_size = max(
            1.0,
            _finite(((policy.get("market_selection") or {}).get("recent_flow") or {}).get("selection_quote_shares"), 5.0) or 5.0,
        )
        size = min(configured_size, max_size) if max_size > 0.0 else configured_size
        # Proposal size is PAPER evidence size and cannot exceed the configured
        # low-sample quote. Capital/risk/OMS remain downstream owners.
        size = max(1e-6, min(size, configured_size))
        point_ev = fill_point * point_per_fill * size
        conservative_ev = fill_lower * conservative_per_fill * size
        if not math.isfinite(point_ev) or not math.isfinite(conservative_ev):
            rejected["NONFINITE_ECONOMICS"] = rejected.get("NONFINITE_ECONOMICS", 0) + 1
            continue
        if point_ev <= 0.0:
            rejected["NONPOSITIVE_POINT_EV"] = rejected.get("NONPOSITIVE_POINT_EV", 0) + 1
            continue
        if evidence_status != "MATURE" or conservative_ev <= 0.0:
            rejected["INSUFFICIENT_CONSERVATIVE_EXECUTION_EVIDENCE"] = rejected.get(
                "INSUFFICIENT_CONSERVATIVE_EXECUTION_EVIDENCE", 0
            ) + 1
            continue

        token_mid = 0.5 * ((_finite(opportunity.get("best_bid"), price) or price) + (_finite(opportunity.get("best_ask"), price) or price))
        if side == "BUY":
            settlement_alpha_ps = fair_triplet[1] - token_mid
            spread_capture_ps = token_mid - price
        else:
            settlement_alpha_ps = token_mid - fair_triplet[1]
            spread_capture_ps = price - token_mid
        expected_adverse = fill_point * adverse * size
        expected_latency = fill_point * latency_buffer * size
        expected_unwind = fill_point * unwind_buffer * size
        packet = _feature_packet(
            opportunity=opportunity, fair_status=fair_status, fill_band=fill_band,
            group=group, action_ev_point=point_ev,
            action_ev_conservative=conservative_ev, decision_ns=decision_ns,
            feature_receive_ns=selection_ts_ms * 1_000_000, model_hash=model_hash,
        )
        identity = _stable_id(
            model_sha, market_status.get("market_id"), token, outcome, side, action,
            f"{price:.8f}", selection_ts_ms, model_hash,
        )
        raw = {
            "schema": "polymarket_v7_opportunity_envelope_v1",
            "version": 1,
            "model_sha": model_sha,
            "config_hash": str(runtime.get("config_hash") or ""),
            "policy_hash": str(runtime.get("policy_hash") or ""),
            "run_id": str(runtime.get("run_id") or ""),
            "source_snapshot_identity": _stable_id(
                "maker-bridge", selection_ts_ms, model_hash,
                fair.get("probability_model_hash"), market_status.get("market_id"),
            ),
            "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
            "component_provenance": ["professional_maker"],
            "market_id": str(market_status.get("market_id") or market.get("market_id") or ""),
            "event_id": str(market_status.get("event_id") or market.get("event_id") or ""),
            "contract_id": token,
            "mapping_identity": semantic_hash,
            "crypto_context": context,
            "action": "MAKE",
            "side": "YES" if outcome == "YES" else "NO",
            "decision_receive_timestamp_ns": decision_ns,
            "source_event_timestamps_ns": [selection_ts_ms * 1_000_000],
            "fair_value": {"lower": fair_triplet[0], "point": fair_triplet[1], "upper": fair_triplet[2]},
            "conservative_expected_wealth_change": conservative_ev,
            "cost_vector": {
                "fee": 0.0,
                "slippage": 0.0,
                "unwind_loss": expected_unwind,
                "capital_cost": 0.0,
                "latency_cost": expected_latency,
                "adverse_markout": expected_adverse,
                "rebate": 0.0,
            },
            "cost_authority": {
                "fee": "AUTHORITATIVE",
                "slippage": "CONSERVATIVE_ZERO",
                "unwind_loss": "CONSERVATIVE_BOUND",
                "capital_cost": "CONSERVATIVE_ZERO",
                "latency_cost": "CONSERVATIVE_BOUND",
                "adverse_markout": "CONSERVATIVE_BOUND",
                "rebate": "CONSERVATIVE_ZERO",
            },
            "uncertainty": {
                "lower_bound": conservative_ev,
                "upper_bound": max(conservative_ev, point_ev),
                "status": "MATURE",
            },
            "calibration_status": "MATURE",
            "latency": {
                "profile_id": "maker-bridge-receive-time-causal-v1",
                "profile_valid": True,
                "economic_percentile": "p99",
                "arrival_ns": int(max(0.0, (_finite(opportunity.get("last_opposite_flow_age_ms"), 0.0) or 0.0)) * 1_000_000),
            },
            "capacity": {
                "executable_size": size,
                "depth_provenance": str(selection.get("source") or "maker_reward_selection"),
            },
            "execution_plan": {
                "atomic_unit_id": identity[:32],
                "execution_style": "SINGLE_LEG",
                "legs": [{
                    "leg_id": "maker-leg-1",
                    "market_id": str(market_status.get("market_id") or market.get("market_id") or ""),
                    "contract_id": token,
                    "token_id": token,
                    "side": side,
                    "target_quantity": size,
                    "limit_price": price,
                    "fee_authority": "AUTHORITATIVE",
                }],
                "partial_fill_plan": "CANCEL_REMAINDER",
                "timeout_ms": int(((policy.get("quoting") or {}).get("max_quote_lifetime_ms") or 5000)),
                "unwind_plan": "NONE",
            },
            "inventory_delta": size if side == "BUY" else -size,
            "portfolio_exposure_delta": size * price,
            "settlement": {
                "definition": str(context.get("settlement_definition") or "rule-bound crypto settlement"),
                "source": str(context.get("oracle_source") or "POLYMARKET_RULE_BOUND_ORACLE"),
                "verified": True,
            },
            "eligible": True,
            "reasons": [
                "VERIFIED_SETTLEMENT_FAIR",
                "MATURE_FILL_EVENT_LOWER_BOUND",
                "POSITIVE_CONSERVATIVE_MAKER_EV",
                f"PLACEMENT_{action}",
            ],
            "deterministic_replay_key": f"maker:{identity}",
            "expires_at_ns": decision_ns + min(
                5_000_000_000,
                int(((policy.get("quoting") or {}).get("max_quote_lifetime_ms") or 5000)) * 1_000_000,
            ),
            "execution_alpha": packet,
        }
        try:
            parsed = OpportunityEnvelope.parse(raw)
        except OpportunityError:
            rejected["CANONICAL_ENVELOPE_REJECTED"] = rejected.get("CANONICAL_ENVELOPE_REJECTED", 0) + 1
            continue
        output.append(parsed.raw)

    output.sort(key=lambda row: (
        -float(row.get("conservative_expected_wealth_change") or 0.0),
        str(row.get("deterministic_replay_key") or ""),
    ))
    return output, {
        "schema": BRIDGE_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "state": "OPERATIONAL" if output else "NO_EXECUTABLE_MAKE",
        "reasons": [],
        "candidate_cells": len(cells),
        "typed_make_opportunities": len(output),
        "rejected": rejected,
        "model_state": model.get("model_state"),
        "model_hash": model_hash,
        "market_id": market_status.get("market_id"),
        "decision_timestamp_ns": decision_ns,
        "selection_timestamp_ms": selection_ts_ms,
        "supported_execution_sides": ["BUY"],
        "inventory_bridge_state": "SELL_DISABLED_UNTIL_CANONICAL_INVENTORY_AUTHORITY",
    }
