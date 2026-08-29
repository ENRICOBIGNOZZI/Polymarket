#!/usr/bin/env python3
"""Fail-closed execution evidence for V6/V7 paper sleeves.

This sidecar deliberately measures each model against the economic target it
claims to trade.  It is not an allocator and cannot mutate configs, intents,
risk limits, credentials, or execution state.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "polymarket_execution_evidence_v1"
ALLOWED_TARGETS = {
    "short_horizon_markout",
    "hedged_convergence",
    "structural_payout",
    "terminal_probability",
}


def number(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    result = number(value, math.nan)
    return int(result) if math.isfinite(result) else default


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            return [dict(row) for row in csv.DictReader(handle) if row]
    except (OSError, csv.Error):
        return []


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def first_number(row: dict[str, str], columns: Iterable[str]) -> float:
    for column in columns:
        value = number(row.get(column), math.nan)
        if math.isfinite(value):
            return value
    return math.nan


def first_text(row: dict[str, str], columns: Iterable[str]) -> str:
    for column in columns:
        value = str(row.get(column) or "").strip()
        if value:
            return value
    return ""


def canonical_action(row: dict[str, str]) -> str:
    return first_text(row, ("action", "event", "status")).upper()


def timestamp(row: dict[str, str]) -> int:
    return integer(first_number(row, ("closed_ts", "timestamp", "exit_ts", "created_ts")), 0)


def event_key(row: dict[str, str], fallback: str) -> str:
    return first_text(row, ("event_id", "condition_id", "bundle_id", "market_id", "slug")) or fallback


def strategy_paths(run_root: Path, model: str) -> tuple[list[Path], list[Path]]:
    """Return (execution rows, submission rows) without assuming a single broker schema."""
    mapping = {
        "micro_maker": (
            [run_root / "maker" / "maker_fills.csv"],
            [run_root / "maker" / "maker_orders.csv", run_root / "maker" / "maker_signals.csv"],
        ),
        "micro_taker": (
            [run_root / "micro_taker" / "fills.csv"],
            [run_root / "micro_taker" / "signals.csv"],
        ),
        "relative_value": (
            [run_root / "bundle_ledger.csv", run_root / "multileg_events.csv"],
            [run_root / "intents.csv"],
        ),
        "graph_hard": (
            [run_root / "hard_arb" / "fills.csv"],
            [run_root / "hard_arb" / "candidates.csv"],
        ),
        "external": (
            [run_root / "external" / "fills.csv"],
            [run_root / "external_signals.csv"],
        ),
    }
    return mapping.get(model, ([], []))


def row_is_fill(row: dict[str, str]) -> bool:
    action = canonical_action(row)
    if not action:
        return False
    return any(token in action for token in ("BUY", "SELL", "FILL", "EXIT", "SETTLE"))


def row_is_submission(row: dict[str, str]) -> bool:
    action = canonical_action(row)
    return action in {"POST", "SUBMIT", "RESTING", "OPEN"} or bool(first_text(row, ("expected_edge", "maker_entry_net_edge")))


def realized_pnl(row: dict[str, str]) -> float:
    return first_number(row, ("net_pnl", "pnl", "realized_pnl"))


def row_has_realized_pnl(row: dict[str, str]) -> bool:
    """Exclude entry/partial-fill zeroes from a realized-PnL sample.

    Bundle ledgers are already terminal rows and normally use ``net_pnl`` with
    no action.  Execution logs need an explicit close, unwind, or settlement
    action before their PnL belongs in the statistical sample.
    """
    if str(row.get("net_pnl") or "").strip():
        return True
    action = canonical_action(row)
    return any(token in action for token in ("SELL", "EXIT", "UNWIND", "SETTLE", "CLOSE"))


def observed_markout(row: dict[str, str]) -> float:
    return first_number(row, ("markout", "forward_markout", "markout_pnl", "post_fill_markout"))


def explicit_cost(row: dict[str, str]) -> float:
    value = first_number(row, ("fee", "fees", "slippage_cost", "execution_cost", "cost"))
    return max(0.0, value) if math.isfinite(value) else math.nan


def terminal_calibration(rows: Iterable[dict[str, str]]) -> tuple[int, float | None]:
    """Return resolved-label count and mean Brier improvement over the market."""
    improvements: list[float] = []
    for row in rows:
        outcome = first_number(row, ("outcome", "resolved_outcome", "label"))
        model = first_number(row, ("model_probability", "fair_probability", "probability", "q"))
        market = first_number(row, ("market_probability", "market_price", "mid_price", "p"))
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in (outcome, model, market)):
            continue
        if outcome not in (0.0, 1.0):
            continue
        improvements.append((market - outcome) ** 2 - (model - outcome) ** 2)
    return len(improvements), statistics.fmean(improvements) if improvements else None


def block_bootstrap_one_sided(values: list[tuple[int, float]], samples: int, seed: int) -> float | None:
    """Day-block bootstrap for the mean PnL; returns P(mean <= 0)."""
    by_day: dict[int, list[float]] = defaultdict(list)
    for ts, value in values:
        if math.isfinite(value):
            by_day[max(0, ts) // 86400].append(value)
    blocks = [sum(group) for _, group in sorted(by_day.items()) if group]
    if len(blocks) < 2 or samples <= 0:
        return None
    rng = random.Random(seed)
    non_positive = 0
    for _ in range(samples):
        mean = sum(rng.choice(blocks) for _ in blocks) / len(blocks)
        non_positive += int(mean <= 0.0)
    return non_positive / samples


def fold_stability(values: list[tuple[int, float]]) -> tuple[int, float | None]:
    by_day: dict[int, list[float]] = defaultdict(list)
    for ts, value in values:
        if math.isfinite(value):
            by_day[max(0, ts) // 86400].append(value)
    days = sorted(by_day)
    if len(days) < 2:
        return 0, None
    midpoint = max(1, len(days) // 2)
    folds = [days[:midpoint], days[midpoint:]]
    pnl = [sum(sum(by_day[day]) for day in fold) for fold in folds if fold]
    if not pnl:
        return 0, None
    return len(pnl), sum(value > 0.0 for value in pnl) / len(pnl)


def default_policy() -> dict[str, Any]:
    models = {
        "micro_maker": "short_horizon_markout",
        "micro_taker": "short_horizon_markout",
        "relative_value": "hedged_convergence",
        "graph_hard": "structural_payout",
        "external": "terminal_probability",
    }
    return {
        "schema": "polymarket_execution_evidence_policy_v1",
        "paper_only": True,
        "allow_capital_reallocation": False,
        "bootstrap_samples": 1000,
        "cost_stress_multiplier": 1.5,
        "models": {
            model: {
                "target": target,
                "horizon_seconds": 45 if target == "short_horizon_markout" else 0,
                "min_fills": 20,
                "min_pnl_observations": 12,
                "min_markout_observations": 12 if target != "structural_payout" else 0,
                "min_fill_rate": 0.01,
                "min_net_pnl": 0.0,
                "min_stressed_net_pnl": 0.0,
                "max_bootstrap_pvalue": 0.10,
                "min_active_folds": 2,
                "min_positive_fold_fraction": 0.50,
                "min_terminal_observations": 12 if target == "terminal_probability" else 0,
                "min_brier_improvement": 0.0 if target == "terminal_probability" else None,
            }
            for model, target in models.items()
        },
    }


def normalize_policy(value: dict[str, Any]) -> dict[str, Any]:
    base = default_policy()
    if not value:
        return base
    merged = dict(base)
    merged.update({key: item for key, item in value.items() if key != "models"})
    models = value.get("models") if isinstance(value.get("models"), dict) else {}
    merged_models: dict[str, dict[str, Any]] = {}
    for name, baseline in base["models"].items():
        candidate = models.get(name) if isinstance(models.get(name), dict) else {}
        combined = dict(baseline)
        combined.update(candidate)
        merged_models[name] = combined
    merged["models"] = merged_models
    return merged


def assess_model(
    run_root: Path,
    model: str,
    contract: dict[str, Any],
    *,
    now: int,
    bootstrap_samples: int,
    stress_multiplier: float,
) -> dict[str, Any]:
    target = str(contract.get("target") or "")
    execution_paths, submission_paths = strategy_paths(run_root, model)
    execution_rows = [row for path in execution_paths for row in read_rows(path)]
    submission_rows = [row for path in submission_paths for row in read_rows(path)]
    fills = [row for row in execution_rows if row_is_fill(row)]
    submissions = [row for row in submission_rows if row_is_submission(row)]
    pnl_values = [(timestamp(row), realized_pnl(row)) for row in execution_rows if row_has_realized_pnl(row)]
    pnl_values = [(ts, value) for ts, value in pnl_values if math.isfinite(value)]
    markouts = [observed_markout(row) for row in execution_rows]
    markouts = [value for value in markouts if math.isfinite(value)]
    costs = [explicit_cost(row) for row in execution_rows]
    costs = [value for value in costs if math.isfinite(value)]
    raw_net_pnl = sum(value for _, value in pnl_values)
    stressed = None
    if pnl_values and costs:
        stressed = raw_net_pnl - max(0.0, stress_multiplier - 1.0) * sum(costs)
    fill_rate = len(fills) / len(submissions) if submissions else None
    bootstrap = block_bootstrap_one_sided(
        pnl_values,
        bootstrap_samples,
        int(hashlib.sha256(model.encode()).hexdigest()[:8], 16),
    )
    active_folds, positive_fold_fraction = fold_stability(pnl_values)
    terminal_rows = [*execution_rows, *submission_rows]
    terminal_observations, brier_improvement = terminal_calibration(terminal_rows)

    reasons: list[str] = []
    if target not in ALLOWED_TARGETS:
        reasons.append("invalid_target_contract")
    if bool(contract.get("allow_terminal_mixture", False)):
        reasons.append("terminal_mixture_forbidden")
    if len(fills) < integer(contract.get("min_fills"), 20):
        reasons.append("insufficient_fills")
    if len(pnl_values) < integer(contract.get("min_pnl_observations"), 12):
        reasons.append("insufficient_realized_pnl_observations")
    min_markouts = integer(contract.get("min_markout_observations"), 0)
    if len(markouts) < min_markouts:
        reasons.append("insufficient_forward_markout_observations")
    required_fill_rate = number(contract.get("min_fill_rate"), 0.0)
    if math.isfinite(required_fill_rate) and required_fill_rate > 0.0:
        if fill_rate is None:
            reasons.append("submission_denominator_missing")
        elif fill_rate < required_fill_rate:
            reasons.append("fill_rate_gate")
    if raw_net_pnl <= number(contract.get("min_net_pnl"), 0.0):
        reasons.append("net_pnl_gate")
    if stressed is None:
        reasons.append("cost_stress_unverifiable")
    elif stressed <= number(contract.get("min_stressed_net_pnl"), 0.0):
        reasons.append("cost_stress_gate")
    max_pvalue = number(contract.get("max_bootstrap_pvalue"), 0.10)
    if bootstrap is None:
        reasons.append("bootstrap_unverifiable")
    elif bootstrap > max_pvalue:
        reasons.append("bootstrap_gate")
    if active_folds < integer(contract.get("min_active_folds"), 2):
        reasons.append("fold_count_gate")
    min_positive = number(contract.get("min_positive_fold_fraction"), 0.50)
    if positive_fold_fraction is None or positive_fold_fraction < min_positive:
        reasons.append("fold_stability_gate")
    if target == "terminal_probability":
        if terminal_observations < integer(contract.get("min_terminal_observations"), 12):
            reasons.append("terminal_calibration_unverifiable")
        min_brier_improvement = number(contract.get("min_brier_improvement"), 0.0)
        if brier_improvement is None or brier_improvement <= min_brier_improvement:
            reasons.append("terminal_brier_improvement_gate")

    state = "PAPER_ELIGIBLE" if not reasons else "INSUFFICIENT_EVIDENCE"
    source_stats = []
    for path in [*execution_paths, *submission_paths]:
        source_stats.append(
            {
                "path": str(path),
                "exists": path.exists(),
                "rows": len(read_rows(path)),
                "mtime": int(path.stat().st_mtime) if path.exists() else 0,
            }
        )
    return {
        "model": model,
        "target": target,
        "horizon_seconds": integer(contract.get("horizon_seconds")),
        "state": state,
        "paper_eligible": state == "PAPER_ELIGIBLE",
        "allocation_mutated": False,
        "orders_submitted": len(submissions) if submissions else None,
        "fills": len(fills),
        "fill_rate": fill_rate,
        "realized_pnl_observations": len(pnl_values),
        "net_pnl": raw_net_pnl,
        "stressed_net_pnl": stressed,
        "forward_markout_observations": len(markouts),
        "mean_forward_markout": statistics.fmean(markouts) if markouts else None,
        "terminal_calibration_observations": terminal_observations,
        "brier_improvement_over_market": brier_improvement,
        "bootstrap_one_sided_pvalue": bootstrap,
        "active_folds": active_folds,
        "positive_fold_fraction": positive_fold_fraction,
        "reason_codes": sorted(set(reasons)),
        "sources": source_stats,
        "as_of_timestamp": now,
    }


def build_report(
    run_root: Path,
    policy: dict[str, Any],
    *,
    now: int | None = None,
) -> dict[str, Any]:
    policy = normalize_policy(policy)
    now = int(time.time()) if now is None else int(now)
    models = policy.get("models") if isinstance(policy.get("models"), dict) else {}
    bootstrap_samples = max(1, integer(policy.get("bootstrap_samples"), 1000))
    stress = max(1.0, number(policy.get("cost_stress_multiplier"), 1.5))
    report_models = {
        name: assess_model(run_root, name, contract if isinstance(contract, dict) else {}, now=now,
                           bootstrap_samples=bootstrap_samples, stress_multiplier=stress)
        for name, contract in sorted(models.items())
    }
    payload = {
        "schema": SCHEMA,
        "timestamp": now,
        "paper_only": bool(policy.get("paper_only", True)),
        "allow_capital_reallocation": False,
        "models": report_models,
        "summary": {
            "models": len(report_models),
            "paper_eligible_models": sum(row["paper_eligible"] for row in report_models.values()),
            "insufficient_evidence_models": sum(not row["paper_eligible"] for row in report_models.values()),
            "capital_allocation_mutated": False,
        },
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    payload["evidence_id"] = digest
    return payload


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Execution evidence", "", f"- evidence id: `{report['evidence_id']}`", "- paper only: `true`", "- capital allocation mutated: `false`", "", "| Model | Target | State | Fills | PnL observations | Net PnL | Stress PnL | Markouts | p-value | Reasons |", "|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for model, row in report["models"].items():
        pnl = "n/a" if row["net_pnl"] is None else f"{row['net_pnl']:.6f}"
        stress = "n/a" if row["stressed_net_pnl"] is None else f"{row['stressed_net_pnl']:.6f}"
        pvalue = "n/a" if row["bootstrap_one_sided_pvalue"] is None else f"{row['bootstrap_one_sided_pvalue']:.4f}"
        lines.append(
            f"| {model} | {row['target']} | {row['state']} | {row['fills']} | "
            f"{row['realized_pnl_observations']} | {pnl} | {stress} | "
            f"{row['forward_markout_observations']} | {pvalue} | "
            f"{', '.join(row['reason_codes']) or 'none'} |"
        )
    lines.extend(["", "The sidecar only measures evidence. It cannot alter order generation, sizing, risk limits, credentials, or real-money execution."])
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=Path("config/v7_execution_evidence.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--now", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args.run_root, read_json(args.policy), now=args.now)
    output = args.output or args.run_root / "v7_execution_evidence.json"
    markdown = args.markdown or args.run_root / "v7_execution_evidence.md"
    atomic_json(output, report)
    atomic_text(markdown, render_markdown(report))
    print(json.dumps(report["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
