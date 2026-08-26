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
    "arb-theory-research", "external-intelligence", "live-api-smoke",
    "v7-cross-sectional-ranking-research", "v7-point-in-time-universe-archive",
    "v7-unified-paper-evidence", "v7-market-cache-relay",
}
PRIVATE_VALIDATION_WORKFLOW = ".github/workflows/private-runtime-single-writer-validation.yml"
OPERATOR_AUTHORITY_WORKFLOW = ".github/workflows/operator-authority-gate.yml"
NON_SCHEDULER_WORKFLOWS = {
    ".github/workflows/grafana-access.yml",
    PRIVATE_VALIDATION_WORKFLOW,
    OPERATOR_AUTHORITY_WORKFLOW,
}
NON_SCHEDULER_FORBIDDEN_TOKENS = (
    "gh pr merge", "git push origin HEAD:main", "git push origin main",
    "git push origin paper-validated", "POLYMARKET_DEPLOY_REF=",
)
RETIRED_TOKENS = (
    "paper_v3", "paper_v4", "paper_v5", "paper_v6",
    "exporter_v4", "exporter_v5", "exporter_v6",
    "v4-live-paper-smoke", "v4-live-smoke.yml",
    "v6-live-data-research", "v6-market-cache-relay",
    "scripts/v4_", "scripts/v5_", "scripts/v6_",
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
    return bool(re.search(r"(?m)^  schedule:\s*$", text) and re.search(r"(?m)^    - cron:\s*['\"]?[^'\"\n]+['\"]?\s*$", text))


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
        if administrator.get("live_champion_manifest") != "config/live_champion.json":
            errors.append("administrator.live_champion_manifest must select config/live_champion.json")
        if administrator.get("paper_promotion_mode") != "automatic_objective_gates":
            errors.append("administrator.paper_promotion_mode must be automatic_objective_gates")
        if administrator.get("manual_approval_required") is not False:
            errors.append("administrator.manual_approval_required must be false")

    schedulers = data.get("schedulers")
    if not isinstance(schedulers, list):
        return errors + ["schedulers must be a list"], []
    required_fields = {"id","workflow","workflow_name","job","cadence","responsibility","critical","merge_authority","deploy_authority","validation_dispatch_authority"}
    ids: set[str] = set(); workflows: set[str] = set(); normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(schedulers):
        if not isinstance(raw, dict):
            errors.append(f"scheduler[{index}] must be an object"); continue
        missing = sorted(required_fields.difference(raw))
        if missing:
            errors.append(f"scheduler[{index}] missing fields: {', '.join(missing)}"); continue
        item = dict(raw); sid = str(item["id"]); workflow = str(item["workflow"]); job = str(item["job"])
        if sid in ids: errors.append(f"duplicate scheduler id: {sid}")
        if workflow in workflows: errors.append(f"duplicate workflow registration: {workflow}")
        ids.add(sid); workflows.add(workflow)
        path = root / workflow
        if not workflow.startswith(".github/workflows/") or not workflow.endswith((".yml", ".yaml")):
            errors.append(f"invalid workflow path for {sid}: {workflow}"); continue
        if not path.is_file():
            errors.append(f"registered workflow does not exist: {workflow}"); continue
        jobs = workflow_job_ids(path)
        if jobs != [job]: errors.append(f"{workflow} must contain exactly one job named {job}; found {jobs or 'none'}")
        if not workflow_has_periodic_schedule(path): errors.append(f"{workflow} must define periodic schedule")
        normalized.append(item)

    if REQUIRED_IDS.difference(ids): errors.append("missing scheduler ids: " + ", ".join(sorted(REQUIRED_IDS.difference(ids))))
    if ids.difference(REQUIRED_IDS): errors.append("unrecognized scheduler ids: " + ", ".join(sorted(ids.difference(REQUIRED_IDS))))

    workflow_dir = root / ".github" / "workflows"
    actual = {str(path.relative_to(root)) for path in workflow_dir.iterdir() if path.is_file() and path.suffix in {".yml", ".yaml"}}
    for relative in sorted(NON_SCHEDULER_WORKFLOWS.intersection(actual)):
        path = root / relative; text = path.read_text(encoding="utf-8")
        if workflow_has_periodic_schedule(path): errors.append(f"non-scheduler workflow must not schedule: {relative}")
        for forbidden in NON_SCHEDULER_FORBIDDEN_TOKENS:
            if forbidden in text: errors.append(f"non-scheduler workflow contains forbidden authority: {relative}: {forbidden}")
        if relative == PRIVATE_VALIDATION_WORKFLOW:
            if "\n  workflow_dispatch:\n" not in text or "\n  pull_request:\n" not in text:
                errors.append("private runtime validation must remain workflow_dispatch/pull_request scoped")
            for forbidden in ("\n  push:\n", "\n  workflow_run:\n", "\n  repository_dispatch:\n"):
                if forbidden in text: errors.append(f"private runtime validation contains forbidden trigger: {forbidden.strip()}")
            if "permissions:\n  contents: read\n" not in text: errors.append("private runtime validation must keep contents read-only")
        if relative == OPERATOR_AUTHORITY_WORKFLOW:
            if "\n  pull_request_target:\n" not in text: errors.append("operator authority gate must use pull_request_target")
            if "permissions:\n  contents: read\n  pull-requests: read\n" not in text: errors.append("operator authority gate must stay read-only")

    managed = actual.difference(NON_SCHEDULER_WORKFLOWS)
    if managed.difference(workflows): errors.append("unregistered workflows: " + ", ".join(sorted(managed.difference(workflows))))
    if workflows.difference(actual): errors.append("registry references missing workflows: " + ", ".join(sorted(workflows.difference(actual))))

    merge_ids = [str(i["id"]) for i in normalized if i["merge_authority"] is True]
    deploy_ids = [str(i["id"]) for i in normalized if i["deploy_authority"] is True]
    dispatch_ids = [str(i["id"]) for i in normalized if i["validation_dispatch_authority"] is True]
    if merge_ids != ["integration-merge"]: errors.append(f"merge authority must belong only to integration-merge; found {merge_ids}")
    if deploy_ids != ["paper-server-deploy"]: errors.append(f"deploy authority must belong only to paper-server-deploy; found {deploy_ids}")
    if dispatch_ids != ["post-merge-validation"]: errors.append(f"validation dispatch authority must belong only to post-merge-validation; found {dispatch_ids}")
    by_id = {str(i["id"]): i for i in normalized}

    bridge = by_id.get("control-plane-event-bridge")
    if bridge:
        text = (root / str(bridge["workflow"])).read_text(encoding="utf-8")
        for required in ('"ci"','"monitoring"','"V7 live PAPER smoke"','"Private runtime single-writer validation"','"Polymarket Promotion Controller"',"gh workflow run promotion-controller.yml --ref main","gh workflow run integration-merge.yml --ref main"):
            if required not in text: errors.append(f"control-plane-event-bridge missing contract: {required}")

    post_merge = by_id.get("post-merge-validation")
    if post_merge:
        text = (root / str(post_merge["workflow"])).read_text(encoding="utf-8")
        if "ci.yml monitoring.yml v7-live-paper-smoke.yml" not in text: errors.append("post-merge-validation must dispatch V7 validators")
        if '-f expected_sha="$EXPECTED_SHA"' not in text: errors.append("post-merge-validation must pass exact SHA")

    live_validation = by_id.get("live-paper-validation")
    if live_validation:
        text = (root / str(live_validation["workflow"])).read_text(encoding="utf-8")
        if "name: V7 live PAPER smoke" not in text: errors.append("live-paper validation must be V7-only")
        if 'test "$validated_sha" = "$main_sha"' not in text: errors.append("live-paper validation must bind paper-validated to main")
        if '-f sha="$validated_sha" -F force=false' not in text: errors.append("live-paper validation must fast-forward paper-validated only")

    deploy = by_id.get("paper-server-deploy")
    if deploy:
        text = (root / str(deploy["workflow"])).read_text(encoding="utf-8")
        if 'workflows: ["V7 live PAPER smoke"]' not in text: errors.append("deploy must trigger only from V7 live PAPER smoke")
        if "champion_version=7" not in text: errors.append("deploy verifier must assert V7 champion")

    for sid in ("code-validation", "monitoring-validation", "live-paper-validation"):
        item = by_id.get(sid)
        if not item: continue
        text = (root / str(item["workflow"])).read_text(encoding="utf-8")
        if "expected_sha:" not in text or "VALIDATION_SHA" not in text: errors.append(f"{sid} must accept expected_sha")
        if 'test "$(git rev-parse HEAD)" = "$VALIDATION_SHA"' not in text: errors.append(f"{sid} must verify exact checkout")

    for base in (root / "ops", root / ".github" / "workflows", root / "monitoring"):
        for path in base.rglob("*"):
            if not path.is_file(): continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for token in RETIRED_TOKENS:
                if token in text: errors.append(f"retired compatibility reference remains: {path.relative_to(root)}: {token}")
    registry_text = registry_path.read_text(encoding="utf-8")
    for token in RETIRED_TOKENS:
        if token in registry_text: errors.append(f"retired compatibility reference remains in registry: {token}")
    return errors, normalized


def render_report(registry_path: Path, errors: list[str], schedulers: list[dict[str, Any]]) -> str:
    lines = ["# Scheduler Registry Validation", "", f"- Registry: `{registry_path}`", f"- Registered schedulers: **{len(schedulers)}**", f"- Status: **{'PASS' if not errors else 'FAIL'}**", ""]
    if errors:
        lines.append("## Errors"); lines.extend(f"- {error}" for error in errors)
    else:
        lines.extend(["## Authority", "- Merge: `integration-merge` only.", "- Deployment: `paper-server-deploy` only.", "- Validation dispatch: `post-merge-validation` only.", "- Runtime/version surface: V7 only."])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--registry", type=Path, default=Path("config/scheduler_registry.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve(); registry = args.registry if args.registry.is_absolute() else root / args.registry
    errors, schedulers = validate(root, registry)
    report = render_report(registry.relative_to(root), errors, schedulers)
    if args.output: args.output.write_text(report, encoding="utf-8")
    else: print(report, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
