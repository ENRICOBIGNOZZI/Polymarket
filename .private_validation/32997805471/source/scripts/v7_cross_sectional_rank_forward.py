#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import v7_cross_sectional_history as history_transport
import v7_cross_sectional_rank as app
import v7_cross_sectional_rank_core as core

SCHEMA = "v7_cross_sectional_rank_forward_v1"
FORWARD_HORIZONS_MINUTES = (120, 360)
TAIL_FRACTION = 0.20
MAX_COMPLETED_AGE_DAYS = 90
MAX_LABEL_DELAY_BUCKETS = 2


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_state(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def market_end_ts(raw: dict[str, Any]) -> int | None:
    candidates: list[Any] = [raw.get("endDate"), raw.get("end_date"), raw.get("endDateIso")]
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        candidates.extend([events[0].get("endDate"), events[0].get("end_date")])
    for value in candidates:
        if isinstance(value, (int, float)):
            ts = int(value)
            if ts > 10_000_000_000:
                ts //= 1000
            if ts > 0:
                return ts
        if isinstance(value, str) and value.strip():
            try:
                return int(datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
            except ValueError:
                continue
    return None


@dataclass(frozen=True)
class TokenMarket:
    market_id: str
    yes_token: str


def empty_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "paper_only": True,
        "research_only": True,
        "submitted_orders": 0,
        "authenticated_execution": False,
        "open_sections": [],
        "completed_sections": [],
        "invalid_sections": [],
    }


def normalize_state(value: dict[str, Any]) -> dict[str, Any]:
    state = empty_state()
    if value.get("schema") == SCHEMA:
        for key in ("open_sections", "completed_sections", "invalid_sections"):
            rows = value.get(key)
            if isinstance(rows, list):
                state[key] = [dict(row) for row in rows if isinstance(row, dict)]
    return state


def section_id(horizon_minutes: int, feature_ts: int) -> str:
    return f"xsec-forward:{int(horizon_minutes)}m:{int(feature_ts)}"


def prediction_rank_rows(
    scored: list[core.ScoreRow],
    token_by_market: dict[str, str],
    end_by_market: dict[str, int | None],
    *,
    horizon_minutes: int,
    feature_ts: int,
    published_ts: int,
    exit_buffer_seconds: int,
) -> list[dict[str, Any]]:
    horizon_seconds = int(horizon_minutes) * 60
    due_ts = int(feature_ts) + horizon_seconds
    eligible = [
        score
        for score in scored
        if token_by_market.get(score.market_id)
        and (
            end_by_market.get(score.market_id) is None
            or int(end_by_market[score.market_id] or 0) > due_ts + int(exit_buffer_seconds)
        )
    ]
    order = sorted(range(len(eligible)), key=lambda i: eligible[i].predicted_logit_move)
    ranks = [0] * len(eligible)
    for rank, index in enumerate(order):
        ranks[index] = rank
    n_tail = max(1, int(len(eligible) * TAIL_FRACTION)) if eligible else 0
    rows: list[dict[str, Any]] = []
    for index, score in enumerate(eligible):
        rank = ranks[index]
        tail = "BOTTOM" if rank < n_tail else "TOP" if rank >= len(eligible) - n_tail else "MIDDLE"
        rows.append(
            {
                "market_id": score.market_id,
                "event_id": score.event_id,
                "group": score.group,
                "yes_token": token_by_market[score.market_id],
                "feature_ts": int(feature_ts),
                "published_ts": int(published_ts),
                "due_ts": due_ts,
                "horizon_minutes": int(horizon_minutes),
                "origin_probability": float(score.probability),
                "predicted_logit_move": float(score.predicted_logit_move),
                "rank": rank,
                "rank_fraction": rank / max(1, len(eligible) - 1),
                "tail": tail,
                "end_ts": end_by_market.get(score.market_id),
            }
        )
    return rows


def mature_section(
    section: dict[str, Any],
    histories: dict[str, dict[int, float]],
    *,
    min_cross_section: int,
    group_weight: float,
    min_group_size: int,
) -> dict[str, Any] | None:
    predictions = [row for row in section.get("predictions", []) if isinstance(row, dict)]
    if not predictions:
        return None
    raw_moves: list[tuple[core.MarketMeta, float]] = []
    observed: dict[str, tuple[dict[str, Any], float]] = {}
    due_ts = int(section["due_ts"])
    for row in predictions:
        market_id = str(row.get("market_id") or "")
        p0 = app.finite(row.get("origin_probability"))
        future = histories.get(market_id, {}).get(due_ts)
        if not market_id or not math.isfinite(p0) or not 0.0 < p0 < 1.0 or future is None or not 0.0 < future < 1.0:
            continue
        meta = core.MarketMeta(market_id, str(row.get("event_id") or market_id), str(row.get("group") or "global"))
        move = core.logit(future) - core.logit(p0)
        raw_moves.append((meta, move))
        observed[market_id] = (row, future)
    required = max(int(min_cross_section), math.ceil(0.80 * len(predictions)))
    if len(raw_moves) < required:
        return None
    targets = core.target_residuals(raw_moves, group_weight, min_group_size)
    labeled: list[dict[str, Any]] = []
    for market_id, target in targets.items():
        row, future = observed[market_id]
        labeled.append(
            {
                **row,
                "future_probability": float(future),
                "realized_logit_move": core.logit(future) - core.logit(float(row["origin_probability"])),
                "target_relative_logit_move": float(target),
            }
        )
    pred = [float(row["predicted_logit_move"]) for row in labeled]
    true = [float(row["target_relative_logit_move"]) for row in labeled]
    top = [float(row["target_relative_logit_move"]) for row in labeled if row.get("tail") == "TOP"]
    bottom = [float(row["target_relative_logit_move"]) for row in labeled if row.get("tail") == "BOTTOM"]
    tail_spread = statistics.fmean(top) - statistics.fmean(bottom) if top and bottom else 0.0
    hit_rows = [
        int((float(row["predicted_logit_move"]) > 0.0) == (float(row["target_relative_logit_move"]) > 0.0))
        for row in labeled
        if abs(float(row["predicted_logit_move"])) > 1e-12 and abs(float(row["target_relative_logit_move"])) > 1e-12
    ]
    return {
        "section_id": section["section_id"],
        "feature_ts": int(section["feature_ts"]),
        "published_ts": int(section["published_ts"]),
        "due_ts": due_ts,
        "matured_ts": int(time.time()),
        "horizon_minutes": int(section["horizon_minutes"]),
        "prediction_count": len(predictions),
        "label_count": len(labeled),
        "label_coverage": len(labeled) / max(1, len(predictions)),
        "rank_ic": core.spearman(pred, true),
        "top_bottom_logit_spread": tail_spread,
        "directional_hit_rate": sum(hit_rows) / len(hit_rows) if hit_rows else 0.0,
        "labeled_predictions": labeled,
    }


def aggregate_completed(sections: list[dict[str, Any]], horizon_minutes: int) -> dict[str, Any]:
    rows = [row for row in sections if int(row.get("horizon_minutes") or 0) == int(horizon_minutes)]
    ics = [float(row["rank_ic"]) for row in rows if math.isfinite(app.finite(row.get("rank_ic")))]
    spreads = [float(row["top_bottom_logit_spread"]) for row in rows if math.isfinite(app.finite(row.get("top_bottom_logit_spread")))]
    days = sorted({int(row.get("feature_ts") or 0) // 86400 for row in rows if int(row.get("feature_ts") or 0) > 0})
    return {
        "horizon_minutes": int(horizon_minutes),
        "completed_sections": len(rows),
        "days": len(days),
        "predictions": sum(int(row.get("label_count") or 0) for row in rows),
        "mean_rank_ic": statistics.fmean(ics) if ics else 0.0,
        "median_rank_ic": statistics.median(ics) if ics else 0.0,
        "positive_rank_ic_fraction": sum(value > 0 for value in ics) / len(ics) if ics else 0.0,
        "mean_top_bottom_logit_spread": statistics.fmean(spreads) if spreads else 0.0,
        "median_top_bottom_logit_spread": statistics.median(spreads) if spreads else 0.0,
        "positive_top_bottom_fraction": sum(value > 0 for value in spreads) / len(spreads) if spreads else 0.0,
    }


def forward_gate(summary: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if int(summary["days"]) < 14:
        reasons.append("minimum_14_forward_days")
    if int(summary["completed_sections"]) < 40:
        reasons.append("minimum_40_forward_sections")
    if float(summary["median_rank_ic"]) < 0.02:
        reasons.append("median_rank_ic")
    if float(summary["positive_rank_ic_fraction"]) < 0.55:
        reasons.append("rank_ic_stability")
    if float(summary["median_top_bottom_logit_spread"]) <= 0.0:
        reasons.append("tail_spread")
    if float(summary["positive_top_bottom_fraction"]) < 0.55:
        reasons.append("tail_stability")
    return not reasons, reasons


def run_observer(
    *,
    cfg: dict[str, Any],
    gamma_url: str,
    clob_url: str,
    market_limit: int,
    state: dict[str, Any],
    now: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    history_cfg = cfg["history"]
    execution_cfg = cfg["execution_shadow"]
    model_cfg = cfg["model"]
    fidelity_minutes = int(history_cfg["fidelity_minutes"])
    bucket_seconds = fidelity_minutes * 60
    min_cross_section = int(history_cfg["minimum_cross_section"])
    failures: list[str] = []

    try:
        markets = app.discover_markets(gamma_url, int(market_limit), float(execution_cfg["minimum_liquidity_usd"]))
    except Exception as exc:
        markets = []
        failures.append(f"discovery:{type(exc).__name__}:{exc}")

    current_by_id = {market.market_id: market for market in markets}
    token_by_market = {market.market_id: market.yes_token for market in markets}
    end_by_market = {market.market_id: market_end_ts(market.raw) for market in markets}

    old_tokens: dict[str, str] = {}
    for section in state["open_sections"]:
        for row in section.get("predictions", []):
            if isinstance(row, dict) and row.get("market_id") and row.get("yes_token"):
                old_tokens[str(row["market_id"])] = str(row["yes_token"])
    fetch_markets: list[Any] = list(markets)
    for market_id, token in sorted(old_tokens.items()):
        if market_id not in current_by_id:
            fetch_markets.append(TokenMarket(market_id, token))

    start_ts = now - int(history_cfg["lookback_hours"]) * 3600
    try:
        histories, history_failures = history_transport.fetch_histories(
            clob_url,
            fetch_markets,
            start_ts,
            now,
            fidelity_minutes,
        ) if fetch_markets else ({}, [])
        failures.extend(history_failures[:50])
    except Exception as exc:
        histories = {}
        failures.append(f"history:{type(exc).__name__}:{exc}")

    current_histories = {market_id: histories[market_id] for market_id in current_by_id if market_id in histories}
    metadata = {
        market.market_id: core.MarketMeta(market.market_id, market.event_id, market.group)
        for market in markets
        if market.market_id in current_histories
    }
    healthy = len(current_histories) >= min_cross_section

    # First mature prior point-in-time sections. They use their stored token/universe
    # identities rather than today's active-universe membership.
    still_open: list[dict[str, Any]] = []
    completed = list(state["completed_sections"])
    invalid = list(state["invalid_sections"])
    for section in state["open_sections"]:
        due_ts = int(section.get("due_ts") or 0)
        if due_ts <= 0 or now < due_ts:
            still_open.append(section)
            continue
        result = mature_section(
            section,
            histories,
            min_cross_section=min_cross_section,
            group_weight=float(history_cfg["group_neutralization_weight"]),
            min_group_size=int(history_cfg["minimum_group_size"]),
        )
        if result is not None:
            completed.append(result)
        elif now <= due_ts + MAX_LABEL_DELAY_BUCKETS * bucket_seconds:
            still_open.append(section)
        else:
            invalid.append(
                {
                    "section_id": section.get("section_id"),
                    "feature_ts": section.get("feature_ts"),
                    "published_ts": section.get("published_ts"),
                    "due_ts": due_ts,
                    "horizon_minutes": section.get("horizon_minutes"),
                    "reason": "insufficient_forward_label_coverage",
                }
            )

    # Point-in-time prediction origin is frozen to the latest completed historical
    # cross-section actually available at publication time. No current book is
    # inserted into the statistical history panel.
    existing_ids = {str(section.get("section_id") or "") for section in still_open + completed}
    new_sections = 0
    if healthy:
        score_ts = app.latest_score_time(current_histories, metadata, bucket_seconds, min_cross_section)
        if score_ts > 0 and now - score_ts <= 2 * bucket_seconds:
            for horizon_minutes in FORWARD_HORIZONS_MINUTES:
                if horizon_minutes % fidelity_minutes != 0:
                    continue
                sid = section_id(horizon_minutes, score_ts)
                if sid in existing_ids:
                    continue
                horizon_steps = horizon_minutes // fidelity_minutes
                rows = core.build_training_rows(
                    current_histories,
                    metadata,
                    bucket_seconds=bucket_seconds,
                    horizon_steps=horizon_steps,
                    min_cross_section=min_cross_section,
                    group_weight=float(history_cfg["group_neutralization_weight"]),
                    min_group_size=int(history_cfg["minimum_group_size"]),
                )
                fit = core.fit_ridge(
                    rows,
                    asof_ts=score_ts,
                    window_seconds=int(history_cfg["training_window_days"]) * 86400,
                    embargo_seconds=int(history_cfg["purge_embargo_buckets"]) * bucket_seconds,
                    ridge=float(model_cfg["ridge_penalty"]),
                    half_life_seconds=int(history_cfg["recency_half_life_days"]) * 86400,
                    min_rows=int(model_cfg["minimum_training_rows"]),
                    min_cross_sections=int(model_cfg["minimum_training_cross_sections"]),
                )
                snapshot = core.score_snapshot(current_histories, metadata, score_ts, bucket_seconds, min_cross_section)
                if fit is None or len(snapshot) < int(model_cfg["minimum_training_cross_sections"]):
                    continue
                scored = core.apply_fit(snapshot, fit, score_ts)
                predictions = prediction_rank_rows(
                    scored,
                    token_by_market,
                    end_by_market,
                    horizon_minutes=horizon_minutes,
                    feature_ts=score_ts,
                    published_ts=now,
                    exit_buffer_seconds=3600,
                )
                if len(predictions) < int(model_cfg["minimum_training_cross_sections"]):
                    continue
                still_open.append(
                    {
                        "section_id": sid,
                        "feature_ts": score_ts,
                        "published_ts": now,
                        "publication_delay_seconds": now - score_ts,
                        "due_ts": score_ts + horizon_minutes * 60,
                        "horizon_minutes": horizon_minutes,
                        "tail_fraction": TAIL_FRACTION,
                        "universe_mode": "prospective_point_in_time_active_markets",
                        "prediction_count": len(predictions),
                        "predictions": predictions,
                    }
                )
                existing_ids.add(sid)
                new_sections += 1

    cutoff_ts = now - MAX_COMPLETED_AGE_DAYS * 86400
    completed = [row for row in completed if int(row.get("feature_ts") or 0) >= cutoff_ts]
    invalid = [row for row in invalid if int(row.get("feature_ts") or 0) >= cutoff_ts]
    new_state = {
        "schema": SCHEMA,
        "paper_only": True,
        "research_only": True,
        "submitted_orders": 0,
        "authenticated_execution": False,
        "updated_ts": now,
        "open_sections": still_open,
        "completed_sections": completed,
        "invalid_sections": invalid,
    }

    summaries: list[dict[str, Any]] = []
    for horizon_minutes in FORWARD_HORIZONS_MINUTES:
        summary = aggregate_completed(completed, horizon_minutes)
        gate, reasons = forward_gate(summary)
        summary["forward_statistical_gate"] = gate
        summary["gate_reasons"] = reasons
        summaries.append(summary)
    report = {
        "schema": SCHEMA,
        "timestamp": now,
        "paper_only": True,
        "research_only": True,
        "submitted_orders": 0,
        "authenticated_execution": False,
        "live_intents_enabled": False,
        "point_in_time_universe": True,
        "horizons_minutes": list(FORWARD_HORIZONS_MINUTES),
        "tail_fraction": TAIL_FRACTION,
        "market_count": len(markets),
        "history_market_count": len(current_histories),
        "history_data_healthy": healthy,
        "history_failures": failures,
        "new_sections": new_sections,
        "open_sections": len(still_open),
        "completed_sections": len(completed),
        "invalid_sections": len(invalid),
        "forward": summaries,
        "economic_pnl_validated": False,
        "promotion_ready": False,
        "promotion_blockers": [
            "forward_statistical_observer_is_not_execution_evidence",
            "shared_execution_ledger_cost_stressed_pnl_not_yet_attached",
            "research_observer_cannot_mutate_live_champion",
        ],
    }
    return new_state, report


def main() -> int:
    parser = argparse.ArgumentParser(description="Prospective V7 cross-sectional ranking observer")
    parser.add_argument("--config", type=Path, default=Path("config/research_v7_cross_sectional_rank.json"))
    parser.add_argument("--state-in", type=Path)
    parser.add_argument("--state-out", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--market-limit", type=int, default=150)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if not cfg.get("paper_only") or not cfg.get("research_only") or cfg.get("live_intents_enabled"):
        raise SystemExit("forward ranking observer requires paper/research-only config with live intents disabled")
    state = normalize_state(read_state(args.state_in))
    new_state, report = run_observer(
        cfg=cfg,
        gamma_url=args.gamma_url,
        clob_url=args.clob_url,
        market_limit=args.market_limit,
        state=state,
        now=int(time.time()),
    )
    atomic_json(args.state_out, new_state)
    atomic_json(args.output_json, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
