#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RESEARCH_PREFIXES = ("research/", "experiment/", "diagnostic/")
SOURCE_RESEARCH_PR_PATTERN = re.compile(
    r"source research pr/branch/commit:\s*#(\d+)\b", flags=re.IGNORECASE
)


def labels(pr: dict[str, Any]) -> set[str]:
    return {str(item.get("name")) for item in pr.get("labels", []) if item.get("name")}


def run_is_production(run: dict[str, Any]) -> bool:
    event = str(run.get("event") or "").lower()
    head_branch = str(run.get("headBranch") or "")
    if event == "pull_request" or head_branch.startswith("refs/pull/"):
        return False
    # Scheduler health is a production-control-plane view. Manual or other runs
    # on research/experiment/fix branches are useful validation evidence, but a
    # newer failure there must not replace the latest main production state.
    # Some schedule/repository-dispatch payloads omit headBranch, so preserve
    # those runs rather than treating missing branch metadata as a failure.
    if head_branch and head_branch != "main":
        return False
    return True


def latest_by_workflow(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        if not run_is_production(run):
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


def has_numbered_source(pr: dict[str, Any]) -> bool:
    return SOURCE_RESEARCH_PR_PATTERN.search(str(pr.get("body") or "")) is not None


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
    queued_for_automatic_promotion = sorted(
        [
            pr for pr in integration_prs
            if not bool(pr.get("isDraft")) and has_numbered_source(pr)
        ],
        key=lambda pr: int(pr.get("number") or 0),
    )
    research_prs = [pr for pr in prs if str(pr.get("headRefName", "")).startswith(RESEARCH_PREFIXES)]
    research_with_evidence_label = [pr for pr in research_prs if "research-approved" in labels(pr)]

    # Multiple candidates are a queue, not a control-plane blocker. The integration
    # scheduler promotes at most one per cycle after objective check validation.
    if len(queued_for_automatic_promotion) > 1:
        warnings.append(
            f"{len(queued_for_automatic_promotion)} automatic paper-promotion candidates are queued; "
            "integration-merge will process one per cycle"
        )
    if validation_relation == "diverged":
        blockers.append("paper-validated is not equal to or an ancestor of main")
    elif validation_relation == "pending_validation":
        warnings.append("main is ahead of paper-validated; exact-SHA post-merge validation is pending")

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
            "failure", "cancelled", "timed_out", "action_required",
        }:
            blockers.append(f"critical scheduler {item.get('id')} latest production state is {state}")
        elif run is None:
            warnings.append(f"no non-PR workflow run is visible yet for {item.get('id')}")

    selected_next = queued_for_automatic_promotion[0].get("number") if queued_for_automatic_promotion else None
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
        f"- promotion policy: `{manifest.get('promotion_policy')}`",
        f"- open PRs: {len(prs)}",
        f"- research PRs: {len(research_prs)}",
        f"- research PRs carrying legacy evidence label: {len(research_with_evidence_label)}",
        f"- integration PRs: {len(integration_prs)}",
        f"- automatic promotion queue: {len(queued_for_automatic_promotion)}",
        f"- next deterministic candidate: {('#' + str(selected_next)) if selected_next else 'none'}",
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
    lines.extend(f"- {item}" for item in sorted(set(blockers))) if blockers else lines.append("- none")
    lines.extend(["", "## Warnings"])
    lines.extend(f"- {item}" for item in sorted(set(warnings))) if warnings else lines.append("- none")

    lines.extend(["", "## Promotion boundary"])
    lines.append(
        "Paper champion promotion is automatic: integration-merge owns the merge after green objective checks and "
        "numbered research provenance. The supervisor itself remains read-only. Exact-SHA validation and "
        "paper-validated deployment remain separate. Authenticated real-money execution is not authorized."
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
            "research_evidence_labeled": len(research_with_evidence_label),
            "integration_prs": len(integration_prs),
            "automatic_promotion_queue": len(queued_for_automatic_promotion),
            "branches": len(branches),
        },
        "next_automatic_promotion_pr": selected_next,
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
    parser.add_argument("--validation-relation", choices=("current", "pending_validation", "diverged"), required=True)
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
        registry, runs, prs, branches, manifest,
        args.main_sha, args.validated_sha, args.validation_relation,
    )
    Path(args.markdown).write_text(report, encoding="utf-8")
    Path(args.json_output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(report, end="")
    return 1 if payload["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
