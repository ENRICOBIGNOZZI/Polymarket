#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVE_STATES = {"queued", "in_progress", "pending", "requested", "waiting"}
SUCCESS_CONCLUSIONS = {"success", "neutral"}
EXACT_SHA_VALIDATORS = {
    "code-validation",
    "monitoring-validation",
    "live-paper-validation",
    "post-merge-validation",
}
DEPLOYMENT_SCHEDULERS = {"paper-server-deploy", "paper-server-health"}


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def latest_default_branch_run(
    runs: list[dict[str, Any]], workflow_name: str
) -> dict[str, Any] | None:
    candidates = [
        run
        for run in runs
        if str(run.get("workflowName") or run.get("name") or "") == workflow_name
        and str(run.get("event") or "") != "pull_request"
        and str(run.get("headBranch") or "main") == "main"
        and run.get("createdAt")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda run: str(run.get("createdAt")))


def run_state(
    run: dict[str, Any] | None,
    *,
    main_sha: str,
    max_staleness_minutes: int,
    now: datetime,
) -> tuple[str, float | None, bool, str]:
    if run is None:
        return "missing", None, False, "no default-branch run is visible"

    created_at = parse_time(str(run["createdAt"]))
    age_minutes = max(0.0, (now - created_at).total_seconds() / 60.0)
    status = str(run.get("status") or "unknown").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    head_sha = str(run.get("headSha") or "")

    if status in ACTIVE_STATES:
        fresh = age_minutes <= max_staleness_minutes and head_sha == main_sha
        reason = "current run active" if fresh else "active run is stale or bound to an old SHA"
        return status, age_minutes, fresh, reason

    if status != "completed":
        return status, age_minutes, False, f"unexpected run status {status}"

    if conclusion not in SUCCESS_CONCLUSIONS:
        return conclusion or "completed_without_conclusion", age_minutes, False, (
            f"latest run concluded {conclusion or 'without a conclusion'}"
        )
    if head_sha != main_sha:
        return conclusion, age_minutes, False, "latest successful run is bound to an old main SHA"
    if age_minutes > max_staleness_minutes:
        return conclusion, age_minutes, False, (
            f"latest successful run is {age_minutes:.1f} minutes old"
        )
    return conclusion, age_minutes, True, "fresh successful run"


def build_plan(
    registry: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    main_sha: str,
    validated_sha: str,
    validation_relation: str,
    deploy_enabled: bool,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    plan: list[dict[str, Any]] = []
    blockers: list[str] = []

    for scheduler in registry.get("schedulers", []):
        scheduler_id = str(scheduler["id"])
        workflow_name = str(scheduler["workflow_name"])
        workflow_file = Path(str(scheduler["workflow"])).name
        max_age = int(scheduler["max_staleness_minutes"])
        dispatchable = bool(scheduler["meta_dispatch"])
        latest = latest_default_branch_run(runs, workflow_name)

        if scheduler_id in DEPLOYMENT_SCHEDULERS and not deploy_enabled:
            rows.append(
                {
                    "id": scheduler_id,
                    "workflow": workflow_file,
                    "state": "disabled",
                    "age_minutes": None,
                    "fresh": True,
                    "reason": "private deployment is disabled",
                    "dispatch": False,
                }
            )
            continue

        state, age, fresh, reason = run_state(
            latest,
            main_sha=main_sha,
            max_staleness_minutes=max_age,
            now=now,
        )
        dispatch = False
        inputs: dict[str, str] = {}

        if scheduler_id == "meta-supervisor":
            fresh = True
            reason = "the current meta-supervisor run is authoritative"
        elif scheduler_id == "post-merge-validation":
            if validation_relation == "diverged":
                blockers.append("main and paper-validated have diverged")
            elif main_sha == validated_sha:
                fresh = True
                reason = "no unvalidated main revision exists"
            elif dispatchable and not fresh:
                dispatch = True
                inputs["expected_sha"] = main_sha
                reason = "main is ahead of paper-validated and validation reconciliation is stale"
        elif scheduler_id == "integration-merge" and validation_relation != "current":
            reason = "integration remains blocked until the current main revision is fully validated"
        elif dispatchable and not fresh:
            dispatch = True
            if scheduler_id in EXACT_SHA_VALIDATORS:
                inputs["expected_sha"] = main_sha

        if dispatch:
            plan.append(
                {
                    "id": scheduler_id,
                    "workflow": workflow_file,
                    "inputs": inputs,
                    "reason": reason,
                }
            )

        rows.append(
            {
                "id": scheduler_id,
                "workflow": workflow_file,
                "state": state,
                "age_minutes": None if age is None else round(age, 1),
                "fresh": fresh,
                "reason": reason,
                "dispatch": dispatch,
            }
        )

    return rows, plan, blockers


def render(
    rows: list[dict[str, Any]],
    plan: list[dict[str, Any]],
    blockers: list[str],
    *,
    main_sha: str,
    validated_sha: str,
    validation_relation: str,
    deploy_enabled: bool,
    now: datetime,
) -> str:
    lines = [
        "# Scheduler meta-supervisor",
        "",
        f"- generated_at: `{now.isoformat()}`",
        f"- main: `{main_sha}`",
        f"- paper-validated: `{validated_sha}`",
        f"- validation relation: `{validation_relation}`",
        f"- private deployment enabled: `{str(deploy_enabled).lower()}`",
        f"- recovery dispatches planned: {len(plan)}",
        "",
        "| Scheduler | State | Age (min) | Fresh | Recovery dispatch | Reason |",
        "|---|---|---:|---:|---:|---|",
    ]
    for row in rows:
        age = "-" if row["age_minutes"] is None else str(row["age_minutes"])
        lines.append(
            f"| {row['id']} | `{row['state']}` | {age} | "
            f"{'yes' if row['fresh'] else 'no'} | {'yes' if row['dispatch'] else 'no'} | "
            f"{str(row['reason']).replace('|', '/')} |"
        )

    lines.extend(["", "## Recovery plan"])
    if plan:
        for item in plan:
            inputs = ", ".join(f"{key}={value}" for key, value in item["inputs"].items())
            suffix = f" ({inputs})" if inputs else ""
            lines.append(f"- `{item['workflow']}`{suffix}: {item['reason']}")
    else:
        lines.append("- no recovery dispatch required")

    lines.extend(["", "## Blockers"])
    if blockers:
        lines.extend(f"- {item}" for item in blockers)
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan fail-closed recovery dispatches for schedulers")
    parser.add_argument("--registry", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--main-sha", required=True)
    parser.add_argument("--validated-sha", required=True)
    parser.add_argument(
        "--validation-relation",
        choices=("current", "pending_validation", "diverged"),
        required=True,
    )
    parser.add_argument("--deploy-enabled", choices=("true", "false"), required=True)
    parser.add_argument("--now")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--markdown", required=True)
    args = parser.parse_args()

    registry = load_json(Path(args.registry))
    runs = load_json(Path(args.runs))
    if not isinstance(registry, dict):
        raise SystemExit("registry must contain a JSON object")
    if not isinstance(runs, list):
        raise SystemExit("runs must contain a JSON array")
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)

    rows, plan, blockers = build_plan(
        registry,
        runs,
        main_sha=args.main_sha,
        validated_sha=args.validated_sha,
        validation_relation=args.validation_relation,
        deploy_enabled=args.deploy_enabled == "true",
        now=now,
    )
    Path(args.plan).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = render(
        rows,
        plan,
        blockers,
        main_sha=args.main_sha,
        validated_sha=args.validated_sha,
        validation_relation=args.validation_relation,
        deploy_enabled=args.deploy_enabled == "true",
        now=now,
    )
    Path(args.markdown).write_text(report, encoding="utf-8")
    print(report, end="")
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
