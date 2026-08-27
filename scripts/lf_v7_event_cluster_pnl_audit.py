#!/usr/bin/env python3
"""Audit whether canonical V7 execution-PnL inference preserves event dependence.

Research-only diagnostic.  It never mutates runtime, risk, authority, refs or
execution state.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any


def load_sidecar(repo_root: Path):
    path = repo_root / "scripts" / "v7_execution_evidence.py"
    spec = importlib.util.spec_from_file_location("v7_execution_evidence", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_rows(*, unique_events: bool, days: int = 20) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for day in range(days):
        rows.append(
            {
                "timestamp": str((day + 1) * 86400),
                "event_id": f"event-{day}" if unique_events else "event-one",
                "bundle_id": f"bundle-{day}",
                "action": "SETTLE",
                "net_pnl": "1.0",
            }
        )
    return rows


def summarize(repo_root: Path) -> dict[str, Any]:
    sidecar = load_sidecar(repo_root)
    one_event = synthetic_rows(unique_events=False)
    many_events = synthetic_rows(unique_events=True)

    def current_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
        pnl = [(sidecar.timestamp(row), sidecar.realized_pnl(row)) for row in rows]
        return {
            "bootstrap_one_sided_pvalue": sidecar.block_bootstrap_one_sided(pnl, 1000, 17),
            "fold_count": sidecar.fold_stability(pnl)[0],
            "positive_fold_fraction": sidecar.fold_stability(pnl)[1],
            "terminal_pnl_observations": len(pnl),
            "net_pnl": sum(value for _, value in pnl),
            "distinct_event_clusters": len({sidecar.event_key(row, f"row-{i}") for i, row in enumerate(rows)}),
        }

    one = current_stats(one_event)
    many = current_stats(many_events)
    return {
        "schema": "lf_v7_event_cluster_pnl_audit_v1",
        "finding": "DAY_BLOCK_INFERENCE_IGNORES_EVENT_CLUSTER_DEPENDENCE",
        "one_event_repeated_across_20_days": one,
        "twenty_distinct_events_across_20_days": many,
        "incumbent_statistics_identical": {
            key: one[key] == many[key]
            for key in (
                "bootstrap_one_sided_pvalue",
                "fold_count",
                "positive_fold_fraction",
                "terminal_pnl_observations",
                "net_pnl",
            )
        },
        "event_cluster_inference": {
            "one_event_identifiable_clusters": 1,
            "one_event_uncertainty_estimable": False,
            "twenty_event_identifiable_clusters": 20,
            "reason": "The canonical bootstrap accepts only (timestamp, pnl) pairs, so event identity cannot affect its resampling law.",
        },
        "required_successor": [
            "bind every mature PnL observation to a canonical economic event_id and model/horizon identity",
            "fail closed when event identity is missing or ambiguous",
            "use dependence-aware inference that preserves event clustering and chronology, e.g. event-cluster or hierarchical event-by-time resampling",
            "do not count repeated sessions/settlements from one event as independent cross-event evidence",
            "keep the same frozen completed/partial economic units and same trades under 1x/1.5x/2x cost stress",
        ],
        "decision": "MORE_EVIDENCE_REQUIRED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = summarize(args.repo_root)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
