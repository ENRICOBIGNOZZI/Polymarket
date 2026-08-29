#!/usr/bin/env python3
"""Derive the explicit Graph/RV slippage component from canonical PAPER events.

Graph records effective execution prices.  This annotator reconstructs the
configured slippage component without changing realized cashflows/PnL and emits
one canonical EXIT cost annotation per terminal bundle.  Fees remain on FILL /
FINAL records; unwind, capital and latency remain on FINAL.  The annotation only
sets ``cost_vector_complete`` after all terminal components are observable.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from v7_execution_ledger import LedgerEvent, iter_events
from v7_ledger_spool import spool_event

STRATEGY = "GRAPH_RV"


def slippage_cost_from_effective_buy(fill_price: float, shares: float, slip: float) -> float:
    if slip <= 0 or fill_price <= 0 or shares <= 0:
        return 0.0
    raw = fill_price / (1.0 + slip)
    return max(0.0, shares * (fill_price - raw))


def slippage_cost_from_effective_sell_cashflow(cashflow: float, slip: float) -> float:
    if slip <= 0 or cashflow <= 0:
        return 0.0
    raw = cashflow / (1.0 - slip)
    return max(0.0, raw - cashflow)


def annotate(run_root: Path, *, model_sha: str, slippage_bps: float) -> dict[str, int]:
    ledger_path = Path(run_root) / "ledger" / "execution.jsonl"
    slip = max(0.0, float(slippage_bps)) / 10000.0
    actions: dict[str, str] = {}
    fills: dict[str, list[Any]] = defaultdict(list)
    finals: dict[str, Any] = {}
    annotated: set[str] = set()
    try:
        events = list(iter_events(ledger_path, expected_model_sha=model_sha))
    except (OSError, ValueError):
        return {"terminal_bundles": 0, "annotations_spooled": 0}
    for event in events:
        if event.strategy != STRATEGY or not event.bundle_id:
            continue
        if event.event_type == "ORDER_SUBMITTED" and event.order_id:
            actions[event.order_id] = str(event.intended_action or "").upper()
        elif event.event_type == "FILL":
            fills[event.bundle_id].append(event)
        elif event.event_type == "FINAL":
            finals[event.bundle_id] = event
        elif event.event_type == "EXIT" and isinstance(event.metadata, dict) and event.metadata.get("graph_cost_annotation") is True:
            annotated.add(event.bundle_id)
    spooled = 0
    for bundle_id, final in finals.items():
        if bundle_id in annotated:
            continue
        entry_slippage = 0.0
        for fill in fills.get(bundle_id, []):
            if actions.get(str(fill.order_id or "")) != "TAKER":
                continue
            entry_slippage += slippage_cost_from_effective_buy(float(fill.fill_price or 0.0), float(fill.filled_size or 0.0), slip)
        metadata = final.metadata if isinstance(final.metadata, dict) else {}
        reason = str(metadata.get("reason") or "")
        exit_slippage = 0.0
        if reason not in {"settled", "no_fill"}:
            exit_slippage = slippage_cost_from_effective_sell_cashflow(float(final.realized_cashflow or 0.0), slip)
        spool_event(Path(run_root), LedgerEvent(
            event_type="EXIT",
            strategy=STRATEGY,
            model_sha=model_sha,
            bundle_id=bundle_id,
            slippage=entry_slippage + exit_slippage,
            metadata={
                "graph_cost_annotation": True,
                "cost_vector_complete": True,
                "slippage_model": "configured_bps_reconstructed_from_effective_execution",
                "entry_slippage": entry_slippage,
                "exit_slippage": exit_slippage,
                "terminal_record_id": final.record_id,
            },
        ))
        spooled += 1
    return {"terminal_bundles": len(finals), "annotations_spooled": spooled}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args()
    print(annotate(args.run_root, model_sha=args.model_sha, slippage_bps=args.slippage_bps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
