#!/usr/bin/env python3
"""Research-only V5 cross-sleeve capital-time selector.

The live V5 architecture runs independent paper books. This module tests a
portfolio-level challenger without changing any expert, execution rule, risk
limit, or live configuration: when actual incumbent opportunities have already
reserved finite portfolio capital in a decision window, ask whether the same
capital would have earned more by selecting cost-robust opportunities according
to expected edge per capital-hour.

The incumbent set is never reconstructed from a guessed ranking rule. Each
input row carries an ``incumbent_selected`` flag taken from a point-in-time
replay/decision ledger. The challenger is capped by the *actual incumbent
capital used in that same decision window* (and by a global cap), so it cannot
manufacture extra activity or extra gross capital. Forecast fields determine
challenger selection; future realized PnL is used only after selection for the
chronological OOS ablation. This file is deliberately offline/research-only.
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
_TRUE = {"1", "true", "yes", "y"}
_FALSE = {"0", "false", "no", "n", ""}


def _finite(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


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
    incumbent_selected: bool

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
        incumbent = _bool(row.get("incumbent_selected"))
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
        return cls(timestamp, strategy, market_id, side, notional, holding, net_edge, cost, realized, incumbent)

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
        # Expected robust dollars per dollar-hour of capital occupancy.
        return self.robust_edge() / self.expected_holding_hours


def _buckets(rows: Sequence[Opportunity], window_seconds: int) -> dict[int, list[int]]:
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    out: dict[int, list[int]] = {}
    for index, row in enumerate(rows):
        out.setdefault(row.timestamp // window_seconds, []).append(index)
    return out


def incumbent_selection(rows: Sequence[Opportunity]) -> set[int]:
    return {index for index, row in enumerate(rows) if row.incumbent_selected}


def select_challenger(
    rows: Sequence[Opportunity],
    *,
    global_capital_budget: float,
    window_seconds: int,
    min_edge: float = 0.0,
) -> set[int]:
    """Select cost-robust rows using no more capital than the incumbent used.

    No incumbent capital in a decision window means no challenger capital in
    that window. This prevents the research layer from turning missing/blocked
    incumbent activity into a fabricated alpha result.
    """
    if global_capital_budget <= 0.0:
        raise ValueError("global_capital_budget must be positive")

    selected: set[int] = set()
    for _, indices in sorted(_buckets(rows, window_seconds).items()):
        incumbent_used = sum(rows[i].notional for i in indices if rows[i].incumbent_selected)
        budget = min(global_capital_budget, incumbent_used)
        if budget <= 0.0:
            continue

        ranked: list[tuple[float, int]] = []
        for index in indices:
            row = rows[index]
            if row.robust_edge() > min_edge:
                ranked.append((row.capital_time_score(), index))
        ranked.sort(key=lambda item: (-item[0], rows[item[1]].timestamp, item[1]))

        remaining = budget
        for _, index in ranked:
            row = rows[index]
            if row.notional <= remaining + 1e-12:
                selected.add(index)
                remaining -= row.notional
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
    global_capital_budget: float,
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
    incumbent = incumbent_selection(ordered)
    challenger = select_challenger(
        ordered,
        global_capital_budget=global_capital_budget,
        window_seconds=window_seconds,
        min_edge=min_edge,
    )

    # Verify capital parity window by window. Challenger may use less, never more.
    capital_windows: list[dict[str, float | int]] = []
    parity_ok = True
    for bucket, indices in sorted(_buckets(ordered, window_seconds).items()):
        incumbent_used = sum(ordered[i].notional for i in indices if i in incumbent)
        challenger_used = sum(ordered[i].notional for i in indices if i in challenger)
        allowed = min(global_capital_budget, incumbent_used)
        if incumbent_used > global_capital_budget + 1e-12:
            # The normalized replay itself violates the declared global cap.
            raise ValueError("incumbent selected capital exceeds global_capital_budget")
        parity_ok = parity_ok and challenger_used <= allowed + 1e-12
        capital_windows.append({
            "bucket": bucket,
            "incumbent_used": incumbent_used,
            "challenger_used": challenger_used,
            "allowed": allowed,
        })

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
    evidence_ready = enough_folds and stable and all_stress_positive and challenger_not_worse_dd and parity_ok
    return {
        "status": "EVIDENCE_READY" if evidence_ready else "MORE_EVIDENCE_REQUIRED",
        "evidence_ready": evidence_ready,
        "rows": len(ordered),
        "folds": len(fold_results),
        "incumbent_selected": len(incumbent),
        "challenger_selected": len(challenger),
        "global_capital_budget": global_capital_budget,
        "window_seconds": window_seconds,
        "fold_seconds": fold_seconds,
        "capital_parity_ok": parity_ok,
        "capital_windows": capital_windows,
        "stress": stress_summary,
        "fold_results": fold_results,
        "positive_fold_fraction_2x": fold_positive_at_2x / len(fold_results) if fold_results else 0.0,
        "selection_contract": {
            "incumbent": "exact point-in-time incumbent_selected replay flags",
            "challenger": "positive two-times-cost robust edge ranked by robust edge per expected capital-hour",
            "challenger_capital_per_window": "no greater than actual incumbent capital used in that window",
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
    parser.add_argument("--global-capital-budget", type=float, required=True)
    parser.add_argument("--window-seconds", type=int, default=60)
    parser.add_argument("--fold-seconds", type=int, default=7 * 24 * 3600)
    parser.add_argument("--min-edge", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = evaluate(
        read_csv(args.input_csv),
        global_capital_budget=args.global_capital_budget,
        window_seconds=args.window_seconds,
        fold_seconds=args.fold_seconds,
        min_edge=args.min_edge,
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        root = Path.cwd().resolve()
        output = args.output.resolve()
        if not output.is_relative_to(root):
            raise ValueError("output must remain inside the current repository/project directory")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
