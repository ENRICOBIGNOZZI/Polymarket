#!/usr/bin/env python3
"""Product-health hardened entrypoint for the Polymarket Meta-Supervisor.

A scheduler run is not healthy merely because GitHub marked it skipped or
successful. The wrapper also evaluates the latest autonomous-research product
once that product channel is explicitly present in the runtime snapshot.
"""
from __future__ import annotations

from typing import Any

import meta_supervisor as legacy


_original_classify = legacy.classify_workflow
_original_build_report = legacy.build_report


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


def _autonomous_product_channel_wired(snapshot: dict[str, Any]) -> bool:
    products = snapshot.get("products")
    return isinstance(products, dict) and "autonomous_research" in products


def _autonomous_product_health(config: dict[str, Any], snapshot: dict[str, Any], now: int) -> dict[str, Any]:
    payload = ((snapshot.get("products") or {}).get("autonomous_research") or {})
    generated = legacy.parse_timestamp(payload.get("generated_ts")) if isinstance(payload, dict) else 0
    age = max(0, now - generated) if generated else None
    limit = legacy.integer((config.get("telemetry") or {}).get("max_autonomous_research_age_seconds"), 7200)
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
    return {
        "present": bool(generated),
        "generated_ts": generated,
        "age_seconds": age,
        "max_age_seconds": limit,
        "reported_status": reported,
        "server_deploy_enabled": deploy_enabled,
        "acceptable_statuses": sorted(acceptable),
        "healthy": not reasons,
        "reasons": reasons,
    }


def build_report(config: dict[str, Any], snapshot: dict[str, Any], now: int) -> dict[str, Any]:
    report = _original_build_report(config, snapshot, now)
    _surface_failure_cooldowns(report)
    wired = _autonomous_product_channel_wired(snapshot)
    report.setdefault("invariants", {})["autonomous_research_product_channel_wired"] = wired
    if not wired:
        report["product_health"] = {
            "autonomous_research": {
                "wired": False,
                "healthy": True,
                "reported_status": "NOT_WIRED",
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
    max_dispatches = legacy.integer(((config.get("coordination") or {}).get("max_dispatches_per_cycle")), 3)
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
                "workflow_name": queue_state.get("workflow_name") or "Polymarket Research Queue",
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
    report["invariants"]["actual_dispatches"] = len(plan)
    report["status"] = "REMEDIATING" if any(
        action.get("workflow_file") == "research-queue.yml" for action in plan if isinstance(action, dict)
    ) else "DEGRADED"
    return report


def main() -> int:
    legacy.classify_workflow = classify_workflow
    legacy.build_report = build_report
    return legacy.main()


if __name__ == "__main__":
    raise SystemExit(main())
