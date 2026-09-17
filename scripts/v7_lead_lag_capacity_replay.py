#!/usr/bin/env python3
"""Read-only capacity replay for LEAD_LAG_TAKER_V1 PAPER evidence.

Uses the exact arrival best ask and visible size retained in the canonical ledger.
It never sends orders and never fabricates deeper-book prices that were not stored.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_SHARE_GRID = (5.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0)
DEFAULT_NOTIONAL_GRID = (5.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0)
DEFAULT_COVERAGES = (0.90, 0.75, 0.50, 0.25)
DEFAULT_DEPTH_FRACTIONS = (1.0, 0.50, 0.25, 0.10)
STRATEGY = "CRYPTO_SETTLEMENT_ENGINE"
MODEL_FAMILY = "lead_lag_taker_v1"


def finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_grid(raw: str) -> tuple[float, ...]:
    values = tuple(float(x.strip()) for x in raw.split(",") if x.strip())
    if not values or any(not math.isfinite(x) or x <= 0 for x in values):
        raise argparse.ArgumentTypeError("grid values must be positive finite numbers")
    return tuple(sorted(set(values)))


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_no}: {exc}") from exc
            if isinstance(row, dict):
                yield row


def collect_candidate_markets(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"attempts": 0, "market_ids": set(), "rejected_events": 0, "rejected_market_ids": set()}
    market_ids: set[str] = set()
    rejected_market_ids: set[str] = set()
    attempts = rejected_events = 0
    for row in iter_jsonl(path):
        if row.get("event") == "CANDIDATE":
            attempts += 1
            if row.get("market_id"):
                market_ids.add(str(row["market_id"]))
        elif row.get("event") == "REJECTED":
            rejected_events += 1
            if row.get("market_id"):
                rejected_market_ids.add(str(row["market_id"]))
    return {"attempts": attempts, "market_ids": market_ids,
            "rejected_events": rejected_events, "rejected_market_ids": rejected_market_ids}


@dataclass(frozen=True)
class Trade:
    order_id: str
    market_id: str
    opened_ms: int
    final_ms: int
    ask: float
    top_visible_shares: float
    total_ask_depth_shares: float | None
    base_filled_shares: float
    fee_per_share: float
    pnl_per_share: float
    won: bool
    book_snapshot_id: str

    @property
    def top_visible_notional_usd(self) -> float:
        return self.ask * self.top_visible_shares

    @property
    def top_visible_cash_usd(self) -> float:
        return (self.ask + self.fee_per_share) * self.top_visible_shares

    @property
    def holding_ms(self) -> int:
        return max(0, self.final_ms - self.opened_ms)


def _is_lead_lag(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return (
        row.get("strategy") == STRATEGY
        and metadata.get("model_family") == MODEL_FAMILY
        and metadata.get("paper_forward_test") is True
    )


def collect_trades(path: Path) -> list[Trade]:
    orders: dict[str, dict[str, Any]] = {}
    fills: dict[str, dict[str, Any]] = {}
    finals: dict[str, dict[str, Any]] = {}
    for row in iter_jsonl(path):
        if not _is_lead_lag(row):
            continue
        if row.get("paper_only") is not True or row.get("authenticated_execution") is not False:
            raise ValueError("capacity replay refuses non-PAPER lead-lag ledger rows")
        if row.get("real_order_submission") not in (None, False):
            raise ValueError("capacity replay refuses rows with real order submission")
        order_id = str(row.get("order_id") or "")
        if not order_id:
            continue
        event_type = row.get("event_type")
        if event_type == "ORDER_SUBMITTED":
            orders[order_id] = row
        elif event_type == "FILL":
            fills[order_id] = row
        elif event_type == "FINAL":
            finals[order_id] = row

    trades: list[Trade] = []
    missing: list[str] = []
    for order_id, order in orders.items():
        fill, final = fills.get(order_id), finals.get(order_id)
        if fill is None or final is None:
            missing.append(order_id)
            continue
        metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
        ask = finite(order.get("ask")) or finite(metadata.get("arrival_best_ask"))
        visible = finite(metadata.get("arrival_best_ask_size"))
        filled = finite(fill.get("filled_size"))
        fee_per_share = finite(metadata.get("arrival_fee_per_share"))
        if fee_per_share is None and filled and filled > 0:
            fee = finite(fill.get("fee"))
            fee_per_share = fee / filled if fee is not None else None
        pnl = finite(final.get("final_pnl"))
        opened = int(finite(order.get("recorded_ts_ms")) or 0)
        closed = int(finite(final.get("recorded_ts_ms")) or 0)
        won = (final.get("metadata") or {}).get("won")
        required = (ask, visible, filled, fee_per_share, pnl)
        if any(value is None for value in required) or not opened or not closed or not isinstance(won, bool):
            missing.append(order_id)
            continue
        if ask <= 0 or ask >= 1 or visible <= 0 or filled <= 0 or closed < opened:
            raise ValueError(f"invalid lead-lag evidence for order {order_id}")
        trades.append(Trade(
            order_id=order_id, market_id=str(order.get("market_id") or ""),
            opened_ms=opened, final_ms=closed, ask=float(ask), top_visible_shares=float(visible),
            total_ask_depth_shares=finite(order.get("ask_depth")), base_filled_shares=float(filled),
            fee_per_share=float(fee_per_share), pnl_per_share=float(pnl) / float(filled),
            won=won, book_snapshot_id=str(order.get("book_snapshot_id") or ""),
        ))

    if missing:
        raise ValueError(f"incomplete lead-lag evidence for {len(missing)} orders")
    if not trades:
        raise ValueError("no settled LEAD_LAG_TAKER_V1 PAPER trades found")
    market_ids = [trade.market_id for trade in trades]
    if len(market_ids) != len(set(market_ids)):
        raise ValueError("capacity replay requires one settled entry per independent market")
    return sorted(trades, key=lambda trade: (trade.opened_ms, trade.market_id))


def empirical_threshold(values: list[float], coverage: float) -> float:
    if not values or not 0 < coverage <= 1:
        return math.nan
    ordered = sorted(values)
    candidates = [value for value in ordered if sum(x + 1e-12 >= value for x in ordered) / len(ordered) >= coverage]
    return max(candidates) if candidates else math.nan


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def peak_concurrent_cash(rows: list[tuple[int, int, float]]) -> float:
    events: list[tuple[int, int, float]] = []
    for opened_ms, final_ms, cash in rows:
        events.append((opened_ms, 1, cash))
        events.append((final_ms, 0, -cash))
    current = peak = 0.0
    for _, _, delta in sorted(events, key=lambda item: (item[0], item[1])):
        current += delta
        peak = max(peak, current)
    return peak


def bootstrap_mean_ci(values: list[float], draws: int, seed: int) -> dict[str, float | int | None]:
    if not values or draws <= 0:
        return {"draws": 0, "mean": None, "ci95_lower": None, "ci95_upper": None}
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(draws))
    lo = means[max(0, int(0.025 * draws) - 1)]
    hi = means[min(draws - 1, int(0.975 * draws))]
    return {"draws": draws, "mean": statistics.mean(values), "ci95_lower": lo, "ci95_upper": hi}


def scenario(
    trades: list[Trade], *, target: float, dimension: str, mode: str,
    bootstrap_draws: int, seed: int, opportunity_market_count: int | None = None,
    depth_fraction: float = 1.0,
) -> dict[str, Any]:
    if not 0 < depth_fraction <= 1:
        raise ValueError("depth_fraction must lie in (0, 1]")
    contributions: list[float] = []
    executions: list[tuple[Trade, float]] = []
    full_fills = clipped = 0
    for trade in trades:
        requested = target if dimension == "shares" else target / trade.ask
        effective_visible = trade.top_visible_shares * depth_fraction
        enough = effective_visible + 1e-12 >= requested
        full_fills += int(enough)
        if mode == "strict":
            filled = requested if enough else 0.0
        elif mode == "clip":
            filled = min(requested, effective_visible)
            clipped += int(filled + 1e-12 < requested)
        else:
            raise ValueError(f"unsupported mode {mode}")
        pnl = trade.pnl_per_share * filled
        contributions.append(pnl)
        if filled > 0:
            executions.append((trade, filled))

    notionals = [trade.ask * filled for trade, filled in executions]
    fees = [trade.fee_per_share * filled for trade, filled in executions]
    cash = [notional + fee for notional, fee in zip(notionals, fees)]
    pnls = [trade.pnl_per_share * filled for trade, filled in executions]
    shares = [filled for _, filled in executions]
    peak_rows = [
        (trade.opened_ms, trade.final_ms, (trade.ask + trade.fee_per_share) * filled)
        for trade, filled in executions
    ]
    total_cash = sum(cash)
    total_pnl = sum(pnls)
    opportunity_market_count = opportunity_market_count or len(trades)
    if opportunity_market_count < len(trades):
        raise ValueError("candidate market count cannot be smaller than settled trade count")
    candidate_contributions = contributions + [0.0] * (opportunity_market_count - len(trades))
    return {
        "dimension": dimension,
        "mode": mode,
        "depth_fraction": depth_fraction,
        "target": target,
        "reference_filled_markets": len(trades),
        "candidate_markets": opportunity_market_count,
        "executed_markets": len(executions),
        "participation_rate": len(executions) / opportunity_market_count,
        "conditional_same_price_coverage": len(executions) / len(trades),
        "full_fill_markets": full_fills,
        "full_fill_rate": full_fills / len(trades),
        "clipped_markets": clipped if mode == "clip" else 0,
        "total_filled_shares": sum(shares),
        "mean_filled_shares": statistics.mean(shares) if shares else None,
        "median_filled_shares": median(shares),
        "total_entry_notional_usd": sum(notionals),
        "total_fee_usd": sum(fees),
        "total_entry_cash_usd": total_cash,
        "mean_entry_cash_per_executed_market_usd": statistics.mean(cash) if cash else None,
        "median_entry_cash_per_executed_market_usd": median(cash),
        "max_entry_cash_single_market_usd": max(cash) if cash else None,
        "peak_concurrent_entry_cash_usd": peak_concurrent_cash(peak_rows),
        "realized_pnl_usd": total_pnl,
        "pnl_per_entry_cash_turnover": total_pnl / total_cash if total_cash else None,
        "wins": sum(1 for trade, _ in executions if trade.won),
        "losses": sum(1 for trade, _ in executions if not trade.won),
        "mean_pnl_per_reference_filled_market_usd": statistics.mean(contributions),
        "mean_pnl_per_candidate_market_usd": statistics.mean(candidate_contributions),
        "bootstrap_mean_pnl_per_candidate_market_usd": bootstrap_mean_ci(
            candidate_contributions, bootstrap_draws, seed
        ),
    }


def summarize_distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": min(ordered),
        "median": statistics.median(ordered),
        "mean": statistics.mean(ordered),
        "max": max(ordered),
        "threshold_by_required_coverage": {
            f"{int(coverage * 100)}pct": empirical_threshold(ordered, coverage)
            for coverage in DEFAULT_COVERAGES
        },
    }


def build_report(
    trades: list[Trade], *, share_grid: tuple[float, ...], notional_grid: tuple[float, ...],
    bootstrap_draws: int, seed: int, candidate_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    share_capacity = [trade.top_visible_shares for trade in trades]
    notional_capacity = [trade.top_visible_notional_usd for trade in trades]
    cash_capacity = [trade.top_visible_cash_usd for trade in trades]
    candidate_evidence = candidate_evidence or {}
    candidate_ids = set(candidate_evidence.get("market_ids") or ())
    if candidate_ids and not set(trade.market_id for trade in trades) <= candidate_ids:
        raise ValueError("settled lead-lag market missing from candidate event evidence")
    candidate_count = len(candidate_ids) if candidate_ids else len(trades)
    strict_share = [scenario(trades, target=x, dimension="shares", mode="strict",
                             bootstrap_draws=bootstrap_draws, seed=seed + i, opportunity_market_count=candidate_count)
                    for i, x in enumerate(share_grid)]
    clipped_share = [scenario(trades, target=x, dimension="shares", mode="clip",
                              bootstrap_draws=bootstrap_draws, seed=seed + 100 + i, opportunity_market_count=candidate_count)
                     for i, x in enumerate(share_grid)]
    strict_notional = [scenario(trades, target=x, dimension="notional_usd", mode="strict",
                                bootstrap_draws=bootstrap_draws, seed=seed + 200 + i, opportunity_market_count=candidate_count)
                       for i, x in enumerate(notional_grid)]
    clipped_notional = [scenario(trades, target=x, dimension="notional_usd", mode="clip",
                                 bootstrap_draws=bootstrap_draws, seed=seed + 300 + i, opportunity_market_count=candidate_count)
                        for i, x in enumerate(notional_grid)]
    observed = scenario(trades, target=5.0, dimension="shares", mode="strict",
                        bootstrap_draws=bootstrap_draws, seed=seed + 999, opportunity_market_count=candidate_count)
    ceiling_target = max(share_capacity) * 1.001
    top_level_ceiling = scenario(trades, target=ceiling_target, dimension="shares", mode="clip",
                                 bootstrap_draws=bootstrap_draws, seed=seed + 1000, opportunity_market_count=candidate_count)
    depth_stress: dict[str, Any] = {}
    for fraction in DEFAULT_DEPTH_FRACTIONS:
        label = f"{int(round(100 * fraction))}pct_displayed_depth_survives"
        depth_stress[label] = {
            "depth_fraction": fraction,
            "strict_fixed_share_grid": [
                scenario(trades, target=x, dimension="shares", mode="strict", bootstrap_draws=0,
                         seed=seed, opportunity_market_count=candidate_count, depth_fraction=fraction)
                for x in share_grid
            ],
            "strict_fixed_notional_grid": [
                scenario(trades, target=x, dimension="notional_usd", mode="strict", bootstrap_draws=0,
                         seed=seed, opportunity_market_count=candidate_count, depth_fraction=fraction)
                for x in notional_grid
            ],
        }
    return {
        "schema": "polymarket_v7_lead_lag_capacity_replay_v1",
        "paper_only": True,
        "real_order_submission": False,
        "strategy_id": "LEAD_LAG_TAKER_V1",
        "statistical_unit": "INDEPENDENT_SETTLED_MARKET",
        "settled_reference_markets": len(trades),
        "candidate_markets": candidate_count,
        "candidate_attempts": int(candidate_evidence.get("attempts") or 0),
        "rejected_events": int(candidate_evidence.get("rejected_events") or 0),
        "unique_rejected_markets": len(set(candidate_evidence.get("rejected_market_ids") or ())),
        "original_eventual_fill_rate": len(trades) / candidate_count,
        "observed_forward_test": observed,
        "same_price_capacity": {
            "top_visible_shares": summarize_distribution(share_capacity),
            "top_visible_notional_usd": summarize_distribution(notional_capacity),
            "top_visible_cash_including_fee_usd": summarize_distribution(cash_capacity),
        },
        "strict_fixed_share_grid": strict_share,
        "clip_to_visible_share_grid": clipped_share,
        "strict_fixed_notional_grid": strict_notional,
        "clip_to_visible_notional_grid": clipped_notional,
        "dynamic_top_level_ceiling": top_level_ceiling,
        "displayed_depth_survival_stress": depth_stress,
        "evidence_boundary": {
            "exact_at_arrival": [
                "best_ask_price", "best_ask_visible_size", "fee_per_share", "settlement_outcome",
                "entry_timestamp", "terminal_timestamp",
            ],
            "not_retained_historically": [
                "full_polymarket_ask_ladder_at_arrival",
                "price_by_level_beyond_best_ask",
            ],
            "full_book_sweep_replay_available": False,
            "interpretation": (
                "Strict scenarios are exact same-price capacity counterfactuals under the frozen "
                "ARRIVAL_BEST_ASK_NO_CHASE rule. Clip scenarios change only sizing by taking no more "
                "than the observed best-ask quantity. Depth-survival stress treats only a fixed fraction of "
                "displayed top-level size as dependable. None of these scenarios assumes unobserved deeper-book prices."
            ),
        },
        "trade_capacity": [
            {
                **asdict(trade),
                "top_visible_notional_usd": trade.top_visible_notional_usd,
                "top_visible_cash_usd": trade.top_visible_cash_usd,
                "holding_seconds": trade.holding_ms / 1000.0,
            }
            for trade in trades
        ],
    }


def write_scenario_csv(path: Path, report: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    for key in (
        "strict_fixed_share_grid", "clip_to_visible_share_grid",
        "strict_fixed_notional_grid", "clip_to_visible_notional_grid",
    ):
        for row in report[key]:
            rows.append({"scenario": key, **row})
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({field for row in rows for field in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            serialized = {
                key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            }
            writer.writerow(serialized)


def write_trade_csv(path: Path, report: dict[str, Any]) -> None:
    rows = report["trade_capacity"]
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({field for row in rows for field in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--shares-grid", type=parse_grid, default=DEFAULT_SHARE_GRID)
    parser.add_argument("--notional-grid", type=parse_grid, default=DEFAULT_NOTIONAL_GRID)
    parser.add_argument("--bootstrap-draws", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-scenarios-csv", type=Path)
    parser.add_argument("--output-trades-csv", type=Path)
    args = parser.parse_args()
    if args.bootstrap_draws < 0:
        parser.error("--bootstrap-draws must be non-negative")
    trades = collect_trades(args.ledger)
    candidate_evidence = collect_candidate_markets(args.events)
    report = build_report(
        trades,
        share_grid=tuple(args.shares_grid),
        notional_grid=tuple(args.notional_grid),
        bootstrap_draws=args.bootstrap_draws,
        seed=args.seed,
        candidate_evidence=candidate_evidence,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.output_scenarios_csv:
        write_scenario_csv(args.output_scenarios_csv, report)
    if args.output_trades_csv:
        write_trade_csv(args.output_trades_csv, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
