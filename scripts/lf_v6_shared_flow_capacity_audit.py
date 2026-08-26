#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PassiveOrder:
    order_id: str
    queue_ahead: float
    target_shares: float


def incumbent_fill(order: PassiveOrder, trade_size: float) -> float:
    """Replicate the per-order capacity implied by the current nested replay."""
    return min(order.target_shares, max(0.0, trade_size - order.queue_ahead))


def incumbent_total_fill(orders: list[PassiveOrder], trade_size: float) -> float:
    return sum(incumbent_fill(order, trade_size) for order in orders)


def shared_queue_fill(orders: list[PassiveOrder], trade_size: float) -> dict[str, float]:
    """Allocate one observed trade once across same-price orders behind one queue.

    The fixture deliberately requires the same external queue snapshot. That is the
    minimal case needed to show that public flow is a shared capacity constraint.
    Orders are allocated by stable order_id only to make the diagnostic deterministic;
    a production successor should use causal arrival/priority state.
    """
    if not orders:
        return {}
    queue = orders[0].queue_ahead
    if any(abs(order.queue_ahead - queue) > 1e-12 for order in orders):
        raise ValueError("shared_queue_fill requires a common queue snapshot")
    remaining = max(0.0, trade_size - queue)
    out: dict[str, float] = {}
    for order in sorted(orders, key=lambda x: x.order_id):
        fill = min(order.target_shares, remaining)
        out[order.order_id] = fill
        remaining -= fill
    return out


def source_contract(source: str) -> dict[str, bool]:
    compact = "".join(source.split())
    return {
        "trade_loop_present": "for(constauto&t:trades)" in compact,
        "leg_loop_present": "for(auto&l:legs_)" in compact,
        "full_trade_size_passed_per_leg": (
            "consume_passive_buy(l.queue_ahead,l.remaining(),l.limit_price,t.price,t.size,true)"
            in compact
        ),
        "shared_trade_capacity_variable_present": any(
            name in source
            for name in (
                "remaining_trade_size",
                "shared_trade_capacity",
                "unallocated_trade_size",
            )
        ),
    }


def run_audit(repo_root: Path) -> dict[str, object]:
    source_path = repo_root / "src" / "multileg_paper.cpp"
    source = source_path.read_text(encoding="utf-8")
    contract = source_contract(source)

    orders = [
        PassiveOrder("bundle-A", queue_ahead=100.0, target_shares=10.0),
        PassiveOrder("bundle-B", queue_ahead=100.0, target_shares=10.0),
    ]
    trade_size = 110.0
    incumbent = {o.order_id: incumbent_fill(o, trade_size) for o in orders}
    shared = shared_queue_fill(orders, trade_size)

    zero_queue_orders = [
        PassiveOrder("A", queue_ahead=0.0, target_shares=10.0),
        PassiveOrder("B", queue_ahead=0.0, target_shares=10.0),
        PassiveOrder("C", queue_ahead=0.0, target_shares=10.0),
    ]
    zero_queue_trade = 15.0
    zero_incumbent = incumbent_total_fill(zero_queue_orders, zero_queue_trade)
    zero_shared = sum(shared_queue_fill(zero_queue_orders, zero_queue_trade).values())

    return {
        "source_contract": contract,
        "same_queue_fixture": {
            "trade_size": trade_size,
            "external_queue_ahead": 100.0,
            "target_shares_per_order": 10.0,
            "incumbent_fills": incumbent,
            "incumbent_total_fill": sum(incumbent.values()),
            "shared_capacity_fills": shared,
            "shared_capacity_total_fill": sum(shared.values()),
            "capacity_overstatement_shares": sum(incumbent.values()) - sum(shared.values()),
        },
        "zero_queue_fixture": {
            "trade_size": zero_queue_trade,
            "orders": len(zero_queue_orders),
            "target_shares_per_order": 10.0,
            "incumbent_total_fill": zero_incumbent,
            "shared_capacity_total_fill": zero_shared,
            "incumbent_fill_over_trade_volume": zero_incumbent / zero_queue_trade,
        },
        "finding": (
            "The current multileg replay applies the full observed public trade size "
            "independently to every matching passive paper leg. Public trade volume is "
            "shared capacity, so overlapping orders can receive aggregate simulated fills "
            "that exceed the flow that actually traded."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_audit(args.repo_root.resolve())
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
