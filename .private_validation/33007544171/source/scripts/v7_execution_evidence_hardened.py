#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

import v7_execution_evidence as base


AGGREGATE_COST_FIELDS = (
    "total_execution_cost",
    "total_cost",
    "execution_cost",
    "cost",
)
TOTAL_FEE_FIELDS = ("total_fees", "fees")
ROW_FEE_FIELDS = ("fee", "fee_paid")
ENTRY_EXIT_FEE_FIELDS = ("entry_fee", "exit_fee", "maker_fee", "taker_fee")
ADDITIVE_COST_FIELDS = (
    "slippage_cost",
    "unwind_cost",
    "partial_unwind_loss",
    "capital_time_cost",
    "capital_latency_cost",
    "cancel_latency_cost",
)


def _present_number(row: dict[str, str], field: str) -> tuple[bool, float]:
    if field not in row or str(row.get(field) or "").strip() == "":
        return False, math.nan
    value = base.number(row.get(field), math.nan)
    return math.isfinite(value), value


def audited_cost(row: dict[str, str]) -> float:
    """Monetary baseline cost for one ledger row without double counting.

    Aggregate cost fields dominate component fields.  Otherwise fees are selected
    from one mutually exclusive fee representation and additive monetary costs are
    summed.  Percentage/bps fields are deliberately ignored because their dollar
    impact cannot be audited without the exact notional they were applied to.
    """
    for field in AGGREGATE_COST_FIELDS:
        present, value = _present_number(row, field)
        if present:
            return max(0.0, value)

    total = 0.0
    observed = False
    for field in TOTAL_FEE_FIELDS:
        present, value = _present_number(row, field)
        if present:
            total += max(0.0, value)
            observed = True
            break
    else:
        for field in ROW_FEE_FIELDS:
            present, value = _present_number(row, field)
            if present:
                total += max(0.0, value)
                observed = True
                break
        if not observed:
            for field in ENTRY_EXIT_FEE_FIELDS:
                present, value = _present_number(row, field)
                if present:
                    total += max(0.0, value)
                    observed = True

    for field in ADDITIVE_COST_FIELDS:
        present, value = _present_number(row, field)
        if present:
            total += max(0.0, value)
            observed = True
    return total if observed else math.nan


def has_auditable_cost_field(row: dict[str, str]) -> bool:
    return math.isfinite(audited_cost(row))


def cost_audit(run_root: Path, model: str) -> dict[str, Any]:
    execution_paths, _ = base.strategy_paths(run_root, model)
    rows = [row for path in execution_paths for row in base.read_rows(path)]
    pnl_rows = [row for row in rows if base.row_has_realized_pnl(row) and math.isfinite(base.realized_pnl(row))]
    cost_rows = [row for row in rows if has_auditable_cost_field(row)]

    cost_keys = {
        base.event_key(row, f"row:{index}")
        for index, row in enumerate(rows)
        if has_auditable_cost_field(row)
    }
    pnl_keys = [base.event_key(row, f"pnl:{index}") for index, row in enumerate(pnl_rows)]
    covered = sum(key in cost_keys for key in pnl_keys)
    coverage = covered / len(pnl_keys) if pnl_keys else None
    total = sum(audited_cost(row) for row in cost_rows)
    return {
        "pnl_observations": len(pnl_rows),
        "cost_rows": len(cost_rows),
        "covered_pnl_observations": covered,
        "cost_observation_coverage": coverage,
        "audited_baseline_cost": total,
        "stress_basis": "aggregate_cost_or_nonoverlapping_monetary_components",
        "bps_only_cost_fields_ignored": True,
    }


def build_report(run_root: Path, policy: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
    original = base.explicit_cost
    base.explicit_cost = audited_cost
    try:
        report = base.build_report(run_root, policy, now=now)
    finally:
        base.explicit_cost = original

    for model, row in report.get("models", {}).items():
        audit = cost_audit(run_root, model)
        row["cost_audit"] = audit
        reasons = set(str(reason) for reason in row.get("reason_codes", []))
        pnl_observations = int(audit["pnl_observations"])
        coverage = audit["cost_observation_coverage"]
        if pnl_observations > 0 and audit["cost_rows"] <= 0:
            reasons.add("cost_stress_unverifiable")
        if pnl_observations > 0 and (coverage is None or float(coverage) < 1.0 - 1e-12):
            reasons.add("cost_observation_coverage_gate")
        row["reason_codes"] = sorted(reasons)
        row["state"] = "PAPER_ELIGIBLE" if not reasons else "INSUFFICIENT_EVIDENCE"
        row["paper_eligible"] = row["state"] == "PAPER_ELIGIBLE"
        row["allocation_mutated"] = False

    models = report.get("models", {})
    report["summary"] = {
        "models": len(models),
        "paper_eligible_models": sum(bool(row.get("paper_eligible")) for row in models.values()),
        "insufficient_evidence_models": sum(not bool(row.get("paper_eligible")) for row in models.values()),
        "capital_allocation_mutated": False,
        "cost_accounting": "audited",
    }
    report["cost_accounting_contract"] = (
        "realized_net_pnl_plus_explicit_nonoverlapping_monetary_cost_stress_with_full_pnl_key_coverage"
    )
    report.pop("evidence_id", None)
    report["evidence_id"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def render_markdown(report: dict[str, Any]) -> str:
    text = base.render_markdown(report).rstrip() + "\n\n## Cost audit\n\n"
    lines = [
        "| Model | Cost rows | PnL coverage | Audited baseline cost |",
        "|---|---:|---:|---:|",
    ]
    for model, row in report.get("models", {}).items():
        audit = row.get("cost_audit", {})
        coverage = audit.get("cost_observation_coverage")
        coverage_text = "n/a" if coverage is None else f"{100.0 * float(coverage):.1f}%"
        lines.append(
            f"| {model} | {int(audit.get('cost_rows', 0))} | {coverage_text} | "
            f"{float(audit.get('audited_baseline_cost', 0.0)):.6f} |"
        )
    lines.extend([
        "",
        "Eligibility fails closed when a realized-PnL observation cannot be linked to an explicit monetary cost record. Bps-only fields are not converted to dollars implicitly.",
    ])
    return text + "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = base.parse_args(argv)
    report = build_report(args.run_root, base.read_json(args.policy), now=args.now)
    output = args.output or args.run_root / "v7_execution_evidence.json"
    markdown = args.markdown or args.run_root / "v7_execution_evidence.md"
    base.atomic_json(output, report)
    base.atomic_text(markdown, render_markdown(report))
    print(json.dumps(report["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
