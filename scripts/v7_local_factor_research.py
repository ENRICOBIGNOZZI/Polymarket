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
import v7_local_factor_core as core
import v7_local_factor_inference as inference
import v7_local_factor_pairs as pairing

HISTORY_WINDOW_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_AUTO_BOOTSTRAP_REPS = 5000
DEFAULT_MINIMUM_TEXT_SIMILARITY = 0.20


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
    """Reuse the V6 parser/transport on bounded absolute history windows.

    Long 30-minute-fidelity absolute ranges are known to be rejected by the public
    CLOB history service. Chunking is a data-acquisition invariant, not a model
    change; merge by timestamp so boundary observations are deterministic.
    """
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
        failures.extend(
            f"{window_start}:{window_end}:{failure}" for failure in partial_failures
        )
        window_start = window_end
    return histories, failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Final repaired V7 Local Factor research challenger")
    parser.add_argument("--config", type=Path, default=Path("config/research_v7_local_factor.json"))
    parser.add_argument("--paper-config", type=Path, default=Path("config/paper_v6.json"))
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--bootstrap-reps", type=int)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if not cfg.get("paper_only") or not cfg.get("research_only") or cfg.get("live_intents_enabled"):
        raise SystemExit("final Local Factor challenger must remain paper/research-only with live intents disabled")
    paper = json.loads(args.paper_config.read_text(encoding="utf-8"))
    gamma = str(paper["gamma_url"]).rstrip("/")
    clob = str(paper["clob_url"]).rstrip("/")
    now = int(time.time())
    universe_cfg = cfg["universe"]
    history_cfg = cfg["history"]
    inference_cfg = cfg["inference"]
    bucket_seconds = int(history_cfg["fidelity_minutes"]) * 60
    requested_reps = max(50, int(args.bootstrap_reps or inference_cfg["bootstrap_repetitions"]))
    min_controls = int(inference_cfg["minimum_pair_controls"])
    bh_q = float(inference_cfg["bh_fdr"])
    min_text_similarity = float(inference_cfg.get("structural_pair_minimum_text_similarity", DEFAULT_MINIMUM_TEXT_SIMILARITY))
    max_auto_reps = max(requested_reps, int(inference_cfg.get("maximum_bootstrap_repetitions", DEFAULT_MAX_AUTO_BOOTSTRAP_REPS)))

    failures: list[str] = []
    try:
        markets = base.discover(
            gamma,
            int(universe_cfg["maximum_markets"]),
            float(universe_cfg["minimum_liquidity_usd"]),
        )
        cluster_rows = base.clusters(markets, int(universe_cfg["maximum_clusters"]))
    except Exception as exc:
        markets, cluster_rows = [], []
        failures.append(f"discovery:{type(exc).__name__}:{exc}")

    # Freeze the pair universe from contract metadata before touching price history.
    # No price, liquidity, residual, p-value or return enters this graph.
    pair_graphs: dict[str, pairing.StructuralPairGraph] = {
        cluster_key: pairing.build_structural_pair_graph(
            cluster_key,
            group,
            min_controls=min_controls,
            minimum_text_similarity=min_text_similarity,
        )
        for cluster_key, group in cluster_rows
    }
    predeclared_pair_count = sum(graph.pair_count for graph in pair_graphs.values())
    repetitions_required = max(50, math.ceil(predeclared_pair_count / max(1e-12, bh_q)) - 1) if predeclared_pair_count else requested_reps
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
    history_required_market_count = 2 + min_controls
    history_data_healthy = len(histories) >= history_required_market_count

    tests: list[dict[str, Any]] = []
    pvalues: dict[tuple[str, str, str], float] = {}
    fit_by_key: dict[tuple[str, str, str], tuple[core.StandardizedPanel, core.PairFit]] = {}
    for cluster_key, group in cluster_rows:
        graph = pair_graphs.get(cluster_key)
        if graph is None or not graph.pairs:
            continue
        market_ids = [market.market_id for market in group]
        panel = core.build_regular_panel(
            histories,
            market_ids,
            bucket_seconds=bucket_seconds,
            min_points=int(history_cfg["minimum_regular_common_points"]),
        )
        if panel is None:
            continue
        boot = inference.panel_pair_iut_pvalues(
            panel,
            pairs=graph.pairs,
            reps=reps,
            seed=20260826 + sum(ord(ch) for ch in cluster_key),
            min_controls=min_controls,
        )
        for (a, b), (fit, pvalue) in boot.items():
            key = (cluster_key, a, b)
            pvalues[key] = pvalue
            fit_by_key[key] = (panel, fit)
            tests.append(
                {
                    "cluster": cluster_key,
                    "pair_graph_method": graph.method,
                    "market_a": a,
                    "market_b": b,
                    "pvalue": pvalue,
                    "pair_stat": fit.pair_stat,
                    "adf_a": fit.adf_a,
                    "adf_b": fit.adf_b,
                    "controls": len(fit.controls),
                    "regular_points": len(panel.times),
                }
            )

    selected_pairs = core.bh_selected(pvalues, bh_q)
    resolution = inference.bh_resolution_diagnostics(len(pvalues), reps, bh_q)
    books: dict[str, Any] = {}
    if selected_pairs:
        try:
            books = base.fetch_books(clob, list(selected.values()))
        except Exception as exc:
            failures.append(f"books:{type(exc).__name__}:{exc}")
    raw_cache: dict[str, dict[str, Any]] = {}
    signals: list[dict[str, Any]] = []
    for key in sorted(selected_pairs):
        cluster_key, a, b = key
        panel, fit = fit_by_key[key]
        market_a = selected.get(a)
        market_b = selected.get(b)
        if market_a is None or market_b is None:
            continue
        yes_a = books.get(market_a.yes)
        yes_b = books.get(market_b.yes)
        if yes_a is None or yes_b is None:
            continue
        raw_a = raw_market(gamma, a, raw_cache)
        raw_b = raw_market(gamma, b, raw_cache)
        if raw_a is None or raw_b is None:
            continue
        signal = core.build_pair_signal(
            fit,
            pvalues[key],
            {a: yes_a.mid, b: yes_b.mid},
            {a: panel.scales[a], b: panel.scales[b]},
            bucket_seconds=bucket_seconds,
            now=now,
            end_ts={a: market_end_ts(raw_a), b: market_end_ts(raw_b)},
            exit_buffer_seconds=int(cfg["forecast"]["time_to_resolution_exit_buffer_seconds"]),
            min_abs_z=float(inference_cfg["minimum_abs_residual_z_after_bh"]),
            max_hold_seconds=int(cfg["forecast"]["maximum_hold_hours"]) * 3600,
            min_weight=float(cfg["hedge"]["minimum_weight"]),
            max_weight=float(cfg["hedge"]["maximum_weight"]),
        )
        if signal is not None:
            signals.append({"cluster": cluster_key, **asdict(signal)})

    promotion_blockers = [
        "joint_fill_state_evidence_not_yet_attached",
        "partial_abort_unwind_economics_not_yet_attached",
        "fill_conditioned_cost_stressed_pnl_not_yet_attached",
        "research_branch_cannot_mutate_live_champion",
    ]
    if not history_data_healthy:
        promotion_blockers.append("price_history_data_health_unhealthy")
    if pvalues and not bool(resolution["singleton_bh_resolution_adequate"]):
        promotion_blockers.append("bootstrap_resolution_too_coarse_for_bh_multiplicity")

    report = {
        "timestamp": now,
        "paper_only": True,
        "research_only": True,
        "live_intents_enabled": False,
        "submitted_orders": 0,
        "markets": len(markets),
        "clusters": len(cluster_rows),
        "histories": len(histories),
        "history_required_market_count": history_required_market_count,
        "history_data_healthy": history_data_healthy,
        "history_window_days": HISTORY_WINDOW_SECONDS // 86400,
        "pair_graph_selection": "contract_metadata_only_before_price_history",
        "pair_graph_minimum_text_similarity": min_text_similarity,
        "predeclared_pair_count": predeclared_pair_count,
        "pair_graphs": {
            cluster_key: {
                "method": graph.method,
                "pair_count": graph.pair_count,
                "threshold_markets": graph.threshold_markets,
                "text_markets": graph.text_markets,
            }
            for cluster_key, graph in pair_graphs.items()
        },
        "pair_pvalue_method": "intersection_union_max_marginal_null_preserving_bootstrap_pvalue",
        "pair_hypotheses": len(pvalues),
        "requested_bootstrap_repetitions": requested_reps,
        "bootstrap_repetitions": reps,
        "bootstrap_repetition_cap": max_auto_reps,
        "predeclared_repetitions_required_for_singleton_bh_resolution": repetitions_required,
        "bootstrap_resolution": resolution,
        "bh_fdr": bh_q,
        "bh_selected_pairs": len(selected_pairs),
        "post_bh_pair_signals": len(signals),
        "tests": tests,
        "signals": signals,
        "failures": failures,
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
