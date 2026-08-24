#!/usr/bin/env python3
"""Top-level coordinator for Polymarket research, validation, and paper runtime.

The meta-supervisor evaluates the workflow dependency graph and emits a bounded,
allowlisted remediation plan. It never deploys directly, changes the champion,
weakens gates, submits orders, or authorizes real-money execution.
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


SCHEMA = "polymarket_control_plane_report_v1"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return output if math.isfinite(output) else default


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


def atomic_json(path: Path, payload: Any) -> None:
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []
    if config.get("paper_only") is not True:
        errors.append("paper_only must be true")
    if config.get("allow_authenticated_execution") is not False:
        errors.append("authenticated execution must remain disabled")
    if config.get("allow_direct_champion_mutation") is not False:
        errors.append("direct champion mutation must remain disabled")
    coordination = config.get("coordination") or {}
    allowlist = set(coordination.get("allowlisted_dispatches") or [])
    forbidden = set(coordination.get("forbidden_dispatches") or [])
    overlap = sorted(allowlist.intersection(forbidden))
    if overlap:
        errors.append("dispatch allowlist overlaps forbidden workflows: " + ", ".join(overlap))
    if "deploy-paper-server.yml" in allowlist or "server-health.yml" in allowlist:
        errors.append("deployment and private health workflows may not be auto-dispatched")
    if errors:
        raise ValueError("; ".join(errors))


def normalize_run(run: dict[str, Any]) -> dict[str, Any]:
    updated = parse_timestamp(
        run.get("updatedAt")
        or run.get("updated_at")
        or run.get("createdAt")
        or run.get("created_at")
    )
    return {
        "database_id": integer(run.get("databaseId") or run.get("id")),
        "workflow_name": str(run.get("workflowName") or run.get("name") or ""),
        "status": str(run.get("status") or "").lower(),
        "conclusion": str(run.get("conclusion") or "").lower(),
        "head_sha": str(run.get("headSha") or run.get("head_sha") or ""),
        "head_branch": str(run.get("headBranch") or run.get("head_branch") or ""),
        "event": str(run.get("event") or ""),
        "created_ts": parse_timestamp(run.get("createdAt") or run.get("created_at")),
        "updated_ts": updated,
        "url": str(run.get("url") or ""),
    }


def latest_main_runs(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for raw in runs:
        if not isinstance(raw, dict):
            continue
        run = normalize_run(raw)
        name = run["workflow_name"]
        if not name:
            continue
        branch = run["head_branch"]
        # Pull-request executions must not mask the status of the default-branch
        # control chain. Older gh versions may omit headBranch, so empty is kept.
        if branch and branch != "main":
            continue
        previous = latest.get(name)
        if previous is None or (run["updated_ts"], run["database_id"]) > (
            previous["updated_ts"],
            previous["database_id"],
        ):
            latest[name] = run
    return latest


def classify_workflow(
    spec: dict[str, Any], latest: dict[str, Any] | None, main_sha: str, now: int, cooldown: int
) -> dict[str, Any]:
    if latest is None:
        return {
            "state": "missing",
            "age_seconds": None,
            "dispatch_needed": bool(spec.get("dispatchable")),
            "reason": "no default-branch run found",
            "latest_run": None,
        }

    age = max(0, now - integer(latest.get("updated_ts"))) if latest.get("updated_ts") else None
    status = str(latest.get("status") or "").lower()
    conclusion = str(latest.get("conclusion") or "").lower()
    head_sha = str(latest.get("head_sha") or "")
    requires_main = spec.get("requires_current_main") is True
    max_age = integer(spec.get("max_age_seconds"))

    if status not in {"completed", ""}:
        state = "running"
        dispatch = False
        reason = f"latest run status={status}"
    elif conclusion in {"success", "neutral", "skipped"}:
        if requires_main and head_sha and head_sha != main_sha:
            state = "outdated_revision"
            dispatch = bool(spec.get("dispatchable"))
            reason = f"successful run covers {head_sha}, not current main {main_sha}"
        elif max_age > 0 and age is not None and age > max_age:
            state = "stale"
            dispatch = bool(spec.get("dispatchable"))
            reason = f"last successful run is {age}s old; limit={max_age}s"
        else:
            state = "healthy"
            dispatch = False
            reason = "latest default-branch run is successful and current"
    else:
        if age is not None and age < cooldown:
            state = "failure_cooldown"
            dispatch = False
            reason = f"latest run concluded {conclusion or 'unknown'} {age}s ago; cooldown={cooldown}s"
        else:
            state = "failed"
            dispatch = bool(spec.get("dispatchable"))
            reason = f"latest run concluded {conclusion or 'unknown'}"

    return {
        "state": state,
        "age_seconds": age,
        "dispatch_needed": dispatch,
        "reason": reason,
        "latest_run": latest,
    }


def build_report(config: dict[str, Any], snapshot: dict[str, Any], now: int) -> dict[str, Any]:
    validate_config(config)
    coordination = config.get("coordination") or {}
    specs = coordination.get("workflows") or {}
    allowlist = set(coordination.get("allowlisted_dispatches") or [])
    forbidden = set(coordination.get("forbidden_dispatches") or [])
    cooldown = integer(coordination.get("dispatch_cooldown_seconds"), 1800)
    max_dispatches = max(0, integer(coordination.get("max_dispatches_per_cycle"), 3))

    main_sha = str(snapshot.get("main_sha") or "")
    validated_sha = str(snapshot.get("paper_validated_sha") or "")
    validated_ancestor = snapshot.get("paper_validated_is_ancestor") is True
    runs = snapshot.get("runs") if isinstance(snapshot.get("runs"), list) else []
    latest = latest_main_runs(runs)

    workflow_status: dict[str, dict[str, Any]] = {}
    for filename, raw_spec in specs.items():
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        name = str(spec.get("name") or filename)
        state = classify_workflow(spec, latest.get(name), main_sha, now, cooldown)
        workflow_status[str(filename)] = {
            "workflow_file": str(filename),
            "workflow_name": name,
            "priority": integer(spec.get("priority"), 1000),
            "dependencies": list(spec.get("dependencies") or []),
            "dispatchable": spec.get("dispatchable") is True,
            **state,
        }

    alerts: list[dict[str, str]] = []
    if not main_sha:
        alerts.append({"severity": "critical", "code": "MAIN_SHA_MISSING", "detail": "cannot coordinate without main revision"})
    if not validated_sha:
        alerts.append({"severity": "critical", "code": "VALIDATED_SHA_MISSING", "detail": "paper-validated ref is unavailable"})
    elif not validated_ancestor:
        alerts.append(
            {
                "severity": "critical",
                "code": "VALIDATED_REF_DIVERGED",
                "detail": "paper-validated is not an ancestor of main; deployment chain must remain blocked",
            }
        )
    elif validated_sha != main_sha:
        alerts.append(
            {
                "severity": "info",
                "code": "VALIDATION_PENDING",
                "detail": "main is ahead of paper-validated; the incumbent validated revision remains deployed",
            }
        )

    for filename, state in workflow_status.items():
        if state["state"] in {"failed", "missing", "stale", "outdated_revision"}:
            severity = "warning" if state.get("dispatchable") else "critical"
            alerts.append(
                {
                    "severity": severity,
                    "code": "WORKFLOW_" + state["state"].upper(),
                    "detail": f"{filename}: {state['reason']}",
                }
            )

    ordered = sorted(
        workflow_status.items(),
        key=lambda item: (integer(item[1].get("priority"), 1000), item[0]),
    )
    dispatch_plan: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    planned_files: set[str] = set()

    for filename, state in ordered:
        if len(dispatch_plan) >= max_dispatches:
            if state.get("dispatch_needed"):
                blocked.append(
                    {
                        "workflow_file": filename,
                        "reason": "per-cycle dispatch budget exhausted",
                    }
                )
            continue
        if not state.get("dispatch_needed"):
            continue
        if filename not in allowlist or filename in forbidden:
            blocked.append(
                {
                    "workflow_file": filename,
                    "reason": "workflow is outside the remediation allowlist",
                }
            )
            continue

        dependencies = list(state.get("dependencies") or [])
        unhealthy: list[str] = []
        for dependency in dependencies:
            dependency_state = workflow_status.get(str(dependency), {}).get("state")
            if dependency_state != "healthy":
                unhealthy.append(f"{dependency}:{dependency_state or 'missing_spec'}")
        if unhealthy:
            blocked.append(
                {
                    "workflow_file": filename,
                    "reason": "dependencies are not healthy: " + ", ".join(unhealthy),
                }
            )
            continue

        dispatch_plan.append(
            {
                "workflow_file": filename,
                "workflow_name": state.get("workflow_name"),
                "reason": state.get("reason"),
                "priority": state.get("priority"),
            }
        )
        planned_files.add(filename)

    # A workflow may be healthy only at an older revision while its dependency is
    # being dispatched this cycle. Targets therefore wait for the next cycle.
    for action in dispatch_plan:
        if action["workflow_file"] in forbidden:
            raise AssertionError("forbidden workflow entered dispatch plan")
    if len(dispatch_plan) > max_dispatches:
        raise AssertionError("dispatch budget violated")

    if not main_sha or (validated_sha and not validated_ancestor):
        dispatch_plan = [
            action for action in dispatch_plan
            if action["workflow_file"] in {"ci.yml", "monitoring.yml", "v4-live-smoke.yml"}
        ]

    if any(alert["severity"] == "critical" for alert in alerts):
        status = "DEGRADED"
    elif dispatch_plan:
        status = "REMEDIATING"
    elif validated_sha != main_sha:
        status = "AWAITING_PAPER_VALIDATION"
    else:
        status = "HEALTHY"

    return {
        "schema": SCHEMA,
        "generated_ts": now,
        "generated_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "status": status,
        "paper_only": True,
        "main_sha": main_sha,
        "paper_validated_sha": validated_sha,
        "paper_validated_is_ancestor": validated_ancestor,
        "validation_relation": (
            "current" if validated_sha and validated_sha == main_sha
            else "pending" if validated_ancestor
            else "diverged_or_missing"
        ),
        "workflow_status": workflow_status,
        "dispatch_plan": dispatch_plan,
        "blocked_actions": blocked,
        "alerts": alerts,
        "invariants": {
            "max_dispatches_per_cycle": max_dispatches,
            "actual_dispatches": len(dispatch_plan),
            "allowlist_respected": all(action["workflow_file"] in allowlist for action in dispatch_plan),
            "deployment_dispatched_directly": False,
            "server_health_dispatched_directly": False,
            "champion_mutated": False,
            "authenticated_execution": False,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Polymarket Meta-Supervisor",
        "",
        f"- generated: `{report['generated_utc']}`",
        f"- status: **{report['status']}**",
        f"- main: `{report.get('main_sha') or 'missing'}`",
        f"- paper-validated: `{report.get('paper_validated_sha') or 'missing'}`",
        f"- validation relation: `{report.get('validation_relation')}`",
        "- authority: workflow coordination and paper/research remediation only",
        "",
        "## Workflow graph",
        "",
        "| Workflow | State | Dispatchable | Reason |",
        "|---|---:|---:|---|",
    ]
    for filename, state in sorted(
        (report.get("workflow_status") or {}).items(),
        key=lambda item: (integer(item[1].get("priority"), 1000), item[0]),
    ):
        reason = str(state.get("reason") or "").replace("|", "\\|")
        lines.append(
            f"| `{filename}` | `{state.get('state')}` | `{str(bool(state.get('dispatchable'))).lower()}` | {reason} |"
        )

    lines.extend(["", "## Bounded remediation plan", ""])
    plan = report.get("dispatch_plan") or []
    if not plan:
        lines.append("- No workflow dispatch is justified in this cycle.")
    for action in plan:
        lines.append(f"- dispatch `{action['workflow_file']}` — {action['reason']}")

    lines.extend(["", "## Blocked actions", ""])
    blocked = report.get("blocked_actions") or []
    if not blocked:
        lines.append("- none")
    for action in blocked:
        lines.append(f"- `{action['workflow_file']}` — {action['reason']}")

    lines.extend(["", "## Alerts", ""])
    alerts = report.get("alerts") or []
    if not alerts:
        lines.append("- none")
    for alert in alerts:
        lines.append(f"- **{alert['severity']}** `{alert['code']}` — {alert['detail']}")

    lines.extend(
        [
            "",
            "## Hard boundaries",
            "",
            "- No direct deployment dispatch.",
            "- No direct `paper-validated` movement.",
            "- No champion mutation.",
            "- No credential use or authenticated order submission.",
            "- Existing OOS, cost, risk, drawdown and kill-switch gates remain authoritative.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/alpha_factory.json"))
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    parser.add_argument("--dispatch-file", type=Path, required=True)
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()

    config = read_json(args.config, {})
    snapshot = read_json(args.snapshot, {})
    now = int(time.time()) if args.now is None else args.now
    report = build_report(config, snapshot, now)

    atomic_json(args.output_json, report)
    atomic_write(args.output_markdown, render_markdown(report) + "\n")
    atomic_write(
        args.dispatch_file,
        "".join(f"{action['workflow_file']}\n" for action in report.get("dispatch_plan") or []),
    )
    print(
        "meta_supervisor"
        f" status={report['status']}"
        f" workflows={len(report['workflow_status'])}"
        f" dispatches={len(report['dispatch_plan'])}"
        f" alerts={len(report['alerts'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
