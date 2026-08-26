#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REQUIRED_IDS = {
    "administrator-supervisor", "research-policy", "research-queue", "promotion-controller",
    "integration-merge", "control-plane-event-bridge", "post-merge-validation", "code-validation",
    "monitoring-validation", "live-paper-validation", "paper-server-deploy", "paper-server-health",
    "forward-maker-research", "alpha-factory", "meta-supervisor", "fast-arb-shadow-research",
    "arb-theory-research", "external-intelligence", "live-api-smoke", "v7-point-in-time-universe-archive",
}
PRIVATE_VALIDATION_WORKFLOW = ".github/workflows/private-runtime-single-writer-validation.yml"
OPERATOR_AUTHORITY_WORKFLOW = ".github/workflows/operator-authority-gate.yml"
NON_SCHEDULER_WORKFLOWS = {
    ".github/workflows/grafana-access.yml",
    PRIVATE_VALIDATION_WORKFLOW,
    OPERATOR_AUTHORITY_WORKFLOW,
}
LEGACY_WORKFLOWS = {
    ".github/workflows/v4-live-smoke.yml",
    ".github/workflows/v6-market-cache-relay.yml",
    ".github/workflows/v6-research-smoke.yml",
    ".github/workflows/v7-cross-sectional-ranking-research.yml",
    ".github/workflows/v7-unified-paper-evidence.yml",
}
NON_SCHEDULER_FORBIDDEN_TOKENS = (
    "gh pr merge",
    "git push origin HEAD:main",
    "git push origin main",
    "git push origin paper-validated",
    "POLYMARKET_DEPLOY_REF=",
)


def workflow_job_ids(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    marker = "\njobs:\n"
    if text.startswith("jobs:\n"):
        tail = text[len("jobs:\n"):]
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


def require_tokens(errors: list[str], label: str, text: str, required: tuple[str, ...]) -> None:
    for token in required:
        if token not in text:
            errors.append(f"{label} is missing contract: {token}")


def forbid_tokens(errors: list[str], label: str, text: str, forbidden: tuple[str, ...]) -> None:
    for token in forbidden:
        if token in text:
            errors.append(f"{label} contains forbidden authority/legacy token: {token}")


def validate(root: Path, registry_path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    data = load_registry(registry_path)
    if data.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    administrator = data.get("administrator")
    if not isinstance(administrator, dict):
        errors.append("administrator must be an object")
    else:
        if administrator.get("live_champion_manifest") != "config/live_champion.json":
            errors.append("administrator.live_champion_manifest must select config/live_champion.json")
        if administrator.get("paper_promotion_mode") != "automatic_objective_gates":
            errors.append("administrator.paper_promotion_mode must be automatic_objective_gates")
        if administrator.get("manual_approval_required") is not False:
            errors.append("administrator.manual_approval_required must be false for paper promotion")

    schedulers = data.get("schedulers")
    if not isinstance(schedulers, list):
        return errors + ["schedulers must be a list"], []

    required_fields = {
        "id", "workflow", "workflow_name", "job", "cadence", "responsibility",
        "critical", "merge_authority", "deploy_authority", "validation_dispatch_authority",
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
        if workflow in LEGACY_WORKFLOWS:
            errors.append(f"legacy workflow must not be registered: {workflow}")
        path = root / workflow
        if not workflow.startswith(".github/workflows/") or not workflow.endswith((".yml", ".yaml")):
            errors.append(f"invalid workflow path for {scheduler_id}: {workflow}")
            continue
        if not path.is_file():
            errors.append(f"registered workflow does not exist: {workflow}")
            continue
        jobs = workflow_job_ids(path)
        if jobs != [expected_job]:
            errors.append(f"{workflow} must contain exactly one job named {expected_job}; found {jobs or 'none'}")
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
    present_legacy = sorted(actual_workflows.intersection(LEGACY_WORKFLOWS))
    if present_legacy:
        errors.append("legacy workflows still present: " + ", ".join(present_legacy))

    for relative in sorted(NON_SCHEDULER_WORKFLOWS.intersection(actual_workflows)):
        path = root / relative
        text = path.read_text(encoding="utf-8")
        if workflow_has_periodic_schedule(path):
            errors.append(f"explicit non-scheduler workflow must not define a periodic schedule: {relative}")
        forbid_tokens(errors, relative, text, NON_SCHEDULER_FORBIDDEN_TOKENS)
        if relative == PRIVATE_VALIDATION_WORKFLOW:
            require_tokens(errors, "private runtime validation", text, ("\n  workflow_dispatch:\n", "\n  pull_request:\n", "permissions:\n  contents: read\n"))
            for forbidden_trigger in ("\n  push:\n", "\n  workflow_run:\n", "\n  repository_dispatch:\n"):
                if forbidden_trigger in text:
                    errors.append(f"private runtime validation contains forbidden trigger: {forbidden_trigger.strip()}")
            forbid_tokens(errors, "private runtime validation", text, ("actions: write", "contents: write", "issues: write", "pull-requests: write"))
        if relative == OPERATOR_AUTHORITY_WORKFLOW:
            require_tokens(errors, "operator authority gate", text, ("\n  pull_request_target:\n", "config/operator_directives.json", "scripts/hard_safety_policy.py", "tests/test_v7_authorized_paper_envelope.py"))

    managed_workflows = actual_workflows.difference(NON_SCHEDULER_WORKFLOWS)
    unregistered = sorted(managed_workflows.difference(workflows))
    stale = sorted(workflows.difference(actual_workflows))
    if unregistered:
        errors.append("unregistered workflows: " + ", ".join(unregistered))
    if stale:
        errors.append("registry references missing workflows: " + ", ".join(stale))

    merge_ids = [str(i["id"]) for i in normalized if i["merge_authority"] is True]
    deploy_ids = [str(i["id"]) for i in normalized if i["deploy_authority"] is True]
    dispatch_ids = [str(i["id"]) for i in normalized if i["validation_dispatch_authority"] is True]
    if merge_ids != ["integration-merge"]:
        errors.append(f"merge authority must belong only to integration-merge; found {merge_ids}")
    if deploy_ids != ["paper-server-deploy"]:
        errors.append(f"deploy authority must belong only to paper-server-deploy; found {deploy_ids}")
    if dispatch_ids != ["post-merge-validation"]:
        errors.append(f"validation dispatch authority must belong only to post-merge-validation; found {dispatch_ids}")

    by_id = {str(i["id"]): i for i in normalized}
    controller = by_id.get("promotion-controller")
    if controller:
        text = (root / str(controller["workflow"])).read_text(encoding="utf-8")
        require_tokens(errors, "promotion-controller", text, ("scripts/promotion_gate.py", "config/promotion_policy.json", "autonomous-promotion-approved", "source-match-files.txt", "economic_source_content_mismatch"))
        forbid_tokens(errors, "promotion-controller", text, ("gh pr merge", "POLYMARKET_DEPLOY_REF=", "git push origin paper-validated"))

    integration = by_id.get("integration-merge")
    if integration:
        text = (root / str(integration["workflow"])).read_text(encoding="utf-8")
        require_tokens(errors, "integration-merge", text, ('gh pr merge "$PR_NUMBER" --squash --delete-branch', "autonomous-promotion-approved", "--require-approval-label", "scripts/promotion_gate.py", "--match-head-commit", "baseRefOid", "source-match-files.txt", "champion-integration-merged"))
        forbid_tokens(errors, "integration-merge", text, ("--admin", "incumbent_health_gate.py"))

    bridge = by_id.get("control-plane-event-bridge")
    if bridge:
        text = (root / str(bridge["workflow"])).read_text(encoding="utf-8")
        require_tokens(errors, "control-plane-event-bridge", text, ("workflow_run:", '"ci"', '"monitoring"', '"v7-live-paper-validation"', '"Private runtime single-writer validation"', '"Polymarket Promotion Controller"'))
        forbid_tokens(errors, "control-plane-event-bridge", text, ("gh pr merge", "git push origin paper-validated", "v4-live-paper-smoke"))

    meta = by_id.get("meta-supervisor")
    if meta:
        text = (root / str(meta["workflow"])).read_text(encoding="utf-8")
        if "promotion-controller.yml" not in text:
            errors.append("meta-supervisor must be able to dispatch the promotion decision controller")
        case_match = re.search(r"case \"\$workflow\" in(?P<body>.*?)esac", text, re.S)
        if case_match and "integration-merge.yml" in case_match.group("body"):
            errors.append("meta-supervisor must not dispatch the merge authority directly")

    post_merge = by_id.get("post-merge-validation")
    if post_merge:
        text = (root / str(post_merge["workflow"])).read_text(encoding="utf-8")
        require_tokens(errors, "post-merge-validation", text, ("ci.yml monitoring.yml v7-live-paper-validation.yml", '-f expected_sha="$EXPECTED_SHA"', 'test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"'))
        forbid_tokens(errors, "post-merge-validation", text, ("gh pr merge", "v4-live-smoke.yml"))

    for scheduler_id in ("code-validation", "monitoring-validation", "live-paper-validation"):
        item = by_id.get(scheduler_id)
        if item:
            text = (root / str(item["workflow"])).read_text(encoding="utf-8")
            require_tokens(errors, scheduler_id, text, ("expected_sha:", "VALIDATION_SHA", 'test "$(git rev-parse HEAD)" = "$VALIDATION_SHA"'))

    live_validation = by_id.get("live-paper-validation")
    if live_validation:
        text = (root / str(live_validation["workflow"])).read_text(encoding="utf-8")
        require_tokens(errors, "live-paper-validation", text, ('test "$validated_sha" = "$main_sha"', '-f sha="$validated_sha" -F force=false', "V7 validation refuses non-V7 champion", "authenticated_execution"))
        forbid_tokens(errors, "live-paper-validation", text, ("paper_v5", "paper_v6", "v4-live-paper-smoke", "v7_champion_candidate"))

    return errors, normalized


def render_report(items: list[dict[str, Any]], errors: list[str]) -> str:
    lines = [
        "# Scheduler registry validation", "", f"- schedulers: {len(items)}", f"- errors: {len(errors)}", "",
        "| Scheduler | Job | Cadence | Responsibility | Merge | Deploy | Validation dispatch |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for item in items:
        lines.append(
            f"| {item['id']} | {item['job']} | {item['cadence']} | {item['responsibility']} | "
            f"{str(bool(item['merge_authority'])).lower()} | {str(bool(item['deploy_authority'])).lower()} | "
            f"{str(bool(item['validation_dispatch_authority'])).lower()} |"
        )
    if errors:
        lines.extend(["", "## Errors"] + [f"- {error}" for error in errors])
    else:
        lines.extend(["", "Registry and one-job-per-workflow contract are valid. V7 is the sole runtime generation."])
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
