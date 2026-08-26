#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import v7_cross_sectional_history as history_transport
import v7_cross_sectional_rank as base
import v7_cross_sectional_rank_core as core
import v7_cross_sectional_rank_frozen as frozen
import v7_cross_sectional_rank_inference as inference
import v7_cross_sectional_relative as relative


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def forward_gate(blocked: dict[str, object], config: dict[str, Any]) -> tuple[bool, list[str]]:
    gate = config["forward_gate"]
    reasons: list[str] = []
    if int(blocked.get("days") or 0) < int(gate["minimum_forward_days"]):
        reasons.append("minimum_forward_days")
    if int(blocked.get("cross_sections") or 0) < int(gate["minimum_forward_cross_sections"]):
        reasons.append("minimum_forward_cross_sections")
    if float(blocked.get("positive_daily_rank_ic_fraction") or 0.0) < float(gate["minimum_positive_daily_rank_ic_fraction"]):
        reasons.append("positive_daily_rank_ic_fraction")
    if float(blocked.get("positive_daily_top_bottom_fraction") or 0.0) < float(gate["minimum_positive_daily_tail_spread_fraction"]):
        reasons.append("positive_daily_tail_spread_fraction")
    p_ic = blocked.get("rank_ic_bootstrap_p_mean_nonpositive")
    if p_ic is None or float(p_ic) > float(gate["maximum_rank_ic_bootstrap_p_mean_nonpositive"]):
        reasons.append("rank_ic_block_bootstrap")
    p_spread = blocked.get("top_bottom_bootstrap_p_mean_nonpositive")
    if p_spread is None or float(p_spread) > float(gate["maximum_tail_spread_bootstrap_p_mean_nonpositive"]):
        reasons.append("tail_spread_block_bootstrap")
    directional = (
        ("rank_ic_first_half_mean", "require_positive_first_half_rank_ic"),
        ("rank_ic_second_half_mean", "require_positive_second_half_rank_ic"),
        ("top_bottom_first_half_mean", "require_positive_first_half_tail_spread"),
        ("top_bottom_second_half_mean", "require_positive_second_half_tail_spread"),
    )
    for key, switch in directional:
        if gate.get(switch, True):
            value = blocked.get(key)
            if value is None or float(value) <= 0.0:
                reasons.append(key)
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="Frozen 2h/6h finance-style cross-sectional relative tail challenger")
    parser.add_argument("--config", type=Path, default=Path("config/research_v7_cross_sectional_rank.json"))
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--market-limit", type=int, default=150)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-pairs-json", type=Path, required=True)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if not cfg.get("paper_only") or not cfg.get("research_only") or cfg.get("live_intents_enabled"):
        raise SystemExit("relative tail challenger must remain paper/research-only with live intents disabled")
    if cfg.get("target") != "cross_sectional_tail_relative_logit_spread":
        raise SystemExit("relative tail challenger target contract is not frozen")
    if list(cfg.get("horizons_minutes") or []) != [120, 360]:
        raise SystemExit("relative tail challenger horizons must remain frozen at 2h and 6h")
    if cfg.get("frequency_registration", {}).get("frozen_holdout_only") is not True:
        raise SystemExit("relative tail challenger requires frozen_holdout_only=true")
    pair_contract = cfg["relative_pair_contract"]
    if pair_contract.get("absolute_single_leg_direction_allowed") is not False:
        raise SystemExit("relative ranking cannot emit absolute single-leg directions")

    now = int(time.time())
    history_cfg = cfg["history"]
    model_cfg = cfg["model"]
    execution_cfg = cfg["execution_shadow"]
    discovery_cfg = cfg["discovery"]
    fidelity_minutes = int(history_cfg["fidelity_minutes"])
    bucket_seconds = fidelity_minutes * 60
    embargo_seconds = int(history_cfg["purge_embargo_buckets"]) * bucket_seconds
    holdout_start = int(discovery_cfg["forward_holdout_start_ts"])
    frozen_training_label_cutoff = holdout_start - embargo_seconds
    if holdout_start <= int(discovery_cfg["discovery_cutoff_ts"]):
        raise SystemExit("forward holdout must begin strictly after the frozen discovery cutoff")

    markets = base.discover_markets(
        args.gamma_url,
        args.market_limit,
        float(execution_cfg["minimum_liquidity_usd"]),
    )
    start_ts = now - int(history_cfg["lookback_hours"]) * 3600
    histories, history_failures = history_transport.fetch_histories(
        args.clob_url,
        markets,
        start_ts,
        now,
        fidelity_minutes,
        max_workers=6,
    )
    metadata = {
        market.market_id: core.MarketMeta(market.market_id, market.event_id, market.group)
        for market in markets
        if market.market_id in histories
    }

    received_ts = int(time.time())
    raw_books = base.fetch_books(args.clob_url, markets)
    current_bucket = (received_ts // bucket_seconds) * bucket_seconds
    for market in markets:
        raw_book = raw_books.get(market.market_id)
        if raw_book is None or market.market_id not in histories:
            continue
        yes_bid, yes_ask, _no_bid, _no_ask = raw_book
        histories[market.market_id][current_bucket] = 0.5 * (yes_bid + yes_ask)

    books: dict[str, core.BookEconomics] = {}
    authoritative = 0
    for market in markets:
        raw_book = raw_books.get(market.market_id)
        if raw_book is None:
            continue
        auth, rate, exponent, taker_only = base.authoritative_fee(market.raw)
        authoritative += int(auth)
        yes_bid, yes_ask, no_bid, no_ask = raw_book
        books[market.market_id] = core.BookEconomics(
            market_id=market.market_id,
            event_id=market.event_id,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            liquidity=market.liquidity,
            fee_rate=rate,
            fee_exponent=exponent,
            taker_only=taker_only,
            authoritative_fee=auth,
            received_ts=received_ts,
        )

    minimum_cross_section = int(history_cfg["minimum_cross_section"])
    history_data_healthy = len(histories) >= minimum_cross_section
    horizon_reports: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []

    for horizon_minutes in cfg["horizons_minutes"]:
        horizon_steps = int(horizon_minutes) // fidelity_minutes
        rows = core.build_training_rows(
            histories,
            metadata,
            bucket_seconds=bucket_seconds,
            horizon_steps=horizon_steps,
            min_cross_section=minimum_cross_section,
            group_weight=float(history_cfg["group_neutralization_weight"]),
            min_group_size=int(history_cfg["minimum_group_size"]),
        )
        fit, forward_metrics = frozen.evaluate(
            rows,
            holdout_start_ts=holdout_start,
            window_seconds=int(history_cfg["training_window_days"]) * 86400,
            embargo_seconds=embargo_seconds,
            ridge=float(model_cfg["ridge_penalty"]),
            half_life_seconds=int(history_cfg["recency_half_life_days"]) * 86400,
            min_train_rows=int(model_cfg["minimum_training_rows"]),
            min_train_cross_sections=int(model_cfg["minimum_training_cross_sections"]),
            tail_fraction=float(model_cfg["tail_fraction"]),
        )
        frozen_fit_valid = (
            fit is not None
            and int(fit.train_end_ts) <= int(frozen_training_label_cutoff)
        )
        blocked = inference.blocked_inference(
            forward_metrics,
            bootstrap_samples=4999,
            seed=20260826 + int(horizon_minutes),
        )
        gate_ok, gate_reasons = forward_gate(blocked, cfg)
        if not frozen_fit_valid:
            gate_ok = False
            gate_reasons = ["frozen_fit_unavailable_or_contaminated"] + gate_reasons
        if not history_data_healthy:
            gate_ok = False
            gate_reasons = ["price_history_data_health"] + gate_reasons

        score_ts = base.latest_score_time(histories, metadata, bucket_seconds, minimum_cross_section)
        current_pairs: list[relative.RelativePairCandidate] = []
        if (
            frozen_fit_valid
            and score_ts > 0
            and received_ts - score_ts <= 2 * bucket_seconds
        ):
            snapshot = core.score_snapshot(
                histories,
                metadata,
                score_ts,
                bucket_seconds,
                minimum_cross_section,
            )
            if snapshot:
                scored = core.apply_fit(snapshot, fit, score_ts)
                current_pairs = relative.select_relative_pairs(
                    scored,
                    books,
                    tail_fraction=float(model_cfg["tail_fraction"]),
                    horizon_seconds=int(horizon_minutes) * 60,
                    now=received_ts,
                    minimum_completed_pair_net_edge=float(execution_cfg["minimum_completed_pair_net_edge"]),
                    max_pairs=int(execution_cfg["maximum_pairs"]),
                    maximum_pair_notional_usd=float(execution_cfg["maximum_pair_notional_usd"]),
                    shadow_sleeve_budget_usd=float(execution_cfg["shadow_sleeve_budget_usd"]),
                    one_contract_per_event=bool(execution_cfg["one_contract_per_event"]),
                    min_liquidity=float(execution_cfg["minimum_liquidity_usd"]),
                    max_spread=float(execution_cfg["maximum_spread"]),
                    slippage_bps_round_trip_leg=float(execution_cfg["slippage_bps_round_trip_leg"]),
                    capital_cost_bps_per_hour=float(execution_cfg["capital_cost_bps_per_hour"]),
                    adverse_penalty_bps=float(execution_cfg["adverse_markout_penalty_bps"]),
                    max_book_age_seconds=int(execution_cfg["maximum_book_age_seconds"]),
                )
        for candidate in current_pairs:
            pair_rows.append({"horizon_minutes": int(horizon_minutes), **asdict(candidate)})

        horizon_reports.append(
            {
                "horizon_minutes": int(horizon_minutes),
                "training_rows": len(rows),
                "forward_holdout_start_ts": holdout_start,
                "forward_cross_sections": len(forward_metrics),
                "forward_blocked_inference": blocked,
                "forward_gate": gate_ok,
                "forward_gate_reasons": gate_reasons,
                "score_timestamp": score_ts,
                "fit_mode": "frozen_at_holdout_start",
                "frozen_fit_asof_ts": holdout_start,
                "frozen_training_label_cutoff_ts": frozen_training_label_cutoff,
                "frozen_fit_validated": frozen_fit_valid,
                "fit": None if fit is None else asdict(fit),
                "relative_pair_candidates": len(current_pairs),
            }
        )

    atomic_json(args.output_pairs_json, pair_rows)
    forward_days_observed = max(
        (int(h["forward_blocked_inference"].get("days") or 0) for h in horizon_reports),
        default=0,
    )
    frozen_holdout_fit_validated = bool(horizon_reports) and all(
        bool(h.get("frozen_fit_validated")) for h in horizon_reports
    )
    report = {
        "timestamp": received_ts,
        "schema_version": 2,
        "paper_only": True,
        "research_only": True,
        "live_intents_enabled": False,
        "submitted_orders": 0,
        "target": cfg["target"],
        "frozen_horizons_minutes": list(cfg["horizons_minutes"]),
        "tail_fraction": float(model_cfg["tail_fraction"]),
        "discovery_source_head": discovery_cfg["source_head"],
        "discovery_cutoff_ts": int(discovery_cfg["discovery_cutoff_ts"]),
        "forward_holdout_start_ts": holdout_start,
        "frozen_training_label_cutoff_ts": frozen_training_label_cutoff,
        "frozen_holdout_fit_validated": frozen_holdout_fit_validated,
        "forward_days_observed": forward_days_observed,
        "market_count": len(markets),
        "history_market_count": len(histories),
        "history_data_healthy": history_data_healthy,
        "history_failures": history_failures[:50],
        "book_market_count": len(raw_books),
        "authoritative_fee_book_count": authoritative,
        "survivorship_safe": False,
        "point_in_time_universe_validated": False,
        "absolute_single_leg_mapping_disabled": True,
        "relative_pair_only": True,
        "common_logit_mode_neutralized_first_order": True,
        "current_relative_pair_candidates": len(pair_rows),
        "joint_fill_state_evidence_validated": False,
        "partial_fill_unwind_economics_validated": False,
        "fill_conditioned_cost_stressed_pnl_validated": False,
        "economic_pnl_validated": False,
        "horizons": horizon_reports,
        "promotion_ready": False,
        "promotion_blockers": [
            "new_forward_holdout_not_yet_promotion_complete",
            "point_in_time_universe_not_yet_attached",
            "empirical_joint_fill_states_not_yet_attached",
            "partial_fill_abort_unwind_economics_not_yet_attached",
            "shared_execution_ledger_cost_stressed_pnl_not_yet_attached",
            "research_branch_cannot_mutate_live_champion",
        ]
        + ([] if frozen_holdout_fit_validated else ["frozen_holdout_fit_not_validated"])
        + ([] if history_data_healthy else ["price_history_data_health_unhealthy"]),
    }
    atomic_json(args.output_json, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
