#!/usr/bin/env python3
"""Research-only multiplicity audit for V7 External Intelligence.

The external worker evaluates a pre-specified family of source/feature/horizon
candidates. A raw bootstrap p-value from the best candidate is not, by itself,
family-level evidence. This audit keeps the production/external bridge fail
closed and reports ordinary BH plus dependence-robust BY diagnostics.

It never materializes q_external, changes operator authority, or submits orders.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Sequence


def _finite_probability(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid p-value: {value!r}") from exc
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"p-value outside [0,1]: {value!r}")
    return parsed


def harmonic_number(count: int) -> float:
    if count <= 0:
        raise ValueError("candidate family must be non-empty")
    return sum(1.0 / index for index in range(1, count + 1))


def step_up_rejections(pvalues: Sequence[float], q: float, *, dependence_robust: bool) -> list[bool]:
    if not 0.0 < q < 1.0:
        raise ValueError("q must lie strictly between 0 and 1")
    parsed = [_finite_probability(value) for value in pvalues]
    if not parsed:
        raise ValueError("candidate family must be non-empty")
    count = len(parsed)
    divisor = harmonic_number(count) if dependence_robust else 1.0
    ordered = sorted(enumerate(parsed), key=lambda item: (item[1], item[0]))
    largest_rank = 0
    cutoff = -1.0
    for rank, (_, pvalue) in enumerate(ordered, start=1):
        threshold = rank * q / (count * divisor)
        if pvalue <= threshold:
            largest_rank = rank
            cutoff = pvalue
    if largest_rank == 0:
        return [False] * count
    return [pvalue <= cutoff for pvalue in parsed]


def audit_report(report: dict[str, Any], q: float = 0.10) -> dict[str, Any]:
    backtest = report.get("backtest")
    if not isinstance(backtest, dict):
        raise ValueError("missing backtest object")
    candidates = backtest.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("missing candidate family")
    declared_count = int(backtest.get("candidate_count", len(candidates)))
    if declared_count != len(candidates):
        raise ValueError(
            f"partial candidate family: declared={declared_count} loaded={len(candidates)}"
        )

    ids: list[str] = []
    pvalues: list[float] = []
    raw_gate: list[bool] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("candidate row is not an object")
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id:
            raise ValueError("candidate row missing candidate_id")
        if candidate_id in ids:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        ids.append(candidate_id)
        pvalues.append(_finite_probability(candidate.get("raw_pvalue")))
        raw_gate.append(bool(candidate.get("gate_pass")))

    bh = step_up_rejections(pvalues, q, dependence_robust=False)
    by = step_up_rejections(pvalues, q, dependence_robust=True)
    count = len(candidates)
    harmonic = harmonic_number(count)

    alpha = report.get("alpha_factory_evidence")
    if not isinstance(alpha, dict):
        raise ValueError("missing alpha_factory_evidence")
    selected_id = str(alpha.get("candidate_id") or "")
    if selected_id not in ids:
        raise ValueError("selected candidate is not in the audited family")
    selected_index = ids.index(selected_id)
    selected_p = pvalues[selected_index]

    rows: list[dict[str, Any]] = []
    for index, candidate_id in enumerate(ids):
        rows.append({
            "candidate_id": candidate_id,
            "raw_pvalue": pvalues[index],
            "raw_gate_pass": raw_gate[index],
            "bh_q_rejected": bh[index],
            "by_q_rejected": by[index],
            "bonferroni_adjusted_pvalue": min(1.0, pvalues[index] * count),
        })

    selected = rows[selected_index]
    raw_passes = [row for row in rows if row["raw_gate_pass"]]
    if selected["raw_gate_pass"] and not selected["bh_q_rejected"]:
        state = "STRONG_RAW_LEAD_MULTIPLICITY_BLOCKED_MORE_EVIDENCE_REQUIRED"
    elif selected["raw_gate_pass"] and selected["bh_q_rejected"] and not selected["by_q_rejected"]:
        state = "BH_ONLY_LEAD_DEPENDENCE_ROBUST_MULTIPLICITY_BLOCKED_MORE_EVIDENCE_REQUIRED"
    elif selected["raw_gate_pass"] and selected["by_q_rejected"]:
        state = "MULTIPLICITY_SURVIVOR_EXACT_EXECUTABLE_REPLAY_STILL_REQUIRED"
    else:
        state = "NO_RAW_EXTERNAL_GATE_PASS"

    return {
        "schema": "v7_external_multiplicity_audit_v1",
        "generated_ts": report.get("generated_ts"),
        "source_report_schema": report.get("schema"),
        "paper_only": True,
        "authenticated_execution": False,
        "q_external_materialized": False,
        "family_size": count,
        "q": q,
        "harmonic_number": harmonic,
        "bh_rank1_threshold": q / count,
        "by_rank1_threshold": q / (count * harmonic),
        "raw_gate_pass_count": len(raw_passes),
        "bh_rejection_count": sum(bh),
        "by_rejection_count": sum(by),
        "selected_candidate": selected,
        "selected_raw_pvalue": selected_p,
        "state": state,
        "promotion_allowed": False,
        "required_next_evidence": [
            "family-level multiplicity must remain pre-specified and point-in-time",
            "freeze the 1x chronological trade/side set before 1.5x/2x cost repricing",
            "exact point-in-time executable CLOB entry/exit replay with depth, fees, slippage and latency",
            "incumbent/no-external ablation on the same chronological sample",
            "materialize q_external only from an approved direct probability source",
        ],
        "candidates": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--q", type=float, default=0.10)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    result = audit_report(report, args.q)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "external_multiplicity_audit"
        f" family={result['family_size']} raw_pass={result['raw_gate_pass_count']}"
        f" bh={result['bh_rejection_count']} by={result['by_rejection_count']}"
        f" state={result['state']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
