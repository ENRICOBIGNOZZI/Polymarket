#!/usr/bin/env python3
"""Price-aware settlement diagnostics for frozen PAPER taker outcomes.

This module never submits orders and never selects a production threshold.
It measures the economics the policy must beat: executable price, fee,
break-even probability, payoff asymmetry, calibration and bounded-risk sizing.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

PRICE_BINS = (
    (0.00, 0.05),
    (0.05, 0.20),
    (0.20, 0.50),
    (0.50, 0.80),
    (0.80, 0.90),
    (0.90, 0.95),
    (0.95, 1.0000001),
)
RISK_CAPS_USD = (1.0, 2.0, 5.0, 10.0, 20.0)


def _finite(value: Any) -> float:
    out = float(value)
    if not math.isfinite(out):
        raise ValueError("non-finite numeric input")
    return out


def normalize_trade(row: dict[str, Any]) -> dict[str, Any]:
    market = str(row.get("market") or row.get("market_id") or "").strip()
    asset = str(row.get("asset") or "").upper()
    horizon = str(row.get("horizon") or "").upper()
    price = _finite(row["entry_price"])
    shares = _finite(row["shares"])
    fee = _finite(row.get("fee_usd", row.get("fee", 0.0)))
    payout = _finite(row["outcome_payout"])
    if not market or not asset or not horizon:
        raise ValueError("trade identity missing")
    if not 0.0 < price < 1.0 or shares <= 0.0 or fee < 0.0 or not 0.0 <= payout <= 1.0:
        raise ValueError("invalid trade economics")
    fee_per_share = fee / shares
    break_even = price + fee_per_share
    loss_per_share = price + fee_per_share
    win_per_share = 1.0 - price - fee_per_share
    net_pnl = shares * (payout - price) - fee
    return {
        "market": market,
        "asset": asset,
        "horizon": horizon,
        "entry_price": price,
        "shares": shares,
        "fee_usd": fee,
        "fee_per_share": fee_per_share,
        "outcome_payout": payout,
        "won": payout > 0.5,
        "break_even_probability": break_even,
        "max_loss_per_share": loss_per_share,
        "win_gain_per_share": win_per_share,
        "loss_to_win_ratio": (
            loss_per_share / win_per_share if win_per_share > 0.0 else math.inf
        ),
        "net_pnl": net_pnl,
    }


def max_loss_capped_shares(
    row: dict[str, Any], max_loss_usd: float, *,
    max_shares: float | None = None, min_shares: float = 0.0,
) -> float:
    trade = normalize_trade(row)
    cap = _finite(max_loss_usd)
    if cap <= 0.0 or min_shares < 0.0:
        raise ValueError("invalid risk cap")
    quantity = cap / trade["max_loss_per_share"]
    quantity = min(quantity, trade["shares"])
    if max_shares is not None:
        maximum = _finite(max_shares)
        if maximum <= 0.0:
            raise ValueError("invalid max shares")
        quantity = min(quantity, maximum)
    if quantity + 1e-12 < min_shares:
        return 0.0
    return max(0.0, quantity)


def _group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "trades": 0, "wins": 0, "win_rate": None, "mean_entry_price": None,
            "fees_usd": 0.0, "net_pnl_usd": 0.0, "pre_fee_pnl_usd": 0.0,
            "mean_break_even_probability": None, "brier_pm_price": None,
            "calibration_gap_outcome_minus_price": None,
        }
    wins = sum(r["won"] for r in rows)
    pre_fee = sum(r["net_pnl"] + r["fee_usd"] for r in rows)
    brier = mean((r["outcome_payout"] - r["entry_price"]) ** 2 for r in rows)
    return {
        "trades": len(rows),
        "wins": wins,
        "win_rate": wins / len(rows),
        "mean_entry_price": mean(r["entry_price"] for r in rows),
        "fees_usd": sum(r["fee_usd"] for r in rows),
        "net_pnl_usd": sum(r["net_pnl"] for r in rows),
        "pre_fee_pnl_usd": pre_fee,
        "mean_break_even_probability": mean(r["break_even_probability"] for r in rows),
        "brier_pm_price": brier,
        "calibration_gap_outcome_minus_price": mean(
            r["outcome_payout"] - r["entry_price"] for r in rows
        ),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trades = [normalize_trade(row) for row in rows]
    if len({r["market"] for r in trades}) != len(trades):
        raise ValueError("one taker settlement observation per market required")
    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_horizon: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        by_asset[row["asset"]].append(row)
        by_horizon[row["horizon"]].append(row)
    bins = []
    for low, high in PRICE_BINS:
        selected = [r for r in trades if low <= r["entry_price"] < high]
        bins.append({"low": low, "high": min(high, 1.0), **_group(selected)})

    sizing = []
    for cap in RISK_CAPS_USD:
        pnl = 0.0
        notional = 0.0
        total_shares = 0.0
        skipped = 0
        for raw, trade in zip(rows, trades):
            q = max_loss_capped_shares(raw, cap)
            if q <= 0.0:
                skipped += 1
                continue
            scale = q / trade["shares"]
            pnl += trade["net_pnl"] * scale
            notional += trade["entry_price"] * q
            total_shares += q
        sizing.append({
            "max_loss_usd_per_trade": cap,
            "net_pnl_usd": pnl,
            "entry_notional_usd": notional,
            "shares": total_shares,
            "skipped": skipped,
            "impact_and_fillability_replayed": False,
        })

    extreme = {
        "price_ge_0_90": _group([r for r in trades if r["entry_price"] >= 0.90]),
        "price_ge_0_95": _group([r for r in trades if r["entry_price"] >= 0.95]),
        "price_le_0_20": _group([r for r in trades if r["entry_price"] <= 0.20]),
        "price_le_0_05": _group([r for r in trades if r["entry_price"] <= 0.05]),
    }
    return {
        "schema": "polymarket_price_aware_settlement_diagnostics_v1",
        "paper_only": True,
        "execution_authority": False,
        "automatic_promotion": False,
        "overall": _group(trades),
        "by_asset": {k: _group(v) for k, v in sorted(by_asset.items())},
        "by_horizon": {k: _group(v) for k, v in sorted(by_horizon.items())},
        "price_bins": bins,
        "extreme_price_diagnostics": extreme,
        "max_loss_sizing_diagnostics": sizing,
        "trades": trades,
        "limitations": [
            "Resolved historical taker trades are not a held-out profitability proof.",
            "Risk-cap sizing scales observed per-share PnL and does not replay depth or impact.",
            "Price bins are diagnostics, not thresholds selected for production.",
            "Polymarket executable entry price is treated as a baseline probability proxy only.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output exists")
    value = json.loads(args.input.read_text())
    rows = value["trades"] if isinstance(value, dict) else value
    report = summarize(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({
        "overall": report["overall"],
        "by_asset": report["by_asset"],
        "by_horizon": report["by_horizon"],
        "output": str(args.output),
    }, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
