#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

HARD_KINDS = {
    "BINARY_COMPLETE_SET",
    "NEGRISK_COMPLETE_SET",
    "NEGRISK_NO_CONVERSION",
    "LOGICAL_IMPLICATION",
    "LOGICAL_MUTUAL_EXCLUSION",
    "LOGICAL_EXHAUSTIVE_PAIR",
}


def as_int(value: object, default: int = -1) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Require per-leg book freshness before hard-arbitrage evidence can promote"
    )
    parser.add_argument("--opportunities", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown", required=True)
    parser.add_argument("--max-leg-age-ms", type=int, default=2000)
    parser.add_argument("--max-leg-skew-ms", type=int, default=1000)
    args = parser.parse_args()

    opportunities = Path(args.opportunities)
    candidate_path = Path(args.candidate)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))

    raw_hard = 0
    qualified_hard = 0
    unverified_hard = 0
    stale_hard = 0
    has_freshness_columns = False

    with opportunities.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        has_freshness_columns = {
            "max_leg_book_age_ms",
            "leg_book_skew_ms",
        }.issubset(fields)
        for row in reader:
            kind = (row.get("kind") or "").strip()
            hard = as_int(row.get("hard_arbitrage"), 0) == 1 or kind in HARD_KINDS
            executable = as_int(row.get("executable"), 0) == 1
            if not (hard and executable):
                continue
            raw_hard += 1
            if not has_freshness_columns:
                unverified_hard += 1
                continue
            max_age = as_int(row.get("max_leg_book_age_ms"), -1)
            skew = as_int(row.get("leg_book_skew_ms"), -1)
            if max_age < 0 or skew < 0:
                unverified_hard += 1
            elif max_age <= args.max_leg_age_ms and skew <= args.max_leg_skew_ms:
                qualified_hard += 1
            else:
                stale_hard += 1

    candidate["hard_executable_observations_raw"] = raw_hard
    candidate["hard_executable_observations_freshness_qualified"] = qualified_hard
    candidate["hard_executable_observations_unverified_freshness"] = unverified_hard
    candidate["hard_executable_observations_stale_or_skewed"] = stale_hard
    candidate["hard_cross_leg_freshness_columns_present"] = has_freshness_columns
    candidate["hard_cross_leg_freshness_max_age_ms"] = args.max_leg_age_ms
    candidate["hard_cross_leg_freshness_max_skew_ms"] = args.max_leg_skew_ms

    gate_reasons = candidate.setdefault("gate_reasons", {}).setdefault("promotion", {})
    gate_reasons["hard_freshness_qualified_at_least_50"] = qualified_hard >= 50
    gate_reasons["hard_freshness_no_unverified_or_stale"] = (
        unverified_hard == 0 and stale_hard == 0
    )
    if raw_hard != qualified_hard:
        candidate["promotion_ready"] = False
        if isinstance(candidate.get("candidate_policy"), dict):
            candidate["candidate_policy"]["promotion_ready"] = False

    candidate_path.write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    guard = {
        "schema_version": 1,
        "mode": "research_only",
        "real_order_submission": False,
        "raw_hard_executable_observations": raw_hard,
        "freshness_qualified_hard_executable_observations": qualified_hard,
        "unverified_freshness_hard_executable_observations": unverified_hard,
        "stale_or_skewed_hard_executable_observations": stale_hard,
        "freshness_columns_present": has_freshness_columns,
        "max_leg_age_ms": args.max_leg_age_ms,
        "max_leg_skew_ms": args.max_leg_skew_ms,
        "promotion_hard_count_eligible": qualified_hard >= 50,
        "all_counted_hard_evidence_fresh": raw_hard == qualified_hard,
    }
    Path(args.output_json).write_text(
        json.dumps(guard, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    lines = [
        "# Hard-arbitrage cross-leg freshness guard",
        "",
        f"- raw hard executable observations: {raw_hard}",
        f"- freshness-qualified hard observations: {qualified_hard}",
        f"- unverified freshness: {unverified_hard}",
        f"- stale/skewed: {stale_hard}",
        f"- per-leg freshness columns present: **{str(has_freshness_columns).lower()}**",
        f"- max allowed leg age: {args.max_leg_age_ms} ms",
        f"- max allowed cross-leg skew: {args.max_leg_skew_ms} ms",
        "",
        "Hard-arbitrage evidence is promotion-ineligible unless every counted hard observation has candidate-level per-leg book freshness within these bounds.",
    ]
    Path(args.output_markdown).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
