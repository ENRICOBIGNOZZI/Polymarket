#!/usr/bin/env python3
"""Canonical PAPER capital partition for V7 strategy workers.

Workers must never each inherit the full account balance. This module creates
atomic per-sleeve configs whose starting capital sums to at most the canonical
PAPER account. Once a child receives its sleeve budget, its own sleeve fraction
is normalized to 100% of that child budget so the allocation is not applied a
second time.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

SLEEVES = {
    "fast_structural": "fast_structural_capital_fraction",
    "graph_rv": "relative_value_capital_fraction",
    "hard_arb": "hard_arb_capital_fraction",
    "micro_taker": "micro_taker_capital_fraction",
    "micro_maker": "micro_maker_capital_fraction",
    "external": "external_capital_fraction",
}


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def allocate(config: dict[str, Any]) -> dict[str, float]:
    if config.get("paper_only") is not True:
        raise ValueError("allocator_requires_paper_only")
    v7 = config.get("v7") if isinstance(config.get("v7"), dict) else {}
    if v7.get("authenticated_execution") is not False or v7.get("real_order_submission") is not False:
        raise ValueError("allocator_requires_authenticated_disabled")
    total = float(config.get("starting_capital", 0.0))
    if total <= 0:
        raise ValueError("starting_capital_must_be_positive")
    fractions = {name: max(0.0, float(v7.get(key, 0.0))) for name, key in SLEEVES.items()}
    reserve = max(0.0, float(v7.get("reserve_fraction", 0.0)))
    used = sum(fractions.values()) + reserve
    if used > 1.0 + 1e-12:
        raise ValueError(f"capital_fractions_exceed_one:{used}")
    budgets = {name: total * fraction for name, fraction in fractions.items()}
    budgets["reserve"] = total * reserve + total * max(0.0, 1.0 - used)
    return budgets


def materialize(base_config: Path, output_dir: Path) -> dict[str, Any]:
    cfg = json.loads(base_config.read_text(encoding="utf-8"))
    budgets = allocate(cfg)
    for sleeve, active_key in SLEEVES.items():
        child = json.loads(json.dumps(cfg))
        child["starting_capital"] = budgets[sleeve]
        child_v7 = child.setdefault("v7", {})
        for other_key in SLEEVES.values():
            child_v7[other_key] = 1.0 if other_key == active_key else 0.0
        child_v7["reserve_fraction"] = 0.0
        child["capital_scope"] = {
            "canonical_account_starting_capital": float(cfg["starting_capital"]),
            "sleeve": sleeve,
            "sleeve_starting_capital": budgets[sleeve],
            "sleeve_internal_fraction": 1.0,
            "double_counting_forbidden": True,
        }
        atomic_json(output_dir / f"{sleeve}.json", child)
    manifest = {
        "schema": "polymarket_v7_capital_allocation_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "account_starting_capital": float(cfg["starting_capital"]),
        "budgets": budgets,
        "allocated_plus_reserve": sum(budgets.values()),
        "double_counting_forbidden": True,
        "child_configs_are_already_capacity_bounded": True,
    }
    atomic_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(materialize(args.config, args.output_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
