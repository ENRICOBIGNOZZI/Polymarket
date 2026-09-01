#!/usr/bin/env python3
"""Fail-closed V7 worst-case scenario risk for binary-condition exposures.

The module is an offline calculator. It accepts signed net PnL in integer base
units for each YES/NO resolution, enumerates only feasible outcomes, and adds
explicit operational stresses. It neither reads accounts nor authorizes an
order; a malformed or incomplete risk book is rejected instead of assumed flat.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_scenario_risk_book_v1"
REPORT_SCHEMA = "polymarket_v7_scenario_risk_report_v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ACTIVE_STATES = {"RESTING", "DELAYED", "UNSETTLED", "SETTLEMENT_PENDING"}
STRESSES = ("delayed_settlement", "market_frozen", "oracle_outage", "external_feed_outage",
            "venue_cancel_only", "unwind_impossible", "reward_removal", "fee_increase", "network_partition")


class ScenarioRiskError(ValueError):
    pass


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise ScenarioRiskError(f"{field}:invalid")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioRiskError(f"{field}:invalid")
    return value


def validate_book(value: Any) -> dict[str, Any]:
    required = {"schema", "model_sha", "positions", "constraints", "stress_cost_base_units"}
    if not isinstance(value, dict) or set(value) != required:
        raise ScenarioRiskError("book:shape")
    if value["schema"] != SCHEMA or not SHA_RE.fullmatch(str(value["model_sha"])):
        raise ScenarioRiskError("book:identity")
    positions = value["positions"]
    if not isinstance(positions, list) or not positions:
        raise ScenarioRiskError("positions:empty")
    position_fields = {"position_id", "condition_id", "event_id", "category", "oracle_id", "state",
                       "yes_net_pnl_base_units", "no_net_pnl_base_units"}
    position_ids: set[str] = set()
    conditions: set[str] = set()
    for row in positions:
        if not isinstance(row, dict) or set(row) != position_fields:
            raise ScenarioRiskError("position:shape")
        position_id = _identifier(row["position_id"], "position_id")
        if position_id in position_ids:
            raise ScenarioRiskError("position:duplicate_id")
        position_ids.add(position_id)
        conditions.add(_identifier(row["condition_id"], "condition_id"))
        for field in ("event_id", "category", "oracle_id"):
            _identifier(row[field], field)
        if row["state"] not in ACTIVE_STATES:
            raise ScenarioRiskError("position:state")
        _integer(row["yes_net_pnl_base_units"], "yes_net_pnl_base_units")
        _integer(row["no_net_pnl_base_units"], "no_net_pnl_base_units")
    if len(conditions) > 16:
        raise ScenarioRiskError("conditions:too_many_for_exact_enumeration")
    constraints = value["constraints"]
    if not isinstance(constraints, dict) or set(constraints) != {"mutually_exclusive", "implications"}:
        raise ScenarioRiskError("constraints:shape")
    groups = constraints["mutually_exclusive"]
    if not isinstance(groups, list):
        raise ScenarioRiskError("constraints:mutually_exclusive")
    seen_groups: set[tuple[str, ...]] = set()
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            raise ScenarioRiskError("constraints:mutually_exclusive")
        normalized = tuple(sorted(_identifier(item, "constraint_condition") for item in group))
        if len(set(normalized)) != len(normalized) or any(item not in conditions for item in normalized) or normalized in seen_groups:
            raise ScenarioRiskError("constraints:mutually_exclusive")
        seen_groups.add(normalized)
    implications = constraints["implications"]
    if not isinstance(implications, list):
        raise ScenarioRiskError("constraints:implications")
    pairs: set[tuple[str, str]] = set()
    for rule in implications:
        if not isinstance(rule, dict) or set(rule) != {"if_true", "then_true"}:
            raise ScenarioRiskError("constraints:implication")
        pair = (_identifier(rule["if_true"], "constraint_condition"), _identifier(rule["then_true"], "constraint_condition"))
        if pair[0] == pair[1] or pair in pairs or any(item not in conditions for item in pair):
            raise ScenarioRiskError("constraints:implication")
        pairs.add(pair)
    stress = value["stress_cost_base_units"]
    if not isinstance(stress, dict) or set(stress) != set(STRESSES):
        raise ScenarioRiskError("stress:shape")
    for field in STRESSES:
        if _integer(stress[field], f"stress:{field}") < 0:
            raise ScenarioRiskError("stress:negative")
    return value


def _feasible(condition_ids: list[str], constraints: dict[str, Any]) -> list[dict[str, bool]]:
    groups = [set(group) for group in constraints["mutually_exclusive"]]
    implications = [(rule["if_true"], rule["then_true"]) for rule in constraints["implications"]]
    outcomes: list[dict[str, bool]] = []
    for values in itertools.product((False, True), repeat=len(condition_ids)):
        outcome = dict(zip(condition_ids, values))
        if any(sum(outcome[condition] for condition in group) > 1 for group in groups):
            continue
        if any(outcome[left] and not outcome[right] for left, right in implications):
            continue
        outcomes.append(outcome)
    if not outcomes:
        raise ScenarioRiskError("constraints:no_feasible_outcome")
    return outcomes


def assess(book: Any) -> dict[str, Any]:
    """Compute exact base-unit worst cases over resolutions and stress states."""
    book = validate_book(book)
    condition_ids = sorted({row["condition_id"] for row in book["positions"]})
    outcomes = _feasible(condition_ids, book["constraints"])
    stress_cases = [("BASE", 0), *[(name.upper(), book["stress_cost_base_units"][name]) for name in STRESSES]]
    combined = sum(book["stress_cost_base_units"].values())
    stress_cases.append(("COMBINED_OPERATIONAL_STRESS", combined))
    worst: dict[str, Any] | None = None
    event_worst: dict[str, int] = {}
    category_worst: dict[str, int] = {}
    oracle_worst: dict[str, int] = {}
    for outcome in outcomes:
        by_event: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_oracle: dict[str, int] = {}
        base = 0
        for row in book["positions"]:
            pnl = row["yes_net_pnl_base_units"] if outcome[row["condition_id"]] else row["no_net_pnl_base_units"]
            base += pnl
            by_event[row["event_id"]] = by_event.get(row["event_id"], 0) + pnl
            by_category[row["category"]] = by_category.get(row["category"], 0) + pnl
            by_oracle[row["oracle_id"]] = by_oracle.get(row["oracle_id"], 0) + pnl
        for name, cost in stress_cases:
            total = base - cost
            candidate = {"stress": name, "net_pnl_base_units": total,
                         "outcomes": {key: outcome[key] for key in condition_ids}}
            if worst is None or total < worst["net_pnl_base_units"]:
                worst = candidate
            for scope, rows in ((event_worst, by_event), (category_worst, by_category), (oracle_worst, by_oracle)):
                for key, pnl in rows.items():
                    stressed = pnl - cost
                    scope[key] = min(scope.get(key, stressed), stressed)
    assert worst is not None
    return {
        "schema": REPORT_SCHEMA, "model_sha": book["model_sha"], "state": "PAPER_SCENARIO_RISK_ONLY",
        "live_execution_authorized": False, "feasible_outcome_count": len(outcomes),
        "active_position_count": len(book["positions"]), "worst_case": worst,
        "worst_case_loss_base_units": max(0, -worst["net_pnl_base_units"]),
        "worst_case_by_event_base_units": dict(sorted(event_worst.items())),
        "worst_case_by_category_base_units": dict(sorted(category_worst.items())),
        "worst_case_by_oracle_base_units": dict(sorted(oracle_worst.items())),
        "stress_cost_base_units": book["stress_cost_base_units"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(assess(json.loads(args.book.read_text(encoding="utf-8"))), sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, ScenarioRiskError) as exc:
        print(f"v7_scenario_risk: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
