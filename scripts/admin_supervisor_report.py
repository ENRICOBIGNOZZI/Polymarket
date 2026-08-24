#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INTEGRATION_LABELS = {
    "approved-for-integration",
    "single-model-reviewed",
    "administrator-approved",
}
RESEARCH_PREFIXES = ("research/", "experiment/", "diagnostic/")


def labels(pr: dict[str, Any]) -> set[str]:
    return {str(item.get("name")) for item in pr.get("labels", []) if item.get("name")}


def latest_by_workflow(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the latest non-PR run for each workflow.

    Failed candidate PR checks are evidence about that candidate, not evidence
    that the deployed control plane is unhealthy. Production supervision uses
    push, schedule, workflow-dispatch, repository-dispatch and workflow-run
    executions only; PR policy and integration gates handle candidate checks.
    """

    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        if str(run.get("event") or "").lower() == "pull_request":
            continue
        name = str(run.get("workflowName") or run.get("name") or "")
        if not name:
            continue
        current = latest.get(name)
        if current is None or str(run.get("createdAt", "")) > str(current.get("createdAt", "")):
            latest[name] = run
    return latest


def scheduler_state(run: dict[str, Any] | None) -> str:
    if run is None:
        return "missing"
    status = str(run.get("status") or "unknown").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    if status != "completed":
        return status
    return conclusion or "completed"


def render(
    registry: dict[str, Any],
    runs: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    branches: list[dict[str, Any]],
    manifest: dict[str, Any],
    main_sha: str,
    validated_sha: str,
    validation_relation: str,
) -> tuple[str, dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    latest = latest_by_workflow(runs)
    schedulers = registry.get("schedulers", [])
    warnings: list[str] = []
    blockers: list[str] = []

    integration_prs = [pr for pr in prs if str(pr.get("headRefName", "")).startswith("integration/")]
    eligible = [
        pr
        for pr in integration_prs
        if not bool(pr.get("isDraft")) and INTEGRATION_LABELS.issubset(labels(pr))
    ]
    research_prs = [pr for pr in prs if str(pr.get("headRefName", "")).startswith(RESEARCH_PREFIXES)]
    approved_research = [pr for pr in research_prs if "research-approved" in labels(pr)]

    if len(eligible) > 1:
        blockers.append("more than one administrator-approved integration candidate is active")
    if validation_relation == "diverged":
        blockers.append("paper-validated is not equal to or an ancestor of main")
    elif validation_relation == "pending_validation":
        warnings.append("main is ahead of paper-validated; the incumbent validated revision remains live")

    scheduler_rows: list[dict[str, Any]] = []
    for item in schedulers:
        workflow_name = str(item.get("workflow_name", ""))
        run = latest.get(workflow_name)
        state = scheduler_state(run)
        scheduler_rows.append(
            {
                "id": item.get("id"),
                "workflow_name": workflow_name,
                "state": state,
                "created_at": None if run is None else run.get("createdAt"),
                "url": None if run is None else run.get("url"),
                "event": None if run is None else run.get("event"),
                "head_branch": None if run is None else run.get("headBranch"),
                "head_sha": None if run is None else run.get("headSha"),
                "critical": bool(item.get("critical")),
            }
        )
        if bool(item.get("critical")) and state in {
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
        }:
            blockers.append(f"critical scheduler {item.get('id')} latest production state is {state}")
        elif run is None:
            warnings.append(f"no non-PR workflow run is visible yet for {item.get('id')}")

    lines = [
        "# Polymarket administrator supervisor",
        "",
        f"- generated_at: `{now}`",
        f"- main: `{main_sha}`",
        f"- paper-validated: `{validated_sha}`",
        f"- validation relation: `{validation_relation}`",
        f"- champion version: `{manifest.get('version')}`",
        f"- champion loop: `{manifest.get('loop')}`",
        f"- champion config: `{manifest.get('config')}`",
        f"- champion run root: `{manifest.get('run_root')}`",
        f"- open PRs: {len(prs)}",
        f"- research PRs: {len(research_prs)}",
        f"- approved research awaiting integration: {len(approved_research)}",
        f"- integration PRs: {len(integration_prs)}",
        f"- eligible administrator-approved integrations: {len(eligible)}",
        f"- branches observed: {len(branches)}",
        "",
        "## Scheduler health",
        "",
        "| Scheduler | State | Event / branch | Latest run |",
        "|---|---|---|---|",
    ]
    for row in scheduler_rows:
        latest_text = row["created_at"] or "not seen"
        if row["url"]:
            latest_text = f"[{latest_text}]({row['url']})"
        origin = "not seen"
        if row["event"] or row["head_branch"]:
            origin = f"{row['event'] or 'unknown'} / {row['head_branch'] or 'detached'}"
        lines.append(f"| {row['id']} | `{row['state']}` | {origin} | {latest_text} |")

    lines.extend(["", "## Control-plane blockers"])
    if blockers:
        lines.extend(f"- {item}" for item in sorted(set(blockers)))
    else:
        lines.append("- none")
    lines.extend(["", "## Warnings"])
    if warnings:
        lines.extend(f"- {item}" for item in sorted(set(warnings)))
    else:
        lines.append("- none")

    lines.extend(["", "## Administrator boundary"])
    lines.append(
        "The supervisor observes and coordinates only. It cannot approve research, merge a pull request, "
        "dispatch post-merge validation, advance `paper-validated`, deploy, or submit orders."
    )

    payload = {
        "schema_version": 1,
        "generated_at": now,
        "main_sha": main_sha,
        "paper_validated_sha": validated_sha,
        "validation_relation": validation_relation,
        "champion": manifest,
        "counts": {
            "open_prs": len(prs),
            "research_prs": len(research_prs),
            "approved_research": len(approved_research),
            "integration_prs": len(integration_prs),
            "eligible_integrations": len(eligible),
            "branches": len(branches),
        },
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "schedulers": scheduler_rows,
    }
    return "\n".join(lines) + "\n", payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the read-only administrator supervisor report")
    parser.add_argument("--registry", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--prs", required=True)
    parser.add_argument("--branches", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--main-sha", required=True)
    parser.add_argument("--validated-sha", required=True)
    parser.add_argument(
        "--validation-relation", choices=("current", "pending_validation", "diverged"), required=True
    )
    parser.add_argument("--markdown", required=True)
    parser.add_argument("--json-output", required=True)
    args = parser.parse_args()

    registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    runs = json.loads(Path(args.runs).read_text(encoding="utf-8"))
    prs = json.loads(Path(args.prs).read_text(encoding="utf-8"))
    branches = json.loads(Path(args.branches).read_text(encoding="utf-8"))
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    for name, value in (("runs", runs), ("prs", prs), ("branches", branches)):
        if not isinstance(value, list):
            raise SystemExit(f"{name} input must be a JSON array")
    report, payload = render(
        registry,
        runs,
        prs,
        branches,
        manifest,
        args.main_sha,
        args.validated_sha,
        args.validation_relation,
    )
    Path(args.markdown).write_text(report, encoding="utf-8")
    Path(args.json_output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(report, end="")
    return 1 if payload["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
