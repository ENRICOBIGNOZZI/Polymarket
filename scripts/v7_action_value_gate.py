#!/usr/bin/env python3
"""Fail-closed MAKE/TAKE/NOTHING action-value comparison contract.

This is research/control-plane logic only. It does not submit orders or grant
runtime authority. Candidate scores are comparable only when they are direct,
OOS-validated joint action values in the same monetary unit. Settlement edge
times an independently estimated fill rate is intentionally rejected.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_action_value_gate_v1"
JOINT = "JOINT_ACTION_VALUE"
USD = "USD"


class ActionValueError(ValueError):
    pass


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActionValueError(name)
    out = float(value)
    if not math.isfinite(out):
        raise ActionValueError(name)
    return out


def _candidate(value: dict[str, Any]) -> dict[str, Any]:
    action = str(value.get("action") or "").upper()
    if action not in {"MAKE", "TAKE"}:
        raise ActionValueError("action")
    semantics = value.get("score_semantics")
    units = value.get("units")
    score = _finite(value.get("conservative_action_value"), "conservative_action_value")
    uncertainty = _finite(value.get("uncertainty"), "uncertainty")
    if uncertainty < 0:
        raise ActionValueError("uncertainty")
    return {
        "action": action,
        "score_semantics": semantics,
        "units": units,
        "conservative_action_value": score,
        "uncertainty": uncertainty,
        "fill_conditioned": value.get("fill_conditioned") is True,
        "action_transport_validated": value.get("action_transport_validated") is True,
        "oos_validated": value.get("oos_validated") is True,
        "model_id": str(value.get("model_id") or ""),
    }


def compare(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    parsed = [_candidate(row) for row in candidates]
    by_action = {row["action"]: row for row in parsed}
    if len(by_action) != len(parsed):
        raise ActionValueError("duplicate_action")

    blockers: list[str] = []
    for row in parsed:
        action = row["action"]
        if row["score_semantics"] != JOINT:
            blockers.append(f"{action}:NOT_JOINT_ACTION_VALUE")
        if row["units"] != USD:
            blockers.append(f"{action}:UNITS_NOT_USD")
        if not row["fill_conditioned"]:
            blockers.append(f"{action}:NOT_FILL_CONDITIONED")
        if not row["action_transport_validated"]:
            blockers.append(f"{action}:ACTION_TRANSPORT_NOT_VALIDATED")
        if not row["oos_validated"]:
            blockers.append(f"{action}:OOS_NOT_VALIDATED")
        if not row["model_id"]:
            blockers.append(f"{action}:MODEL_ID_MISSING")

    if blockers:
        return {
            "schema": SCHEMA,
            "state": "NOT_COMPARABLE",
            "selected_action": None,
            "nothing_value": 0.0,
            "blockers": sorted(blockers),
            "paper_only": True,
            "execution_authority": False,
            "factorized_fill_times_edge_forbidden": True,
        }

    # NOTHING is an explicit zero incremental-value action. A new-risk action
    # must beat it on the conservative joint monetary score.
    ranked = sorted(parsed, key=lambda row: (row["conservative_action_value"], row["action"]), reverse=True)
    best = ranked[0] if ranked else None
    selected = best["action"] if best and best["conservative_action_value"] > 0.0 else "NOTHING"
    return {
        "schema": SCHEMA,
        "state": "COMPARABLE_RESEARCH_ONLY",
        "selected_action": selected,
        "nothing_value": 0.0,
        "candidates": parsed,
        "paper_only": True,
        "execution_authority": False,
        "automatic_promotion": False,
        "factorized_fill_times_edge_forbidden": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit("input must be a JSON array")
    result = compare(raw)
    if args.output.exists():
        raise SystemExit("refusing to overwrite action-value evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
                           encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
