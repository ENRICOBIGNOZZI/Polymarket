#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from polymarket_fees import resolve_fee_details
import v6_local_factor_intents as base
import v7_pca_stat_arb_core as core
import v7_pca_stat_arb_inference as inference

HISTORY_WINDOW_SECONDS = 7 * 24 * 60 * 60


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


def fetch_histories_chunked(
    clob: str,
    token_by_market: dict[str, str],
    start_ts: int,
    end_ts: int,
    fidelity_minutes: int,
) -> tuple[dict[str, dict[int, float]], list[str]]:
    histories: dict[str, dict[int, float]] = {}
    failures: list[str] = []
    window_start = start_ts
    while window_start < end_ts:
        window_end = min(end_ts, window_start + HISTORY_WINDOW_SECONDS)
        partial, partial_failures = base.fetch_histories(
            clob,
            token_by_market,
            window_start,
            window_end,
            fidelity_minutes,
        )
        for market_id, series in partial.items():
            histories.setdefault(market_id, {}).update(series)
        failures.extend(f"{window_start}:{window_end}:{failure}" for failure in partial_failures)
        window_start = window_end
    return histories, failures


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 single-leg PCA residual statistical-arbitrage research")
    parser.add_argument("--config", type=Path, default=Path("config/research_v7_pca_stat_arb.json"))
    parser.add_argument("--paper-config", type=Path, default=Path("config/paper_v7.json"))
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--bootstrap-reps", type=int)
    parser.add_argument("--market-limit", type=int)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if int(cfg.get("schema_version", 0)) < 2:
        raise SystemExit("PCA successor requires schema_version >= 2")
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
    if pca_cfg.get("multiplicity_method") != "benjamini_yekutieli_arbitrary_dependence":
        raise SystemExit("V7 PCA requires dependence-robust Benjamini-Yekutieli multiplicity")

    failures: list[str] = []
    try:
        markets = base.discover(gamma, market_limit, float(universe_cfg["minimum_liquidity_usd"]))
        clusters = base.clusters(markets, int(universe_cfg["maximum_clusters"]))
    except Exception as exc:
        markets, clusters = [], []
        failures.append(f"discovery:{type(exc).__name__}:{exc}")

    # Freeze target hypotheses and bounded nuisance controls from contract metadata
    # BEFORE price history is fetched. Missing controls later remain p=1; they are
    # never replaced using observed missingness, returns, residuals, edge or PnL.
    predeclared: dict[str, tuple[str, str, inference.TargetControlPlan]] = {}
    for cluster_key, group in clusters:
        for market in sorted(group, key=lambda item: item.market_id):
            plan = inference.predeclare_target_controls(
                group,
                market.market_id,
                minimum_controls=int(pca_cfg["minimum_predeclared_controls"]),
                maximum_controls=int(pca_cfg["maximum_predeclared_controls"]),
            )
            if plan is not None:
                identity = f"{cluster_key}|{market.market_id}"
                predeclared[identity] = (cluster_key, market.market_id, plan)

    selected = {market.market_id: market for _key, group in clusters for market in group}
    start = now - int(history_cfg["lookback_hours"]) * 3600
    histories, history_failures = fetch_histories_chunked(
        clob,
        {market.market_id: market.yes for market in selected.values()},
        start,
        now,
        fidelity_minutes,
    ) if selected else ({}, [])
    failures.extend(history_failures[:50])
    history_required_market_count = int(universe_cfg["minimum_cluster_markets"])
    history_data_healthy = len(histories) >= history_required_market_count

    try:
        token_books = base.fetch_books(clob, list(selected.values())) if selected else {}
        books_received_ts = int(time.time())
    except Exception as exc:
        token_books = {}
        books_received_ts = int(time.time())
        failures.append(f"books:{type(exc).__name__}:{exc}")

    hypotheses: dict[str, tuple[str, str, core.RawPanel, core.PcaTargetModel, float]] = {}
    pvalues: dict[str, float] = {identity: 1.0 for identity in predeclared}
    tests: list[dict[str, Any]] = []
    unestimable = 0
    for identity in sorted(predeclared):
        cluster_key, target, plan = predeclared[identity]
        panel = inference.build_predeclared_target_panel(
            histories,
            plan,
            bucket_seconds=bucket_seconds,
            min_points=int(history_cfg["minimum_regular_common_points"]),
        )
        if panel is None:
            unestimable += 1
            tests.append({
                "hypothesis": identity,
                "cluster": cluster_key,
                "target": target,
                "controls": list(plan.controls),
                "estimable": False,
                "pvalue": 1.0,
                "reason": "predeclared_target_or_control_history_unavailable",
            })
            continue
        result = inference.conditional_target_bootstrap_pvalue(
            panel,
            target,
            reps=reps,
            seed=20260826 + sum(ord(ch) for ch in identity),
            max_components=int(pca_cfg["maximum_components"]),
            explained_variance_threshold=float(pca_cfg["control_only_explained_variance_threshold"]),
            ridge=float(pca_cfg["target_factor_ridge"]),
        )
        if result is None:
            unestimable += 1
            tests.append({
                "hypothesis": identity,
                "cluster": cluster_key,
                "target": target,
                "controls": list(plan.controls),
                "estimable": False,
                "pvalue": 1.0,
                "reason": "conditional_null_inference_unestimable",
            })
            continue
        model, pvalue = result
        pvalues[identity] = pvalue
        hypotheses[identity] = (cluster_key, target, panel, model, pvalue)
        tests.append({
            "hypothesis": identity,
            "cluster": cluster_key,
            "target": target,
            "controls": list(plan.controls),
            "estimable": True,
            "pvalue": pvalue,
            "adf_t": model.adf_t,
            "phi": model.phi,
            "training_points": model.training_points,
            "control_count": len(model.controls),
            "components": len(model.eigenvalues),
            "explained_variance": model.explained_variance,
            "target_excluded": target not in model.controls,
            "null": "target_residual_i1_conditional_on_fixed_observed_controls",
        })

    fdr_q = float(pca_cfg["fdr_q"])
    selected_hypotheses = inference.benjamini_yekutieli_selected(pvalues, fdr_q)

    current_logits: dict[str, float] = {}
    books_by_market: dict[str, core.BookEconomics] = {}
    end_times: dict[str, int | None] = {}
    authoritative_fees = 0
    fee_failures = 0
    raw_cache: dict[str, dict[str, Any]] = {}
    for market_id, market in selected.items():
        yes_book = token_books.get(market.yes)
        no_book = token_books.get(market.no)
        if yes_book is None or no_book is None:
            continue
        current_logits[market_id] = core.logit(yes_book.mid)
        raw = raw_market(gamma, market_id, raw_cache)
        if raw is None:
            continue
        try:
            fee = resolve_fee_details(raw, clob, base.request_json)
            fee_rate = fee.rate if fee.enabled else 0.0
            fee_exponent = fee.exponent
            fee_ok = True
            authoritative_fees += 1
        except Exception as exc:
            fee_rate = 0.0
            fee_exponent = 1.0
            fee_ok = False
            fee_failures += 1
            if len(failures) < 60:
                failures.append(f"fee:{market_id}:{type(exc).__name__}")
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
            received_ts=books_received_ts,
        )

    horizon_reports: list[dict[str, Any]] = []
    uncertainty_z = max(0.0, float(execution_cfg["uncertainty_z"]))
    for horizon_minutes in cfg["horizons_minutes"]:
        if int(horizon_minutes) % fidelity_minutes != 0:
            raise SystemExit(f"horizon {horizon_minutes} is not a multiple of fidelity {fidelity_minutes}")
        horizon_steps = int(horizon_minutes) // fidelity_minutes
        horizon_seconds = int(horizon_minutes) * 60
        candidate_rows: list[tuple[core.SingleLegCandidate, float]] = []
        score_rows: list[dict[str, Any]] = []
        for identity in sorted(selected_hypotheses):
            item = hypotheses.get(identity)
            if item is None:
                continue
            cluster_key, target, panel, model, pvalue = item
            residual_z = abs((model.residual_last - model.residual_mean) / model.residual_sd)
            if residual_z < float(pca_cfg["minimum_abs_residual_z_after_multiplicity"]):
                continue
            if any(control not in current_logits for control in model.controls) or target not in current_logits:
                continue
            score = inference.score_with_total_single_leg_risk(panel, model, current_logits, horizon_steps)
            if score is None:
                continue
            end_ts = end_times.get(target)
            if end_ts is None or end_ts - books_received_ts - 3600 < horizon_seconds:
                continue
            book = books_by_market.get(target)
            if book is None or book.liquidity < float(execution_cfg["minimum_liquidity_usd"]):
                continue
            side_spread = (book.yes_ask - book.yes_bid) if score.predicted_logit_move > 0 else (book.no_ask - book.no_bid)
            if side_spread > float(execution_cfg["maximum_spread"]):
                continue
            raw_candidate = core.executable_candidate(
                score,
                book,
                horizon_seconds=horizon_seconds,
                now=books_received_ts,
                slippage_bps=float(execution_cfg["slippage_bps"]),
                capital_cost_bps_per_hour=float(execution_cfg["capital_cost_bps_per_hour"]),
                adverse_penalty_bps=float(execution_cfg["adverse_markout_penalty_bps"]),
                max_book_age_seconds=int(execution_cfg["maximum_book_age_seconds"]),
            )
            uncertainty_penalty = 0.0
            candidate = None
            if raw_candidate is not None:
                uncertainty_penalty = uncertainty_z * raw_candidate.uncertainty_probability
                adjusted_net = raw_candidate.net_edge - uncertainty_penalty
                adjusted_score = adjusted_net / max(raw_candidate.uncertainty_probability, 1e-5) / math.sqrt(
                    max(1.0 / 12.0, horizon_seconds / 3600.0)
                )
                candidate = replace(raw_candidate, net_edge=adjusted_net, economic_score=adjusted_score)
            score_rows.append({
                "hypothesis": identity,
                "cluster": cluster_key,
                "pvalue": pvalue,
                "multiplicity": "BY",
                "total_single_leg_risk": True,
                "uncertainty_z": uncertainty_z,
                "uncertainty_penalty_probability": uncertainty_penalty,
                **asdict(score),
            })
            if candidate is not None and candidate.net_edge >= float(execution_cfg["minimum_net_edge"]) and candidate.economic_score > 0.0:
                candidate_rows.append((candidate, uncertainty_penalty))

        best_by_event: dict[str, tuple[core.SingleLegCandidate, float]] = {}
        for candidate, uncertainty_penalty in sorted(candidate_rows, key=lambda item: item[0].economic_score, reverse=True):
            current = best_by_event.get(candidate.event_id)
            if current is None or candidate.economic_score > current[0].economic_score:
                best_by_event[candidate.event_id] = (candidate, uncertainty_penalty)
        selected_candidates = sorted(best_by_event.values(), key=lambda item: item[0].economic_score, reverse=True)[
            : int(execution_cfg["maximum_candidates_per_horizon"])
        ]
        horizon_reports.append({
            "horizon_minutes": int(horizon_minutes),
            "scored_hypotheses": len(score_rows),
            "scores": score_rows,
            "shadow_candidates": [
                {**asdict(candidate), "uncertainty_penalty": uncertainty_penalty}
                for candidate, uncertainty_penalty in selected_candidates
            ],
        })

    report = {
        "timestamp": books_received_ts,
        "schema_version": 2,
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
        "history_required_market_count": history_required_market_count,
        "history_data_healthy": history_data_healthy,
        "history_window_days": HISTORY_WINDOW_SECONDS // 86400,
        "books": len(books_by_market),
        "authoritative_fee_books": authoritative_fees,
        "authoritative_fee_failures": fee_failures,
        "bootstrap_repetitions": reps,
        "bootstrap_type": pca_cfg["bootstrap_type"],
        "predeclared_target_hypotheses": len(predeclared),
        "unestimable_predeclared_hypotheses": unestimable,
        "unestimable_pvalue": 1.0,
        "testable_target_hypotheses": len(hypotheses),
        "multiplicity_method": "benjamini_yekutieli_arbitrary_dependence",
        "fdr_q": fdr_q,
        "by_effective_q": inference.by_effective_q(len(pvalues), fdr_q),
        "by_selected_hypotheses": len(selected_hypotheses),
        "tests": tests,
        "horizons": horizon_reports,
        "failures": failures,
        "total_single_leg_forecast_risk": True,
        "uncertainty_deducted_from_executable_ev": True,
        "shared_authoritative_fee_resolver": True,
        "fill_conditioned_pnl_validated": False,
        "promotion_ready": False,
        "promotion_blockers": [
            "point_in_time_or_forward_universe_not_yet_attached",
            "shared_execution_ledger_forward_pnl_not_yet_attached",
            "fill_conditioned_cost_stressed_pnl_not_yet_attached",
            "research_branch_cannot_mutate_live_champion",
        ] + ([] if history_data_healthy else ["price_history_data_health_unhealthy"]),
    }
    atomic_json(args.output_json, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
