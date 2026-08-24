#!/usr/bin/env python3
"""Attach external-intelligence evidence to Alpha Factory reports fail-closed.

This adapter makes the external worker visible to the existing Alpha Factory without
letting a telemetry producer promote itself. It may append a research candidate and
next experiment, but it never changes the champion, active/recommended canary, live
manifest, risk gates, deployment state or execution permissions.
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
        output = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return output


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    atomic_text(path, payload)


def validate_external(report: dict[str, Any]) -> None:
    if not report:
        return
    errors: list[str] = []
    if report.get("schema") != "polymarket_external_intelligence_report_v1":
        errors.append("unexpected external report schema")
    if report.get("paper_only") is not True:
        errors.append("external report must be paper_only")
    if report.get("submitted_orders") != 0:
        errors.append("external report must have submitted_orders=0")
    for key in ("authenticated_execution", "direct_champion_mutation", "production_signal_write"):
        if report.get(key) is not False:
            errors.append(f"external report must set {key}=false")
    if errors:
        raise ValueError("; ".join(errors))


def attach(
    alpha_report: dict[str, Any],
    alpha_state: dict[str, Any],
    external_report: dict[str, Any],
    *,
    now: int,
    max_age_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_external(external_report)
    report = dict(alpha_report)
    state = dict(alpha_state)
    generated = integer(external_report.get("generated_ts")) if external_report else 0
    age = max(0, now - generated) if generated else None
    fresh = bool(generated and age is not None and age <= max_age_seconds)
    collection = external_report.get("collection") or {}
    backtest = external_report.get("backtest") or {}
    diagnostics = dict(report.get("diagnostics") or {})
    diagnostics["external"] = {
        "present": bool(external_report),
        "generated_ts": generated,
        "age_seconds": age,
        "fresh": fresh,
        "status": external_report.get("status") if external_report else "missing",
        "new_observations": integer(collection.get("new_observations")),
        "fresh_rows": integer(collection.get("accepted_signal_rows")),
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
        candidate["decision"] = "continue_shadow"
        candidate["consecutive_passes"] = 0
        candidate["first_seen_ts"] = now
        candidate["last_seen_ts"] = now
        candidate["fdr"] = {
            "raw_pvalue": finite(candidate.get("raw_pvalue"), 1.0),
            "adjusted_pvalue": 1.0,
            "rejected": False,
            "rank": None,
            "tests": 0,
            "note": "external adapter cannot self-authorize FDR/promotion",
        }
        reasons = list(candidate.get("reasons") or [])
        reasons.extend(candidate.get("integration_reasons") or [])
        reasons.append("external_evidence_requires_alpha_factory_replay")
        candidate["reasons"] = sorted(set(str(reason) for reason in reasons))
        candidate["integration_evidence_pass"] = False

        candidates = [
            item
            for item in (report.get("candidates") or [])
            if isinstance(item, dict) and str(item.get("candidate_id")) != identifier
        ]
        candidates.append(candidate)
        report["candidates"] = candidates

        state_candidates = dict(state.get("candidates") or {})
        state_candidates[identifier] = candidate
        state["candidates"] = state_candidates
        state["external_evidence_candidate"] = identifier

        experiments = [item for item in (report.get("next_experiments") or []) if isinstance(item, dict)]
        experiment_id = (
            "external_exact_clob_replay_and_incumbent_ablation"
            if candidate.get("gate_pass_before_fdr")
            else "accumulate_external_point_in_time_history"
        )
        if not any(str(item.get("experiment_id")) == experiment_id for item in experiments):
            experiments.append(
                {
                    "experiment_id": experiment_id,
                    "priority": 5 if candidate.get("gate_pass_before_fdr") else 7,
                    "hypothesis": (
                        "The best external feature adds executable incremental utility beyond the incumbent."
                        if candidate.get("gate_pass_before_fdr")
                        else "More point-in-time observations are required before external information can be evaluated reliably."
                    ),
                    "triggering_evidence": identifier,
                    "success_metric": (
                        "purged incumbent ablation with exact historical executable quotes, 1.5x/2x cost stress and stable folds"
                        if candidate.get("gate_pass_before_fdr")
                        else "enough chronologically labeled observations and trades to satisfy the external backtest gates"
                    ),
                    "owner_workflow": "live-smoke.yml",
                }
            )
        report["next_experiments"] = sorted(
            experiments,
            key=lambda item: (integer(item.get("priority"), 999), str(item.get("experiment_id"))),
        )[:10]

    # Preserve privileged Alpha Factory decisions exactly.
    state["external_evidence_updated_ts"] = generated
    state.setdefault("invariants", {})
    state["invariants"]["external_direct_champion_mutation"] = False
    state["invariants"]["external_authenticated_execution"] = False
    report["direct_champion_mutation"] = False
    report["authenticated_execution"] = False
    report["submitted_orders"] = 0
    return report, state


def render_appendix(report: dict[str, Any]) -> str:
    external = report.get("external_intelligence") or {}
    diagnostic = (report.get("diagnostics") or {}).get("external") or {}
    candidates = [
        item for item in (report.get("candidates") or [])
        if isinstance(item, dict) and item.get("family") == "external_information"
    ]
    lines = [
        "",
        "## External intelligence handoff",
        "",
        f"- fresh: `{str(bool(external.get('fresh'))).lower()}`",
        f"- status: `{external.get('status') or 'missing'}`",
        f"- new observations: {integer(diagnostic.get('new_observations'))}",
        f"- passing external backtests: {integer(diagnostic.get('passing_candidates'))}",
    ]
    if not candidates:
        lines.append("- no fresh standardized external candidate")
    for candidate in candidates:
        lines.extend(
            [
                f"- candidate: `{candidate.get('candidate_id')}`",
                f"- decision: `{candidate.get('decision')}`",
                "- promotion boundary: exact executable replay, incumbent ablation and normal integration gates remain required",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alpha-report", type=Path, required=True)
    parser.add_argument("--alpha-state", type=Path, required=True)
    parser.add_argument("--external-report", type=Path, required=True)
    parser.add_argument("--alpha-markdown", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    parser.add_argument("--output-report", type=Path, required=True)
    parser.add_argument("--output-state", type=Path, required=True)
    parser.add_argument("--max-age-seconds", type=int, default=10800)
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()

    now = int(time.time()) if args.now is None else args.now
    report, state = attach(
        read_json(args.alpha_report, {}),
        read_json(args.alpha_state, {}),
        read_json(args.external_report, {}),
        now=now,
        max_age_seconds=max(1, args.max_age_seconds),
    )
    atomic_json(args.output_report, report)
    atomic_json(args.output_state, state)
    if args.output_markdown:
        base = ""
        if args.alpha_markdown and args.alpha_markdown.exists():
            base = args.alpha_markdown.read_text(encoding="utf-8").rstrip() + "\n"
        atomic_text(args.output_markdown, base + render_appendix(report))
    print(
        "alpha_external_adapter"
        f" fresh={int(bool((report.get('diagnostics') or {}).get('external', {}).get('fresh')))}"
        f" candidate={state.get('external_evidence_candidate') or 'none'}"
        f" recommended_canary={report.get('recommended_canary') or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
