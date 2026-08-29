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

import v7_local_factor_data as base
import v7_local_factor_core as core
import v7_local_factor_inference as inference
import v7_local_factor_multiplicity as multiplicity
import v7_local_factor_pairs as pairing

HISTORY_WINDOW_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_AUTO_BOOTSTRAP_REPS = 15000
DEFAULT_MINIMUM_TEXT_SIMILARITY = 0.20
DEFAULT_MAXIMUM_PAIR_CONTROLS = 4
DEFAULT_MAXIMUM_HISTORY_STATE_AGE_BUCKETS = 2.0


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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
                pass
    return None


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
        partial, partial_failures = base.fetch_histories(clob, token_by_market, window_start, window_end, fidelity_minutes)
        for market_id, series in partial.items():
            histories.setdefault(market_id, {}).update(series)
        failures.extend(f"{window_start}:{window_end}:{failure}" for failure in partial_failures)
        window_start = window_end
    return histories, failures


def freshness_row(key: tuple[str, str, str], assessment: core.PanelFreshness, *, stage: str) -> dict[str, Any]:
    cluster_key, market_a, market_b = key
    return {
        "cluster": cluster_key,
        "market_a": market_a,
        "market_b": market_b,
        "stage": stage,
        **asdict(assessment),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Dependence-robust sparse V7 Local Factor research challenger")
    parser.add_argument("--config", type=Path, default=Path("config/research_v7_local_factor.json"))
    parser.add_argument("--paper-config", type=Path, default=Path("config/paper_v7.json"))
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--bootstrap-reps", type=int)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if not cfg.get("paper_only") or not cfg.get("research_only") or cfg.get("live_intents_enabled"):
        raise SystemExit("Local Factor challenger must remain paper/research-only with live intents disabled")
    if int(cfg.get("schema_version", 0)) < 2:
        raise SystemExit("Local Factor BY successor requires schema_version >= 2")

    paper = json.loads(args.paper_config.read_text(encoding="utf-8"))
    gamma = str(paper["gamma_url"]).rstrip("/")
    clob = str(paper["clob_url"]).rstrip("/")
    now = int(time.time())
    universe_cfg = cfg["universe"]
    history_cfg = cfg["history"]
    inference_cfg = cfg["inference"]
    bucket_seconds = int(history_cfg["fidelity_minutes"]) * 60
    if history_cfg.get("completed_buckets_only") is not True:
        raise SystemExit("Local Factor V7 requires completed_buckets_only=true")
    if history_cfg.get("fail_closed_on_stale_state") is not True:
        raise SystemExit("Local Factor V7 requires fail_closed_on_stale_state=true")
    maximum_history_state_age_buckets = float(history_cfg.get("maximum_history_state_age_buckets", DEFAULT_MAXIMUM_HISTORY_STATE_AGE_BUCKETS))
    if not math.isfinite(maximum_history_state_age_buckets) or maximum_history_state_age_buckets < 0.0:
        raise SystemExit("invalid maximum_history_state_age_buckets")

    requested_reps = max(50, int(args.bootstrap_reps or inference_cfg["bootstrap_repetitions"]))
    min_controls = int(inference_cfg["minimum_pair_controls"])
    max_controls = max(min_controls, int(inference_cfg.get("maximum_pair_controls", DEFAULT_MAXIMUM_PAIR_CONTROLS)))
    fdr_q = float(inference_cfg["fdr_q"])
    if inference_cfg.get("multiplicity_method") != "benjamini_yekutieli_arbitrary_dependence":
        raise SystemExit("Local Factor successor requires dependence-robust Benjamini-Yekutieli FDR")
    min_text_similarity = float(inference_cfg.get("structural_pair_minimum_text_similarity", DEFAULT_MINIMUM_TEXT_SIMILARITY))
    max_auto_reps = max(requested_reps, int(inference_cfg.get("maximum_bootstrap_repetitions", DEFAULT_MAX_AUTO_BOOTSTRAP_REPS)))

    failures: list[str] = []
    try:
        markets = base.discover(gamma, int(universe_cfg["maximum_markets"]), float(universe_cfg["minimum_liquidity_usd"]))
        cluster_rows = base.clusters(markets, int(universe_cfg["maximum_clusters"]))
    except Exception as exc:
        markets, cluster_rows = [], []
        failures.append(f"discovery:{type(exc).__name__}:{exc}")

    pair_graphs: dict[str, pairing.StructuralPairGraph] = {
        cluster_key: pairing.build_structural_pair_graph(cluster_key, group, min_controls=min_controls, minimum_text_similarity=min_text_similarity)
        for cluster_key, group in cluster_rows
    }
    pair_control_plans: dict[str, dict[tuple[str, str], tuple[str, ...]]] = {
        cluster_key: pairing.predeclare_pair_controls(group, pair_graphs[cluster_key].pairs, min_controls=min_controls, max_controls=max_controls)
        for cluster_key, group in cluster_rows
        if cluster_key in pair_graphs
    }
    predeclared_keys = {(cluster_key, a, b) for cluster_key, graph in pair_graphs.items() for a, b in graph.pairs}
    predeclared_pair_count = len(predeclared_keys)
    requested_resolution = multiplicity.by_resolution_diagnostics(predeclared_pair_count, requested_reps, fdr_q)
    repetitions_required = int(requested_resolution["repetitions_required_for_singleton_by_resolution"])
    reps = min(max_auto_reps, max(requested_reps, repetitions_required))

    selected = {market.market_id: market for _, group in cluster_rows for market in group}
    start = now - int(history_cfg["lookback_hours"]) * 3600
    histories, history_failures = fetch_histories_chunked(
        clob,
        {market.market_id: market.yes for market in selected.values()},
        start,
        now,
        int(history_cfg["fidelity_minutes"]),
    ) if selected else ({}, [])
    failures.extend(history_failures[:50])
    completed_histories = core.completed_history_view(histories, now=now, bucket_seconds=bucket_seconds)
    history_required_market_count = 2 + min_controls
    history_data_healthy = len(completed_histories) >= history_required_market_count

    tests: list[dict[str, Any]] = []
    freshness_rejections: list[dict[str, Any]] = []
    pvalues: dict[tuple[str, str, str], float] = {key: 1.0 for key in predeclared_keys}
    fit_by_key: dict[tuple[str, str, str], tuple[core.StandardizedPanel, core.PairFit]] = {}
    for cluster_key, group in cluster_rows:
        graph = pair_graphs.get(cluster_key)
        if graph is None or not graph.pairs:
            continue
        control_plan = pair_control_plans.get(cluster_key, {})
        for market_a, market_b in graph.pairs:
            key = (cluster_key, market_a, market_b)
            controls = control_plan.get((market_a, market_b), ())
            if len(controls) < min_controls:
                continue
            pair_market_ids = [market_a, market_b, *controls]
            panel = core.build_regular_panel(completed_histories, pair_market_ids, bucket_seconds=bucket_seconds, min_points=int(history_cfg["minimum_regular_common_points"]))
            if panel is None or any(market_id not in panel.values for market_id in pair_market_ids):
                continue
            fit_freshness = core.assess_panel_freshness(panel, now=now, bucket_seconds=bucket_seconds, maximum_age_buckets=maximum_history_state_age_buckets)
            if not fit_freshness.fresh:
                freshness_rejections.append(freshness_row(key, fit_freshness, stage="pre_inference"))
                continue
            boot = inference.panel_pair_iut_pvalues(
                panel,
                pairs=[(market_a, market_b)],
                reps=reps,
                seed=20260826 + sum(ord(ch) for ch in f"{cluster_key}:{market_a}:{market_b}"),
                min_controls=min_controls,
            )
            result = boot.get((market_a, market_b))
            if result is None:
                continue
            fit, pvalue = result
            if key not in pvalues:
                raise RuntimeError("estimated Local Factor pair was not in the predeclared structural family")
            pvalues[key] = pvalue
            fit_by_key[key] = (panel, fit)
            tests.append({
                "cluster": cluster_key,
                "pair_graph_method": graph.method,
                "market_a": market_a,
                "market_b": market_b,
                "pvalue": pvalue,
                "pair_stat": fit.pair_stat,
                "adf_a": fit.adf_a,
                "adf_b": fit.adf_b,
                "controls": len(fit.controls),
                "control_ids": list(controls),
                "regular_points": len(panel.times),
                "latest_completed_bucket_end_ts": fit_freshness.latest_completed_bucket_end_ts,
                "history_state_age_seconds_at_inference": fit_freshness.state_age_seconds,
                "maximum_history_state_age_seconds": fit_freshness.maximum_state_age_seconds,
            })

    selected_pairs = multiplicity.by_selected(pvalues, fdr_q)
    resolution = multiplicity.by_resolution_diagnostics(predeclared_pair_count, reps, fdr_q)
    books: dict[str, Any] = {}
    if selected_pairs:
        try:
            books = base.fetch_books(clob, list(selected.values()))
        except Exception as exc:
            failures.append(f"books:{type(exc).__name__}:{exc}")

    raw_cache: dict[str, dict[str, Any]] = {}
    signals: list[dict[str, Any]] = []
    for key in sorted(selected_pairs):
        if key not in fit_by_key:
            continue
        cluster_key, market_a_id, market_b_id = key
        panel, fit = fit_by_key[key]
        signal_now = int(time.time())
        signal_freshness = core.assess_panel_freshness(panel, now=signal_now, bucket_seconds=bucket_seconds, maximum_age_buckets=maximum_history_state_age_buckets)
        if not signal_freshness.fresh:
            freshness_rejections.append(freshness_row(key, signal_freshness, stage="pre_signal_current_book"))
            continue
        market_a = selected.get(market_a_id)
        market_b = selected.get(market_b_id)
        if market_a is None or market_b is None:
            continue
        yes_a = books.get(market_a.yes)
        yes_b = books.get(market_b.yes)
        if yes_a is None or yes_b is None:
            continue
        raw_a = raw_market(gamma, market_a_id, raw_cache)
        raw_b = raw_market(gamma, market_b_id, raw_cache)
        if raw_a is None or raw_b is None:
            continue
        signal = core.build_pair_signal(
            fit,
            pvalues[key],
            {market_a_id: yes_a.mid, market_b_id: yes_b.mid},
            {market_a_id: panel.scales[market_a_id], market_b_id: panel.scales[market_b_id]},
            bucket_seconds=bucket_seconds,
            now=signal_now,
            end_ts={market_a_id: market_end_ts(raw_a), market_b_id: market_end_ts(raw_b)},
            exit_buffer_seconds=int(cfg["forecast"]["time_to_resolution_exit_buffer_seconds"]),
            min_abs_z=float(inference_cfg["minimum_abs_residual_z_after_multiplicity"]),
            max_hold_seconds=int(cfg["forecast"]["maximum_hold_hours"]) * 3600,
            min_weight=float(cfg["hedge"]["minimum_weight"]),
            max_weight=float(cfg["hedge"]["maximum_weight"]),
        )
        if signal is not None:
            signals.append({
                "cluster": cluster_key,
                "latest_completed_bucket_end_ts": signal_freshness.latest_completed_bucket_end_ts,
                "history_state_age_seconds_at_signal": signal_freshness.state_age_seconds,
                "maximum_history_state_age_seconds": signal_freshness.maximum_state_age_seconds,
                **asdict(signal),
            })

    promotion_blockers = [
        "joint_fill_state_evidence_not_yet_attached",
        "partial_abort_unwind_economics_not_yet_attached",
        "fill_conditioned_cost_stressed_pnl_not_yet_attached",
        "point_in_time_survivorship_safe_universe_not_yet_attached",
        "research_branch_cannot_mutate_live_champion",
    ]
    if not selected_pairs:
        promotion_blockers.append("no_dependence_robust_statistically_selected_pair")
    if selected_pairs and not signals:
        promotion_blockers.append("no_fresh_post_multiplicity_pair_signal")
    if not history_data_healthy:
        promotion_blockers.append("price_history_data_health_unhealthy")
    if predeclared_pair_count and not bool(resolution["singleton_by_resolution_adequate"]):
        promotion_blockers.append("bootstrap_resolution_too_coarse_for_by_multiplicity")

    report = {
        "timestamp": int(time.time()),
        "schema_version": 3,
        "paper_only": True,
        "research_only": True,
        "live_intents_enabled": False,
        "submitted_orders": 0,
        "markets": len(markets),
        "clusters": len(cluster_rows),
        "raw_histories": len(histories),
        "histories": len(completed_histories),
        "history_required_market_count": history_required_market_count,
        "history_data_healthy": history_data_healthy,
        "history_window_days": HISTORY_WINDOW_SECONDS // 86400,
        "history_completed_buckets_only": True,
        "history_current_bucket_excluded": True,
        "maximum_history_state_age_buckets": maximum_history_state_age_buckets,
        "maximum_history_state_age_seconds": int(maximum_history_state_age_buckets * bucket_seconds),
        "history_freshness_rejections": freshness_rejections,
        "history_freshness_rejection_count": len(freshness_rejections),
        "survivorship_safe": False,
        "pair_graph_selection": "contract_metadata_only_before_price_history",
        "pair_panel_construction": "predeclared_pair_plus_bounded_metadata_only_controls_completed_regular_buckets_only",
        "pair_controls_frozen_before_price_history": True,
        "maximum_pair_controls": max_controls,
        "pair_graph_minimum_text_similarity": min_text_similarity,
        "predeclared_pair_count": predeclared_pair_count,
        "testable_pair_hypotheses": len(fit_by_key),
        "unestimable_predeclared_hypotheses": predeclared_pair_count - len(fit_by_key),
        "unestimable_predeclared_pvalue": 1.0,
        "pair_control_plans": [
            {
                "cluster": cluster_key,
                "market_a": market_a,
                "market_b": market_b,
                "controls": list(pair_control_plans.get(cluster_key, {}).get((market_a, market_b), ())),
            }
            for cluster_key, graph in pair_graphs.items()
            for market_a, market_b in graph.pairs
        ],
        "pair_graphs": {
            cluster_key: {
                "method": graph.method,
                "pair_count": graph.pair_count,
                "threshold_markets": graph.threshold_markets,
                "text_markets": graph.text_markets,
            }
            for cluster_key, graph in pair_graphs.items()
        },
        "pair_pvalue_method": "intersection_union_max_marginal_conditional_null_preserving_bootstrap_pvalue",
        "multiplicity_method": "benjamini_yekutieli_arbitrary_dependence",
        "fdr_q": fdr_q,
        "pair_hypotheses": predeclared_pair_count,
        "requested_bootstrap_repetitions": requested_reps,
        "bootstrap_repetitions": reps,
        "bootstrap_repetition_cap": max_auto_reps,
        "predeclared_repetitions_required_for_singleton_by_resolution": repetitions_required,
        "bootstrap_resolution": resolution,
        "by_selected_pairs": len(selected_pairs),
        "post_multiplicity_pair_signals": len(signals),
        "minimum_testable_pair_pvalue": min((row["pvalue"] for row in tests), default=None),
        "tests": tests,
        "signals": signals,
        "failures": failures,
        "history_state_freshness_validated": True,
        "execution_joint_state_validated": False,
        "fill_conditioned_pnl_validated": False,
        "promotion_ready": False,
        "promotion_blockers": promotion_blockers,
    }
    atomic_json(args.output_json, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
