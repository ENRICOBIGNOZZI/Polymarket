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
    "monitoring-validation", "alpha-factory", "meta-supervisor", "fast-arb-shadow-research",
    "arb-theory-research", "external-intelligence", "live-api-smoke",
    "v7-cross-sectional-ranking-research", "v7-point-in-time-universe-archive",
    "v7-unified-paper-evidence", "v7-paper-deploy", "v7-server-health",
}
NON_SCHEDULER_WORKFLOWS = {
    ".github/workflows/private-runtime-single-writer-validation.yml",
    ".github/workflows/operator-authority-gate.yml",
}


def job_ids(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    tail = text.split("\njobs:\n", 1)[1] if "\njobs:\n" in text else ""
    return re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", tail)


def has_schedule(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return "\n  schedule:\n" in text and bool(re.search(r"(?m)^    - cron:", text))


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def validate(root: Path, registry_path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    data = load(registry_path)
    if data.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    admin = data.get("administrator") if isinstance(data.get("administrator"), dict) else {}
    if admin.get("live_champion_manifest") != "config/live_champion.json":
        errors.append("administrator.live_champion_manifest must select config/live_champion.json")
    if admin.get("paper_promotion_mode") != "automatic_objective_gates":
        errors.append("administrator.paper_promotion_mode must be automatic_objective_gates")
    if admin.get("manual_approval_required") is not False:
        errors.append("administrator.manual_approval_required must be false")

    champion: dict[str, Any] = {}
    champion_enabled = False
    champion_path = root / str(admin.get("live_champion_manifest") or "config/live_champion.json")
    try:
        champion = load(champion_path)
        champion_enabled = champion.get("enabled") is True
    except Exception as exc:
        errors.append(f"live champion manifest invalid: {exc}")
    if champion_enabled:
        if champion.get("version") != 7:
            errors.append("enabled champion must be V7")
        if champion.get("paper_only") is not True or champion.get("authenticated_execution") is not False:
            errors.append("enabled champion must preserve PAPER/authenticated-execution boundary")
        if champion.get("deployment_ref") != "paper-validated":
            errors.append("enabled champion must deploy from paper-validated")

    raw_items = data.get("schedulers")
    if not isinstance(raw_items, list):
        return errors + ["schedulers must be a list"], []
    required_fields = {
        "id", "workflow", "workflow_name", "job", "cadence", "responsibility", "critical",
        "merge_authority", "deploy_authority", "validation_dispatch_authority",
    }
    items: list[dict[str, Any]] = []
    ids: set[str] = set()
    workflows: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append(f"scheduler[{index}] must be an object")
            continue
        missing = sorted(required_fields.difference(raw))
        if missing:
            errors.append(f"scheduler[{index}] missing: {', '.join(missing)}")
            continue
        item = dict(raw)
        sid = str(item["id"])
        rel = str(item["workflow"])
        if sid in ids:
            errors.append(f"duplicate scheduler id: {sid}")
        if rel in workflows:
            errors.append(f"duplicate workflow registration: {rel}")
        ids.add(sid); workflows.add(rel)
        path = root / rel
        if not path.is_file():
            errors.append(f"registered workflow does not exist: {rel}")
            continue
        observed_jobs = job_ids(path)
        if observed_jobs != [str(item["job"])]:
            errors.append(f"{rel} must contain exactly one job named {item['job']}; found {observed_jobs}")
        if not has_schedule(path):
            errors.append(f"{rel} must retain a periodic recovery schedule")
        items.append(item)

    if ids != REQUIRED_IDS:
        missing = sorted(REQUIRED_IDS.difference(ids))
        extra = sorted(ids.difference(REQUIRED_IDS))
        if missing: errors.append("missing scheduler ids: " + ", ".join(missing))
        if extra: errors.append("unrecognized scheduler ids: " + ", ".join(extra))

    workflow_dir = root / ".github" / "workflows"
    actual = {str(p.relative_to(root)) for p in workflow_dir.iterdir() if p.is_file() and p.suffix in {".yml", ".yaml"}}
    managed = actual.difference(NON_SCHEDULER_WORKFLOWS)
    if managed.difference(workflows):
        errors.append("unregistered workflows: " + ", ".join(sorted(managed.difference(workflows))))
    if workflows.difference(actual):
        errors.append("registry references missing workflows: " + ", ".join(sorted(workflows.difference(actual))))

    merge_ids = [str(i["id"]) for i in items if i["merge_authority"] is True]
    deploy_ids = [str(i["id"]) for i in items if i["deploy_authority"] is True]
    dispatch_ids = [str(i["id"]) for i in items if i["validation_dispatch_authority"] is True]
    if merge_ids != ["integration-merge"]:
        errors.append(f"merge authority must belong only to integration-merge; found {merge_ids}")
    if champion_enabled:
        if deploy_ids != ["v7-paper-deploy"]:
            errors.append(f"enabled V7 champion requires sole deploy authority v7-paper-deploy; found {deploy_ids}")
    elif deploy_ids:
        errors.append(f"no deployment authority is allowed while champion is disabled; found {deploy_ids}")
    if dispatch_ids != ["post-merge-validation"]:
        errors.append(f"validation dispatch authority must belong only to post-merge-validation; found {dispatch_ids}")

    by_id = {str(i["id"]): i for i in items}
    controller = root / str(by_id.get("promotion-controller", {}).get("workflow", ""))
    if controller.is_file():
        text = controller.read_text(encoding="utf-8")
        for required in ("scripts/promotion_gate.py", "autonomous-promotion-approved", "source-match-files.txt"):
            if required not in text: errors.append(f"promotion-controller missing contract: {required}")
        if "gh pr merge" in text: errors.append("promotion-controller must not merge")

    integration = root / str(by_id.get("integration-merge", {}).get("workflow", ""))
    if integration.is_file():
        text = integration.read_text(encoding="utf-8")
        if 'gh pr merge "$PR_NUMBER" --squash --delete-branch' not in text:
            errors.append("integration-merge must use bounded squash merge")
        if "--admin" in text: errors.append("integration-merge must never use --admin")

    bridge = root / str(by_id.get("control-plane-event-bridge", {}).get("workflow", ""))
    if bridge.is_file():
        text = bridge.read_text(encoding="utf-8")
        for required in ('"ci"', '"monitoring"', '"Private runtime single-writer validation"', '"Polymarket Promotion Controller"',
                         "gh workflow run promotion-controller.yml --ref main", "gh workflow run integration-merge.yml --ref main"):
            if required not in text: errors.append(f"control-plane-event-bridge missing contract: {required}")
        for forbidden in ('"v4-live-paper-smoke"', "gh pr merge", "git push origin paper-validated"):
            if forbidden in text: errors.append(f"control-plane-event-bridge contains retired/forbidden authority: {forbidden}")

    post = root / str(by_id.get("post-merge-validation", {}).get("workflow", ""))
    if post.is_file():
        text = post.read_text(encoding="utf-8")
        if "ci.yml monitoring.yml" not in text or "v4-live-smoke.yml" in text:
            errors.append("post-merge-validation must dispatch only CI and monitoring during no-champion transition")
        if '-f expected_sha="$EXPECTED_SHA"' not in text:
            errors.append("post-merge-validation must pass exact SHA")

    for sid in ("code-validation", "monitoring-validation"):
        path = root / str(by_id.get(sid, {}).get("workflow", ""))
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if "expected_sha:" not in text or "VALIDATION_SHA" not in text:
                errors.append(f"{sid} must accept expected_sha")
            if 'test "$(git rev-parse HEAD)" = "$VALIDATION_SHA"' not in text:
                errors.append(f"{sid} must verify exact checkout")

    evidence = root / str(by_id.get("v7-unified-paper-evidence", {}).get("workflow", ""))
    if evidence.is_file():
        text = evidence.read_text(encoding="utf-8")
        for required in ("config/v7_evidence_runtime.json", "by-sha", "Private runtime single-writer validation", "contents: read"):
            if required not in text: errors.append(f"V7 evidence runtime missing contract: {required}")
        for forbidden in ("contents: write", "git push origin paper-validated", "POLYMARKET_DEPLOY_REF="):
            if forbidden in text: errors.append(f"V7 evidence runtime contains forbidden authority: {forbidden}")

    deploy = root / str(by_id.get("v7-paper-deploy", {}).get("workflow", ""))
    if deploy.is_file():
        text = deploy.read_text(encoding="utf-8")
        for required in ("paper-validated", "POLYMARKET_EXPECTED_SHA", "manifest.get('version') != 7", "--require-monitoring"):
            if required not in text: errors.append(f"V7 deploy workflow missing contract: {required}")
        for forbidden in ("v4-live-paper-smoke", "paper_v6_loop.sh", "authenticated_execution=true"):
            if forbidden in text: errors.append(f"V7 deploy workflow contains retired/unsafe contract: {forbidden}")

    health = root / str(by_id.get("v7-server-health", {}).get("workflow", ""))
    if health.is_file():
        text = health.read_text(encoding="utf-8")
        for required in ("paper-validated", "scripts/v7_server_health.py", "--require-monitoring", "health_result=no_operational_champion"):
            if required not in text: errors.append(f"V7 server health workflow missing contract: {required}")

    return errors, items


def render(items: list[dict[str, Any]], errors: list[str]) -> str:
    lines = ["# Scheduler registry validation", "", f"- schedulers: {len(items)}", f"- errors: {len(errors)}"]
    if errors:
        lines += ["", "## Errors", *[f"- {e}" for e in errors]]
    else:
        lines += ["", "Registry is valid: V3-V6 schedulers are absent; V7 deploy/health are registered and deployment authority follows the champion manifest."]
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default="."); p.add_argument("--registry", default="config/scheduler_registry.json"); p.add_argument("--output")
    a = p.parse_args(); root = Path(a.root).resolve(); registry = root / a.registry
    try: errors, items = validate(root, registry)
    except Exception as exc: errors, items = [str(exc)], []
    report = render(items, errors)
    if a.output: Path(a.output).write_text(report, encoding="utf-8")
    print(report, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
