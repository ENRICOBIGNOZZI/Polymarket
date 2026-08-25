#!/usr/bin/env python3
"""Research-only V5 cross-sleeve capital-time selector.

The live V5 architecture runs independent paper books.  This module tests a
portfolio-level challenger without changing any expert, execution rule, risk
limit, or live configuration: when several opportunities compete for the same
finite capital in the same decision window, rank them by conservative expected
net edge per capital-hour instead of one-times-cost edge alone.

Inputs are point-in-time opportunity rows.  Forecast fields are used for
selection; future realized PnL is used only after selection for chronological
OOS ablation.  This file is deliberately offline/research-only.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

STRESS_LEVELS = (1.0, 1.5, 2.0)


def _finite(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


@dataclass(frozen=True)
class Opportunity:
    timestamp: int
    strategy: str
    market_id: str
    side: str
    notional: float
    expected_holding_hours: float
    net_edge_1x: float
    cost_per_dollar: float
    realized_pnl_1x: float

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Opportunity":
        timestamp = int(_finite(row.get("timestamp")))
        strategy = str(row.get("strategy", "")).strip()
        market_id = str(row.get("market_id", "")).strip()
        side = str(row.get("side", "")).strip().upper()
        notional = _finite(row.get("notional", row.get("desired_notional")))
        holding = _finite(row.get("expected_holding_hours"))
        net_edge = _finite(row.get("net_edge_1x", row.get("net_edge")))
        cost = _finite(row.get("cost_per_dollar"))
        realized = _finite(row.get("realized_pnl_1x"))
        if timestamp <= 0:
            raise ValueError("timestamp must be positive")
        if not strategy or not market_id or side not in {"YES", "NO"}:
            raise ValueError("strategy, market_id and side=YES/NO are required")
        if notional <= 0.0:
            raise ValueError("notional must be positive")
        if holding <= 0.0:
            raise ValueError("expected_holding_hours must be positive and point-in-time")
        if cost < 0.0:
            raise ValueError("cost_per_dollar must be non-negative")
        return cls(timestamp, strategy, market_id, side, notional, holding, net_edge, cost, realized)

    def forecast_edge(self, stress: float) -> float:
        if stress < 1.0:
            raise ValueError("cost stress must be >= 1")
        return self.net_edge_1x - (stress - 1.0) * self.cost_per_dollar

    def realized_pnl(self, stress: float) -> float:
        if stress < 1.0:
            raise ValueError("cost stress must be >= 1")
        return self.realized_pnl_1x - (stress - 1.0) * self.notional * self.cost_per_dollar

    def robust_edge(self) -> float:
        return min(self.forecast_edge(level) for level in STRESS_LEVELS)

    def capital_time_score(self) -> float:
        # Expected dollars of robust edge per dollar-hour of capital occupancy.
        return self.robust_edge() / self.expected_holding_hours


@dataclass(frozen=True)
class Selection:
    indices: tuple[int, ...]
    used_capital: float


def _select(
    rows: Sequence[Opportunity],
    *,
    capital_budget: float,
    score_kind: str,
    min_edge: float = 0.0,
) -> Selection:
    if capital_budget <= 0.0:
        return Selection((), 0.0)
    if score_kind not in {"incumbent", "capital_time"}:
        raise ValueError("unknown score kind")

    ranked: list[tuple[float, int]] = []
    for index, row in enumerate(rows):
        if score_kind == "incumbent":
            score = row.net_edge_1x
            admissible = row.net_edge_1x > min_edge
        else:
            score = row.capital_time_score()
            # The challenger may not rescue a candidate that fails 2x executable-cost stress.
            admissible = row.robust_edge() > min_edge
        if admissible:
            ranked.append((score, index))
    ranked.sort(key=lambda item: (-item[0], rows[item[1]].timestamp, item[1]))

    selected: list[int] = []
    remaining = capital_budget
    used = 0.0
    for _, index in ranked:
        row = rows[index]
        if row.notional <= remaining + 1e-12:
            selected.append(index)
            remaining -= row.notional
            used += row.notional
    return Selection(tuple(sorted(selected)), used)


def select_by_decision_window(
    rows: Sequence[Opportunity],
    *,
    capital_budget: float,
    window_seconds: int,
    score_kind: str,
    min_edge: float = 0.0,
) -> set[int]:
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    buckets: dict[int, list[int]] = {}
    for index, row in enumerate(rows):
        bucket = row.timestamp // window_seconds
        buckets.setdefault(bucket, []).append(index)

    selected: set[int] = set()
    for bucket in sorted(buckets):
        indices = buckets[bucket]
        local = [rows[index] for index in indices]
        pick = _select(local, capital_budget=capital_budget, score_kind=score_kind, min_edge=min_edge)
        selected.update(indices[local_index] for local_index in pick.indices)
    return selected


def _max_drawdown(pnls: Iterable[float]) -> float:
    equity = peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def evaluate(
    rows: Sequence[Opportunity],
    *,
    capital_budget: float,
    window_seconds: int = 60,
    fold_seconds: int = 7 * 24 * 3600,
    min_edge: float = 0.0,
) -> dict[str, object]:
    if not rows:
        return {
            "status": "MORE_EVIDENCE_REQUIRED",
            "reason": "no_opportunities",
            "rows": 0,
            "folds": 0,
            "evidence_ready": False,
        }
    if fold_seconds <= 0:
        raise ValueError("fold_seconds must be positive")

    ordered = sorted(rows, key=lambda row: (row.timestamp, row.market_id, row.side, row.strategy))
    incumbent = select_by_decision_window(
        ordered,
        capital_budget=capital_budget,
        window_seconds=window_seconds,
        score_kind="incumbent",
        min_edge=min_edge,
    )
    challenger = select_by_decision_window(
        ordered,
        capital_budget=capital_budget,
        window_seconds=window_seconds,
        score_kind="capital_time",
        min_edge=min_edge,
    )

    start = ordered[0].timestamp
    folds: dict[int, list[int]] = {}
    for index, row in enumerate(ordered):
        folds.setdefault((row.timestamp - start) // fold_seconds, []).append(index)

    stress_summary: dict[str, dict[str, float]] = {}
    all_stress_positive = True
    challenger_not_worse_dd = True
    fold_positive_at_2x = 0
    for stress in STRESS_LEVELS:
        inc_sequence = [ordered[i].realized_pnl(stress) if i in incumbent else 0.0 for i in range(len(ordered))]
        ch_sequence = [ordered[i].realized_pnl(stress) if i in challenger else 0.0 for i in range(len(ordered))]
        inc_pnl = sum(inc_sequence)
        ch_pnl = sum(ch_sequence)
        incremental = ch_pnl - inc_pnl
        inc_dd = _max_drawdown(inc_sequence)
        ch_dd = _max_drawdown(ch_sequence)
        stress_summary[str(stress)] = {
            "incumbent_pnl": inc_pnl,
            "challenger_pnl": ch_pnl,
            "incremental_pnl": incremental,
            "incumbent_max_drawdown_usd": inc_dd,
            "challenger_max_drawdown_usd": ch_dd,
        }
        all_stress_positive = all_stress_positive and incremental > 0.0
        challenger_not_worse_dd = challenger_not_worse_dd and ch_dd <= inc_dd + 1e-12

    fold_results: list[dict[str, object]] = []
    for fold, indices in sorted(folds.items()):
        inc_2x = sum(ordered[i].realized_pnl(2.0) for i in indices if i in incumbent)
        ch_2x = sum(ordered[i].realized_pnl(2.0) for i in indices if i in challenger)
        incremental = ch_2x - inc_2x
        fold_positive_at_2x += int(incremental > 0.0)
        fold_results.append({
            "fold": fold,
            "rows": len(indices),
            "incumbent_pnl_2x": inc_2x,
            "challenger_pnl_2x": ch_2x,
            "incremental_pnl_2x": incremental,
        })

    enough_folds = len(fold_results) >= 2
    stable = enough_folds and fold_positive_at_2x / len(fold_results) >= 0.5
    evidence_ready = enough_folds and stable and all_stress_positive and challenger_not_worse_dd
    return {
        "status": "EVIDENCE_READY" if evidence_ready else "MORE_EVIDENCE_REQUIRED",
        "evidence_ready": evidence_ready,
        "rows": len(ordered),
        "folds": len(fold_results),
        "incumbent_selected": len(incumbent),
        "challenger_selected": len(challenger),
        "capital_budget": capital_budget,
        "window_seconds": window_seconds,
        "fold_seconds": fold_seconds,
        "stress": stress_summary,
        "fold_results": fold_results,
        "positive_fold_fraction_2x": fold_positive_at_2x / len(fold_results) if fold_results else 0.0,
        "selection_contract": {
            "incumbent": "positive one-times-cost net edge ranked by net_edge_1x",
            "challenger": "positive two-times-cost robust edge ranked by robust edge per expected capital-hour",
            "forecast_only_selection": True,
            "future_realized_pnl_used_for_selection": False,
        },
    }


def read_csv(path: Path) -> list[Opportunity]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [Opportunity.from_row(dict(row)) for row in csv.DictReader(handle)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Research V5 cross-sleeve capital-time netting")
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--capital-budget", type=float, required=True)
    parser.add_argument("--window-seconds", type=int, default=60)
    parser.add_argument("--fold-seconds", type=int, default=7 * 24 * 3600)
    parser.add_argument("--min-edge", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = evaluate(
        read_csv(args.input_csv),
        capital_budget=args.capital_budget,
        window_seconds=args.window_seconds,
        fold_seconds=args.fold_seconds,
        min_edge=args.min_edge,
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
