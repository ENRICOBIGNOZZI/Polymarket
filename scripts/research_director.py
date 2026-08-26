#!/usr/bin/env python3
"""Bounded V7 research router driven by Alpha Factory evidence.

The Research Director owns no model, merge, validation, deployment, champion or
execution authority. It only maps explicit V7 research experiments to the small
set of registered evidence-producing workflows and enforces cooldown/budget
constraints.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_research_director_v1"
STATE_SCHEMA = "polymarket_research_director_state_v1"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def parse_timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw // 1000 if raw > 10_000_000_000 else raw
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        raw = int(float(text))
        return raw // 1000 if raw > 10_000_000_000 else raw
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    if config.get("paper_only") is not True:
        errors.append("paper_only must be true")
    if config.get("allow_authenticated_execution") is not False:
        errors.append("authenticated execution must remain disabled")
    if config.get("allow_direct_champion_mutation") is not False:
        errors.append("direct champion mutation must remain disabled")
    owners = config.get("owner_workflows")
    if not isinstance(owners, dict) or not owners:
        errors.append("owner_workflows must contain registered V7 research workers")
        owners = {}
    forbidden = set(config.get("forbidden_workflows") or [])
    overlap = sorted(set(owners).intersection(forbidden))
    if overlap:
        errors.append("research owner overlaps forbidden authority: " + ", ".join(overlap))
    expected = {
        "forward-maker-research.yml",
        "external-intelligence.yml",
        "fast-arb-hourly.yml",
        "arb-theory-hourly.yml",
    }
    if set(owners) != expected:
        errors.append("owner_workflows must equal the canonical V7 research-worker set")
    if errors:
        raise ValueError("; ".join(errors))


def normalize_run(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflow_name": str(raw.get("workflowName") or raw.get("name") or ""),
        "status": str(raw.get("status") or "").lower(),
        "conclusion": str(raw.get("conclusion") or "").lower(),
        "updated_ts": parse_timestamp(raw.get("updatedAt") or raw.get("updated_at") or raw.get("createdAt") or raw.get("created_at")),
        "database_id": integer(raw.get("databaseId") or raw.get("id")),
        "head_branch": str(raw.get("headBranch") or raw.get("head_branch") or ""),
    }


def latest_runs(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for raw in runs:
        if not isinstance(raw, dict):
            continue
        run = normalize_run(raw)
        if run["head_branch"] and run["head_branch"] != "main":
            continue
        name = run["workflow_name"]
        if not name:
            continue
        previous = latest.get(name)
        if previous is None or (run["updated_ts"], run["database_id"]) > (previous["updated_ts"], previous["database_id"]):
            latest[name] = run
    return latest


def resolve_owner(experiment: dict[str, Any], config: dict[str, Any]) -> str:
    owners = config.get("owner_workflows") or {}
    explicit = str(experiment.get("owner_workflow") or "")
    remap = config.get("owner_remap") if isinstance(config.get("owner_remap"), dict) else {}
    explicit = str(remap.get(explicit) or explicit)
    if explicit in owners:
        return explicit
    experiment_id = str(experiment.get("experiment_id") or "")
    prefixes = config.get("experiment_prefix_owner") if isinstance(config.get("experiment_prefix_owner"), dict) else {}
    for prefix, owner in sorted(prefixes.items(), key=lambda item: len(str(item[0])), reverse=True):
        if experiment_id.startswith(str(prefix)) and str(owner) in owners:
            return str(owner)
    return ""


def _candidate_pnl(candidate: dict[str, Any]) -> float:
    metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) else {}
    for key in (
        "stressed_2_0x_net_pnl_usd",
        "stressed_1_5x_net_pnl_usd",
        "oos_net_pnl_usd",
        "total_pnl_ex_rewards_usd",
        "net_pnl_usd",
    ):
        if key in metrics:
            return finite(metrics.get(key))
    return 0.0


def economic_progress(alpha: dict[str, Any], previous: dict[str, Any], now: int, config: dict[str, Any]) -> dict[str, Any]:
    candidates = [row for row in (alpha.get("candidates") or []) if isinstance(row, dict)]
    ready = sum(str(row.get("decision") or "") in {"integration_ready", "paper_canary_ready"} for row in candidates)
    observations = sum(max(0, integer(row.get("observations"))) for row in candidates)
    positive = sum(_candidate_pnl(row) > 0.0 for row in candidates)
    diagnostics = alpha.get("diagnostics") if isinstance(alpha.get("diagnostics"), dict) else {}
    runtime_fills = integer(diagnostics.get("runtime_total_fills"))
    execution_eligible = integer(diagnostics.get("execution_evidence_eligible_models"))
    signature = {
        "ready_candidates": ready,
        "candidate_observations": observations,
        "positive_pnl_candidates": positive,
        "runtime_total_fills": runtime_fills,
        "execution_evidence_eligible_models": execution_eligible,
    }
    prior = previous.get("economic_progress") if isinstance(previous.get("economic_progress"), dict) else {}
    prior_signature = prior.get("signature") if isinstance(prior.get("signature"), dict) else {}
    changed = signature != prior_signature
    previous_progress_ts = parse_timestamp(prior.get("last_progress_ts"))
    if changed or previous_progress_ts <= 0:
        last_progress_ts = now
    else:
        last_progress_ts = previous_progress_ts
    seconds_since = max(0, now - last_progress_ts)
    stagnation = max(300, integer(config.get("stagnation_seconds"), 7200))
    state = "PROGRESSING" if changed else ("STAGNANT" if seconds_since >= stagnation else "STEADY")
    return {
        "state": state,
        "signature": signature,
        "last_progress_ts": last_progress_ts,
        "seconds_since_progress": seconds_since,
        **signature,
    }


def build_report(
    config: dict[str, Any],
    alpha: dict[str, Any],
    previous: dict[str, Any],
    runs: list[dict[str, Any]],
    now: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_config(config)
    alpha_ts = parse_timestamp(alpha.get("generated_ts"))
    alpha_age = max(0, now - alpha_ts) if alpha_ts else None
    alpha_limit = max(300, integer(config.get("alpha_factory_max_age_seconds"), 10800))
    alpha_fresh = bool(alpha_ts and alpha_age is not None and alpha_age <= alpha_limit)
    owners = config["owner_workflows"]
    forbidden = set(config.get("forbidden_workflows") or [])
    cooldowns = config.get("owner_cooldowns_seconds") if isinstance(config.get("owner_cooldowns_seconds"), dict) else {}
    max_dispatches = max(0, integer(config.get("max_dispatches_per_cycle"), 2))
    latest = latest_runs(runs)

    prior_state = previous.get("experiments") if isinstance(previous.get("experiments"), dict) else {}
    next_state = {key: dict(value) for key, value in prior_state.items() if isinstance(value, dict)}
    frontier: list[dict[str, Any]] = []
    for raw in alpha.get("next_experiments") or []:
        if not isinstance(raw, dict):
            continue
        experiment_id = str(raw.get("experiment_id") or "").strip()
        if not experiment_id:
            continue
        owner = resolve_owner(raw, config)
        previous_row = next_state.get(experiment_id, {})
        last_dispatch = parse_timestamp(previous_row.get("last_dispatch_ts"))
        owner_spec = owners.get(owner) if owner else None
        owner_name = str((owner_spec or {}).get("workflow_name") or "") if isinstance(owner_spec, dict) else ""
        owner_run = latest.get(owner_name) if owner_name else None
        owner_updated = integer((owner_run or {}).get("updated_ts"))
        cooldown = max(0, integer(cooldowns.get(owner), 3600)) if owner else 0
        age_since_dispatch = max(0, now - last_dispatch) if last_dispatch else None
        age_since_owner_run = max(0, now - owner_updated) if owner_updated else None
        eligible = bool(alpha_fresh and owner and owner not in forbidden)
        reasons: list[str] = []
        if not alpha_fresh:
            reasons.append("alpha_factory_stale_or_missing")
        if not owner:
            reasons.append("no_registered_v7_research_owner")
        elif owner in forbidden:
            reasons.append("owner_is_forbidden_authority")
        if eligible and last_dispatch and age_since_dispatch is not None and age_since_dispatch < cooldown:
            eligible = False
            reasons.append(f"experiment_cooldown:{age_since_dispatch}<{cooldown}")
        if eligible and owner_run and str(owner_run.get("status")) not in {"", "completed"}:
            eligible = False
            reasons.append("owner_workflow_already_running")
        row = {
            "experiment_id": experiment_id,
            "priority": integer(raw.get("priority"), 999),
            "owner_workflow": owner,
            "owner_workflow_name": owner_name,
            "hypothesis": str(raw.get("hypothesis") or ""),
            "triggering_evidence": str(raw.get("triggering_evidence") or ""),
            "success_metric": str(raw.get("success_metric") or ""),
            "eligible": eligible,
            "reason": ", ".join(reasons) if reasons else "eligible",
            "last_dispatch_ts": last_dispatch,
            "owner_last_run_age_seconds": age_since_owner_run,
        }
        frontier.append(row)
        state_row = dict(previous_row)
        state_row.update({
            "experiment_id": experiment_id,
            "owner_workflow": owner,
            "last_seen_ts": now,
        })
        next_state[experiment_id] = state_row

    frontier.sort(key=lambda row: (integer(row.get("priority"), 999), str(row.get("experiment_id"))))
    plan: list[dict[str, Any]] = []
    used_owners: set[str] = set()
    for row in frontier:
        if len(plan) >= max_dispatches:
            break
        owner = str(row.get("owner_workflow") or "")
        if not row.get("eligible") or not owner or owner in used_owners:
            continue
        used_owners.add(owner)
        plan.append({
            "workflow_file": owner,
            "experiment_id": row["experiment_id"],
            "priority": row["priority"],
            "hypothesis": row["hypothesis"],
            "success_metric": row["success_metric"],
            "triggering_evidence": row["triggering_evidence"],
        })
        next_state[row["experiment_id"]]["last_dispatch_ts"] = now

    progress = economic_progress(alpha, previous, now, config)
    status = "HEALTHY" if alpha_fresh else "DEGRADED"
    report = {
        "schema": SCHEMA,
        "generated_ts": now,
        "generated_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "status": status,
        "research_state": progress["state"],
        "paper_only": True,
        "alpha_factory_generated_ts": alpha_ts,
        "alpha_factory_age_seconds": alpha_age,
        "alpha_factory_fresh": alpha_fresh,
        "economic_progress": progress,
        "dispatch_plan": plan,
        "experiments": frontier,
        "submitted_orders": 0,
        "authenticated_execution": False,
        "direct_champion_mutation": False,
        "invariants": {
            "append_only_external_store": True,
            "bounded_allowlisted_research": True,
            "real_order_submission": False,
            "direct_champion_mutation": False,
            "max_dispatches_per_cycle": max_dispatches,
            "actual_dispatches": len(plan),
            "forbidden_dispatches_excluded": all(row["workflow_file"] not in forbidden for row in plan),
            "unique_owner_per_cycle": len({row["workflow_file"] for row in plan}) == len(plan),
            "canonical_v7_research_owners_only": all(row["workflow_file"] in owners for row in plan),
        },
    }
    state = {
        "schema": STATE_SCHEMA,
        "generated_ts": now,
        "alpha_factory_generated_ts": alpha_ts,
        "experiments": next_state,
        "economic_progress": progress,
    }
    return report, state


def render_markdown(report: dict[str, Any]) -> str:
    progress = report.get("economic_progress") or {}
    lines = [
        "# Polymarket V7 Research Director",
        "",
        f"- generated: `{report.get('generated_utc')}`",
        f"- status: **{report.get('status')}**",
        f"- research state: **{report.get('research_state')}**",
        "- boundary: **paper/read-only research only**",
        "",
        "## Economic progress",
        "",
        f"- ready candidates: {integer(progress.get('ready_candidates'))}",
        f"- candidate observations: {integer(progress.get('candidate_observations'))}",
        f"- positive-PnL candidates: {integer(progress.get('positive_pnl_candidates'))}",
        f"- V7 runtime fills: {integer(progress.get('runtime_total_fills'))}",
        f"- execution-evidence eligible models: {integer(progress.get('execution_evidence_eligible_models'))}",
        f"- seconds since measured progress: {integer(progress.get('seconds_since_progress'))}",
        "",
        "## Research dispatch plan",
        "",
    ]
    plan = report.get("dispatch_plan") or []
    if not plan:
        lines.append("- No additional V7 research worker dispatch is justified this cycle.")
    for row in plan:
        lines.append(f"- `{row.get('workflow_file')}` ← `{row.get('experiment_id')}` (priority {row.get('priority')}): {row.get('hypothesis')}")
    lines.extend(["", "## Experiment frontier", ""])
    for row in report.get("experiments") or []:
        lines.append(f"- `{row.get('experiment_id')}` → `{row.get('owner_workflow') or 'unroutable'}`: {'eligible' if row.get('eligible') else 'hold'}; {row.get('reason')}")
    lines.extend([
        "",
        "## Hard boundaries",
        "",
        "- No merge, validation or deployment dispatch.",
        "- No champion mutation.",
        "- No credential use or authenticated order submission.",
        "- Only registered V7 evidence-producing research workers may be dispatched.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--alpha-report", required=True)
    parser.add_argument("--state-in")
    parser.add_argument("--runs")
    parser.add_argument("--state-out", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown", required=True)
    parser.add_argument("--dispatch-file", required=True)
    parser.add_argument("--now", type=int, default=0)
    args = parser.parse_args()

    config = read_json(Path(args.config), {})
    alpha = read_json(Path(args.alpha_report), {})
    previous = read_json(Path(args.state_in), {}) if args.state_in else {}
    runs = read_json(Path(args.runs), []) if args.runs else []
    runs = runs if isinstance(runs, list) else []
    now = args.now or int(time.time())

    report, state = build_report(config, alpha, previous, runs, now)
    atomic_json(Path(args.output_json), report)
    atomic_json(Path(args.state_out), state)
    atomic_write(Path(args.output_markdown), render_markdown(report))
    atomic_write(Path(args.dispatch_file), "".join(f"{row['workflow_file']}\n" for row in report.get("dispatch_plan") or []))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
