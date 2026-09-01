#!/usr/bin/env python3
"""Canonical PAPER capital envelopes for the two V7 economic engines.

Only ``V7_CANONICAL_ALLOCATOR`` owns account capital. Engine envelopes are
capacity limits, not independent accounts. Component observation budgets are
counterfactual sizing/compute limits and never enter account equity, capital
reservation, OMS, inventory, or canonical PnL.

Temporary component-named child files adapt the existing runtime command line
to engine envelopes. They declare their canonical replacement and may not
create a third authority. The migration manifest owns their deletion gate.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


ALLOCATOR_OWNER = "V7_CANONICAL_ALLOCATOR"
ENGINES = ("BTC_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE")
ENGINE_ADAPTERS = {
    "external": "BTC_SETTLEMENT_ENGINE",
    "hard_arb": "STRUCTURAL_ARB_ENGINE",
}
COMPONENT_OBSERVERS = {
    "micro_maker": "professional_maker",
    "fast_structural": "fast_structural",
}
RESEARCH_VIEWS = {
    "graph_rv": "graph_rv",
    "micro_taker": "micro_taker",
}


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name}_must_be_nonnegative")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}_must_be_nonnegative") from exc
    if result < 0.0 or result == float("inf") or result != result:
        raise ValueError(f"{name}_must_be_nonnegative")
    return result


def allocate(config: dict[str, Any]) -> dict[str, float]:
    if config.get("paper_only") is not True:
        raise ValueError("allocator_requires_paper_only")
    v7 = config.get("v7") if isinstance(config.get("v7"), dict) else {}
    if (
        v7.get("authenticated_execution") is not False
        or v7.get("real_order_submission") is not False
        or v7.get("capital_authority_owner") != ALLOCATOR_OWNER
    ):
        raise ValueError("allocator_requires_canonical_owner_and_authenticated_disabled")
    total = _finite_nonnegative(config.get("starting_capital"), "starting_capital")
    if total <= 0.0:
        raise ValueError("starting_capital_must_be_positive")
    raw = v7.get("engine_capital_fractions")
    if not isinstance(raw, dict) or set(raw) != set(ENGINES):
        raise ValueError("engine_capital_fraction_partition")
    fractions = {
        engine: _finite_nonnegative(raw[engine], f"engine_fraction:{engine}")
        for engine in ENGINES
    }
    reserve_fraction = _finite_nonnegative(v7.get("reserve_fraction"), "reserve_fraction")
    used = sum(fractions.values()) + reserve_fraction
    if used > 1.0 + 1e-12:
        raise ValueError(f"capital_fractions_exceed_one:{used}")
    budgets = {engine: total * fractions[engine] for engine in ENGINES}
    budgets["reserve"] = total * (reserve_fraction + max(0.0, 1.0 - used))
    return budgets


def component_observation_budgets(config: dict[str, Any]) -> dict[str, float]:
    v7 = config.get("v7") if isinstance(config.get("v7"), dict) else {}
    raw = v7.get("component_observation_budget_fractions")
    expected = {"professional_maker", "crypto_informed_taker", "fast_structural"}
    if not isinstance(raw, dict) or set(raw) != expected:
        raise ValueError("component_observation_partition")
    total = _finite_nonnegative(config.get("starting_capital"), "starting_capital")
    return {
        component: total * _finite_nonnegative(raw[component], f"observation_fraction:{component}")
        for component in sorted(expected)
    }


def _child(
    config: dict[str, Any], *, view_id: str, scope_class: str,
    engine_id: str | None, component: str | None,
    execution_budget: float, observation_budget: float,
    canonical_replacement: str,
) -> dict[str, Any]:
    child = json.loads(json.dumps(config))
    child["starting_capital"] = execution_budget
    child["capital_scope"] = {
        "schema": "polymarket_v7_capital_scope_v3",
        "allocator_owner": ALLOCATOR_OWNER,
        "view_id": view_id,
        "scope_class": scope_class,
        "engine_id": engine_id,
        "component": component,
        "execution_budget": execution_budget,
        "observation_budget": observation_budget,
        "observation_budget_is_capital": False,
        "independent_capital_authority": False,
        "independent_risk_authority": False,
        "independent_oms_authority": False,
        "independent_inventory_authority": False,
        "independent_ledger_authority": False,
        "canonical_replacement": canonical_replacement,
        "double_counting_forbidden": True,
    }
    return child


def materialize(base_config: Path, output_dir: Path) -> dict[str, Any]:
    cfg = json.loads(base_config.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("base_config_not_object")
    budgets = allocate(cfg)
    observations = component_observation_budgets(cfg)
    for engine_id in ENGINES:
        atomic_json(output_dir / f"{engine_id.lower()}.json", _child(
            cfg, view_id=engine_id.lower(), scope_class="ENGINE_ENVELOPE",
            engine_id=engine_id, component=None,
            execution_budget=budgets[engine_id], observation_budget=0.0,
            canonical_replacement=f"{engine_id.lower()}.json",
        ))
    for view_id, engine_id in ENGINE_ADAPTERS.items():
        atomic_json(output_dir / f"{view_id}.json", _child(
            cfg, view_id=view_id, scope_class="TEMPORARY_ENGINE_ADAPTER",
            engine_id=engine_id, component=view_id,
            execution_budget=budgets[engine_id], observation_budget=0.0,
            canonical_replacement=f"{engine_id.lower()}.json",
        ))
    for view_id, component in COMPONENT_OBSERVERS.items():
        engine_id = (
            "BTC_SETTLEMENT_ENGINE" if component == "professional_maker"
            else "STRUCTURAL_ARB_ENGINE"
        )
        atomic_json(output_dir / f"{view_id}.json", _child(
            cfg, view_id=view_id, scope_class="COMPONENT_OBSERVATION",
            engine_id=engine_id, component=component,
            execution_budget=0.0, observation_budget=observations[component],
            canonical_replacement=f"{engine_id.lower()}.json",
        ))
    for view_id, component in RESEARCH_VIEWS.items():
        atomic_json(output_dir / f"{view_id}.json", _child(
            cfg, view_id=view_id, scope_class="RESEARCH_ZERO_AUTHORITY",
            engine_id=None, component=component,
            execution_budget=0.0, observation_budget=0.0,
            canonical_replacement="ZERO_AUTHORITY_RESEARCH_MANIFEST",
        ))
    manifest = {
        "schema": "polymarket_v7_capital_allocation_v3",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "automatic_transfer": False,
        "account_starting_capital": float(cfg["starting_capital"]),
        "capital_authority_owner": ALLOCATOR_OWNER,
        "capital_authority_owner_count": 1,
        "engine_budgets": {engine: budgets[engine] for engine in ENGINES},
        "engine_count": len(ENGINES),
        "engine_budget_sum": sum(budgets[engine] for engine in ENGINES),
        "reserve_budget": budgets["reserve"],
        "component_observation_budgets": observations,
        "component_observation_budgets_are_capital": False,
        "research_budgets": {},
        "research_has_capital": False,
        "allocated_plus_reserve": sum(budgets.values()),
        "double_counting_forbidden": True,
        "temporary_engine_adapters": ENGINE_ADAPTERS,
        "temporary_runtime_accounting_views": {
            "external": "BTC_SETTLEMENT_ENGINE",
            "hard_arb": "STRUCTURAL_ARB_ENGINE",
            "micro_maker": None,
            "fast_structural": None,
            "micro_taker": None,
            "graph_rv": None,
        },
        "temporary_adapter_deletion_gate": "DECLARATIVE_PROCESS_MANIFEST_CUTOVER_PROVEN",
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
