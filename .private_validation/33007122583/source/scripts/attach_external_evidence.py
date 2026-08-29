#!/usr/bin/env python3
"""Attach external-intelligence telemetry to an Alpha Factory report fail-closed.

The adapter may append a shadow research candidate and a next experiment. It can
never recommend/promote that candidate, mutate the champion, deploy, or execute.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def finite(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def validate_external(report: dict[str, Any]) -> None:
    if not report:
        return
    errors: list[str] = []
    if report.get("schema") != "polymarket_external_intelligence_report_v1":
        errors.append("unexpected external report schema")
    if report.get("paper_only") is not True:
        errors.append("paper_only must be true")
    if report.get("submitted_orders") != 0:
        errors.append("submitted_orders must be zero")
    for key in ("authenticated_execution", "direct_champion_mutation", "production_signal_write"):
        if report.get(key) is not False:
            errors.append(f"{key} must be false")
    if errors:
        raise ValueError("; ".join(errors))


def attach(alpha_report: dict[str, Any], alpha_state: dict[str, Any], external_report: dict[str, Any],
           *, now: int, max_age_seconds: int) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_external(external_report)
    report = dict(alpha_report)
    state = dict(alpha_state)
    generated = integer(external_report.get("generated_ts")) if external_report else 0
    age = max(0, now - generated) if generated else None
    fresh = bool(generated and age is not None and age <= max_age_seconds)
    collection = external_report.get("collection") or {}
    backtest = external_report.get("backtest") or {}

    diagnostics = dict(report.get("diagnostics") or {})
    diagnostics["external_intelligence"] = {
        "present": bool(external_report),
        "generated_ts": generated,
        "age_seconds": age,
        "fresh": fresh,
        "status": external_report.get("status") if external_report else "missing",
        "new_observations": integer(collection.get("new_observations")),
        "accepted_signal_rows": integer(collection.get("accepted_signal_rows")),
        "candidate_count": integer(backtest.get("candidate_count")),
        "passing_candidates": integer(backtest.get("passing_candidates")),
    }
    report["diagnostics"] = diagnostics
    report["external_intelligence"] = {
        "fresh": fresh,
        "status": external_report.get("status") if external_report else "missing",
        "source_reliability": external_report.get("source_reliability") or {},
        "source_health": external_report.get("source_health") or {},
    }

    evidence = external_report.get("alpha_factory_evidence") if fresh else None
    if isinstance(evidence, dict) and evidence.get("candidate_id"):
        candidate = dict(evidence)
        identifier = str(candidate["candidate_id"])
        candidate.update({
            "decision": "continue_shadow",
            "consecutive_passes": 0,
            "first_seen_ts": now,
            "last_seen_ts": now,
            "integration_evidence_pass": False,
            "fdr": {
                "raw_pvalue": finite(candidate.get("raw_pvalue"), 1.0),
                "adjusted_pvalue": 1.0,
                "rejected": False,
                "rank": None,
                "tests": 0,
                "note": "external telemetry cannot self-authorize FDR or promotion",
            },
        })
        reasons = list(candidate.get("reasons") or [])
        reasons.extend(candidate.get("integration_reasons") or [])
        reasons.append("external_evidence_requires_alpha_factory_replay")
        candidate["reasons"] = sorted(set(str(reason) for reason in reasons))
        report["candidates"] = [
            item for item in (report.get("candidates") or [])
            if isinstance(item, dict) and str(item.get("candidate_id")) != identifier
        ] + [candidate]

        candidates = dict(state.get("candidates") or {})
        candidates[identifier] = candidate
        state["candidates"] = candidates
        state["external_evidence_candidate"] = identifier

        experiment_id = (
            "external_exact_clob_replay_and_incumbent_ablation"
            if candidate.get("gate_pass_before_fdr")
            else "accumulate_external_point_in_time_history"
        )
        experiments = [item for item in (report.get("next_experiments") or []) if isinstance(item, dict)]
        if not any(str(item.get("experiment_id")) == experiment_id for item in experiments):
            experiments.append({
                "experiment_id": experiment_id,
                "priority": 5 if candidate.get("gate_pass_before_fdr") else 7,
                "hypothesis": (
                    "The best external feature adds executable utility beyond the incumbent."
                    if candidate.get("gate_pass_before_fdr")
                    else "More point-in-time external observations are needed before inference."
                ),
                "triggering_evidence": identifier,
                "success_metric": (
                    "purged incumbent ablation with exact historical executable quotes and 1.5x/2x cost stress"
                    if candidate.get("gate_pass_before_fdr")
                    else "enough chronologically labeled observations and trades to satisfy external gates"
                ),
                "owner_workflow": "external-intelligence.yml",
            })
        report["next_experiments"] = sorted(
            experiments, key=lambda item: (integer(item.get("priority"), 999), str(item.get("experiment_id")))
        )[:10]

    # Privileged decisions are preserved exactly; the adapter only adds evidence.
    state["external_evidence_updated_ts"] = generated
    invariants = dict(state.get("invariants") or {})
    invariants["external_direct_champion_mutation"] = False
    invariants["external_authenticated_execution"] = False
    state["invariants"] = invariants
    report["direct_champion_mutation"] = False
    report["authenticated_execution"] = False
    report["submitted_orders"] = 0
    return report, state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alpha-report", type=Path, required=True)
    parser.add_argument("--alpha-state", type=Path, required=True)
    parser.add_argument("--external-report", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    parser.add_argument("--output-state", type=Path, required=True)
    parser.add_argument("--alpha-markdown", type=Path)
    parser.add_argument("--max-age-seconds", type=int, default=10800)
    parser.add_argument("--now", type=int)
    args = parser.parse_args()
    now = args.now if args.now is not None else int(time.time())
    report, state = attach(
        read_json(args.alpha_report, {}), read_json(args.alpha_state, {}),
        read_json(args.external_report, {}), now=now, max_age_seconds=max(1, args.max_age_seconds),
    )
    atomic_json(args.output_report, report)
    atomic_json(args.output_state, state)
    if args.alpha_markdown:
        try:
            existing = args.alpha_markdown.read_text(encoding="utf-8").rstrip()
        except OSError:
            existing = "# Polymarket Alpha Factory"
        ext = (report.get("diagnostics") or {}).get("external_intelligence") or {}
        candidate = state.get("external_evidence_candidate") or "none"
        section = (
            "\n\n## External Intelligence\n\n"
            f"- fresh: `{str(bool(ext.get('fresh'))).lower()}`\n"
            f"- status: `{ext.get('status') or 'missing'}`\n"
            f"- shadow candidate: `{candidate}`\n"
            "- self-promotion: `disabled`\n"
        )
        args.alpha_markdown.write_text(existing + section, encoding="utf-8")
    print(
        "alpha_external_adapter"
        f" fresh={int(bool((report.get('diagnostics') or {}).get('external_intelligence', {}).get('fresh')))}"
        f" candidate={state.get('external_evidence_candidate') or 'none'}"
        f" recommended_canary={report.get('recommended_canary') or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
