#!/usr/bin/env python3
"""Build an online-safe direct joint bundle-state policy from the canonical ledger.

No product/minimum of marginal fill probabilities is ever computed.  A maker or
mixed entry style is published only after a minimum number of mature PAPER
bundles on the exact SHA.  Until then downstream execution must abstain or use
fully crossed execution whose completion is deterministically depth-checked.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from v7_execution_ledger import iter_events


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def build(ledger_path: Path, *, model_sha: str, strategy: str = "GRAPH_RV", min_bundles: int = 20) -> dict[str, Any]:
    bundles: dict[str, dict[str, Any]] = {}
    for event in iter_events(ledger_path, expected_model_sha=model_sha):
        if event.strategy != strategy or not event.bundle_id:
            continue
        b = bundles.setdefault(event.bundle_id, {"orders": {}, "fills": defaultdict(float), "final": None, "style": ""})
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        if metadata.get("entry_style"):
            b["style"] = str(metadata["entry_style"])
        if event.event_type == "ORDER_SUBMITTED" and event.order_id and event.leg_id and event.intended_size:
            b["orders"][event.leg_id] = {"size": float(event.intended_size), "action": str(event.intended_action or "").upper()}
        elif event.event_type == "FILL" and event.leg_id and event.filled_size:
            b["fills"][event.leg_id] += float(event.filled_size)
        elif event.event_type == "FINAL" and metadata.get("realized") is True:
            b["final"] = event
    groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for bundle in bundles.values():
        orders = bundle["orders"]
        if not orders:
            continue
        style = bundle["style"] or "/".join(orders[leg]["action"] for leg in sorted(orders))
        complete = all(bundle["fills"].get(leg, 0.0) + 1e-12 >= row["size"] for leg, row in orders.items())
        any_fill = any(value > 0 for value in bundle["fills"].values())
        final = bundle["final"]
        # Complete bundles mature immediately for fillability; partial/no-fill
        # bundles require terminal PAPER evidence so unresolved resting orders are censored.
        if not complete and final is None:
            continue
        state = "COMPLETE" if complete else "PARTIAL" if any_fill else "NO_FILL"
        unwind_per_unit = 0.0
        if state == "PARTIAL" and final is not None:
            targets = [row["size"] for row in orders.values() if row["size"] > 0]
            unit = min(targets) if targets else 0.0
            unwind_per_unit = float(final.unwind_loss or 0.0) / unit if unit > 0 else 0.0
        groups[(len(orders), style)].append({"state": state, "unwind_per_unit": unwind_per_unit})
    signatures: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, Any] = {}
    for (leg_count, style), sample in sorted(groups.items()):
        key = f"{leg_count}:{style}"
        counts = {name: sum(row["state"] == name for row in sample) for name in ("NO_FILL", "PARTIAL", "COMPLETE")}
        diagnostics[key] = {"n": len(sample), "states": counts, "eligible": len(sample) >= min_bundles}
        if len(sample) < min_bundles:
            continue
        signatures.setdefault(str(leg_count), {})[style] = {
            "n": len(sample),
            "p_complete": counts["COMPLETE"] / len(sample),
            "p_partial": counts["PARTIAL"] / len(sample),
            "p_no_fill": counts["NO_FILL"] / len(sample),
            "expected_partial_unwind_per_unit": sum(row["unwind_per_unit"] for row in sample) / len(sample),
            "source": "direct_empirical_canonical_bundle_states",
        }
    return {
        "schema": "polymarket_v7_joint_execution_policy_v1",
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "strategy": strategy,
        "min_mature_bundles": min_bundles,
        "uses_product_of_marginals": False,
        "uses_minimum_marginal_proxy": False,
        "signatures": signatures,
        "diagnostics": diagnostics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strategy", default="GRAPH_RV")
    parser.add_argument("--min-bundles", type=int, default=20)
    args = parser.parse_args()
    report = build(args.ledger, model_sha=args.model_sha, strategy=args.strategy, min_bundles=max(1, args.min_bundles))
    atomic_json(args.output, report)
    print(json.dumps({"published_signatures": sum(len(v) for v in report["signatures"].values()), "diagnostics": len(report["diagnostics"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
