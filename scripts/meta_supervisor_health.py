#!/usr/bin/env python3
"""Product-health extension for the canonical Polymarket Meta-Supervisor."""
from __future__ import annotations

from typing import Any

import meta_supervisor as core


_original_classify = core.classify_workflow
_original_build_report = core.build_report


def classify_workflow(
    spec: dict[str, Any], latest: dict[str, Any] | None, main_sha: str, now: int, cooldown: int
) -> dict[str, Any]:
    result = _original_classify(spec, latest, main_sha, now, cooldown)
    if latest is None:
        return result
    status = str(latest.get("status") or "").lower()
    conclusion = str(latest.get("conclusion") or "").lower()
    if status in {"completed", ""} and conclusion == "skipped":
        result.update(
            {
                "state": "failed",
                "dispatch_needed": bool(spec.get("dispatchable")),
                "reason": "latest run was skipped and produced no validated scheduler product",
            }
        )
    return result


def _without_expected_scheduled_skips(
    config: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Ignore explicitly expected timer skips when selecting evidence-bearing runs."""
    specs = ((config.get("coordination") or {}).get("workflows") or {})
    ignored_names = {
        str(spec.get("name") or filename)
        for filename, spec in specs.items()
        if isinstance(spec, dict) and spec.get("ignore_scheduled_skips") is True
    }
    runs = snapshot.get("runs")
    if not ignored_names or not isinstance(runs, list):
        return snapshot, 0

    filtered: list[Any] = []
    ignored = 0
    for raw in runs:
        if not isinstance(raw, dict):
            filtered.append(raw)
            continue
        name = str(raw.get("workflowName") or raw.get("name") or "")
        event = str(raw.get("event") or "").lower()
        status = str(raw.get("status") or "").lower()
        conclusion = str(raw.get("conclusion") or "").lower()
        expected_skip = (
            name in ignored_names
            and event == "schedule"
            and status in {"completed", ""}
            and conclusion == "skipped"
        )
        if expected_skip:
            ignored += 1
            continue
        filtered.append(raw)

    if ignored == 0:
        return snapshot, 0
    cleaned = dict(snapshot)
    cleaned["runs"] = filtered
    return cleaned, ignored


def _surface_failure_cooldowns(report: dict[str, Any]) -> None:
    alerts = report.setdefault("alerts", [])
    existing = {
        (str(alert.get("code")), str(alert.get("detail")))
        for alert in alerts
        if isinstance(alert, dict)
    }
    critical = False
    for filename, state in (report.get("workflow_status") or {}).items():
        if not isinstance(state, dict) or state.get("state") != "failure_cooldown":
            continue
        severity = "warning" if state.get("dispatchable") else "critical"
        detail = f"{filename}: {state.get('reason') or 'recent workflow failure is inside retry cooldown'}"
        key = ("WORKFLOW_FAILURE_COOLDOWN", detail)
        if key not in existing:
            alerts.append(
                {
                    "severity": severity,
                    "code": "WORKFLOW_FAILURE_COOLDOWN",
                    "detail": detail,
                }
            )
            existing.add(key)
        if severity == "critical":
            critical = True
    if critical:
        report["status"] = "DEGRADED"
    report.setdefault("invariants", {})["failure_cooldown_is_health_evidence"] = False


def _surface_repository_governance(report: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    observed = snapshot.get("main_branch_protected")
    invariants = report.setdefault("invariants", {})
    invariants["main_branch_protection_observed"] = observed in {True, False}
    invariants["main_branch_protection_enforced"] = observed if observed in {True, False} else None
    if observed is not False:
        return False

    alerts = report.setdefault("alerts", [])
    if not any(
        isinstance(alert, dict) and alert.get("code") == "MAIN_BRANCH_UNPROTECTED"
        for alert in alerts
    ):
        alerts.append(
            {
                "severity": "critical",
                "code": "MAIN_BRANCH_UNPROTECTED",
                "detail": (
                    "GitHub reports main as unprotected; workflow provenance gates remain fail-closed, "
                    "but repository settings still permit direct pushes and require administrator-side branch protection"
                ),
            }
        )
    blocked = report.setdefault("blocked_actions", [])
    if not any(
        isinstance(action, dict) and action.get("workflow_file") == "repository-settings/main-branch-protection"
        for action in blocked
    ):
        blocked.append(
            {
                "workflow_file": "repository-settings/main-branch-protection",
                "reason": "requires GitHub repository branch-protection/ruleset settings; no workflow may self-grant this authority",
            }
        )
    report["status"] = "DEGRADED"
    return True


def _autonomous_product_channel_wired(snapshot: dict[str, Any]) -> bool:
    products = snapshot.get("products")
    return isinstance(products, dict) and "autonomous_research" in products


def _autonomous_product_health(config: dict[str, Any], snapshot: dict[str, Any], now: int) -> dict[str, Any]:
    payload = ((snapshot.get("products") or {}).get("autonomous_research") or {})
    generated = core.parse_timestamp(payload.get("generated_ts")) if isinstance(payload, dict) else 0
    age = max(0, now - generated) if generated else None
    limit = core.integer((config.get("telemetry") or {}).get("max_autonomous_research_age_seconds"), 7200)
    deploy_enabled = snapshot.get("server_deploy_enabled") is True
    reported = str(payload.get("status") or "MISSING") if isinstance(payload, dict) else "MISSING"
    acceptable = {"HEALTHY"} if deploy_enabled else {"HEALTHY", "WAITING_RUNTIME"}
    reasons: list[str] = []
    if not generated:
        reasons.append("autonomous_research_product_missing")
    elif age is not None and age > limit:
        reasons.append(f"autonomous_research_product_stale:{age}>{limit}")
    if reported not in acceptable:
        reasons.append(f"autonomous_research_reported_status:{reported}")
    invariants = payload.get("invariants") if isinstance(payload, dict) else {}
    if isinstance(invariants, dict):
        if invariants.get("append_only_external_store") is not True:
            reasons.append("append_only_external_store_not_certified")
        if invariants.get("bounded_allowlisted_research") is not True:
            reasons.append("bounded_research_not_certified")
        if invariants.get("real_order_submission") is not False:
            reasons.append("real_order_submission_boundary_violation")

    economic = payload.get("economic_progress") if isinstance(payload, dict) else {}
    economic = economic if isinstance(economic, dict) else {}
    economic_state = str(economic.get("state") or "").upper()
    seconds_since_progress = core.integer(economic.get("seconds_since_progress"), 0)
    if economic_state == "STAGNANT":
        reasons.append(
            "autonomous_research_economic_stagnation"
            + (f":{seconds_since_progress}s" if seconds_since_progress else "")
        )

    return {
        "present": bool(generated),
        "generated_ts": generated,
        "age_seconds": age,
        "max_age_seconds": limit,
        "reported_status": reported,
        "server_deploy_enabled": deploy_enabled,
        "acceptable_statuses": sorted(acceptable),
        "economic_state": economic_state or "UNREPORTED",
        "seconds_since_economic_progress": seconds_since_progress,
        "healthy": not reasons,
        "reasons": reasons,
    }


def build_report(config: dict[str, Any], snapshot: dict[str, Any], now: int) -> dict[str, Any]:
    evidence_snapshot, ignored_scheduled_skips = _without_expected_scheduled_skips(config, snapshot)
    report = _original_build_report(config, evidence_snapshot, now)
    report.setdefault("invariants", {})["expected_scheduled_skips_ignored"] = ignored_scheduled_skips
    _surface_failure_cooldowns(report)
    governance_blocked = _surface_repository_governance(report, snapshot)
    wired = _autonomous_product_channel_wired(snapshot)
    report.setdefault("invariants", {})["autonomous_research_product_channel_wired"] = wired
    if not wired:
        report["product_health"] = {
            "autonomous_research": {
                "wired": False,
                "healthy": True,
                "reported_status": "NOT_WIRED",
                "economic_state": "NOT_WIRED",
                "reasons": [],
            }
        }
        return report

    product = _autonomous_product_health(config, snapshot, now)
    product["wired"] = True
    report["product_health"] = {"autonomous_research": product}
    if product["healthy"]:
        return report

    state = (report.get("workflow_status") or {}).get("research-queue.yml")
    if isinstance(state, dict):
        state["state"] = "product_degraded"
        state["reason"] = "; ".join(product["reasons"])
        state["dispatch_needed"] = bool(state.get("dispatchable"))

    report.setdefault("alerts", []).append(
        {
            "severity": "critical",
            "code": "AUTONOMOUS_RESEARCH_PRODUCT_DEGRADED",
            "detail": "; ".join(product["reasons"]),
        }
    )

    plan = report.setdefault("dispatch_plan", [])
    max_dispatches = core.integer(((config.get("coordination") or {}).get("max_dispatches_per_cycle")), 3)
    already = {str(action.get("workflow_file")) for action in plan if isinstance(action, dict)}
    queue_state = (report.get("workflow_status") or {}).get("research-queue.yml") or {}
    dependencies = list(queue_state.get("dependencies") or [])
    unhealthy_dependencies = [
        dependency
        for dependency in dependencies
        if ((report.get("workflow_status") or {}).get(str(dependency)) or {}).get("state") != "healthy"
    ]
    if (
        "research-queue.yml" not in already
        and len(plan) < max_dispatches
        and queue_state.get("dispatchable") is True
        and not unhealthy_dependencies
    ):
        plan.append(
            {
                "workflow_file": "research-queue.yml",
                "workflow_name": queue_state.get("workflow_name") or "Polymarket Research Director",
                "reason": "; ".join(product["reasons"]),
                "priority": queue_state.get("priority", 35),
            }
        )
    else:
        report.setdefault("blocked_actions", []).append(
            {
                "workflow_file": "research-queue.yml",
                "reason": (
                    "autonomous product recovery blocked by unhealthy dependencies: "
                    + ", ".join(unhealthy_dependencies)
                    if unhealthy_dependencies
                    else "autonomous product recovery dispatch budget unavailable"
                ),
            }
        )

    report["invariants"]["product_health_checked"] = True
    report["invariants"]["scheduler_success_without_product_is_healthy"] = False
    report["invariants"]["economic_stagnation_is_health_evidence"] = False
    report["invariants"]["actual_dispatches"] = len(plan)
    report["status"] = "REMEDIATING" if any(
        action.get("workflow_file") == "research-queue.yml" for action in plan if isinstance(action, dict)
    ) else "DEGRADED"
    if governance_blocked:
        report["status"] = "DEGRADED"
    return report


def main() -> int:
    core.classify_workflow = classify_workflow
    core.build_report = build_report
    return core.main()


if __name__ == "__main__":
    raise SystemExit(main())
