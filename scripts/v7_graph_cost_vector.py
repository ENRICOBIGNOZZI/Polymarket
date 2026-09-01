#!/usr/bin/env python3
"""Derive Graph/RV slippage evidence from historical canonical PAPER events.

This is a zero-authority research reader. It reconstructs the configured
slippage component without changing realized cashflows/PnL and writes a
standalone evidence artifact. It cannot write the ledger transport.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
from typing import Any

from v7_execution_ledger import iter_events

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


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def annotate(run_root: Path, *, model_sha: str, slippage_bps: float) -> dict[str, Any]:
    ledger_path = Path(run_root) / "ledger" / "execution.jsonl"
    slip = max(0.0, float(slippage_bps)) / 10000.0
    actions: dict[str, str] = {}
    fills: dict[str, list[Any]] = defaultdict(list)
    finals: dict[str, Any] = {}
    annotated: set[str] = set()
    try:
        events = list(iter_events(ledger_path, expected_model_sha=model_sha))
    except (OSError, ValueError):
        return {
            "schema": "polymarket_v7_graph_cost_research_v1",
            "model_sha": model_sha,
            "research_only": True,
            "capital_authority": False,
            "oms_authority": False,
            "inventory_authority": False,
            "ledger_authority": False,
            "order_authority": False,
            "promotion_authority": False,
            "terminal_bundles": 0,
            "evidence_rows": [],
        }
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
    evidence_rows: list[dict[str, Any]] = []
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
        evidence_rows.append({
            "bundle_id": bundle_id,
            "slippage": entry_slippage + exit_slippage,
            "slippage_model": "configured_bps_reconstructed_from_effective_execution",
            "entry_slippage": entry_slippage,
            "exit_slippage": exit_slippage,
            "terminal_record_id": final.record_id,
        })
    return {
        "schema": "polymarket_v7_graph_cost_research_v1",
        "model_sha": model_sha,
        "research_only": True,
        "capital_authority": False,
        "oms_authority": False,
        "inventory_authority": False,
        "ledger_authority": False,
        "order_authority": False,
        "promotion_authority": False,
        "terminal_bundles": len(finals),
        "evidence_rows": evidence_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = annotate(args.run_root, model_sha=args.model_sha, slippage_bps=args.slippage_bps)
    output = args.output or args.run_root / "research" / "evidence" / "graph_cost_vector.json"
    atomic_json(output, result)
    print({"terminal_bundles": result["terminal_bundles"], "evidence_rows": len(result["evidence_rows"])})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
