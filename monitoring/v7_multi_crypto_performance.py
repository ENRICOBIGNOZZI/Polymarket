#!/usr/bin/env python3
"""Read-only multi-crypto PAPER performance attribution for V7 monitoring."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
HORIZONS = ("M5", "M15")
CRYPTO_ENGINE = "CRYPTO_SETTLEMENT_ENGINE"
_FIXED_FAMILY_SCOPE = {
    "lead_lag_taker_v1": ("BTC", "M5", "LEAD_LAG_TAKER_V1"),
    "crypto_informed_taker": ("BTC", "M5", "CRYPTO_INFORMED_TAKER"),
}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _safe_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _metric(name: str, value: Any, labels: dict[str, Any] | None = None) -> str:
    if isinstance(value, bool):
        number = 1.0 if value else 0.0
    else:
        parsed = _finite(value)
        if parsed is None:
            raise ValueError(f"{name}: metric value is not finite")
        number = parsed
    suffix = ""
    if labels:
        suffix = "{" + ",".join(f'{k}="{_safe_label(v)}"' for k, v in labels.items()) + "}"
    return f"{name}{suffix} {number:.12g}"


def _strategy_name(metadata: dict[str, Any], fallback: str) -> str:
    family = str(metadata.get("model_family") or metadata.get("component") or "").strip()
    if family in _FIXED_FAMILY_SCOPE:
        return _FIXED_FAMILY_SCOPE[family][2]
    return family.upper() if family else fallback


def _lane_from_row(row: dict[str, Any]) -> tuple[str, str, str] | None:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    context = metadata.get("crypto_context") if isinstance(metadata.get("crypto_context"), dict) else {}
    receipt = metadata.get("native_settlement_receipt") if isinstance(metadata.get("native_settlement_receipt"), dict) else {}
    asset = str(context.get("asset") or metadata.get("asset") or receipt.get("asset") or row.get("asset") or "").upper()
    horizon = str(context.get("horizon") or metadata.get("horizon") or receipt.get("horizon") or row.get("horizon") or "").upper()
    family = str(metadata.get("model_family") or metadata.get("component") or "").strip()
    if (asset not in ASSETS or horizon not in HORIZONS) and family in _FIXED_FAMILY_SCOPE:
        asset, horizon, _ = _FIXED_FAMILY_SCOPE[family]
    if asset not in ASSETS or horizon not in HORIZONS:
        return None
    return asset, horizon, _strategy_name(metadata, str(row.get("strategy") or CRYPTO_ENGINE))


def _blank_lane(asset: str, horizon: str) -> dict[str, Any]:
    return {
        "asset": asset, "horizon": horizon, "registered": False, "research_only": True,
        "new_risk_authorized": False, "authority": "UNKNOWN", "economic_evidence_present": False,
        "fills": 0, "finals": 0, "wins": 0, "wins_known": 0, "realized_pnl": 0.0,
        "realized_pnl_known": False, "fees": 0.0, "turnover": 0.0,
        "fill_position_ids": set(), "final_position_ids": set(), "position_id_missing": 0,
        "position_open_costs": {}, "open_cost_at_risk": None, "open_cost_at_risk_known": False,
        "strategies": {},
    }


def _blank_strategy(name: str) -> dict[str, Any]:
    return {"strategy": name, "fills": 0, "finals": 0, "wins": 0, "wins_known": 0,
            "realized_pnl": 0.0, "realized_pnl_known": False, "fees": 0.0, "turnover": 0.0}


def _crypto_risk(global_coordinator: dict[str, Any]) -> dict[str, Any]:
    nested = global_coordinator.get("crypto_correlation_risk")
    if isinstance(nested, dict):
        return nested
    # Compatibility with older monitoring fixtures. Missing data stays missing.
    aliases = {
        "gross_crypto_exposure_usd": global_coordinator.get("crypto_gross_exposure_usd"),
        "net_directional_crypto_exposure_usd": global_coordinator.get("crypto_net_directional_exposure_usd"),
        "correlated_crypto_cluster_exposure_usd": global_coordinator.get("crypto_cluster_exposure_usd"),
    }
    return {key: value for key, value in aliases.items() if _finite(value) is not None}


def summarize_multi_crypto(
    run_root: Path, *, expected_sha: str, portfolio: dict[str, Any], canonical: dict[str, Any],
    global_coordinator: dict[str, Any], crypto_registry: dict[str, Any],
    crypto_model_registry: dict[str, Any], ledger_valid: bool,
    canonical_mtime_ms: float | None = None,
) -> dict[str, Any]:
    lanes = {(asset, horizon): _blank_lane(asset, horizon) for asset in ASSETS for horizon in HORIZONS}
    for row in crypto_registry.get("contexts", []) if isinstance(crypto_registry.get("contexts"), list) else []:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("asset") or "").upper(), str(row.get("horizon") or "").upper())
        if key not in lanes:
            continue
        lanes[key].update({"registered": row.get("enabled") is True,
                           "research_only": row.get("research_only") is True,
                           "authority": str(row.get("authority") or "UNKNOWN")})
    models = crypto_model_registry.get("models") if isinstance(crypto_model_registry.get("models"), list) else []
    for row in models:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("asset") or "").upper(), str(row.get("horizon") or "").upper())
        if key in lanes:
            lanes[key]["new_risk_authorized"] = row.get("new_risk_authorized") is True

    diagnostics = {"rows": 0, "crypto_rows": 0, "unsafe_rows": 0, "sha_mismatch_rows": 0,
                   "unattributed_fill_rows": 0, "unattributed_final_rows": 0,
                   "final_rows_missing_pnl": 0, "latest_final_recorded_ts_ms": None}
    fill_position_by_id: dict[str, str] = {}
    ledger_path = Path(run_root) / "ledger/execution.jsonl"
    try:
        handle = ledger_path.open("r", encoding="utf-8")
    except OSError:
        handle = None
    if handle is not None:
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                diagnostics["rows"] += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict) or row.get("strategy") != CRYPTO_ENGINE:
                    continue
                diagnostics["crypto_rows"] += 1
                if row.get("paper_only") is not True or row.get("authenticated_execution") is not False:
                    diagnostics["unsafe_rows"] += 1
                    continue
                if expected_sha and row.get("model_sha") != expected_sha:
                    diagnostics["sha_mismatch_rows"] += 1
                    continue
                event_type = str(row.get("event_type") or "")
                if event_type not in {"FILL", "FINAL"}:
                    continue
                if event_type == "FINAL":
                    recorded = _finite(row.get("recorded_ts_ms"))
                    if recorded is not None:
                        previous = _finite(diagnostics.get("latest_final_recorded_ts_ms"))
                        diagnostics["latest_final_recorded_ts_ms"] = recorded if previous is None else max(previous, recorded)
                lane_key = _lane_from_row(row)
                if lane_key is None:
                    diagnostics["unattributed_fill_rows" if event_type == "FILL" else "unattributed_final_rows"] += 1
                    continue
                asset, horizon, strategy_name = lane_key
                lane = lanes[(asset, horizon)]
                strategy = lane["strategies"].setdefault(strategy_name, _blank_strategy(strategy_name))
                lane["economic_evidence_present"] = True
                position_id = str(row.get("position_id") or "").strip()
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                if event_type == "FILL":
                    lane["fills"] += 1; strategy["fills"] += 1
                    price, size, fee = _finite(row.get("fill_price")), _finite(row.get("filled_size")), _finite(row.get("fee"))
                    if fee is not None:
                        lane["fees"] += fee; strategy["fees"] += fee
                    if price is not None and size is not None and size >= 0:
                        turnover = price * size; lane["turnover"] += turnover; strategy["turnover"] += turnover
                    if position_id:
                        lane["fill_position_ids"].add(position_id)
                        fill_id = str(row.get("fill_id") or "").strip()
                        if fill_id:
                            fill_position_by_id[fill_id] = position_id
                        if price is not None and size is not None and size >= 0 and str(row.get("side") or "BUY").upper() == "BUY":
                            lane["position_open_costs"][position_id] = lane["position_open_costs"].get(position_id, 0.0) + price * size + max(0.0, fee or 0.0)
                        else:
                            lane["position_id_missing"] += 1
                    else:
                        lane["position_id_missing"] += 1
                else:
                    lane["finals"] += 1; strategy["finals"] += 1
                    pnl = _finite(row.get("final_pnl"))
                    if pnl is None:
                        diagnostics["final_rows_missing_pnl"] += 1
                    else:
                        lane["realized_pnl"] += pnl; strategy["realized_pnl"] += pnl
                    won = metadata.get("won")
                    if isinstance(won, bool):
                        lane["wins_known"] += 1; strategy["wins_known"] += 1
                        lane["wins"] += int(won); strategy["wins"] += int(won)
                    included_fill_ids = metadata.get("included_fill_ids") if isinstance(metadata.get("included_fill_ids"), list) else []
                    linked_positions = {
                        fill_position_by_id[str(fill_id)]
                        for fill_id in included_fill_ids
                        if str(fill_id) in fill_position_by_id
                    }
                    if linked_positions:
                        lane["final_position_ids"].update(linked_positions)
                    elif position_id:
                        lane["final_position_ids"].add(position_id)
                    else:
                        lane["position_id_missing"] += 1

    lane_final_count = 0
    attributed_realized = 0.0
    for lane in lanes.values():
        if lane["finals"] > 0:
            lane["realized_pnl_known"] = True
            lane_final_count += lane["finals"]
            attributed_realized += lane["realized_pnl"]
        for strategy in lane["strategies"].values():
            strategy["realized_pnl_known"] = strategy["finals"] > 0
        lane["open_positions_known"] = lane["position_id_missing"] == 0 and lane["fills"] > 0
        lane["open_positions"] = max(0, len(lane["fill_position_ids"] - lane["final_position_ids"])) if lane["open_positions_known"] else None
        lane["open_cost_at_risk_known"] = lane["open_positions_known"]
        if lane["open_cost_at_risk_known"]:
            lane["open_cost_at_risk"] = sum(cost for pid, cost in lane["position_open_costs"].items() if pid not in lane["final_position_ids"])

    engine_rows = portfolio.get("engines") if isinstance(portfolio.get("engines"), dict) else {}
    engine = engine_rows.get(CRYPTO_ENGINE) if isinstance(engine_rows.get(CRYPTO_ENGINE), dict) else {}
    budget, equity = _finite(engine.get("budget")), _finite(engine.get("equity"))
    total_pnl = equity - budget if equity is not None and budget is not None else None
    strategy_pnl = canonical.get("strategy_net_pnl") if isinstance(canonical.get("strategy_net_pnl"), dict) else {}
    canonical_realized = _finite(strategy_pnl.get(CRYPTO_ENGINE))
    gap = canonical_realized - attributed_realized if canonical_realized is not None else None
    tolerance = 1e-8 * max(1.0, abs(canonical_realized or 0.0), abs(attributed_realized))
    ledger_complete = bool(
        ledger_valid and diagnostics["unsafe_rows"] == 0 and diagnostics["sha_mismatch_rows"] == 0
        and diagnostics["unattributed_final_rows"] == 0 and diagnostics["final_rows_missing_pnl"] == 0
    )
    latest_final_ms = _finite(diagnostics.get("latest_final_recorded_ts_ms"))
    canonical_mtime = _finite(canonical_mtime_ms)
    canonical_stale_vs_ledger = bool(
        canonical_realized is not None and latest_final_ms is not None and canonical_mtime is not None
        and latest_final_ms > canonical_mtime + 1.0
    )
    attribution_reconciled = bool(
        ledger_complete and canonical_realized is not None and gap is not None and abs(gap) <= tolerance
    )
    if attribution_reconciled:
        attribution_state = "RECONCILED"
        display_realized = canonical_realized
        display_realized_source = "CANONICAL_ECONOMICS"
    elif ledger_complete and canonical_stale_vs_ledger and lane_final_count > 0:
        attribution_state = "PENDING_CANONICAL_REFRESH"
        display_realized = attributed_realized
        display_realized_source = "CANONICAL_LEDGER_PROVISIONAL"
    elif not ledger_complete or canonical_realized is None:
        attribution_state = "UNVERIFIABLE"
        display_realized = canonical_realized
        display_realized_source = "CANONICAL_ECONOMICS" if canonical_realized is not None else "UNKNOWN"
    else:
        attribution_state = "DIVERGED"
        display_realized = canonical_realized
        display_realized_source = "CANONICAL_ECONOMICS"
    unrealized = total_pnl - display_realized if total_pnl is not None and display_realized is not None else None
    account_drawdown = _finite(portfolio.get("drawdown"))
    crypto_risk = _crypto_risk(global_coordinator)
    per_asset_exposure = crypto_risk.get("per_asset_exposure_usd")
    per_asset_exposure = per_asset_exposure if isinstance(per_asset_exposure, dict) else {}
    per_horizon_exposure = crypto_risk.get("per_horizon_exposure_usd")
    per_horizon_exposure = per_horizon_exposure if isinstance(per_horizon_exposure, dict) else {}
    registered_lanes = sum(int(lane["registered"]) for lane in lanes.values())
    known_lanes = sum(int(lane["economic_evidence_present"]) for lane in lanes.values())
    authorized_lanes = sum(int(lane["new_risk_authorized"]) for lane in lanes.values())
    return {
        "schema": "polymarket_v7_multi_crypto_performance_v1", "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False, "real_capital_at_risk": False,
        "assets": list(ASSETS), "horizons": list(HORIZONS), "lane_count": len(lanes),
        "registered_lanes": registered_lanes, "known_economic_lanes": known_lanes,
        "new_risk_authorized_lanes": authorized_lanes, "ledger_valid": bool(ledger_valid),
        "portfolio": {"budget": budget, "equity": equity, "total_pnl": total_pnl,
                      "realized_pnl": display_realized, "unrealized_pnl": unrealized,
                      "account_drawdown": account_drawdown,
                      "candidate_gross_exposure": _finite(crypto_risk.get("gross_crypto_exposure_usd")),
                      "candidate_net_exposure": _finite(crypto_risk.get("net_directional_crypto_exposure_usd")),
                      "candidate_cluster_exposure": _finite(crypto_risk.get("correlated_crypto_cluster_exposure_usd")),
                      "candidate_oracle_concentration": _finite(crypto_risk.get("oracle_concentration_fraction")),
                      "candidate_exchange_concentration": _finite(crypto_risk.get("exchange_source_concentration_fraction"))},
        "exposure": {
            "per_asset": {str(k): v for k, v in per_asset_exposure.items() if _finite(v) is not None},
            "per_horizon": {str(k): v for k, v in per_horizon_exposure.items() if _finite(v) is not None},
        },
        "attribution": {"attributed_realized_pnl": attributed_realized if lane_final_count else None,
                        "canonical_realized_pnl": canonical_realized,
                        "ledger_realized_pnl": attributed_realized if lane_final_count else None,
                        "display_realized_source": display_realized_source,
                        "state": attribution_state,
                        "status_code": 2 if attribution_state == "RECONCILED" else (1 if attribution_state == "PENDING_CANONICAL_REFRESH" else 0),
                        "canonical_stale_vs_ledger": canonical_stale_vs_ledger,
                        "canonical_mtime_ms": canonical_mtime,
                        "latest_final_recorded_ts_ms": latest_final_ms,
                        "gap": gap, "reconciled": attribution_reconciled,
                        "unattributed_final_rows": diagnostics["unattributed_final_rows"],
                        "unattributed_fill_rows": diagnostics["unattributed_fill_rows"]},
        "diagnostics": diagnostics,
        "lanes": [{k: v for k, v in lane.items() if k not in {"fill_position_ids", "final_position_ids", "position_open_costs"}}
                  for lane in lanes.values()],
    }


def render_prometheus(summary: dict[str, Any]) -> list[str]:
    if summary.get("schema") != "polymarket_v7_multi_crypto_performance_v1":
        return []
    lines = [
        _metric("polymarket_mc_info", 1, {"mode": "PAPER"}),
        _metric("polymarket_mc_lane_count", summary.get("lane_count")),
        _metric("polymarket_mc_registered_lanes", summary.get("registered_lanes")),
        _metric("polymarket_mc_known_economic_lanes", summary.get("known_economic_lanes")),
        _metric("polymarket_mc_new_risk_authorized_lanes", summary.get("new_risk_authorized_lanes")),
        _metric("polymarket_mc_ledger_valid", summary.get("ledger_valid") is True),
    ]
    portfolio = summary.get("portfolio") if isinstance(summary.get("portfolio"), dict) else {}
    for key, metric in (
        ("budget", "polymarket_mc_portfolio_budget_usd"), ("equity", "polymarket_mc_portfolio_equity_usd"),
        ("total_pnl", "polymarket_mc_total_pnl_usd"), ("realized_pnl", "polymarket_mc_realized_pnl_usd"),
        ("unrealized_pnl", "polymarket_mc_unrealized_pnl_usd"), ("account_drawdown", "polymarket_mc_account_drawdown_ratio"),
        ("candidate_gross_exposure", "polymarket_mc_coordinator_candidate_gross_exposure_usd"),
        ("candidate_net_exposure", "polymarket_mc_coordinator_candidate_net_exposure_usd"),
        ("candidate_cluster_exposure", "polymarket_mc_coordinator_candidate_cluster_exposure_usd"),
        ("candidate_oracle_concentration", "polymarket_mc_coordinator_candidate_oracle_concentration_ratio"),
        ("candidate_exchange_concentration", "polymarket_mc_coordinator_candidate_exchange_concentration_ratio"),
    ):
        if _finite(portfolio.get(key)) is not None:
            lines.append(_metric(metric, portfolio[key]))
    exposure = summary.get("exposure") if isinstance(summary.get("exposure"), dict) else {}
    for asset, value in sorted((exposure.get("per_asset") or {}).items()):
        if asset in ASSETS and _finite(value) is not None:
            lines.append(_metric("polymarket_mc_coordinator_candidate_asset_exposure_usd", value, {"asset": asset}))
    for horizon, value in sorted((exposure.get("per_horizon") or {}).items()):
        if horizon in HORIZONS and _finite(value) is not None:
            lines.append(_metric("polymarket_mc_coordinator_candidate_horizon_exposure_usd", value, {"horizon": horizon}))
    attribution = summary.get("attribution") if isinstance(summary.get("attribution"), dict) else {}
    lines.append(_metric("polymarket_mc_attribution_reconciled", attribution.get("reconciled") is True))
    lines.append(_metric("polymarket_mc_attribution_status_code", attribution.get("status_code") or 0))
    lines.append(_metric("polymarket_mc_attribution_state_info", 1, {"state": attribution.get("state", "UNVERIFIABLE"), "realized_source": attribution.get("display_realized_source", "UNKNOWN")}))
    lines.append(_metric("polymarket_mc_canonical_stale_vs_ledger", attribution.get("canonical_stale_vs_ledger") is True))
    lines.append(_metric("polymarket_mc_unattributed_final_rows", attribution.get("unattributed_final_rows") or 0))
    lines.append(_metric("polymarket_mc_unattributed_fill_rows", attribution.get("unattributed_fill_rows") or 0))
    if _finite(attribution.get("gap")) is not None:
        lines.append(_metric("polymarket_mc_attribution_gap_usd", attribution["gap"]))
    if _finite(attribution.get("canonical_realized_pnl")) is not None:
        lines.append(_metric("polymarket_mc_canonical_realized_pnl_usd", attribution["canonical_realized_pnl"]))
    if _finite(attribution.get("ledger_realized_pnl")) is not None:
        lines.append(_metric("polymarket_mc_ledger_realized_pnl_usd", attribution["ledger_realized_pnl"]))
    if _finite(attribution.get("attributed_realized_pnl")) is not None:
        lines.append(_metric("polymarket_mc_attributed_realized_pnl_usd", attribution["attributed_realized_pnl"]))
    for lane in summary.get("lanes", []) if isinstance(summary.get("lanes"), list) else []:
        labels = {"asset": lane.get("asset"), "horizon": lane.get("horizon")}
        lines.extend([
            _metric("polymarket_mc_lane_registered", lane.get("registered") is True, labels),
            _metric("polymarket_mc_lane_research_only", lane.get("research_only") is True, labels),
            _metric("polymarket_mc_lane_new_risk_authorized", lane.get("new_risk_authorized") is True, labels),
            _metric("polymarket_mc_lane_economic_evidence_present", lane.get("economic_evidence_present") is True, labels),
        ])
        if lane.get("economic_evidence_present") is True:
            lines.extend([
                _metric("polymarket_mc_lane_fills_total", lane.get("fills"), labels),
                _metric("polymarket_mc_lane_finals_total", lane.get("finals"), labels),
                _metric("polymarket_mc_lane_fees_usd", lane.get("fees"), labels),
                _metric("polymarket_mc_lane_turnover_usd", lane.get("turnover"), labels),
            ])
            if lane.get("realized_pnl_known") is True:
                realized = float(lane.get("realized_pnl") or 0.0)
                lines.append(_metric("polymarket_mc_lane_realized_pnl_usd", realized, labels))
                if int(lane.get("finals") or 0) > 0:
                    lines.append(_metric("polymarket_mc_lane_avg_pnl_per_final_usd", realized / int(lane["finals"]), labels))
                turnover = _finite(lane.get("turnover"))
                if turnover is not None and turnover > 0:
                    lines.append(_metric("polymarket_mc_lane_realized_return_on_turnover", realized / turnover, labels))
                    lines.append(_metric("polymarket_mc_lane_fees_bps", 10000.0 * float(lane.get("fees") or 0.0) / turnover, labels))
            if lane.get("open_positions_known") is True:
                lines.append(_metric("polymarket_mc_lane_open_positions", lane.get("open_positions"), labels))
            if lane.get("open_cost_at_risk_known") is True:
                lines.append(_metric("polymarket_mc_lane_open_cost_at_risk_usd", lane.get("open_cost_at_risk"), labels))
            if int(lane.get("wins_known") or 0) == int(lane.get("finals") or 0) and int(lane.get("finals") or 0) > 0:
                lines.append(_metric("polymarket_mc_lane_win_rate", int(lane.get("wins") or 0) / int(lane["finals"]), labels))
        for name, strategy in sorted((lane.get("strategies") or {}).items()):
            strategy_labels = {**labels, "strategy": name}
            lines.append(_metric("polymarket_mc_strategy_fills_total", strategy.get("fills"), strategy_labels))
            lines.append(_metric("polymarket_mc_strategy_finals_total", strategy.get("finals"), strategy_labels))
            lines.append(_metric("polymarket_mc_strategy_fees_usd", strategy.get("fees"), strategy_labels))
            lines.append(_metric("polymarket_mc_strategy_turnover_usd", strategy.get("turnover"), strategy_labels))
            if strategy.get("realized_pnl_known") is True:
                lines.append(_metric("polymarket_mc_strategy_realized_pnl_usd", strategy.get("realized_pnl"), strategy_labels))
    return lines


def summarize_shadow_runtime(run_root: Path | None, *, now_ns: int | None = None) -> dict[str, Any]:
    """Read the zero-authority six-crypto SHADOW supervisor status without mutating it."""
    base = {
        "schema": "polymarket_v7_multi_crypto_shadow_monitor_v1",
        "present": False, "valid": False, "safe": False, "ready": False,
    }
    if run_root is None:
        return base
    path = Path(run_root) / "control/runtime_status.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {**base, "run_root": str(run_root)}
    if not isinstance(value, dict):
        return {**base, "run_root": str(run_root), "present": True}
    valid = value.get("schema") == "polymarket_v7_multi_crypto_shadow_runtime_status_v1"
    safe = bool(valid and value.get("paper_only") is True
                and value.get("authenticated_execution") is False
                and value.get("real_order_submission") is False
                and value.get("real_capital_at_risk") is False
                and value.get("execution_authority") is False
                and value.get("automatic_promotion") is False)
    timestamp_ns = int(value.get("timestamp_ns") or 0)
    current_ns = time.time_ns() if now_ns is None else int(now_ns)
    age_seconds = max(0.0, (current_ns - timestamp_ns) / 1e9) if timestamp_ns > 0 else None
    children = value.get("children") if isinstance(value.get("children"), dict) else {}
    child_total = len(children)
    child_alive = sum(1 for row in children.values() if isinstance(row, dict) and row.get("alive") is True)
    state = str(value.get("state") or "UNKNOWN")
    return {
        **base, "present": True, "valid": valid, "safe": safe,
        "ready": safe and state == "RUNNING_SHADOW" and value.get("contract_all_active_ready") is True,
        "run_root": str(run_root), "code_sha": str(value.get("code_sha") or ""),
        "state": state, "age_seconds": age_seconds,
        "external_ready_assets": int(value.get("external_ready_assets") or 0),
        "oracle_healthy_assets": int(value.get("oracle_healthy_assets") or 0),
        "contract_active_markets": int(value.get("contract_active_markets") or 0),
        "contract_active_ready_markets": int(value.get("contract_active_ready_markets") or 0),
        "contract_all_active_ready": value.get("contract_all_active_ready") is True,
        "book_evidence_complete": value.get("book_evidence_complete") is True,
        "label_evidence_complete": value.get("label_evidence_complete") is True,
        "feature_tape_emitted": int(value.get("feature_tape_emitted") or 0),
        "child_total": child_total, "child_alive": child_alive,
    }


def render_shadow_prometheus(summary: dict[str, Any]) -> list[str]:
    lines = [_metric("polymarket_mc_shadow_present", summary.get("present") is True)]
    if summary.get("present") is not True:
        return lines
    lines.extend([
        _metric("polymarket_mc_shadow_valid", summary.get("valid") is True),
        _metric("polymarket_mc_shadow_safe", summary.get("safe") is True),
        _metric("polymarket_mc_shadow_ready", summary.get("ready") is True),
        _metric("polymarket_mc_shadow_state_info", 1, {"state": summary.get("state", "UNKNOWN"), "code_sha": summary.get("code_sha", "")}),
        _metric("polymarket_mc_shadow_external_ready_assets", summary.get("external_ready_assets") or 0),
        _metric("polymarket_mc_shadow_oracle_healthy_assets", summary.get("oracle_healthy_assets") or 0),
        _metric("polymarket_mc_shadow_contract_active_markets", summary.get("contract_active_markets") or 0),
        _metric("polymarket_mc_shadow_contract_ready_markets", summary.get("contract_active_ready_markets") or 0),
        _metric("polymarket_mc_shadow_contract_all_ready", summary.get("contract_all_active_ready") is True),
        _metric("polymarket_mc_shadow_book_evidence_complete", summary.get("book_evidence_complete") is True),
        _metric("polymarket_mc_shadow_label_evidence_complete", summary.get("label_evidence_complete") is True),
        _metric("polymarket_mc_shadow_feature_tape_emitted", summary.get("feature_tape_emitted") or 0),
        _metric("polymarket_mc_shadow_children_total", summary.get("child_total") or 0),
        _metric("polymarket_mc_shadow_children_alive", summary.get("child_alive") or 0),
    ])
    if _finite(summary.get("age_seconds")) is not None:
        lines.append(_metric("polymarket_mc_shadow_status_age_seconds", summary["age_seconds"]))
    return lines
