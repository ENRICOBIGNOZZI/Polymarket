"""Executable-cost scan metrics and guarded OOS promotion tests."""
from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any

def fnum(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        value = float(row.get(key, default) or default)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def summarize_scan(path: Path, family: str, notional_cap: float) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    records: list[dict[str, float]] = []
    for row in rows:
        maker = fnum(row, "maker_entry_net_edge")
        taker = fnum(row, "taker_net_edge")
        raw = fnum(row, "raw_expected_edge")
        notional = max(0.0, fnum(row, "executable_notional"))
        stability = max(0.0, fnum(row, "stability"))
        hedge_error = max(0.0, fnum(row, "hedge_error")) if family == "B2" else 0.0
        tradable_notional = min(notional, max(0.0, notional_cap))
        records.append({
            "maker": maker,
            "taker": taker,
            "raw": raw,
            "notional": notional,
            "tradable_notional": tradable_notional,
            "stability": stability,
            "hedge_error": hedge_error,
        })

    positive = [r for r in records if r["maker"] > 0.0 and r["tradable_notional"] > 0.0]
    positive.sort(key=lambda r: r["maker"], reverse=True)
    expected_dollars = [r["maker"] * r["tradable_notional"] for r in positive]
    total_positive_notional = sum(r["tradable_notional"] for r in positive)
    top1_share = (
        max((r["tradable_notional"] for r in positive), default=0.0) / total_positive_notional
        if total_positive_notional > 1e-12 else 1.0
    )
    weights = [r["tradable_notional"] / total_positive_notional for r in positive] if total_positive_notional > 1e-12 else []
    hhi = sum(w * w for w in weights)
    top_edges = [r["maker"] for r in positive[:5]]
    # Concentration is penalized, but a genuinely strong single opportunity is
    # not forced to zero merely because the current cross-section is sparse.
    score = sum(expected_dollars) / (1.0 + hhi)

    return {
        "rows": len(records),
        "raw_positive": sum(r["raw"] > 0.0 for r in records),
        "taker_positive": sum(r["taker"] > 0.0 for r in records),
        "maker_positive": len(positive),
        "best_maker_edge": max((r["maker"] for r in records), default=0.0),
        "median_top5_maker_edge": statistics.median(top_edges) if top_edges else 0.0,
        "positive_executable_notional": total_positive_notional,
        "expected_edge_dollars": sum(expected_dollars),
        "diversified_screen_score": score,
        "top1_positive_notional_share": top1_share,
        "positive_notional_hhi": hhi,
        "median_stability": statistics.median([r["stability"] for r in positive]) if positive else 0.0,
        "median_hedge_error": statistics.median([r["hedge_error"] for r in positive]) if family == "B2" and positive else None,
    }


def screen_candidate(metrics: dict[str, Any], champion: dict[str, Any], gates: dict[str, Any]) -> tuple[bool, list[str], dict[str, float]]:
    min_rows = int(gates.get("min_rows", 1))
    min_positive = int(gates.get("min_maker_positive", 1))
    min_edge = float(gates.get("min_best_maker_edge", 0.0005))
    min_notional = float(gates.get("min_positive_executable_notional", 25.0))
    max_top1 = float(gates.get("max_top1_notional_share", 0.90))
    min_ratio = float(gates.get("min_score_improvement_ratio", 1.05))
    min_abs = float(gates.get("min_score_improvement", 0.0))

    failures: list[str] = []
    if metrics["rows"] < min_rows:
        failures.append("insufficient_scanner_rows")
    if metrics["maker_positive"] < min_positive:
        failures.append("insufficient_maker_positive")
    if metrics["best_maker_edge"] < min_edge:
        failures.append("best_edge_below_screen_floor")
    if metrics["positive_executable_notional"] < min_notional:
        failures.append("insufficient_positive_notional")
    if metrics["maker_positive"] > 1 and metrics["top1_positive_notional_share"] > max_top1:
        failures.append("positive_notional_too_concentrated")

    candidate_score = float(metrics["diversified_screen_score"])
    champion_score = float(champion["diversified_screen_score"])
    improvement = candidate_score - champion_score
    required_score = champion_score * min_ratio if champion_score > 0.0 else min_abs
    if candidate_score + 1e-15 < required_score or improvement + 1e-15 < min_abs:
        failures.append("no_incremental_screen_score")

    return not failures, failures, {
        "candidate_score": candidate_score,
        "champion_score": champion_score,
        "absolute_improvement": improvement,
        "improvement_ratio": candidate_score / champion_score if champion_score > 1e-15 else (999.0 if candidate_score > 0 else 0.0),
    }


def resolve_report_path(template: str, run_root: Path) -> Path:
    return Path(template.replace("{run_root}", str(run_root)))


def read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def promotion_gate(
    challenger_oos: dict[str, Any] | None,
    champion_oos: dict[str, Any] | None,
    gates: dict[str, Any],
    tested_challengers: int,
) -> tuple[bool, list[str], dict[str, Any]]:
    failures: list[str] = []
    if challenger_oos is None:
        return False, ["missing_challenger_oos"], {}
    if champion_oos is None:
        return False, ["missing_champion_oos"], {}

    challenger = challenger_oos.get("oos", {}) if isinstance(challenger_oos.get("oos"), dict) else {}
    challenger_stress = challenger_oos.get("oos_cost_stress", {}) if isinstance(challenger_oos.get("oos_cost_stress"), dict) else {}
    champion = champion_oos.get("oos", {}) if isinstance(champion_oos.get("oos"), dict) else {}
    champion_stress = champion_oos.get("oos_cost_stress", {}) if isinstance(champion_oos.get("oos_cost_stress"), dict) else {}

    min_trades = int(gates.get("min_oos_trades", 30))
    min_mean_delta = float(gates.get("min_incremental_mean_return", 0.0))
    min_stress_delta = float(gates.get("min_incremental_stressed_mean_return", 0.0))
    max_dd_increase = float(gates.get("max_drawdown_increase", 0.0))
    familywise_p = float(gates.get("max_familywise_pvalue", 0.10))
    min_positive_folds = int(gates.get("min_positive_active_folds", 2))

    trades = int(challenger.get("trades", 0) or 0)
    ch_mean = float(challenger.get("mean_return", 0.0) or 0.0)
    cp_mean = float(champion.get("mean_return", 0.0) or 0.0)
    ch_stress = float(challenger_stress.get("mean_return", 0.0) or 0.0)
    cp_stress = float(champion_stress.get("mean_return", 0.0) or 0.0)
    ch_dd = float(challenger.get("max_drawdown", 1.0) or 0.0)
    cp_dd = float(champion.get("max_drawdown", 0.0) or 0.0)
    raw_p = float(challenger_oos.get("bootstrap_one_sided_pvalue", 1.0) or 1.0)
    adjusted_p = min(1.0, raw_p * max(1, tested_challengers))
    positive_folds = int(challenger_oos.get("positive_active_folds", 0) or 0)
    eligible = bool(challenger_oos.get("eligible_for_tiny_pilot", False))

    if not eligible:
        failures.append("challenger_oos_gate_failed")
    if trades < min_trades:
        failures.append("insufficient_challenger_oos_trades")
    if ch_mean - cp_mean <= min_mean_delta:
        failures.append("no_incremental_oos_mean_return")
    if ch_stress - cp_stress <= min_stress_delta:
        failures.append("no_incremental_stressed_mean_return")
    if ch_dd > cp_dd + max_dd_increase:
        failures.append("incremental_drawdown_gate")
    if adjusted_p > familywise_p:
        failures.append("multiple_testing_gate")
    if positive_folds < min_positive_folds:
        failures.append("positive_fold_gate")

    evidence = {
        "challenger_trades": trades,
        "challenger_mean_return": ch_mean,
        "champion_mean_return": cp_mean,
        "incremental_mean_return": ch_mean - cp_mean,
        "challenger_stressed_mean_return": ch_stress,
        "champion_stressed_mean_return": cp_stress,
        "incremental_stressed_mean_return": ch_stress - cp_stress,
        "challenger_max_drawdown": ch_dd,
        "champion_max_drawdown": cp_dd,
        "raw_bootstrap_pvalue": raw_p,
        "multiplicity_adjusted_pvalue": adjusted_p,
        "positive_active_folds": positive_folds,
    }
    return not failures, failures, evidence

