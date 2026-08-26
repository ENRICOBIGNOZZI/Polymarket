#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


EXPECTED_CLEANUP_SEQUENCE = (
    "v7_implementation_then_tests_then_same_sha_paper_then_main_then_"
    "paper_validated_then_deploy_then_server_health_then_legacy_deletion"
)
EXPECTED_AUTHORITY = "latest_explicit_user_instruction"
PLANNED_FORWARD_SCHEDULERS = {
    "live-paper-validation",
    "paper-server-deploy",
    "paper-server-health",
    "forward-maker-research",
}
LEGACY_ONLY_SCHEDULERS = {
    "v6-live-data-research",
    "v6-market-cache-relay",
}


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def validate(root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    context_path = root / "config/project_context.json"
    registry_path = root / "config/scheduler_registry.json"
    champion_path = root / "config/live_champion.json"
    ssh_path = root / "ops/ssh_config_polymarket"
    snapshot = root / "scripts/project_context_snapshot.py"
    for path in (context_path, registry_path, champion_path, ssh_path, snapshot):
        if not path.is_file():
            errors.append(f"missing context surface: {path.relative_to(root)}")
    if errors:
        return errors, notes

    context = load(context_path)
    registry = load(registry_path)
    champion = load(champion_path)
    directives_rel = str(context.get("operator_directives") or "")
    directives_path = root / directives_rel
    if not directives_rel or not directives_path.is_file():
        return ["config/project_context.json must point to operator directives"], notes
    directives = load(directives_path)

    if context.get("schema_version") != 1:
        errors.append("project context schema_version must equal 1")
    if context.get("repository") != "ENRICOBIGNOZZI/Polymarket":
        errors.append("repository mismatch")
    if context.get("canonical_branch") != "main":
        errors.append("canonical branch must be main")
    if context.get("deployment_ref") != "paper-validated":
        errors.append("deployment ref must be paper-validated")
    policy = context.get("context_policy") if isinstance(context.get("context_policy"), dict) else {}
    if policy.get("scope") != "entire_repository":
        errors.append("context scope must be entire_repository")
    if policy.get("require_full_worktree_inventory") is not True:
        errors.append("full worktree inventory must be required")
    if policy.get("require_operator_directives") is not True:
        errors.append("operator directives must be required")
    context_rule = str(policy.get("rule") or "")
    for required in ("V7", "same-SHA PAPER", "paper-validated", "server health"):
        if required not in context_rule:
            errors.append(f"project context rule missing current lifecycle term: {required}")

    if champion.get("enabled") is not False:
        errors.append("V7 cutover repair requires the temporary champion state to remain explicitly disabled")
    for key in ("version", "loop", "config", "run_root"):
        if champion.get(key) is not None:
            errors.append(f"disabled champion requires {key}=null")
    if champion.get("paper_only") is not True or champion.get("authenticated_execution") is not False:
        errors.append("disabled champion manifest must preserve PAPER-only/authenticated-disabled boundary")

    if directives.get("schema_version") != 1:
        errors.append("operator directives schema_version must equal 1")
    if directives.get("authority") != EXPECTED_AUTHORITY:
        errors.append("operator authority mismatch")
    if directives.get("repository") != "ENRICOBIGNOZZI/Polymarket":
        errors.append("operator repository mismatch")
    architecture = directives.get("architecture") if isinstance(directives.get("architecture"), dict) else {}
    if architecture.get("operational_champion_may_be_absent") is not True:
        errors.append("operator directive must allow the temporary no-champion recovery state")
    if architecture.get("cleanup_sequence") != EXPECTED_CLEANUP_SEQUENCE:
        errors.append("operator cleanup/cutover sequence does not match the current same-SHA lifecycle")
    legacy_rule = str(architecture.get("legacy_rule") or "")
    if "Do not add new logic to V3/V4/V5/V6" not in legacy_rule:
        errors.append("operator legacy rule must forbid new V3-V6 development")
    recovery = str(architecture.get("recovery_after_premature_cleanup") or "")
    if "Do not restore" not in recovery or "V7/common/control-plane" not in recovery:
        errors.append("operator architecture must require forward repair rather than wholesale legacy restoration")

    auth = directives.get("paper_v7_authorization") if isinstance(directives.get("paper_v7_authorization"), dict) else {}
    if auth.get("paper_only") is not True or auth.get("authenticated_execution") is not False:
        errors.append("V7 must remain PAPER-only and authenticated-disabled")
    if auth.get("fixed_dollar_trade_cap_enabled") is not False or auth.get("hard_arb_fixed_dollar_trade_cap_enabled") is not False:
        errors.append("obsolete fixed-dollar caps must remain disabled")
    for key in (
        "max_trade_fraction",
        "max_market_fraction",
        "max_event_fraction",
        "max_gross_fraction",
        "hard_arb_max_trade_fraction",
    ):
        if num(auth.get(key)) != 1.0:
            errors.append(f"operator V7 authorization requires {key}=1.0")
    if num(auth.get("fractional_kelly_ceiling")) != 0.25:
        errors.append("fractional Kelly ceiling must be 0.25")
    if num(auth.get("max_drawdown")) != 0.15:
        errors.append("max drawdown must be 0.15")

    schedulers = registry.get("schedulers") if isinstance(registry.get("schedulers"), list) else []
    active_ids = {str(x.get("id")) for x in schedulers if isinstance(x, dict) and x.get("id")}
    assignments = directives.get("scheduler_assignments") if isinstance(directives.get("scheduler_assignments"), dict) else {}
    missing = sorted(active_ids.difference(assignments))
    if missing:
        errors.append("operator directives missing active scheduler assignments: " + ", ".join(missing))

    extra = set(assignments).difference(active_ids)
    planned = sorted(extra.intersection(PLANNED_FORWARD_SCHEDULERS))
    for sid in planned:
        notes.append(f"{sid}: operator-owned V7 forward-repair scheduler not yet registered")
    unexpected_extra = sorted(extra.difference(PLANNED_FORWARD_SCHEDULERS))
    for sid in unexpected_extra:
        if not str(assignments.get(sid, "")).startswith("RETIRED_IMMEDIATELY"):
            errors.append(f"inactive scheduler assignment is neither planned forward repair nor explicitly retired: {sid}")

    present_legacy = sorted(active_ids.intersection(LEGACY_ONLY_SCHEDULERS))
    if present_legacy:
        errors.append("legacy-only scheduler identities remain active: " + ", ".join(present_legacy))

    priorities = directives.get("current_priority_order")
    if not isinstance(priorities, list) or len(priorities) < 8:
        errors.append("complete current priority order is required")
    forbidden = directives.get("forbidden_regressions")
    if not isinstance(forbidden, list) or not any("Do not delete or omit V7 cutover primitives" in str(x) for x in forbidden):
        errors.append("V7 same-SHA cutover primitives must remain protected from premature deletion")

    for rel in [str(x) for x in context.get("required_surfaces", [])]:
        if not (root / rel).exists():
            errors.append(f"required repository surface is missing: {rel}")

    if context.get("model_architecture_manifest") is not None:
        errors.append("no legacy architecture manifest may be active before V7 integration")
    runtime = context.get("runtime") if isinstance(context.get("runtime"), dict) else {}
    if runtime.get("active_champion") is not False:
        errors.append("project context must expose the temporary no-champion recovery state")
    if runtime.get("state") != "v7_cutover_repair_required":
        errors.append("project context runtime state must identify V7 cutover repair")
    grafana = context.get("grafana") if isinstance(context.get("grafana"), dict) else {}
    if grafana.get("active") is not False or grafana.get("dashboard_uid") is not None or grafana.get("dashboard_file") is not None:
        errors.append("legacy champion-specific Grafana must stay inactive until V7 monitoring is rebuilt")

    ssh = ssh_path.read_text(encoding="utf-8")
    for required in ("Host polymarket", "HostName 100.104.183.109", "Port 22", "User enrico"):
        if required not in ssh:
            errors.append(f"canonical SSH config missing: {required}")
    if "BEGIN OPENSSH PRIVATE KEY" in ssh or "BEGIN RSA PRIVATE KEY" in ssh:
        errors.append("private key material must not be in repository")

    for item in schedulers:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("workflow") or "")
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if "sparse-checkout:" in text or "sparse-checkout-cone-mode:" in text:
            errors.append(f"{item.get('id')} narrows repository checkout")
        if "actions/checkout@v4" not in text and not any(x in text for x in ("cd ~/polymarket", 'cd "$HOME/polymarket"')):
            errors.append(f"{item.get('id')} has no complete worktree")
        else:
            notes.append(f"{item.get('id')}: active V7/control-plane scheduler")
    return errors, notes


def render(errors: list[str], notes: list[str]) -> str:
    lines = [
        "# Scheduler project-context validation",
        "",
        f"- active/planned scheduler notes: {len(notes)}",
        f"- errors: {len(errors)}",
        "- operational champion: temporarily disabled pending V7 cutover repair",
    ]
    if errors:
        lines += ["", "## Errors", *[f"- {e}" for e in errors]]
    else:
        lines += [
            "",
            "V7 forward-repair context is internally consistent: no wholesale legacy restore, PAPER-only safety is intact, and the same-SHA cutover lifecycle remains required.",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=".")
    p.add_argument("--output", default="project-context-validation.md")
    a = p.parse_args()
    root = Path(a.root).resolve()
    try:
        errors, notes = validate(root)
    except Exception as exc:
        errors, notes = [str(exc)], []
    report = render(errors, notes)
    Path(a.output).write_text(report, encoding="utf-8")
    print(report, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
