#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def summarize_arm(name: str, run_dir: Path) -> dict[str, Any]:
    fills = _rows(run_dir / "maker_fills.csv")
    orders = _rows(run_dir / "maker_order_log.csv")
    equity = _rows(run_dir / "maker_equity.csv")

    inventory_shares: dict[str, float] = defaultdict(float)
    inventory_cost: dict[str, float] = defaultdict(float)
    realized_pnl = 0.0
    closed_shares = 0.0
    maker_filled_shares = 0.0
    maker_fill_events = 0
    exit_events = 0

    for row in fills:
        action = (row.get("action") or "").upper()
        market = row.get("market_id") or ""
        shares = max(0.0, _f(row.get("shares")))
        price = _f(row.get("price"))
        fee = max(0.0, _f(row.get("fee")))
        if action in {"BUY_MAKER", "BUY_MAKER_PARTIAL"}:
            inventory_shares[market] += shares
            inventory_cost[market] += shares * price + fee
            maker_filled_shares += shares
            maker_fill_events += 1
        elif action in {"SELL_TAKER", "SELL_TAKER_PARTIAL"} and shares > 0.0:
            held = inventory_shares.get(market, 0.0)
            if held <= 1e-12:
                continue
            matched = min(held, shares)
            avg_cost = inventory_cost[market] / held if held > 1e-12 else 0.0
            proceeds = matched * price
            proportional_fee = fee * (matched / shares) if shares > 1e-12 else 0.0
            realized_pnl += proceeds - proportional_fee - matched * avg_cost
            closed_shares += matched
            exit_events += 1
            remaining = held - matched
            inventory_shares[market] = max(0.0, remaining)
            inventory_cost[market] = max(0.0, avg_cost * remaining)

    action_counts = Counter((row.get("action") or "UNKNOWN").upper() for row in orders)
    reserved_capital_seconds = 0.0
    if len(equity) >= 2:
        points: list[tuple[int, float]] = []
        for row in equity:
            try:
                points.append((int(float(row.get("timestamp") or 0)), max(0.0, _f(row.get("reserved_cash")))))
            except (TypeError, ValueError):
                continue
        points.sort()
        for (t0, r0), (t1, _) in zip(points, points[1:]):
            if t1 > t0:
                reserved_capital_seconds += r0 * (t1 - t0)

    open_shares = sum(max(0.0, x) for x in inventory_shares.values())
    pnl_per_closed_share = realized_pnl / closed_shares if closed_shares > 1e-12 else None
    filled_notional = 0.0
    for row in fills:
        if (row.get("action") or "").upper() in {"BUY_MAKER", "BUY_MAKER_PARTIAL"}:
            filled_notional += max(0.0, _f(row.get("shares"))) * max(0.0, _f(row.get("price")))

    return {
        "arm": name,
        "run_dir": str(run_dir),
        "maker_fill_events": maker_fill_events,
        "maker_filled_shares": maker_filled_shares,
        "maker_filled_notional": filled_notional,
        "exit_events": exit_events,
        "closed_shares": closed_shares,
        "open_shares": open_shares,
        "realized_fill_conditioned_pnl": realized_pnl,
        "realized_pnl_per_closed_share": pnl_per_closed_share,
        "reserved_capital_seconds": reserved_capital_seconds,
        "order_actions": dict(sorted(action_counts.items())),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# HF maker common-window A/B",
        "",
        "| Arm | Maker fill events | Filled shares | Closed shares | Realized PnL | PnL/closed share | Reserved $-sec |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in payload["arms"]:
        pps = arm["realized_pnl_per_closed_share"]
        pps_text = "n/a" if pps is None else f"{pps:.8f}"
        lines.append(
            f"| {arm['arm']} | {arm['maker_fill_events']} | {arm['maker_filled_shares']:.6f} | "
            f"{arm['closed_shares']:.6f} | {arm['realized_fill_conditioned_pnl']:.8f} | {pps_text} | "
            f"{arm['reserved_capital_seconds']:.2f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", action="append", required=True, help="NAME:RUN_DIR")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown", required=True)
    args = parser.parse_args()

    arms = []
    for item in args.arm:
        name, sep, directory = item.partition(":")
        if not sep or not name or not directory:
            raise SystemExit(f"invalid --arm {item!r}; expected NAME:RUN_DIR")
        arms.append(summarize_arm(name, Path(directory)))

    payload = {
        "schema": "hf_maker_common_window_ab_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "arms": arms,
    }
    Path(args.output_json).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path(args.output_markdown).write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
