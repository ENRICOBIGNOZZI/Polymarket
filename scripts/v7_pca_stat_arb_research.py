#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import v6_local_factor_intents as base
import v7_pca_stat_arb_core as core


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def finite(value: Any, default: float = math.nan) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return output if math.isfinite(output) else default


def raw_market(gamma: str, market_id: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if market_id in cache:
        return cache[market_id]
    try:
        value = base.request_json(f"{gamma.rstrip('/')}/markets/{market_id}")
    except Exception:
        return None
    if isinstance(value, dict):
        cache[market_id] = value
        return value
    return None


def market_end_ts(raw: dict[str, Any]) -> int | None:
    candidates: list[Any] = [raw.get("endDate"), raw.get("end_date"), raw.get("endDateIso")]
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        candidates.extend([events[0].get("endDate"), events[0].get("end_date")])
    for value in candidates:
        if isinstance(value, (int, float)):
            timestamp = int(value)
            if timestamp > 10_000_000_000:
                timestamp //= 1000
            if timestamp > 0:
                return timestamp
        if isinstance(value, str) and value.strip():
            try:
                return int(datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
            except ValueError:
                continue
    return None


def explicit_gamma_fee(raw: dict[str, Any]) -> tuple[bool, float, float]:
    if raw.get("feesEnabled") is False:
        return True, 0.0, 1.0
    schedule = raw.get("feeSchedule")
    if not isinstance(schedule, dict):
        return False, 0.0, 1.0
    rate = finite(schedule.get("rate"))
    exponent = finite(schedule.get("exponent"), 1.0)
    if not math.isfinite(rate) or rate < 0.0:
        return False, 0.0, 1.0
    return True, rate, max(0.0, exponent)


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 single-leg PCA residual statistical-arbitrage research")
    parser.add_argument("--config", type=Path, default=Path("config/research_v7_pca_stat_arb.json"))
    parser.add_argument("--paper-config", type=Path, default=Path("config/paper_v6.json"))
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--bootstrap-reps", type=int)
    parser.add_argument("--market-limit", type=int)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if not cfg.get("paper_only") or not cfg.get("research_only") or cfg.get("live_intents_enabled"):
        raise SystemExit("PCA challenger must remain paper/research-only with live intents disabled")
    if cfg.get("hedge_legs_allowed"):
        raise SystemExit("V7 PCA stat-arb forbids hedge legs")

    paper = json.loads(args.paper_config.read_text(encoding="utf-8"))
    gamma = str(paper["gamma_url"]).rstrip("/")
    clob = str(paper["clob_url"]).rstrip("/")
    now = int(time.time())
    universe_cfg = cfg["universe"]
    history_cfg = cfg["history"]
    pca_cfg = cfg["pca"]
    execution_cfg = cfg["execution_shadow"]
    fidelity_minutes = int(history_cfg["fidelity_minutes"])
    bucket_seconds = fidelity_minutes * 60
    reps = max(50, int(args.bootstrap_reps or pca_cfg["bootstrap_repetitions"]))
    market_limit = min(int(universe_cfg["maximum_markets"]), int(args.market_limit or universe_cfg["maximum_markets"]))

    failures: list[str] = []
    try:
        markets = base.discover(gamma, market_limit, float(universe_cfg["minimum_liquidity_usd"]))
        clusters = base.clusters(markets, int(universe_cfg["maximum_clusters"]))
    except Exception as exc:
        markets, clusters = [], []
        failures.append(f"discovery:{type(exc).__name__}:{exc}")

    selected = {market.market_id: market for _key, group in clusters for market in group}
    start = now - int(history_cfg["lookback_hours"]) * 3600
    histories, history_failures = base.fetch_histories(
        clob,
        {market.market_id: market.yes for market in selected.values()},
        start,
        now,
        fidelity_minutes,
    ) if selected else ({}, [])
    failures.extend(history_failures[:30])

    try:
        token_books = base.fetch_books(clob, list(selected.values())) if selected else {}
    except Exception as exc:
        token_books = {}
        failures.append(f"books:{type(exc).__name__}:{exc}")

    raw_cache: dict[str, dict[str, Any]] = {}
    hypotheses: dict[str, tuple[str, str, core.RawPanel, core.PcaTargetModel, float]] = {}
    pvalues: dict[str, float] = {}
    tests: list[dict[str, Any]] = []
    for cluster_key, group in clusters:
        ids = [market.market_id for market in group]
        panel = core.build_raw_panel(
            histories,
            ids,
            bucket_seconds=bucket_seconds,
            min_points=int(history_cfg["minimum_regular_common_points"]),
        )
        if panel is None:
            continue
        for target in sorted(panel.values):
            result = core.target_bootstrap_pvalue(
                panel,
                target,
                reps=reps,
                seed=20260826 + sum(ord(ch) for ch in cluster_key + target),
                max_components=int(pca_cfg["maximum_components"]),
                explained_variance_threshold=float(pca_cfg["control_only_explained_variance_threshold"]),
            )
            if result is None:
                continue
            model, pvalue = result
            identity = f"{cluster_key}|{target}"
            pvalues[identity] = pvalue
            hypotheses[identity] = (cluster_key, target, panel, model, pvalue)
            tests.append(
                {
                    "hypothesis": identity,
                    "cluster": cluster_key,
                    "target": target,
                    "pvalue": pvalue,
                    "adf_t": model.adf_t,
                    "phi": model.phi,
                    "training_points": model.training_points,
                    "control_count": len(model.controls),
                    "components": len(model.eigenvalues),
                    "explained_variance": model.explained_variance,
                    "target_excluded": target not in model.controls,
                }
            )

    selected_hypotheses = core.bh_selected(pvalues, float(pca_cfg["bh_fdr"]))
    current_logits: dict[str, float] = {}
    books_by_market: dict[str, core.BookEconomics] = {}
    end_times: dict[str, int | None] = {}
    authoritative_fees = 0
    received_ts = int(time.time())
    for market_id, market in selected.items():
        yes_book = token_books.get(market.yes)
        no_book = token_books.get(market.no)
        if yes_book is None or no_book is None:
            continue
        current_logits[market_id] = core.logit(yes_book.mid)
        raw = raw_market(gamma, market_id, raw_cache)
        if raw is None:
            continue
        fee_ok, fee_rate, fee_exponent = explicit_gamma_fee(raw)
        authoritative_fees += int(fee_ok)
        end_times[market_id] = market_end_ts(raw)
        books_by_market[market_id] = core.BookEconomics(
            market_id=market_id,
            event_id=market.event_id,
            yes_bid=yes_book.bid,
            yes_ask=yes_book.ask,
            no_bid=no_book.bid,
            no_ask=no_book.ask,
            liquidity=market.liquidity,
            fee_rate=fee_rate,
            fee_exponent=fee_exponent,
            authoritative_fee=fee_ok,
            received_ts=received_ts,
        )

    horizon_reports: list[dict[str, Any]] = []
    for horizon_minutes in cfg["horizons_minutes"]:
        if int(horizon_minutes) % fidelity_minutes != 0:
            raise SystemExit(f"horizon {horizon_minutes} is not a multiple of fidelity {fidelity_minutes}")
        horizon_steps = int(horizon_minutes) // fidelity_minutes
        horizon_seconds = int(horizon_minutes) * 60
        candidates: list[core.SingleLegCandidate] = []
        score_rows: list[dict[str, Any]] = []
        for identity in sorted(selected_hypotheses):
            cluster_key, target, panel, model, pvalue = hypotheses[identity]
            if abs((model.residual_last - model.residual_mean) / model.residual_sd) < float(pca_cfg["minimum_abs_residual_z_after_bh"]):
                continue
            if any(control not in current_logits for control in model.controls) or target not in current_logits:
                continue
            score = core.score_current(model, current_logits, horizon_steps)
            if score is None:
                continue
            end_ts = end_times.get(target)
            if end_ts is None or end_ts - received_ts - 3600 < horizon_seconds:
                continue
            book = books_by_market.get(target)
            if book is None or book.liquidity < float(execution_cfg["minimum_liquidity_usd"]):
                continue
            side_spread = (book.yes_ask - book.yes_bid) if score.predicted_logit_move > 0 else (book.no_ask - book.no_bid)
            if side_spread > float(execution_cfg["maximum_spread"]):
                continue
            candidate = core.executable_candidate(
                score,
                book,
                horizon_seconds=horizon_seconds,
                now=received_ts,
                slippage_bps=float(execution_cfg["slippage_bps"]),
                capital_cost_bps_per_hour=float(execution_cfg["capital_cost_bps_per_hour"]),
                adverse_penalty_bps=float(execution_cfg["adverse_markout_penalty_bps"]),
                max_book_age_seconds=int(execution_cfg["maximum_book_age_seconds"]),
            )
            score_rows.append({"hypothesis": identity, "pvalue": pvalue, **asdict(score)})
            if candidate is not None and candidate.net_edge >= float(execution_cfg["minimum_net_edge"]) and candidate.economic_score > 0.0:
                candidates.append(candidate)
        # A target can occur in multiple structural clusters.  Keep the strongest
        # economic thesis per target/event; do not stack duplicate exposures.
        best_by_event: dict[str, core.SingleLegCandidate] = {}
        for candidate in sorted(candidates, key=lambda item: item.economic_score, reverse=True):
            current = best_by_event.get(candidate.event_id)
            if current is None or candidate.economic_score > current.economic_score:
                best_by_event[candidate.event_id] = candidate
        selected_candidates = sorted(best_by_event.values(), key=lambda item: item.economic_score, reverse=True)[: int(execution_cfg["maximum_candidates_per_horizon"])]
        horizon_reports.append(
            {
                "horizon_minutes": int(horizon_minutes),
                "scored_hypotheses": len(score_rows),
                "scores": score_rows,
                "shadow_candidates": [asdict(candidate) for candidate in selected_candidates],
            }
        )

    report = {
        "timestamp": received_ts,
        "paper_only": True,
        "research_only": True,
        "live_intents_enabled": False,
        "submitted_orders": 0,
        "hedge_legs_allowed": False,
        "single_leg_only": True,
        "terminal_probability_interpretation": False,
        "universe_backtest_mode": "current_active_survivors",
        "survivorship_safe": False,
        "markets": len(markets),
        "clusters": len(clusters),
        "histories": len(histories),
        "books": len(books_by_market),
        "authoritative_fee_books": authoritative_fees,
        "bootstrap_repetitions": reps,
        "target_hypotheses": len(pvalues),
        "bh_selected_hypotheses": len(selected_hypotheses),
        "tests": tests,
        "horizons": horizon_reports,
        "failures": failures,
        "fill_conditioned_pnl_validated": False,
        "promotion_ready": False,
        "promotion_blockers": [
            "point_in_time_or_forward_universe_not_yet_attached",
            "shared_authoritative_fee_resolver_not_yet_integrated",
            "shared_execution_ledger_forward_pnl_not_yet_attached",
            "fill_conditioned_cost_stressed_pnl_not_yet_attached",
            "research_branch_cannot_mutate_live_champion"
        ]
    }
    atomic_json(args.output_json, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
