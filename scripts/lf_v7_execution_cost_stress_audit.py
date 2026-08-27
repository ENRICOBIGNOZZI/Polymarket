#!/usr/bin/env python3
"""Research-only audit of canonical V7 execution-evidence cost stress.

The canonical sidecar already tightens fill/PnL maturity.  This audit asks a
separate economic question: does its stress calculation consume the complete
explicit cost vector required by the current operator objective?

It never changes execution, sizing, risk, authority, or canonical refs.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any


DISJOINT_COST_COLUMNS = (
    "fee",
    "slippage_cost",
    "unwind_cost",
    "capital_cost",
    "latency_cost",
)
INCUMBENT_FIRST_COLUMNS = (
    "fee",
    "fees",
    "slippage_cost",
    "execution_cost",
    "cost",
)


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def incumbent_first_cost(row: dict[str, Any]) -> float:
    """Replicate the current first-populated-field semantics."""
    for column in INCUMBENT_FIRST_COLUMNS:
        if column not in row or row[column] in (None, ""):
            continue
        value = number(row[column], float("nan"))
        if value == value:  # NaN-safe, dependency-free finite-enough fixture.
            return max(0.0, value)
    return 0.0


def full_disjoint_cost(row: dict[str, Any]) -> float:
    return sum(max(0.0, number(row.get(column), 0.0)) for column in DISJOINT_COST_COLUMNS)


def stressed_net_pnl(baseline_net_pnl: float, baseline_cost: float, multiplier: float) -> float:
    """Stress a baseline-cost-net PnL by the incremental cost multiplier."""
    return baseline_net_pnl - max(0.0, multiplier - 1.0) * baseline_cost


def deterministic_counterexample() -> dict[str, Any]:
    # Each cost field below is deliberately disjoint.  net_pnl is assumed to be
    # after the 1x baseline costs, which is exactly how an additional cost
    # multiplier should be applied when net PnL is the starting statistic.
    row = {
        "net_pnl": 0.020,
        "fee": 0.006,
        "slippage_cost": 0.004,
        "unwind_cost": 0.006,
        "capital_cost": 0.004,
        "latency_cost": 0.004,
    }
    incumbent_cost = incumbent_first_cost(row)
    full_cost = full_disjoint_cost(row)
    result = {
        "row": row,
        "incumbent_recognized_baseline_cost": incumbent_cost,
        "full_disjoint_baseline_cost": full_cost,
        "recognized_fraction": incumbent_cost / full_cost if full_cost else None,
        "stress": {},
    }
    for multiplier in (1.0, 1.5, 2.0):
        incumbent = stressed_net_pnl(row["net_pnl"], incumbent_cost, multiplier)
        full = stressed_net_pnl(row["net_pnl"], full_cost, multiplier)
        result["stress"][str(multiplier)] = {
            "incumbent": incumbent,
            "full_vector": full,
            "difference": incumbent - full,
        }
    result["two_x_sign_disagreement"] = (
        result["stress"]["2.0"]["incumbent"] > 0.0
        and result["stress"]["2.0"]["full_vector"] <= 0.0
    )
    return result


def inspect_source(root: Path) -> dict[str, Any]:
    path = root / "scripts" / "v7_execution_evidence.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    explicit_cost_source = ""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "explicit_cost":
            explicit_cost_source = ast.get_source_segment(text, node) or ""
            break
    return {
        "path": str(path),
        "uses_first_number": "first_number" in explicit_cost_source,
        "mentions_fee": '"fee"' in explicit_cost_source,
        "mentions_slippage_cost": '"slippage_cost"' in explicit_cost_source,
        "mentions_unwind_cost": '"unwind_cost"' in explicit_cost_source,
        "mentions_capital_cost": '"capital_cost"' in explicit_cost_source,
        "mentions_latency_cost": '"latency_cost"' in explicit_cost_source,
        "function_source": explicit_cost_source,
    }


def inspect_test_registration(root: Path) -> dict[str, Any]:
    cmake = (root / "CMakeLists.txt").read_text(encoding="utf-8")
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    name = "test_v7_execution_evidence.py"
    return {
        "focused_test": f"tests/{name}",
        "registered_in_cmake": name in cmake,
        "called_explicitly_by_ci": name in ci,
    }


def build_report(root: Path) -> dict[str, Any]:
    source = inspect_source(root)
    registration = inspect_test_registration(root)
    counterexample = deterministic_counterexample()
    full_vector_missing = not all(
        source[key]
        for key in (
            "mentions_fee",
            "mentions_slippage_cost",
            "mentions_unwind_cost",
            "mentions_capital_cost",
            "mentions_latency_cost",
        )
    )
    defects = []
    if source["uses_first_number"] or full_vector_missing:
        defects.append("cost_stress_does_not_verify_complete_disjoint_cost_vector")
    if not registration["registered_in_cmake"] and not registration["called_explicitly_by_ci"]:
        defects.append("focused_execution_evidence_regression_not_in_main_ci")
    return {
        "schema": "lf_v7_execution_cost_stress_audit_v1",
        "research_only": True,
        "source": source,
        "test_registration": registration,
        "counterexample": counterexample,
        "defects": defects,
        "material": bool(defects) and counterexample["two_x_sign_disagreement"],
        "decision": "MORE_EVIDENCE_REQUIRED",
        "required_successor": [
            "define disjoint authoritative fee/slippage/unwind/capital/latency cost provenance or one verified non-overlapping total-cost field",
            "reconstruct 1x realized PnL from audited cashflows/costs instead of inferring economics from whichever generic cost field appears first",
            "stress the same frozen realized observations at 1x, 1.5x and 2x without reselecting trades",
            "fail closed when the cost decomposition is missing, ambiguous, overlapping or unverifiable",
            "keep the focused execution-evidence regression in the required exact-head CI suite",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args.root.resolve())
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 1 if not report["material"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
