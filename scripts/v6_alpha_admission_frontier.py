#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import v6_local_factor_intents as lf

KV = re.compile(r"([A-Za-z0-9_]+)=([^\s]+)")


def finite(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return x if math.isfinite(x) else default


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def last_tick(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        if not line.strip():
            continue
        out: dict[str, Any] = {}
        for key, raw in KV.findall(line):
            raw = raw.rstrip(",")
            try:
                out[key] = float(raw) if any(ch in raw for ch in ".eE") else int(raw)
            except ValueError:
                out[key] = raw
        return out
    return {}


def parse_thresholds(text: str, canonical: float) -> list[float]:
    values = [canonical]
    for raw in (text or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        value = finite(raw, math.nan)
        if not math.isfinite(value) or value < 0.0 or value > 0.05:
            raise ValueError(f"invalid frontier min-edge: {raw}")
        values.append(value)
    return sorted(set(round(value, 12) for value in values))


def pair_key(rows: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(row.get("market_id") or ""), str(row.get("side") or "")) for row in rows))


def evaluate_threshold_grid(
    by_cluster: dict[str, list[Any]],
    eligible: set[int],
    books: dict[str, Any],
    now: int,
    thresholds: list[float],
    max_trade_usd: float,
    fee_rate: float,
    fee_exp: float,
    slippage_bps: float,
    stress_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0),
    build_pair: Callable[..., list[dict[str, Any]]] = lf.build_pair_intent,
) -> list[dict[str, Any]]:
    """Evaluate lower admission floors without changing the canonical output.

    Every threshold is evaluated on the same candidate/book snapshot. The
    underlying pair builder already requires positive per-leg expected PnL
    after spread, fee and slippage assumptions; zero is therefore the most
    aggressive admissible floor and negative-cost edge is never admitted.
    """
    results: list[dict[str, Any]] = []
    for threshold in thresholds:
        stress_rows: list[dict[str, Any]] = []
        key_sets: list[set[tuple[tuple[str, str], ...]]] = []
        for multiplier in stress_multipliers:
            rows: list[dict[str, Any]] = []
            keys: set[tuple[tuple[str, str], ...]] = set()
            serial = 0
            for cluster, candidates in by_cluster.items():
                selected = [candidate for candidate in candidates if id(candidate) in eligible]
                intent = build_pair(
                    cluster,
                    selected,
                    books,
                    now,
                    threshold,
                    max_trade_usd,
                    fee_rate,
                    fee_exp,
                    slippage_bps * multiplier,
                    serial,
                )
                if intent:
                    rows.extend(intent)
                    keys.add(pair_key(intent))
                    serial += 1
            stress_rows.append(
                {
                    "cost_multiplier": multiplier,
                    "slippage_bps": slippage_bps * multiplier,
                    "bundles": len(keys),
                    "intent_rows": len(rows),
                    "best_edge": max((finite(row.get("expected_edge")) for row in rows), default=0.0),
                }
            )
            key_sets.append(keys)
        robust = set.intersection(*key_sets) if key_sets else set()
        results.append(
            {
                "min_edge": threshold,
                "stress": stress_rows,
                "price_cost_robust_pairs": len(robust),
                "pair_keys": [list(key) for key in sorted(robust)],
            }
        )
    return results


def local_factor_frontier(
    config: Path,
    markets: int,
    min_liquidity: float,
    lookback_hours: int,
    fidelity_minutes: int,
    max_clusters: int,
    min_common_points: int,
    min_z: float,
    fdr: float,
    canonical_edge: float,
    thresholds: list[float],
    max_trade_usd: float,
    slippage_bps: float,
) -> dict[str, Any]:
    cfg = json.loads(config.read_text(encoding="utf-8"))
    gamma, clob = cfg["gamma_url"], cfg["clob_url"]
    fee_rate = max(0.0, finite((cfg.get("v6") or {}).get("assumed_fee_rate"), 0.07))
    fee_exp = max(0.0, finite((cfg.get("v6") or {}).get("assumed_fee_exponent"), 1.0))
    now = int(time.time())
    failures: list[str] = []
    try:
        discovered = lf.discover(gamma, markets, min_liquidity)
        clusters = lf.clusters(discovered, max_clusters)
        selected = {market.market_id: market for _, group in clusters for market in group}
        books = lf.fetch_books(clob, list(selected.values()))
    except Exception as exc:
        discovered, clusters, selected, books = [], [], {}, {}
        failures.append(f"market_data:{type(exc).__name__}:{exc}")
    start = now - lookback_hours * 3600
    if selected:
        series, history_failures = lf.fetch_histories(
            clob,
            {market.market_id: market.yes for market in selected.values()},
            start,
            now,
            fidelity_minutes,
        )
    else:
        series, history_failures = {}, []
    failures.extend(history_failures[:30])

    all_candidates: list[Any] = []
    by_cluster: dict[str, list[Any]] = defaultdict(list)
    for key, group in clusters:
        candidates = lf.local_candidates(key, group, series, min_common_points, min_z)
        all_candidates.extend(candidates)
        by_cluster[key].extend(candidates)
    cutoff = lf.bh_cutoff([candidate.pvalue for candidate in all_candidates], max(1e-4, min(0.5, fdr)))
    eligible = {id(candidate) for candidate in all_candidates if cutoff > 0.0 and candidate.pvalue <= cutoff}

    grid = evaluate_threshold_grid(
        by_cluster,
        eligible,
        books,
        now,
        thresholds,
        max_trade_usd,
        fee_rate,
        fee_exp,
        slippage_bps,
    )
    signature_payload = {
        "markets": sorted(selected),
        "series_lengths": {market_id: len(values) for market_id, values in sorted(series.items())},
        "eligible": sorted(
            str(candidate.market.market_id)
            for candidate in all_candidates
            if id(candidate) in eligible
        ),
    }
    signature = hashlib.sha256(json.dumps(signature_payload, sort_keys=True).encode("utf-8")).hexdigest()
    canonical = next((row for row in grid if abs(finite(row.get("min_edge")) - canonical_edge) < 1e-12), {})
    canonical_robust = int(canonical.get("price_cost_robust_pairs", 0) or 0)
    lower = [row for row in grid if finite(row.get("min_edge")) < canonical_edge - 1e-12]
    lower_best = max((int(row.get("price_cost_robust_pairs", 0) or 0) for row in lower), default=canonical_robust)
    if lower_best > canonical_robust:
        verdict = "LOWER_ADMISSION_CREATES_PRICE_COST_ROBUST_PAIRS"
    elif eligible and canonical_robust == 0:
        verdict = "NO_PRICE_COST_ROBUST_PAIR_FROM_THRESHOLD_RELAXATION"
    else:
        verdict = "NO_INCREMENTAL_ROBUST_PAIR_FROM_THRESHOLD_RELAXATION"
    return {
        "paper_only": True,
        "common_sample_signature": signature,
        "markets": len(discovered),
        "clusters": len(clusters),
        "histories": len(series),
        "reversion_tests": len(all_candidates),
        "fdr": fdr,
        "bh_pvalue_cutoff": cutoff,
        "fdr_eligible_signals": len(eligible),
        "canonical_min_edge": canonical_edge,
        "frontier": grid,
        "verdict": verdict,
        "failures": failures,
    }


def classify_relation(raw: dict[str, Any], guarded: dict[str, Any]) -> dict[str, Any]:
    raw_bundles = int(raw.get("bundles", 0) or 0)
    raw_best = finite(raw.get("best_edge"))
    accepted = int(guarded.get("accepted_rows", 0) or 0)
    rejections = guarded.get("rejections") if isinstance(guarded.get("rejections"), dict) else {}
    stress_rejections = int(rejections.get("stress_edge", 0) or 0)
    if raw_bundles > 0 and accepted == 0 and stress_rejections > 0:
        bottleneck = "EXECUTION_STRESS_BOUND"
    elif raw_bundles == 0:
        bottleneck = "NO_RELATION_BUNDLE"
    elif accepted > 0:
        bottleneck = "GUARD_SURVIVOR_REQUIRES_FORWARD_EXECUTION_EVIDENCE"
    else:
        bottleneck = "RELATION_GUARD_BOUND"
    return {
        "raw_bundles": raw_bundles,
        "raw_intent_rows": int(raw.get("intent_rows", 0) or 0),
        "raw_best_edge": raw_best,
        "accepted_rows": accepted,
        "guard_best_edge": finite(guarded.get("best_edge")),
        "rejections": rejections,
        "bottleneck": bottleneck,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("config/paper_v6.json"))
    ap.add_argument("--run-root", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--markets", type=int, default=220)
    ap.add_argument("--min-liquidity", type=float, default=10.0)
    ap.add_argument("--lookback-hours", type=int, default=168)
    ap.add_argument("--fidelity-minutes", type=int, default=60)
    ap.add_argument("--max-clusters", type=int, default=12)
    ap.add_argument("--min-common-points", type=int, default=48)
    ap.add_argument("--min-z", type=float, default=1.0)
    ap.add_argument("--fdr", type=float, default=0.10)
    ap.add_argument("--canonical-min-edge", type=float, default=0.00020)
    ap.add_argument("--frontier-min-edges", default="0,0.00005,0.00010")
    ap.add_argument("--max-trade-usd", type=float, default=60.0)
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    args = ap.parse_args()

    thresholds = parse_thresholds(args.frontier_min_edges, args.canonical_min_edge)
    try:
        local = local_factor_frontier(
            args.config,
            args.markets,
            args.min_liquidity,
            args.lookback_hours,
            args.fidelity_minutes,
            args.max_clusters,
            args.min_common_points,
            args.min_z,
            args.fdr,
            args.canonical_min_edge,
            thresholds,
            args.max_trade_usd,
            args.slippage_bps,
        )
    except Exception as exc:
        local = {
            "paper_only": True,
            "frontier": [],
            "verdict": "FRONTIER_UNAVAILABLE",
            "failures": [f"frontier:{type(exc).__name__}:{exc}"],
        }

    maker = last_tick(args.run_root / "maker_latest.log")
    multileg = last_tick(args.run_root / "multileg_latest.log")
    relation_raw = read_json(args.run_root / "relation_status.json")
    relation_guard = read_json(args.run_root / "relation_guard_status.json")
    external = read_json(args.run_root / "external_bridge_status.json")
    hard_arb = read_json(args.run_root / "hard_arb" / "status.json")

    micro_bottleneck = "UNKNOWN"
    if int(maker.get("posted", 0) or 0) > 0 and int(maker.get("resting", 0) or 0) > 0 and int(maker.get("positions", 0) or 0) == 0:
        micro_bottleneck = "QUEUE_FILLABILITY_BOUND"
    elif int(maker.get("signals", 0) or 0) == 0:
        micro_bottleneck = "SIGNAL_ADMISSION_BOUND"
    elif int(maker.get("positions", 0) or 0) > 0:
        micro_bottleneck = "FILLS_PRESENT_MEASURE_MARKOUT"

    relation = classify_relation(relation_raw, relation_guard)
    external_passing = int(external.get("passing_direct_models", 0) or 0)
    report = {
        "schema": "polymarket_v6_alpha_admission_frontier_v1",
        "timestamp": int(time.time()),
        "paper_only": True,
        "authenticated_execution": False,
        "direct_champion_mutation": False,
        "research_state": "MORE_EVIDENCE_REQUIRED",
        "promotable_alpha": False,
        "micro": {
            "signals": int(maker.get("signals", 0) or 0),
            "posted": int(maker.get("posted", 0) or 0),
            "resting": int(maker.get("resting", 0) or 0),
            "positions": int(maker.get("positions", 0) or 0),
            "bottleneck": micro_bottleneck,
        },
        "local_factor": local,
        "relation": relation,
        "multileg": {
            "trades_processed": int(multileg.get("trades_processed", 0) or 0),
            "bundles": int(multileg.get("bundles", 0) or 0),
            "complete": int(multileg.get("complete", 0) or 0),
        },
        "hard_arb": {
            "positive_candidates": int(hard_arb.get("positive_candidates", 0) or 0),
            "best_edge": finite(hard_arb.get("best_edge")),
        },
        "external": {
            "passing_direct_models": external_passing,
            "materialized_signals": int(external.get("materialized_signals", 0) or 0),
            "report_status": str(external.get("report_status") or "UNKNOWN"),
        },
        "aggression_decision": {
            "micro": "DO_NOT_LOWER_SIGNAL_FLOOR_FIRST" if micro_bottleneck == "QUEUE_FILLABILITY_BOUND" else "RESEARCH_SWEEP_ALLOWED",
            "local_factor": local.get("verdict", "FRONTIER_UNAVAILABLE"),
            "relation": "DO_NOT_RELAX_STRESS_GUARD_WITHOUT_MEASURED_EXECUTION_COST" if relation.get("bottleneck") == "EXECUTION_STRESS_BOUND" else "KEEP_GUARD_AND_COLLECT_FORWARD_EVIDENCE",
            "external": "NO_PROMOTION_WITHOUT_PASSING_MODEL" if external_passing == 0 else "FORWARD_VALIDATE_PASSING_MODEL",
        },
    }
    args.status.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.status.with_suffix(args.status.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(args.status)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
