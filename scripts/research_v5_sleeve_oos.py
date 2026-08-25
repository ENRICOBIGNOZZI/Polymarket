#!/usr/bin/env python3
"""Research-only forward attribution for the independent V5 paper sleeves.

The production V5 manager runs five child books under
``runs/paper_v5_live/strategies/<name>``.  The legacy top-level walk-forward
report, however, is built from the separate multi-leg ``bundle_ledger.csv``.
This tool reconstructs *closed child-book paper trades* from each child's
``fills.csv`` and binds each entry to the most recent causal ``signals.csv``
row for the same market/side.

It is intentionally an evidence tool only: it never changes a live config,
order path, risk gate, or champion.  Missing entry-signal lineage fails closed
for economic/OOS eligibility while still being reported for accounting audit.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Signal:
    ts: int
    market_id: str
    side: str
    executable_price: float
    net_edge: float
    desired_notional: float


@dataclass
class Lot:
    strategy: str
    market_id: str
    slug: str
    side: str
    created_ts: int
    shares: float
    entry_price: float
    entry_fee_per_share: float
    expected_edge: float | None
    signal_ts: int | None
    signal_exec_price: float | None


@dataclass(frozen=True)
class ClosedTrade:
    strategy: str
    market_id: str
    slug: str
    side: str
    created_ts: int
    closed_ts: int
    exit_action: str
    shares: float
    entry_price: float
    exit_price: float
    entry_notional: float
    exit_notional: float
    entry_fee: float
    exit_fee: float
    fees: float
    gross_pnl: float
    net_pnl: float
    capital: float
    ret: float
    expected_edge: float | None
    signal_ts: int | None
    signal_age_seconds: int | None
    signal_exec_price: float | None
    entry_price_drift_cost: float | None
    lineage_ok: bool


def _float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def load_signals(path: Path) -> dict[tuple[str, str], tuple[list[int], list[Signal]]]:
    grouped: dict[tuple[str, str], list[Signal]] = {}
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            ts = _int(row.get("timestamp"), -1)
            market_id = str(row.get("market_id") or "")
            side = str(row.get("side") or "").upper()
            if ts < 0 or not market_id or side not in {"YES", "NO"}:
                continue
            signal = Signal(
                ts=ts,
                market_id=market_id,
                side=side,
                executable_price=_float(row.get("exec_price")),
                net_edge=_float(row.get("net_edge")),
                desired_notional=_float(row.get("desired_notional")),
            )
            grouped.setdefault((market_id, side), []).append(signal)
    indexed: dict[tuple[str, str], tuple[list[int], list[Signal]]] = {}
    for key, values in grouped.items():
        values.sort(key=lambda item: item.ts)
        indexed[key] = ([item.ts for item in values], values)
    return indexed


def causal_entry_signal(
    index: dict[tuple[str, str], tuple[list[int], list[Signal]]],
    market_id: str,
    side: str,
    fill_ts: int,
    max_lag_seconds: int,
) -> Signal | None:
    packed = index.get((market_id, side))
    if not packed:
        return None
    times, signals = packed
    pos = bisect.bisect_right(times, fill_ts) - 1
    if pos < 0:
        return None
    signal = signals[pos]
    if fill_ts - signal.ts > max_lag_seconds:
        return None
    return signal


def reconstruct_strategy(
    strategy: str,
    run_dir: Path,
    *,
    signal_max_lag_seconds: int = 15,
    epsilon: float = 1e-9,
) -> tuple[list[ClosedTrade], dict[str, object]]:
    signal_index = load_signals(run_dir / "signals.csv")
    fills_path = run_dir / "fills.csv"
    lots: dict[tuple[str, str], list[Lot]] = {}
    trades: list[ClosedTrade] = []
    audit = {
        "strategy": strategy,
        "fills_rows": 0,
        "buy_rows": 0,
        "exit_rows": 0,
        "settle_rows": 0,
        "entry_signal_missing": 0,
        "unmatched_exit_rows": 0,
        "over_exit_shares": 0.0,
        "open_lots": 0,
        "open_shares": 0.0,
        "closed_trades": 0,
        "lineage_ok_closed_trades": 0,
    }
    if not fills_path.exists():
        return trades, audit

    with fills_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows.sort(key=lambda row: (_int(row.get("timestamp")), str(row.get("market_id") or "")))

    for row in rows:
        audit["fills_rows"] = int(audit["fills_rows"]) + 1
        ts = _int(row.get("timestamp"), -1)
        market_id = str(row.get("market_id") or "")
        slug = str(row.get("slug") or "")
        action = str(row.get("action") or "").upper()
        side = str(row.get("side") or "").upper()
        shares = max(0.0, _float(row.get("shares")))
        price = _float(row.get("price"))
        fee = max(0.0, _float(row.get("fee")))
        if ts < 0 or not market_id or side not in {"YES", "NO"} or shares <= epsilon:
            continue
        key = (market_id, side)

        if action == "BUY":
            audit["buy_rows"] = int(audit["buy_rows"]) + 1
            signal = causal_entry_signal(signal_index, market_id, side, ts, signal_max_lag_seconds)
            if signal is None:
                audit["entry_signal_missing"] = int(audit["entry_signal_missing"]) + 1
            lots.setdefault(key, []).append(
                Lot(
                    strategy=strategy,
                    market_id=market_id,
                    slug=slug,
                    side=side,
                    created_ts=ts,
                    shares=shares,
                    entry_price=price,
                    entry_fee_per_share=fee / shares,
                    expected_edge=None if signal is None else signal.net_edge,
                    signal_ts=None if signal is None else signal.ts,
                    signal_exec_price=None if signal is None else signal.executable_price,
                )
            )
            continue

        if action not in {"SELL", "SETTLE"}:
            continue
        audit["exit_rows"] = int(audit["exit_rows"]) + 1
        if action == "SETTLE":
            audit["settle_rows"] = int(audit["settle_rows"]) + 1
        queue = lots.get(key, [])
        remaining = shares
        if not queue:
            audit["unmatched_exit_rows"] = int(audit["unmatched_exit_rows"]) + 1
            audit["over_exit_shares"] = float(audit["over_exit_shares"]) + remaining
            continue

        while remaining > epsilon and queue:
            lot = queue[0]
            matched = min(remaining, lot.shares)
            entry_fee = lot.entry_fee_per_share * matched
            exit_fee = fee * (matched / shares)
            entry_notional = lot.entry_price * matched
            exit_notional = price * matched
            gross = exit_notional - entry_notional
            net = gross - entry_fee - exit_fee
            capital = entry_notional + entry_fee
            signal_age = None if lot.signal_ts is None else lot.created_ts - lot.signal_ts
            price_drift = None
            if lot.signal_exec_price is not None:
                price_drift = max(0.0, lot.entry_price - lot.signal_exec_price) * matched
            trades.append(
                ClosedTrade(
                    strategy=strategy,
                    market_id=market_id,
                    slug=lot.slug or slug,
                    side=side,
                    created_ts=lot.created_ts,
                    closed_ts=ts,
                    exit_action=action,
                    shares=matched,
                    entry_price=lot.entry_price,
                    exit_price=price,
                    entry_notional=entry_notional,
                    exit_notional=exit_notional,
                    entry_fee=entry_fee,
                    exit_fee=exit_fee,
                    fees=entry_fee + exit_fee,
                    gross_pnl=gross,
                    net_pnl=net,
                    capital=capital,
                    ret=net / capital if capital > epsilon else 0.0,
                    expected_edge=lot.expected_edge,
                    signal_ts=lot.signal_ts,
                    signal_age_seconds=signal_age,
                    signal_exec_price=lot.signal_exec_price,
                    entry_price_drift_cost=price_drift,
                    lineage_ok=lot.signal_ts is not None,
                )
            )
            lot.shares -= matched
            remaining -= matched
            if lot.shares <= epsilon:
                queue.pop(0)
        if remaining > epsilon:
            audit["unmatched_exit_rows"] = int(audit["unmatched_exit_rows"]) + 1
            audit["over_exit_shares"] = float(audit["over_exit_shares"]) + remaining
        if not queue:
            lots.pop(key, None)

    open_lots = [lot for queue in lots.values() for lot in queue if lot.shares > epsilon]
    audit["open_lots"] = len(open_lots)
    audit["open_shares"] = sum(lot.shares for lot in open_lots)
    audit["closed_trades"] = len(trades)
    audit["lineage_ok_closed_trades"] = sum(trade.lineage_ok for trade in trades)
    return trades, audit


def stressed_pnl(trade: ClosedTrade, multiplier: float, slippage_bps: float) -> float:
    """Stress explicit variable costs on top of the already-realized paper prices.

    Base PnL uses actual paper fill prices, so the observed spread/price movement is
    already embedded.  Stress adds only incremental fee/slippage cost to avoid
    double-counting the base executable price.
    """
    turnover = trade.entry_notional + trade.exit_notional
    modeled_slippage = max(0.0, slippage_bps) * 1e-4 * turnover
    explicit_cost = trade.fees + modeled_slippage
    return trade.net_pnl - max(0.0, multiplier - 1.0) * explicit_cost


def summarize(trades: Iterable[ClosedTrade], *, multiplier: float, slippage_bps: float) -> dict[str, float | int]:
    rows = list(trades)
    pnls = [stressed_pnl(row, multiplier, slippage_bps) for row in rows]
    returns = [pnl / row.capital if row.capital > 0 else 0.0 for pnl, row in zip(pnls, rows)]
    pos = sum(value for value in pnls if value > 0)
    neg = -sum(value for value in pnls if value < 0)
    return {
        "trades": len(rows),
        "capital_sum": sum(row.capital for row in rows),
        "net_pnl": sum(pnls),
        "mean_return": statistics.fmean(returns) if returns else 0.0,
        "hit_rate": sum(value > 0 for value in pnls) / len(pnls) if pnls else 0.0,
        "profit_factor": pos / neg if neg > 1e-12 else (999.0 if pos > 0 else 0.0),
        "fees": sum(row.fees for row in rows),
        "entry_price_drift_cost": sum(row.entry_price_drift_cost or 0.0 for row in rows),
    }


def chronological_folds(
    trades: list[ClosedTrade], *, folds: int, multiplier: float, slippage_bps: float
) -> list[dict[str, object]]:
    rows = sorted(trades, key=lambda row: (row.created_ts, row.closed_ts, row.strategy, row.market_id))
    if not rows:
        return []
    k = max(1, min(folds, len(rows)))
    result = []
    for index in range(k):
        lo = index * len(rows) // k
        hi = (index + 1) * len(rows) // k
        block = rows[lo:hi]
        if not block:
            continue
        result.append(
            {
                "fold": index + 1,
                "start_ts": block[0].created_ts,
                "end_ts": max(row.closed_ts for row in block),
                "summary": summarize(block, multiplier=multiplier, slippage_bps=slippage_bps),
            }
        )
    return result


def count_bundle_oos_inputs(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("status") or "").upper() not in {"CLOSED", "UNWOUND"}:
                continue
            if _float(row.get("entry_cash")) <= 1e-9:
                continue
            count += 1
    return count


def write_ledger(path: Path, trades: list[ClosedTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(trades[0]).keys()) if trades else [field.name for field in ClosedTrade.__dataclass_fields__.values()]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            writer.writerow(asdict(trade))


def evaluate_run_root(
    run_root: Path,
    *,
    strategies: list[str] | None = None,
    signal_max_lag_seconds: int = 15,
    folds: int = 4,
    slippage_bps: float = 5.0,
    min_closed_trades: int = 30,
) -> tuple[dict[str, object], list[ClosedTrade]]:
    strategies_root = run_root / "strategies"
    if strategies is None:
        strategies = sorted(path.name for path in strategies_root.iterdir() if path.is_dir()) if strategies_root.exists() else []
    all_trades: list[ClosedTrade] = []
    audits = []
    for strategy in strategies:
        trades, audit = reconstruct_strategy(
            strategy,
            strategies_root / strategy,
            signal_max_lag_seconds=signal_max_lag_seconds,
        )
        all_trades.extend(trades)
        audits.append(audit)

    linked = [trade for trade in all_trades if trade.lineage_ok]
    bundle_inputs = count_bundle_oos_inputs(run_root / "bundle_ledger.csv")
    by_strategy = {
        strategy: {
            "1x": summarize([row for row in linked if row.strategy == strategy], multiplier=1.0, slippage_bps=slippage_bps),
            "1.5x": summarize([row for row in linked if row.strategy == strategy], multiplier=1.5, slippage_bps=slippage_bps),
            "2x": summarize([row for row in linked if row.strategy == strategy], multiplier=2.0, slippage_bps=slippage_bps),
        }
        for strategy in strategies
    }
    folds_2x = chronological_folds(linked, folds=folds, multiplier=2.0, slippage_bps=slippage_bps)
    positive_folds_2x = sum(float(fold["summary"]["net_pnl"]) > 0 for fold in folds_2x)
    lineage_missing = sum(int(audit["entry_signal_missing"]) for audit in audits)
    unmatched_exits = sum(int(audit["unmatched_exit_rows"]) for audit in audits)
    evidence_ready = (
        len(linked) >= min_closed_trades
        and len(folds_2x) >= 2
        and positive_folds_2x * 2 > len(folds_2x)
        and float(summarize(linked, multiplier=2.0, slippage_bps=slippage_bps)["net_pnl"]) > 0
        and lineage_missing == 0
        and unmatched_exits == 0
    )
    report: dict[str, object] = {
        "schema": "polymarket_v5_sleeve_oos_attribution_v1",
        "research_only": True,
        "run_root": str(run_root),
        "strategies": strategies,
        "lineage": {
            "signal_max_lag_seconds": signal_max_lag_seconds,
            "closed_trade_fragments": len(all_trades),
            "linked_closed_trade_fragments": len(linked),
            "missing_entry_signals": lineage_missing,
            "unmatched_exit_rows": unmatched_exits,
            "audits": audits,
        },
        "coverage_ablation": {
            "legacy_bundle_closed_inputs": bundle_inputs,
            "v5_child_closed_inputs": len(linked),
            "incremental_attributed_inputs": len(linked) - bundle_inputs,
            "note": "coverage comparison only; it is not an alpha/PnL comparison",
        },
        "cost_assumptions": {
            "base": "recorded paper entry/exit prices plus recorded fees",
            "stress_slippage_bps_on_turnover": slippage_bps,
            "multipliers": [1.0, 1.5, 2.0],
            "stress_rule": "base realized net PnL minus (multiplier-1)*(recorded fees + modeled slippage on entry+exit turnover)",
        },
        "aggregate": {
            "1x": summarize(linked, multiplier=1.0, slippage_bps=slippage_bps),
            "1.5x": summarize(linked, multiplier=1.5, slippage_bps=slippage_bps),
            "2x": summarize(linked, multiplier=2.0, slippage_bps=slippage_bps),
        },
        "by_strategy": by_strategy,
        "chronological_folds_2x": folds_2x,
        "positive_folds_2x": positive_folds_2x,
        "evidence_ready": evidence_ready,
        "decision": "EVIDENCE_READY" if evidence_ready else "MORE_EVIDENCE_REQUIRED",
        "promotion_boundary": "No production change; use this evidence only as an input to later incumbent/challenger ablations.",
    }
    return report, all_trades


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v5_live"))
    parser.add_argument("--strategies", nargs="+")
    parser.add_argument("--signal-max-lag-seconds", type=int, default=15)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--stress-slippage-bps", type=float, default=5.0)
    parser.add_argument("--min-closed-trades", type=int, default=30)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ledger-output", type=Path)
    args = parser.parse_args()

    report, trades = evaluate_run_root(
        args.run_root,
        strategies=args.strategies,
        signal_max_lag_seconds=max(0, args.signal_max_lag_seconds),
        folds=max(1, args.folds),
        slippage_bps=max(0.0, args.stress_slippage_bps),
        min_closed_trades=max(1, args.min_closed_trades),
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    if args.ledger_output:
        write_ledger(args.ledger_output, trades)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
