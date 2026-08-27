#!/usr/bin/env python3
"""Bounded paper-only research director for the Polymarket research plane.

The director turns Alpha Factory `next_experiments` into a small dispatch plan.
It never merges, deploys, mutates the champion, or submits authenticated orders.
It applies per-workflow cooldowns and experiment stagnation penalties so research
budget moves toward experiments that are still producing new evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_research_director_report_v1"
STATE_SCHEMA = "polymarket_research_director_state_v1"


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


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
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_json(path: Path, payload: Any) -> None:
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema") != "polymarket_research_director_config_v1":
        errors.append("unexpected research-director schema")
    if config.get("paper_only") is not True:
        errors.append("paper_only must be true")
    if config.get("allow_authenticated_execution") is not False:
        errors.append("authenticated execution must remain disabled")
    if config.get("allow_direct_champion_mutation") is not False:
        errors.append("direct champion mutation must remain disabled")
    owners = config.get("owner_workflows") or {}
    forbidden = set(config.get("forbidden_workflows") or [])
    if not isinstance(owners, dict) or not owners:
        errors.append("owner_workflows must be non-empty")
    overlap = sorted(set(owners).intersection(forbidden))
    if overlap:
        errors.append("owner workflow allowlist overlaps forbidden workflows: " + ", ".join(overlap))
    hard_forbidden = {
        "integration-merge.yml",
        "v7-deploy-paper-server.yml",
        "v7-paper-server-health.yml",
        "promotion-controller.yml",
    }
    bad = sorted(set(owners).intersection(hard_forbidden))
    if bad:
        errors.append("merge/deploy/promotion workflows may not be research owners: " + ", ".join(bad))
    if errors:
        raise ValueError("; ".join(errors))


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalized_runs(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for raw in runs:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("workflowName") or raw.get("name") or "")
        if not name:
            continue
        branch = str(raw.get("headBranch") or raw.get("head_branch") or "")
        if branch and branch != "main":
            continue
        updated = parse_timestamp(
            raw.get("updatedAt") or raw.get("updated_at") or raw.get("createdAt") or raw.get("created_at")
        )
        current = latest.get(name)
        if current is None or updated >= integer(current.get("_updated")):
            latest[name] = {
                "_updated": updated,
                "status": str(raw.get("status") or "").lower(),
                "conclusion": str(raw.get("conclusion") or "").lower(),
            }
    return latest


def route_owner(experiment: dict[str, Any], config: dict[str, Any]) -> str:
    """Resolve Alpha Factory logical owners onto actual evidence-producing workers."""
    declared = str(experiment.get("owner_workflow") or "")
    identifier = str(experiment.get("experiment_id") or "")
    remap = config.get("owner_remap") or {}
    if declared in remap:
        declared = str(remap[declared])
    prefix_remap = config.get("experiment_prefix_owner") or {}
    for prefix, workflow in prefix_remap.items():
        if identifier.startswith(str(prefix)):
            return str(workflow)
    return declared


def economic_progress(alpha: dict[str, Any], previous: dict[str, Any], now: int, config: dict[str, Any]) -> dict[str, Any]:
    diagnostics = alpha.get("diagnostics") or {}
    oos = diagnostics.get("oos") or {}
    candidates = alpha.get("candidates") or []
    candidates = candidates if isinstance(candidates, list) else []

    oos_trades = integer(oos.get("selected_trades"))
    ready = sum(
        str(candidate.get("decision") or "") in {"integration_ready", "paper_canary_ready"}
        for candidate in candidates
        if isinstance(candidate, dict)
    )
    observations = sum(max(0, integer(candidate.get("observations"))) for candidate in candidates if isinstance(candidate, dict))
    positive_pnl = 0
    best_oos_pnl = 0.0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        metrics = candidate.get("metrics") or {}
        pnl_values = [
            finite(metrics.get("oos_net_pnl_usd")),
            finite(metrics.get("net_pnl_per_share")),
            finite(metrics.get("total_pnl_ex_rewards_usd")),
        ]
        candidate_best = max(pnl_values, default=0.0)
        best_oos_pnl = max(best_oos_pnl, candidate_best)
        if candidate_best > 0:
            positive_pnl += 1
    external = diagnostics.get("external_intelligence") or {}
    external_pass = integer(external.get("passing_candidates"))

    vector = {
        "oos_trades": oos_trades,
        "ready_candidates": ready,
        "candidate_observations": observations,
        "positive_pnl_candidates": positive_pnl,
        "best_observed_pnl": best_oos_pnl,
        "external_passing_candidates": external_pass,
        "b1_maker_positive": integer((diagnostics.get("b1") or {}).get("maker_positive")),
        "b2_maker_positive": integer((diagnostics.get("b2") or {}).get("maker_positive")),
    }
    score = (
        10.0 * ready
        + 2.0 * positive_pnl
        + 0.5 * oos_trades
        + math.log1p(max(0, observations)) / 10.0
        + external_pass
        + max(0.0, best_oos_pnl)
    )
    fingerprint = canonical_hash(vector)
    prior_progress = previous.get("economic_progress") or {}
    prior_fingerprint = str(prior_progress.get("fingerprint") or "")
    last_change = integer(prior_progress.get("last_change_ts"), now)
    if fingerprint != prior_fingerprint:
        last_change = now
        state = "LEARNING"
    else:
        stagnant_after = integer(config.get("economic_stagnation_seconds"), 7200)
        state = "STAGNANT" if now - last_change >= stagnant_after else "ACCUMULATING"

    return {
        **vector,
        "score": score,
        "fingerprint": fingerprint,
        "last_change_ts": last_change,
        "seconds_since_progress": max(0, now - last_change),
        "state": state,
    }


def build_report(
    config: dict[str, Any],
    alpha: dict[str, Any],
    previous: dict[str, Any],
    runs: list[dict[str, Any]],
    now: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_config(config)
    owners = config.get("owner_workflows") or {}
    forbidden = set(config.get("forbidden_workflows") or [])
    cooldown = max(0, integer(config.get("dispatch_cooldown_seconds"), 1800))
    max_dispatches = max(0, integer(config.get("max_dispatches_per_cycle"), 2))
    max_stagnant = max(1, integer(config.get("max_stagnant_cycles"), 4))
    stagnation_retry = max(cooldown, integer(config.get("stagnation_retry_seconds"), 7200))
    latest_runs = normalized_runs(runs)

    alpha_ts = parse_timestamp(alpha.get("generated_ts"))
    alpha_age = max(0, now - alpha_ts) if alpha_ts else None
    max_alpha_age = max(1, integer(config.get("max_alpha_report_age_seconds"), 10800))
    alpha_fresh = bool(alpha_ts and alpha_age is not None and alpha_age <= max_alpha_age)
    experiments = alpha.get("next_experiments") or []
    experiments = experiments if isinstance(experiments, list) else []
    old_experiments = previous.get("experiments") or {}
    old_experiments = old_experiments if isinstance(old_experiments, dict) else {}

    ranked: list[dict[str, Any]] = []
    next_state: dict[str, Any] = {}
    diagnostics_fingerprint = canonical_hash(alpha.get("diagnostics") or {})
    candidate_fingerprint = canonical_hash(
        [
            {
                "candidate_id": row.get("candidate_id"),
                "decision": row.get("decision"),
                "observations": row.get("observations"),
                "metrics": row.get("metrics"),
                "reasons": row.get("reasons"),
            }
            for row in (alpha.get("candidates") or [])
            if isinstance(row, dict)
        ]
    )

    for raw in experiments:
        if not isinstance(raw, dict):
            continue
        identifier = str(raw.get("experiment_id") or "").strip()
        if not identifier:
            continue
        owner = route_owner(raw, config)
        if owner not in owners or owner in forbidden:
            ranked.append(
                {
                    "experiment_id": identifier,
                    "owner_workflow": owner,
                    "eligible": False,
                    "reason": "owner_outside_research_allowlist",
                    "effective_priority": 10_000,
                }
            )
            continue

        prior = old_experiments.get(identifier) if isinstance(old_experiments, dict) else {}
        prior = prior if isinstance(prior, dict) else {}
        fingerprint = canonical_hash(
            {
                "experiment": raw,
                "diagnostics": diagnostics_fingerprint,
                "candidates": candidate_fingerprint,
            }
        )
        prior_alpha_ts = integer(prior.get("last_alpha_ts"))
        prior_fingerprint = str(prior.get("fingerprint") or "")
        stagnant = integer(prior.get("stagnant_cycles"))
        if alpha_ts > prior_alpha_ts:
            stagnant = stagnant + 1 if prior_fingerprint == fingerprint else 0

        last_dispatch = integer(prior.get("last_dispatch_ts"))
        workflow_spec = owners.get(owner) if isinstance(owners.get(owner), dict) else {}
        workflow_name = str(workflow_spec.get("workflow_name") or owner)
        latest = latest_runs.get(workflow_name) or {}
        latest_age = max(0, now - integer(latest.get("_updated"))) if latest.get("_updated") else None
        running = str(latest.get("status") or "") not in {"", "completed"}
        recent = latest_age is not None and latest_age < cooldown
        state_cooldown = last_dispatch > 0 and now - last_dispatch < cooldown
        stagnation_hold = stagnant >= max_stagnant and last_dispatch > 0 and now - last_dispatch < stagnation_retry

        base_priority = integer(raw.get("priority"), 100)
        bias = integer(workflow_spec.get("priority_bias"), 0)
        effective = base_priority + bias + (100 if stagnant >= max_stagnant else 0)
        reasons: list[str] = []
        if running:
            reasons.append("owner_workflow_running")
        if recent:
            reasons.append(f"owner_workflow_recent:{latest_age}<{cooldown}")
        if state_cooldown:
            reasons.append(f"director_cooldown:{now-last_dispatch}<{cooldown}")
        if stagnation_hold:
            reasons.append(f"stagnation_backoff:{stagnant}>={max_stagnant}")
        if not alpha_fresh:
            reasons.append("alpha_factory_report_missing_or_stale")

        eligible = not reasons
        next_state[identifier] = {
            "owner_workflow": owner,
            "fingerprint": fingerprint,
            "last_alpha_ts": alpha_ts,
            "stagnant_cycles": stagnant,
            "last_dispatch_ts": last_dispatch,
        }
        ranked.append(
            {
                **raw,
                "experiment_id": identifier,
                "owner_workflow": owner,
                "workflow_name": workflow_name,
                "eligible": eligible,
                "effective_priority": effective,
                "stagnant_cycles": stagnant,
                "reason": "; ".join(reasons) if reasons else "eligible",
            }
        )

    ranked.sort(key=lambda row: (integer(row.get("effective_priority"), 10_000), str(row.get("experiment_id") or "")))
    plan: list[dict[str, Any]] = []
    selected_owners: set[str] = set()
    for row in ranked:
        if len(plan) >= max_dispatches:
            break
        owner = str(row.get("owner_workflow") or "")
        if not row.get("eligible") or owner in selected_owners:
            continue
        selected_owners.add(owner)
        plan.append(
            {
                "workflow_file": owner,
                "workflow_name": row.get("workflow_name"),
                "experiment_id": row.get("experiment_id"),
                "priority": row.get("effective_priority"),
                "hypothesis": row.get("hypothesis"),
                "success_metric": row.get("success_metric"),
                "triggering_evidence": row.get("triggering_evidence"),
            }
        )
        if row["experiment_id"] in next_state:
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
        "experiments": ranked,
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
        "# Polymarket Research Director",
        "",
        f"- generated: `{report.get('generated_utc')}`",
        f"- status: **{report.get('status')}**",
        f"- research state: **{report.get('research_state')}**",
        "- boundary: **paper/read-only research only**",
        "",
        "## Economic progress",
        "",
        f"- OOS trades: {integer(progress.get('oos_trades'))}",
        f"- ready candidates: {integer(progress.get('ready_candidates'))}",
        f"- candidate observations: {integer(progress.get('candidate_observations'))}",
        f"- positive-PnL candidates: {integer(progress.get('positive_pnl_candidates'))}",
        f"- seconds since measured progress: {integer(progress.get('seconds_since_progress'))}",
        "",
        "## Research dispatch plan",
        "",
    ]
    plan = report.get("dispatch_plan") or []
    if not plan:
        lines.append("- No additional worker dispatch is justified this cycle.")
    for row in plan:
        lines.append(
            f"- `{row.get('workflow_file')}` ← `{row.get('experiment_id')}` "
            f"(priority {row.get('priority')}): {row.get('hypothesis')}"
        )
    lines.extend(["", "## Experiment frontier", ""])
    for row in report.get("experiments") or []:
        lines.append(
            f"- `{row.get('experiment_id')}` → `{row.get('owner_workflow')}`: "
            f"{'eligible' if row.get('eligible') else 'hold'}; "
            f"stagnant_cycles={integer(row.get('stagnant_cycles'))}; {row.get('reason')}"
        )
    lines.extend(
        [
            "",
            "## Hard boundaries",
            "",
            "- No merge or deployment dispatch.",
            "- No champion mutation.",
            "- No credential use or authenticated order submission.",
            "- Existing OOS, cost, drawdown, FDR and promotion gates remain authoritative.",
        ]
    )
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
    atomic_write(
        Path(args.dispatch_file),
        "".join(f"{row['workflow_file']}\n" for row in report.get("dispatch_plan") or []),
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
