#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
from typing import Any

EXPECTED_CLEANUP_SEQUENCE = (
    "v7_implementation_then_tests_then_same_sha_paper_then_main_then_"
    "paper_validated_then_deploy_then_server_health_then_legacy_deletion"
)
EXPECTED_V7_PATHS = {
    "canonical_loop": "scripts/paper_v7_loop.sh",
    "canonical_config": "config/paper_v7.json",
    "canonical_run_root": "runs/paper_v7_live",
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


def safe_relative(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return value


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

    cutover = context.get("cutover") if isinstance(context.get("cutover"), dict) else {}
    if cutover.get("target_version") != 7:
        errors.append("project context cutover target_version must be 7")
    if cutover.get("required_sequence") != EXPECTED_CLEANUP_SEQUENCE:
        errors.append("project context cutover sequence must match the V7 master lifecycle")
    for key, expected in EXPECTED_V7_PATHS.items():
        if cutover.get(key) != expected:
            errors.append(f"project context cutover requires {key}={expected}")
    if not isinstance(cutover.get("current_state"), str) or not cutover.get("current_state"):
        errors.append("project context cutover current_state is required")

    if directives.get("schema_version") != 1:
        errors.append("operator directives schema_version must equal 1")
    if directives.get("authority") != "latest_explicit_user_instruction":
        errors.append("operator authority mismatch")
    if directives.get("operator_instruction_id") != "user-v7-master-multi-agent-operating-prompt-20260827":
        errors.append("operator instruction is not the current V7 master directive")
    if directives.get("repository") != "ENRICOBIGNOZZI/Polymarket":
        errors.append("operator repository mismatch")
    architecture = directives.get("architecture") if isinstance(directives.get("architecture"), dict) else {}
    if architecture.get("cleanup_sequence") != EXPECTED_CLEANUP_SEQUENCE:
        errors.append("operator cleanup sequence must preserve exact-SHA V7 cutover before final legacy deletion")
    if architecture.get("operational_champion_may_be_absent") is not True:
        errors.append("operator directive must allow the temporary no-champion recovery state")

    auth = directives.get("paper_v7_authorization") if isinstance(directives.get("paper_v7_authorization"), dict) else {}
    if auth.get("paper_only") is not True or auth.get("authenticated_execution") is not False:
        errors.append("V7 must remain PAPER-only and authenticated-disabled")
    if auth.get("fixed_dollar_trade_cap_enabled") is not False or auth.get("hard_arb_fixed_dollar_trade_cap_enabled") is not False:
        errors.append("obsolete fixed-dollar caps must remain disabled")
    for key in ("max_trade_fraction", "max_market_fraction", "max_event_fraction", "max_gross_fraction", "hard_arb_max_trade_fraction"):
        if num(auth.get(key)) != 1.0:
            errors.append(f"operator V7 authorization requires {key}=1.0")
    if num(auth.get("fractional_kelly_ceiling")) != 0.25:
        errors.append("fractional Kelly ceiling must be 0.25")
    if num(auth.get("max_drawdown")) != 0.15:
        errors.append("max drawdown must be 0.15")

    runtime = context.get("runtime") if isinstance(context.get("runtime"), dict) else {}
    grafana = context.get("grafana") if isinstance(context.get("grafana"), dict) else {}
    champion_enabled = champion.get("enabled") is True
    if champion_enabled:
        if champion.get("version") != 7:
            errors.append("enabled operational champion must be version 7")
        expected_paths = {
            "loop": EXPECTED_V7_PATHS["canonical_loop"],
            "config": EXPECTED_V7_PATHS["canonical_config"],
            "run_root": EXPECTED_V7_PATHS["canonical_run_root"],
        }
        for key, expected in expected_paths.items():
            if champion.get(key) != expected:
                errors.append(f"enabled V7 champion requires {key}={expected}")
        if champion.get("deployment_ref") != "paper-validated":
            errors.append("enabled V7 champion must deploy from paper-validated")
        if champion.get("paper_only") is not True or champion.get("authenticated_execution") is not False:
            errors.append("enabled V7 champion must preserve PAPER-only/authenticated-disabled boundary")
        for key in ("loop", "config"):
            rel = safe_relative(champion.get(key))
            if rel is None or not (root / rel).is_file():
                errors.append(f"enabled V7 champion references missing/unsafe {key}: {champion.get(key)!r}")
        if runtime.get("active_champion") is not True:
            errors.append("project context runtime.active_champion must be true for enabled V7 champion")
        dashboard_uid = grafana.get("dashboard_uid")
        dashboard_file = safe_relative(grafana.get("dashboard_file"))
        if grafana.get("active") is not True or not isinstance(dashboard_uid, str) or not dashboard_uid:
            errors.append("enabled V7 champion requires active canonical Grafana with dashboard_uid")
        if dashboard_file is None or not (root / dashboard_file).is_file():
            errors.append("enabled V7 champion requires an existing canonical dashboard_file")
        notes.append("operational champion: V7 enabled")
    else:
        if champion.get("enabled") is not False:
            errors.append("live champion must be explicitly enabled or disabled")
        for key in ("version", "loop", "config", "run_root"):
            if champion.get(key) is not None:
                errors.append(f"disabled champion requires {key}=null")
        if champion.get("paper_only") is not True or champion.get("authenticated_execution") is not False:
            errors.append("disabled champion manifest must preserve PAPER-only/authenticated-disabled boundary")
        if runtime.get("active_champion") is not False:
            errors.append("project context runtime.active_champion must be false during recovery")
        if grafana.get("active") is not False or grafana.get("dashboard_uid") is not None or grafana.get("dashboard_file") is not None:
            errors.append("champion-specific Grafana must be inactive during no-champion recovery")
        notes.append("operational champion: temporarily disabled during V7 recovery")

    schedulers = registry.get("schedulers") if isinstance(registry.get("schedulers"), list) else []
    active_ids = {str(x.get("id")) for x in schedulers if isinstance(x, dict) and x.get("id")}
    assignments = directives.get("scheduler_assignments") if isinstance(directives.get("scheduler_assignments"), dict) else {}
    missing = sorted(active_ids.difference(assignments))
    if missing:
        errors.append("operator directives missing active scheduler assignments: " + ", ".join(missing))
    for sid in sorted(set(assignments).difference(active_ids)):
        notes.append(f"{sid}: reserved/inactive operator assignment")

    retired_ids = {"v6-live-data-research", "v6-market-cache-relay"}
    present = sorted(active_ids.intersection(retired_ids))
    if present:
        errors.append("retired V6 schedulers still active: " + ", ".join(present))

    priorities = directives.get("current_priority_order")
    if not isinstance(priorities, list) or len(priorities) < 8:
        errors.append("complete current priority order is required")
    forbidden = directives.get("forbidden_regressions")
    if not isinstance(forbidden, list) or not forbidden:
        errors.append("forbidden-regressions contract is required")

    for rel in [str(x) for x in context.get("required_surfaces", [])]:
        if not (root / rel).exists():
            errors.append(f"required repository surface is missing: {rel}")

    model_manifest = context.get("model_architecture_manifest")
    if model_manifest is not None:
        rel = safe_relative(model_manifest)
        if rel is None or not (root / rel).is_file():
            errors.append("model_architecture_manifest must be null or an existing safe repository path")

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
    champion_notes = [note for note in notes if note.startswith("operational champion:")]
    champion_state = champion_notes[0].split(":", 1)[1].strip() if champion_notes else "unknown"
    lines = [
        "# Scheduler project-context validation",
        "",
        f"- active/context notes: {len(notes)}",
        f"- errors: {len(errors)}",
        f"- operational champion: {champion_state}",
    ]
    if errors:
        lines += ["", "## Errors", *[f"- {error}" for error in errors]]
    else:
        lines += ["", "V7 master authority, exact-SHA cutover semantics and scheduler context are coherent."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="project-context-validation.md")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        errors, notes = validate(root)
    except Exception as exc:
        errors, notes = [str(exc)], []
    report = render(errors, notes)
    Path(args.output).write_text(report, encoding="utf-8")
    print(report, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
