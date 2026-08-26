#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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
    reps = max(50, int(args.bootstrap_reps or inference_cfg["bootstrap_repetitions"]))

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

    selected = {market.market_id: market for _, group in cluster_rows for market in group}
    start = now - int(history_cfg["lookback_hours"]) * 3600
    histories, history_failures = base.fetch_histories(
        clob,
        {market.market_id: market.yes for market in selected.values()},
        start,
        now,
        int(history_cfg["fidelity_minutes"]),
    ) if selected else ({}, [])
    failures.extend(history_failures[:30])

    tests: list[dict[str, Any]] = []
    pvalues: dict[tuple[str, str, str], float] = {}
    fit_by_key: dict[tuple[str, str, str], tuple[core.StandardizedPanel, core.PairFit]] = {}
    for cluster_key, group in cluster_rows:
        market_ids = [market.market_id for market in group]
        panel = core.build_regular_panel(
            histories,
            market_ids,
            bucket_seconds=bucket_seconds,
            min_points=int(history_cfg["minimum_regular_common_points"]),
        )
        if panel is None:
            continue
        boot = core.panel_pair_bootstrap_pvalues(
            panel,
            reps=reps,
            seed=20260826 + sum(ord(ch) for ch in cluster_key),
            min_controls=int(inference_cfg["minimum_pair_controls"]),
        )
        for (a, b), (fit, pvalue) in boot.items():
            key = (cluster_key, a, b)
            pvalues[key] = pvalue
            fit_by_key[key] = (panel, fit)
            tests.append(
                {
                    "cluster": cluster_key,
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

    selected_pairs = core.bh_selected(pvalues, float(inference_cfg["bh_fdr"]))
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

    report = {
        "timestamp": now,
        "paper_only": True,
        "research_only": True,
        "live_intents_enabled": False,
        "submitted_orders": 0,
        "markets": len(markets),
        "clusters": len(cluster_rows),
        "histories": len(histories),
        "pair_hypotheses": len(pvalues),
        "bootstrap_repetitions": reps,
        "bh_fdr": float(inference_cfg["bh_fdr"]),
        "bh_selected_pairs": len(selected_pairs),
        "post_bh_pair_signals": len(signals),
        "tests": tests,
        "signals": signals,
        "failures": failures,
        "execution_joint_state_validated": False,
        "fill_conditioned_pnl_validated": False,
        "promotion_ready": False,
        "promotion_blockers": [
            "joint_fill_state_evidence_not_yet_attached",
            "partial_abort_unwind_economics_not_yet_attached",
            "fill_conditioned_cost_stressed_pnl_not_yet_attached",
            "research_branch_cannot_mutate_live_champion"
        ]
    }
    atomic_json(args.output_json, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
