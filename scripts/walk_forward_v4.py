#!/usr/bin/env python3
"""Chronological out-of-sample evaluation for realized V4 bundle ledger.

Thresholds are chosen on calibration data only, after an embargo. Test folds are
never used to select the edge threshold. Metrics are trade-level unless stated
otherwise; no annualized Sharpe is reported because bundle holding horizons are
irregular.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Trade:
    bundle_id: str
    strategy: str
    created_ts: int
    closed_ts: int
    status: str
    expected_edge: float
    capital: float
    gross_pnl: float
    fees: float
    slippage: float
    net_pnl: float
    ret: float


def fnum(row, key, default=0.0):
    try:
        return float(row.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def load_ledger(path: Path) -> list[Trade]:
    out = []
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            status = (r.get("status") or "").upper()
            if status not in {"CLOSED", "UNWOUND"}:
                continue
            try:
                created, closed = int(r["created_ts"]), int(r["closed_ts"])
            except (KeyError, ValueError, TypeError):
                continue
            capital = max(0.0, fnum(r, "entry_cash"))
            if capital <= 1e-9:
                continue
            net = fnum(r, "net_pnl")
            ret = fnum(r, "return_on_capital", net / capital)
            out.append(Trade(
                r.get("bundle_id", ""), r.get("strategy", "UNKNOWN"), created, closed, status,
                fnum(r, "expected_edge"), capital, fnum(r, "gross_pnl"), fnum(r, "fees"),
                fnum(r, "slippage"), net, ret,
            ))
    out.sort(key=lambda x: (x.created_ts, x.closed_ts, x.bundle_id))
    return out


def mean_se(xs):
    if not xs:
        return float("-inf"), float("inf")
    m = statistics.fmean(xs)
    se = statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else abs(xs[0]) + 1e-12
    return m, se


def choose_threshold(cal: list[Trade], grid: list[float], min_cal_trades: int) -> tuple[float | None, float]:
    best_t, best_score = None, float("-inf")
    for t in grid:
        rs = [x.ret for x in cal if x.expected_edge >= t]
        if len(rs) < min_cal_trades:
            continue
        m, se = mean_se(rs)
        score = m - se
        if score > best_score:
            best_t, best_score = t, score
    return best_t, best_score


def max_drawdown(pnls: list[float], starting_capital: float) -> float:
    eq = peak = starting_capital
    dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        if peak > 0:
            dd = max(dd, 1.0 - eq / peak)
    return dd


def summarize(trades: list[Trade], starting_capital: float, cost_mult: float = 1.0) -> dict:
    if cost_mult == 1.0:
        pnls = [x.net_pnl for x in trades]
    else:
        pnls = [x.gross_pnl - cost_mult * (x.fees + x.slippage) for x in trades]
    rets = [p / x.capital if x.capital > 0 else 0.0 for p, x in zip(pnls, trades)]
    pos = sum(p for p in pnls if p > 0)
    neg = -sum(p for p in pnls if p < 0)
    m = statistics.fmean(rets) if rets else 0.0
    sd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    downside = math.sqrt(statistics.fmean([min(0.0, r) ** 2 for r in rets])) if rets else 0.0
    return {
        "trades": len(trades),
        "capital_sum": sum(x.capital for x in trades),
        "gross_pnl": sum(x.gross_pnl for x in trades),
        "fees": sum(x.fees for x in trades),
        "slippage": sum(x.slippage for x in trades),
        "net_pnl": sum(pnls),
        "mean_return": m,
        "trade_sharpe": m / sd if sd > 1e-12 else 0.0,
        "trade_sortino": m / downside if downside > 1e-12 else 0.0,
        "hit_rate": sum(p > 0 for p in pnls) / len(pnls) if pnls else 0.0,
        "profit_factor": pos / neg if neg > 1e-12 else (999.0 if pos > 0 else 0.0),
        "max_drawdown": max_drawdown(pnls, starting_capital),
        "turnover": sum(x.capital for x in trades) / starting_capital if starting_capital > 0 else 0.0,
    }


def circular_block_bootstrap_pvalue(rets: list[float], block: int, reps: int, seed: int) -> float:
    """One-sided p-value for positive mean under a centered circular block null."""
    n = len(rets)
    if n < 2 or reps <= 0:
        return 1.0
    observed = statistics.fmean(rets)
    centered = [x - observed for x in rets]
    rng = random.Random(seed)
    exceed = 0
    b = max(1, min(block, n))
    for _ in range(reps):
        sample = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(centered[(start + j) % n] for j in range(b))
        if statistics.fmean(sample[:n]) >= observed:
            exceed += 1
    return (exceed + 1) / (reps + 1)


def make_folds(trades: list[Trade], folds: int, train_frac: float, cal_frac: float, embargo_seconds: int):
    if not trades:
        return []
    start, end = trades[0].created_ts, trades[-1].created_ts
    span = max(1, end - start + 1)
    initial = max(train_frac + cal_frac, 0.55)
    test_start0 = start + int(span * min(initial, 0.85))
    remaining = max(1, end - test_start0 + 1)
    width = max(1, remaining // max(1, folds))
    result = []
    for k in range(max(1, folds)):
        test_start = test_start0 + k * width
        test_end = end + 1 if k == folds - 1 else min(end + 1, test_start + width)
        if test_start >= end + 1:
            break
        pre_end = test_start - embargo_seconds
        historical = [x for x in trades if x.created_ts < pre_end]
        if len(historical) < 2:
            continue
        cal_n = max(1, int(len(historical) * cal_frac / max(train_frac + cal_frac, 1e-9)))
        train = historical[:-cal_n]
        cal = historical[-cal_n:]
        cal = [x for x in cal if x.closed_ts < test_start - embargo_seconds]
        test = [x for x in trades if test_start <= x.created_ts < test_end]
        result.append((train, cal, test, test_start, test_end))
    return result


def evaluate(trades: list[Trade], args) -> dict:
    grid = sorted(set(args.thresholds))
    folds_out, selected_oos = [], []
    for i, (train, cal, test, ts0, ts1) in enumerate(make_folds(trades, args.folds, args.train_frac, args.cal_frac, args.embargo_seconds)):
        threshold, cal_score = choose_threshold(cal, grid, args.min_cal_trades)
        chosen = [] if threshold is None else [x for x in test if x.expected_edge >= threshold]
        selected_oos.extend(chosen)
        folds_out.append({
            "fold": i + 1,
            "test_start": ts0,
            "test_end": ts1,
            "train_trades": len(train),
            "calibration_trades": len(cal),
            "test_candidates": len(test),
            "threshold": threshold,
            "calibration_mean_minus_se": None if threshold is None else cal_score,
            "test": summarize(chosen, args.starting_capital),
            "test_stress": summarize(chosen, args.starting_capital, args.cost_stress),
        })

    base = summarize(selected_oos, args.starting_capital)
    stress = summarize(selected_oos, args.starting_capital, args.cost_stress)
    rets = [x.ret for x in selected_oos]
    p = circular_block_bootstrap_pvalue(rets, args.bootstrap_block, args.bootstrap_reps, args.seed)
    positive_folds = sum(f["test"]["net_pnl"] > 0 for f in folds_out if f["test"]["trades"] > 0)
    active_folds = sum(f["test"]["trades"] > 0 for f in folds_out)
    by_strategy = {}
    for s in sorted({x.strategy for x in selected_oos}):
        by_strategy[s] = summarize([x for x in selected_oos if x.strategy == s], args.starting_capital)

    final_cal_n = max(1, int(len(trades) * args.cal_frac)) if trades else 0
    production_cal = trades[-final_cal_n:] if final_cal_n else []
    production_threshold, production_score = choose_threshold(production_cal, grid, args.min_cal_trades)

    reasons = []
    if base["trades"] < args.min_oos_trades: reasons.append("insufficient_oos_trades")
    if base["net_pnl"] <= 0: reasons.append("nonpositive_net_pnl")
    if stress["net_pnl"] <= 0: reasons.append("nonpositive_stressed_pnl")
    if base["max_drawdown"] > args.max_oos_drawdown: reasons.append("drawdown_gate")
    if base["profit_factor"] < args.min_profit_factor: reasons.append("profit_factor_gate")
    if p > args.max_bootstrap_pvalue: reasons.append("bootstrap_gate")
    if active_folds < 2 or positive_folds * 2 <= active_folds: reasons.append("fold_stability_gate")

    return {
        "schema": "polymarket_walk_forward_v4",
        "input_trades": len(trades),
        "folds": folds_out,
        "oos": base,
        "oos_cost_stress": stress,
        "cost_stress_multiplier": args.cost_stress,
        "bootstrap_one_sided_pvalue": p,
        "positive_active_folds": positive_folds,
        "active_folds": active_folds,
        "by_strategy": by_strategy,
        "production_threshold": production_threshold,
        "production_calibration_mean_minus_se": None if production_threshold is None else production_score,
        "eligible_for_tiny_pilot": not reasons and production_threshold is not None,
        "gate_failures": reasons + (["no_production_threshold"] if production_threshold is None else []),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", type=Path, default=Path("runs/paper_v4/bundle_ledger.csv"))
    ap.add_argument("--output", type=Path, default=Path("runs/paper_v4/walk_forward.json"))
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--train-frac", type=float, default=0.55)
    ap.add_argument("--cal-frac", type=float, default=0.20)
    ap.add_argument("--embargo-seconds", type=int, default=1800)
    ap.add_argument("--thresholds", type=float, nargs="+", default=[0.001, 0.002, 0.003, 0.005, 0.008, 0.012])
    ap.add_argument("--min-cal-trades", type=int, default=8)
    ap.add_argument("--starting-capital", type=float, default=10000.0)
    ap.add_argument("--cost-stress", type=float, default=1.5)
    ap.add_argument("--bootstrap-block", type=int, default=5)
    ap.add_argument("--bootstrap-reps", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260823)
    ap.add_argument("--min-oos-trades", type=int, default=30)
    ap.add_argument("--max-oos-drawdown", type=float, default=0.10)
    ap.add_argument("--min-profit-factor", type=float, default=1.10)
    ap.add_argument("--max-bootstrap-pvalue", type=float, default=0.10)
    args = ap.parse_args()

    trades = load_ledger(args.ledger)
    result = evaluate(trades, args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
