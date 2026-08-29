#!/usr/bin/env python3
"""Run V4 walk-forward evaluation and attach non-overlapping evidence lineage.

The legacy evaluator remains authoritative for threshold selection and aggregate
OOS metrics. This wrapper reconstructs the selected OOS trades, partitions them
into fixed non-overlapping trade batches and emits a stable evidence ID. Re-runs
of the same ledger therefore cannot increase consecutive promotion passes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import walk_forward_v4 as legacy
from research_common import atomic_json, sha256_json


def option_value(name: str, default: str) -> str:
    for index, value in enumerate(sys.argv[1:]):
        if value == name:
            absolute = index + 1
            if absolute + 1 < len(sys.argv):
                return sys.argv[absolute + 1]
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
    return default


def selected_oos_trades(trades: list[Any], folds: list[dict[str, Any]]) -> list[Any]:
    selected: dict[str, Any] = {}
    for fold in folds:
        threshold = fold.get("threshold")
        if threshold is None:
            continue
        start = int(fold.get("test_start") or 0)
        end = int(fold.get("test_end") or 0)
        for trade in trades:
            if start <= trade.created_ts < end and trade.expected_edge >= float(threshold):
                key = trade.bundle_id or f"{trade.created_ts}:{trade.closed_ts}:{trade.strategy}"
                selected[key] = trade
    return sorted(selected.values(), key=lambda trade: (trade.created_ts, trade.closed_ts, trade.bundle_id))


def trade_record(trade: Any) -> dict[str, Any]:
    return {
        "bundle_id": trade.bundle_id,
        "strategy": trade.strategy,
        "created_ts": trade.created_ts,
        "closed_ts": trade.closed_ts,
        "status": trade.status,
        "expected_edge": trade.expected_edge,
        "capital": trade.capital,
        "gross_pnl": trade.gross_pnl,
        "fees": trade.fees,
        "slippage": trade.slippage,
        "net_pnl": trade.net_pnl,
        "return": trade.ret,
    }


def attach_lineage(result: dict[str, Any], trades: list[Any], batch_size: int, starting_capital: float) -> dict[str, Any]:
    records = [trade_record(trade) for trade in trades]
    dataset_hash = sha256_json(records)
    selected = selected_oos_trades(trades, list(result.get("folds") or []))
    complete_batches = len(selected) // max(1, batch_size)
    cost_multiplier = float(result.get("cost_stress_multiplier") or 1.5)

    evidence: dict[str, Any] = {
        "schema": "polymarket_independent_evidence_v1",
        "certified": False,
        "evidence_sequence": complete_batches,
        "batch_size": max(1, batch_size),
        "selected_oos_trades": len(selected),
        "dataset_hash": dataset_hash,
        "test_window_start": 0,
        "test_window_end": 0,
        "evidence_id": None,
        "pass": False,
        "gate_failures": ["no_complete_non_overlapping_oos_batch"],
    }

    if complete_batches > 0:
        start_index = (complete_batches - 1) * batch_size
        batch = selected[start_index : start_index + batch_size]
        normal = legacy.summarize(batch, starting_capital)
        stressed = legacy.summarize(batch, starting_capital, cost_multiplier)
        returns = [trade.ret for trade in batch]
        pvalue = legacy.circular_block_bootstrap_pvalue(
            returns,
            block=max(1, min(5, len(returns))),
            reps=2000,
            seed=20260824 + complete_batches,
        )
        failures: list[str] = []
        if normal["net_pnl"] <= 0.0:
            failures.append("nonpositive_independent_net_pnl")
        if stressed["net_pnl"] <= 0.0:
            failures.append("nonpositive_independent_stressed_pnl")
        if normal["max_drawdown"] > 0.10:
            failures.append("independent_drawdown_gate")
        if normal["profit_factor"] < 1.10:
            failures.append("independent_profit_factor_gate")
        if pvalue > 0.10:
            failures.append("independent_bootstrap_gate")
        window_start = min(trade.created_ts for trade in batch)
        window_end = max(trade.closed_ts for trade in batch)
        batch_records = [trade_record(trade) for trade in batch]
        evidence_id = sha256_json(
            {
                "protocol": "non_overlapping_selected_oos_trade_batches_v1",
                "sequence": complete_batches,
                "batch_records": batch_records,
                "cost_stress_multiplier": cost_multiplier,
                "normal": normal,
                "stressed": stressed,
                "bootstrap_one_sided_pvalue": pvalue,
            }
        )
        evidence = {
            "schema": "polymarket_independent_evidence_v1",
            "certified": True,
            "protocol": "non_overlapping_selected_oos_trade_batches_v1",
            "evidence_sequence": complete_batches,
            "batch_size": batch_size,
            "selected_oos_trades": len(selected),
            "dataset_hash": dataset_hash,
            "test_window_start": window_start,
            "test_window_end": window_end,
            "evidence_id": evidence_id,
            "normal": normal,
            "cost_stress": stressed,
            "cost_stress_multiplier": cost_multiplier,
            "bootstrap_one_sided_pvalue": pvalue,
            "pass": not failures,
            "gate_failures": failures,
            "bundle_ids": [trade.bundle_id for trade in batch],
        }

    result = dict(result)
    result["lineage_schema"] = "polymarket_walk_forward_lineage_v1"
    result["dataset_hash"] = dataset_hash
    result["information_cutoff"] = max((trade.closed_ts for trade in trades), default=0)
    result["cost_model_version"] = "realized_bundle_fees_slippage_v1"
    result["independent_evidence"] = evidence
    result["evidence_id"] = evidence.get("evidence_id")
    result["evidence_sequence"] = evidence.get("evidence_sequence", 0)
    result["test_window_start"] = evidence.get("test_window_start", 0)
    result["test_window_end"] = evidence.get("test_window_end", 0)
    return result


def main() -> int:
    rc = legacy.main()
    if rc != 0:
        return rc
    output = Path(option_value("--output", "runs/paper_v4/walk_forward.json"))
    ledger = Path(option_value("--ledger", "runs/paper_v4/bundle_ledger.csv"))
    batch_size = max(1, int(float(option_value("--min-oos-trades", "30"))))
    starting_capital = float(option_value("--starting-capital", "10000"))
    result = json.loads(output.read_text(encoding="utf-8"))
    trades = legacy.load_ledger(ledger)
    augmented = attach_lineage(result, trades, batch_size, starting_capital)
    atomic_json(output, augmented)
    print(json.dumps(augmented, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
