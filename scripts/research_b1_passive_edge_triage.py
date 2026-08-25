#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _float(row: dict[str, Any], key: str) -> float:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid or missing {key}") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"non-finite {key}")
    return value


def candidate_key(row: dict[str, Any]) -> str:
    required = ("y_market", "y_side", "x_market", "x_side")
    values: list[str] = []
    for key in required:
        value = str(row.get(key, "")).strip()
        if not value:
            raise ValueError(f"invalid or missing {key}")
        values.append(value)
    return f"{values[0]}:{values[1]}|{values[2]}:{values[3]}"


def counterfactual_break_even_completion(maker_edge: float, taker_edge: float) -> float | None:
    """Binary maker-vs-taker fallback hurdle used for research triage only.

    This is deliberately not the live broker model. The live multi-leg broker can
    partially fill, cancel, and unwind inventory. The hurdle only answers whether
    a maker-dependent edge is large enough to justify candidate-specific replay.
    """
    if maker_edge <= 0.0:
        return None
    if taker_edge >= 0.0:
        return 0.0
    denom = maker_edge - taker_edge
    if denom <= 0.0:
        return None
    return max(0.0, min(1.0, -taker_edge / denom))


def b1_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = payload.get("candidates", {})
    rows = candidates.get("b1", []) if isinstance(candidates, dict) else []
    if not isinstance(rows, list):
        raise ValueError("candidates.b1 must be a list")
    return [row for row in rows if isinstance(row, dict)]


def analyze_snapshots(
    payloads: list[dict[str, Any]],
    *,
    max_break_even_completion: float = 0.25,
    min_maker_edge: float = 0.00025,
    stability_warning: float = 0.55,
) -> dict[str, Any]:
    if not payloads:
        raise ValueError("at least one snapshot is required")
    if not 0.0 < max_break_even_completion <= 1.0:
        raise ValueError("max_break_even_completion must be in (0,1]")

    recurrence: dict[str, int] = {}
    latest_rows: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        seen_this_snapshot: set[str] = set()
        for row in b1_rows(payload):
            key = candidate_key(row)
            maker = _float(row, "maker_entry_net_edge")
            if maker > 0.0:
                seen_this_snapshot.add(key)
            latest_rows[key] = row
        for key in seen_this_snapshot:
            recurrence[key] = recurrence.get(key, 0) + 1

    evaluated: list[dict[str, Any]] = []
    for key, row in latest_rows.items():
        maker = _float(row, "maker_entry_net_edge")
        taker = _float(row, "taker_net_edge")
        raw = _float(row, "raw_expected_edge")
        stability = _float(row, "stability")
        hurdle = counterfactual_break_even_completion(maker, taker)
        repeated = recurrence.get(key, 0)

        flags: list[str] = []
        if stability < stability_warning:
            flags.append("marginal_parameter_stability")
        if repeated < 2:
            flags.append("single_snapshot_only")
        if taker < 0.0:
            flags.append("maker_dependent_edge")

        if maker <= 0.0:
            action = "REJECT_NONPOSITIVE_MAKER_EDGE"
        elif maker < min_maker_edge:
            action = "DEPRIORITIZE_SMALL_MAKER_EDGE"
        elif taker < 0.0 and hurdle is not None and hurdle <= max_break_even_completion:
            action = "PRIORITIZE_CANDIDATE_SPECIFIC_REPLAY"
        elif taker >= 0.0:
            action = "PRIORITIZE_EXECUTABLE_REPLAY"
        else:
            action = "MORE_EVIDENCE_REQUIRED"

        evaluated.append(
            {
                "candidate_key": key,
                "relation": str(row.get("relation", "")),
                "maker_entry_net_edge": maker,
                "taker_net_edge": taker,
                "raw_expected_edge": raw,
                "maker_taker_wedge": maker - taker,
                "counterfactual_break_even_completion": hurdle,
                "recurring_maker_positive_snapshots": repeated,
                "stability": stability,
                "flags": flags,
                "action": action,
                "evidence_state": "MORE_EVIDENCE_REQUIRED",
            }
        )

    evaluated.sort(
        key=lambda item: (
            item["action"] not in {"PRIORITIZE_CANDIDATE_SPECIFIC_REPLAY", "PRIORITIZE_EXECUTABLE_REPLAY"},
            -(item["maker_entry_net_edge"]),
        )
    )
    priority = [item for item in evaluated if item["action"].startswith("PRIORITIZE_")]
    return {
        "schema": "polymarket_b1_passive_edge_triage_v1",
        "snapshot_count": len(payloads),
        "candidate_count": len(evaluated),
        "priority_replay_count": len(priority),
        "decision": "MORE_EVIDENCE_REQUIRED",
        "live_broker_model_note": (
            "counterfactual_break_even_completion is triage only; promotion requires candidate-specific "
            "event-time evidence under the live partial-fill/cancel/unwind contract"
        ),
        "candidates": evaluated,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Research-only triage for maker-dependent B1 edges")
    parser.add_argument("--snapshot", action="append", required=True, help="Chronological live-smoke JSON; repeat for multiple snapshots")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-break-even-completion", type=float, default=0.25)
    parser.add_argument("--min-maker-edge", type=float, default=0.00025)
    parser.add_argument("--stability-warning", type=float, default=0.55)
    args = parser.parse_args()

    payloads = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.snapshot]
    report = analyze_snapshots(
        payloads,
        max_break_even_completion=args.max_break_even_completion,
        min_maker_edge=args.min_maker_edge,
        stability_warning=args.stability_warning,
    )
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
