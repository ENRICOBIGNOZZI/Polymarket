#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REQUIRED_IDS = {
    "administrator-supervisor",
    "research-policy",
    "research-queue",
    "integration-merge",
    "post-merge-validation",
    "code-validation",
    "monitoring-validation",
    "live-paper-validation",
    "paper-server-deploy",
    "paper-server-health",
    "forward-maker-research",
    "alpha-factory",
    "meta-supervisor",
    "fast-arb-shadow-research",
    "arb-theory-research",
    "live-api-smoke",
}

NON_SCHEDULER_WORKFLOWS = {
    ".github/workflows/grafana-access.yml",
}


def workflow_job_ids(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    marker = "\njobs:\n"
    if text.startswith("jobs:\n"):
        tail = text[len("jobs:\n") :]
    elif marker in text:
        tail = text.split(marker, 1)[1]
    else:
        return []
    return re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", tail)


def workflow_has_periodic_schedule(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return bool(
        re.search(r"(?m)^  schedule:\s*$", text)
        and re.search(r"(?m)^    - cron:\s*['\"]?[^'\"\n]+['\"]?\s*$", text)
    )


def load_registry(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("registry root must be an object")
    return data


def validate(root: Path, registry_path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    data = load_registry(registry_path)
    if data.get("schema_version") != 1:
        errors.append("schema_version must equal 1")

    administrator = data.get("administrator")
    if not isinstance(administrator, dict):
        errors.append("administrator must be an object")
    else:
        if administrator.get("approval_label") != "administrator-approved":
            errors.append("administrator.approval_label must be administrator-approved")
        if administrator.get("live_champion_manifest") != "config/live_champion.json":
            errors.append("administrator.live_champion_manifest must select config/live_champion.json")

    schedulers = data.get("schedulers")
    if not isinstance(schedulers, list):
        return errors + ["schedulers must be a list"], []

    required_fields = {
        "id",
        "workflow",
        "workflow_name",
        "job",
        "cadence",
        "responsibility",
        "critical",
        "merge_authority",
        "deploy_authority",
        "validation_dispatch_authority",
    }
    ids: set[str] = set()
    workflows: set[str] = set()
    normalized: list[dict[str, Any]] = []

    for index, raw in enumerate(schedulers):
        if not isinstance(raw, dict):
            errors.append(f"scheduler[{index}] must be an object")
            continue
        missing = sorted(required_fields.difference(raw))
        if missing:
            errors.append(f"scheduler[{index}] is missing fields: {', '.join(missing)}")
            continue
        item = dict(raw)
        scheduler_id = str(item["id"])
        workflow = str(item["workflow"])
        expected_job = str(item["job"])
        if scheduler_id in ids:
            errors.append(f"duplicate scheduler id: {scheduler_id}")
        ids.add(scheduler_id)
        if workflow in workflows:
            errors.append(f"duplicate workflow registration: {workflow}")
        workflows.add(workflow)
        if not workflow.startswith(".github/workflows/") or not workflow.endswith((".yml", ".yaml")):
            errors.append(f"invalid workflow path for {scheduler_id}: {workflow}")
            continue
        path = root / workflow
        if not path.is_file():
            errors.append(f"registered workflow does not exist: {workflow}")
            continue
        job_ids = workflow_job_ids(path)
        if job_ids != [expected_job]:
            errors.append(
                f"{workflow} must contain exactly one job named {expected_job}; found {job_ids or 'none'}"
            )
        if not workflow_has_periodic_schedule(path):
            errors.append(f"{workflow} must define a periodic schedule/cron trigger")
        normalized.append(item)

    missing_ids = sorted(REQUIRED_IDS.difference(ids))
    extra_ids = sorted(ids.difference(REQUIRED_IDS))
    if missing_ids:
        errors.append("missing scheduler ids: " + ", ".join(missing_ids))
    if extra_ids:
        errors.append("unrecognized scheduler ids: " + ", ".join(extra_ids))

    workflow_dir = root / ".github" / "workflows"
    actual_workflows = {
        str(path.relative_to(root))
        for path in workflow_dir.iterdir()
        if path.is_file() and path.suffix in {".yml", ".yaml"}
    }
    for relative in sorted(NON_SCHEDULER_WORKFLOWS.intersection(actual_workflows)):
        text = (root / relative).read_text(encoding="utf-8")
        if re.search(r"(?m)^\s{2}schedule:\s*$", text):
            errors.append(f"non-scheduler workflow unexpectedly has a schedule trigger: {relative}")
    managed_workflows = actual_workflows.difference(NON_SCHEDULER_WORKFLOWS)
    unregistered = sorted(managed_workflows.difference(workflows))
    stale = sorted(workflows.difference(actual_workflows))
    if unregistered:
        errors.append("unregistered workflows: " + ", ".join(unregistered))
    if stale:
        errors.append("registry references missing workflows: " + ", ".join(stale))

    merge_ids = [str(item["id"]) for item in normalized if item["merge_authority"] is True]
    deploy_ids = [str(item["id"]) for item in normalized if item["deploy_authority"] is True]
    dispatch_ids = [
        str(item["id"]) for item in normalized if item["validation_dispatch_authority"] is True
    ]
    if merge_ids != ["integration-merge"]:
        errors.append(f"merge authority must belong only to integration-merge; found {merge_ids}")
    if deploy_ids != ["paper-server-deploy"]:
        errors.append(f"deploy authority must belong only to paper-server-deploy; found {deploy_ids}")
    if dispatch_ids != ["post-merge-validation"]:
        errors.append(
            "validation dispatch authority must belong only to post-merge-validation; "
            f"found {dispatch_ids}"
        )

    by_id = {str(item["id"]): item for item in normalized}
    admin = by_id.get("administrator-supervisor")
    if admin:
        admin_text = (root / str(admin["workflow"])).read_text(encoding="utf-8")
        for forbidden in (
            "gh pr merge",
            "gh workflow run",
            "repository_dispatch",
            "POLYMARKET_DEPLOY_REF=",
            "git push origin paper-validated",
        ):
            if forbidden in admin_text:
                errors.append(f"administrator-supervisor contains forbidden mutation: {forbidden}")

    integration = by_id.get("integration-merge")
    if integration:
        integration_text = (root / str(integration["workflow"])).read_text(encoding="utf-8")
        if 'gh pr merge "$PR_NUMBER" --squash --delete-branch' not in integration_text:
            errors.append("integration-merge must use a bounded squash merge without admin bypass")
        if "--admin" in integration_text:
            errors.append("integration-merge must never use --admin")
        if "administrator-approved" not in integration_text:
            errors.append("integration-merge must require administrator-approved")
        if "gh workflow run" in integration_text:
            errors.append("integration-merge must hand off validation instead of dispatching it directly")
        for required in (
            "BASE_MAIN_SHA",
            "BASE_VALIDATED_SHA",
            "candidate-final.json",
            "--match-head-commit",
            "current_main_after_merge",
            '"event_type": "champion-integration-merged"',
        ):
            if required not in integration_text:
                errors.append(f"integration-merge is missing race-safe contract: {required}")

    post_merge = by_id.get("post-merge-validation")
    if post_merge:
        post_text = (root / str(post_merge["workflow"])).read_text(encoding="utf-8")
        if "ci.yml monitoring.yml v4-live-smoke.yml" not in post_text:
            errors.append("post-merge-validation must dispatch CI, monitoring and live-paper validation")
        if "gh pr merge" in post_text:
            errors.append("post-merge-validation must not merge pull requests")
        if '-f expected_sha="$EXPECTED_SHA"' not in post_text:
            errors.append("post-merge-validation must pass the exact merged SHA to every validator")
        if 'test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"' not in post_text:
            errors.append("post-merge-validation must checkout and verify the exact merged SHA")

    for scheduler_id in ("code-validation", "monitoring-validation", "live-paper-validation"):
        item = by_id.get(scheduler_id)
        if not item:
            continue
        text = (root / str(item["workflow"])).read_text(encoding="utf-8")
        if "expected_sha:" not in text:
            errors.append(f"{scheduler_id} must accept expected_sha on workflow_dispatch")
        if "VALIDATION_SHA" not in text:
            errors.append(f"{scheduler_id} must bind execution to VALIDATION_SHA")
        if 'test "$(git rev-parse HEAD)" = "$VALIDATION_SHA"' not in text:
            errors.append(f"{scheduler_id} must verify its exact checkout revision")

    live_validation = by_id.get("live-paper-validation")
    if live_validation:
        live_text = (root / str(live_validation["workflow"])).read_text(encoding="utf-8")
        if 'test "$validated_sha" = "$main_sha"' not in live_text:
            errors.append("live-paper validation must refuse to advance a stale main revision")
        if '-f sha="$validated_sha" -F force=false' not in live_text:
            errors.append("live-paper validation must advance paper-validated to the tested SHA only")

    return errors, normalized


def render_report(items: list[dict[str, Any]], errors: list[str]) -> str:
    lines = [
        "# Scheduler registry validation",
        "",
        f"- schedulers: {len(items)}",
        f"- errors: {len(errors)}",
        "",
        "| Scheduler | Job | Cadence | Responsibility | Merge | Deploy | Validation dispatch |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for item in items:
        lines.append(
            "| {id} | `{job}` | {cadence} | {responsibility} | {merge} | {deploy} | {dispatch} |".format(
                id=item["id"],
                job=item["job"],
                cadence=str(item["cadence"]).replace("|", "/"),
                responsibility=str(item["responsibility"]).replace("|", "/"),
                merge="yes" if item["merge_authority"] else "no",
                deploy="yes" if item["deploy_authority"] else "no",
                dispatch="yes" if item["validation_dispatch_authority"] else "no",
            )
        )
    if errors:
        lines.extend(["", "## Errors"])
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.extend(
            [
                "",
                "Registry and one-job-per-workflow contract are valid. Every registered scheduler has a periodic schedule trigger.",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the Polymarket scheduler control plane")
    parser.add_argument("--root", default=".")
    parser.add_argument("--registry", default="config/scheduler_registry.json")
    parser.add_argument("--output")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    registry_path = Path(args.registry)
    if not registry_path.is_absolute():
        registry_path = root / registry_path
    try:
        errors, items = validate(root, registry_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors, items = [str(exc)], []
    report = render_report(items, errors)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    print(report, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
