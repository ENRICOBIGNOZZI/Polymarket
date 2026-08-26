#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain an object")
    return data


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def validate(root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    context_path = root / "config/project_context.json"
    registry_path = root / "config/scheduler_registry.json"
    ssh_path = root / "ops/ssh_config_polymarket"
    snapshot_script = root / "scripts/project_context_snapshot.py"
    for path in (context_path, registry_path, ssh_path, snapshot_script):
        if not path.is_file():
            errors.append(f"missing context surface: {path.relative_to(root)}")
    if errors:
        return errors, notes

    context = load_json(context_path)
    registry = load_json(registry_path)
    directives_rel = str(context.get("operator_directives") or "")
    architecture_rel = str(context.get("model_architecture_manifest") or "")
    directives_path = root / directives_rel if directives_rel else None
    architecture_path = root / architecture_rel if architecture_rel else None
    if directives_path is None or not directives_path.is_file():
        errors.append("config/project_context.json must point to an existing operator_directives manifest")
        return errors, notes
    if architecture_path is None or not architecture_path.is_file():
        errors.append("config/project_context.json must point to the canonical V7 architecture manifest")
        return errors, notes
    directives = load_json(directives_path)
    architecture = load_json(architecture_path)

    if context.get("schema_version") != 1:
        errors.append("config/project_context.json schema_version must equal 1")
    if context.get("repository") != "ENRICOBIGNOZZI/Polymarket":
        errors.append("project context repository must be ENRICOBIGNOZZI/Polymarket")
    if context.get("canonical_branch") != "main":
        errors.append("canonical branch must be main")
    if context.get("deployment_ref") != "paper-validated":
        errors.append("deployment ref must be paper-validated")
    if context.get("live_champion_manifest") != "config/live_champion.json":
        errors.append("project context must use config/live_champion.json")
    if architecture_rel != "config/v7_model_architecture.json":
        errors.append("project context must use config/v7_model_architecture.json")

    policy = context.get("context_policy") or {}
    if policy.get("scope") != "entire_repository":
        errors.append("scheduler context scope must be entire_repository")
    if policy.get("require_full_worktree_inventory") is not True:
        errors.append("full worktree inventory must be required")
    if policy.get("require_operator_directives") is not True:
        errors.append("operator directives must be required scheduler context")

    if architecture.get("engine_version") != 7:
        errors.append("canonical architecture manifest must declare engine_version=7")
    if architecture.get("paper_only") is not True:
        errors.append("canonical architecture manifest must be PAPER-only")
    if architecture.get("authenticated_execution") is not False:
        errors.append("canonical architecture manifest must keep authenticated execution disabled")
    if architecture.get("legacy_runtime_supported") is not False:
        errors.append("canonical architecture manifest must reject legacy runtime support")
    runtime_arch = architecture.get("runtime") if isinstance(architecture.get("runtime"), dict) else {}
    if runtime_arch.get("entrypoint") != "scripts/paper_v7_loop.sh":
        errors.append("canonical architecture entrypoint must be scripts/paper_v7_loop.sh")
    if runtime_arch.get("execution_loop") != "scripts/paper_v7_execution_loop.sh":
        errors.append("canonical architecture execution loop must be scripts/paper_v7_execution_loop.sh")
    for key in ("single_runtime_owner", "single_execution_ledger", "single_broker_authority"):
        if runtime_arch.get(key) is not True:
            errors.append(f"canonical architecture must require {key}")

    champion = load_json(root / "config/live_champion.json")
    if int(champion.get("version") or 0) != 7:
        errors.append("live champion must be V7")
    if champion.get("loop") != runtime_arch.get("entrypoint"):
        errors.append("live champion loop must match the V7 architecture manifest")
    if champion.get("config") != "config/paper_v7.json":
        errors.append("live champion config must be config/paper_v7.json")
    if champion.get("run_root") != runtime_arch.get("run_root"):
        errors.append("live champion run root must match the V7 architecture manifest")
    if champion.get("paper_only") is not True or champion.get("authenticated_execution") is not False:
        errors.append("live champion must remain PAPER-only with authenticated execution disabled")

    if directives.get("schema_version") != 1:
        errors.append("operator directives schema_version must equal 1")
    if directives.get("authority") != "latest_explicit_user_instruction":
        errors.append("operator directives must declare latest_explicit_user_instruction authority")
    if directives.get("repository") != "ENRICOBIGNOZZI/Polymarket":
        errors.append("operator directives repository mismatch")

    authorization = directives.get("paper_v7_authorization") if isinstance(directives.get("paper_v7_authorization"), dict) else {}
    exact = {
        "max_trade_fraction": 1.0,
        "max_market_fraction": 1.0,
        "max_event_fraction": 1.0,
        "max_gross_fraction": 1.0,
        "hard_arb_max_trade_fraction": 1.0,
    }
    if authorization.get("paper_only") is not True:
        errors.append("operator V7 authorization must remain PAPER-only")
    if authorization.get("authenticated_execution") is not False:
        errors.append("operator V7 authorization must keep authenticated execution disabled")
    if authorization.get("fixed_dollar_trade_cap_enabled") is not False:
        errors.append("operator V7 authorization must keep the fixed-dollar trade cap disabled")
    if authorization.get("hard_arb_fixed_dollar_trade_cap_enabled") is not False:
        errors.append("operator V7 hard-arb fixed-dollar cap must remain disabled")
    for key, expected in exact.items():
        value = number(authorization.get(key))
        if value is None or abs(value - expected) > 1e-12:
            errors.append(f"operator V7 authorization requires {key}=1.0")
    if number(authorization.get("fractional_kelly_ceiling")) != 0.25:
        errors.append("operator V7 fractional Kelly ceiling must remain 0.25")
    if number(authorization.get("max_drawdown")) != 0.15:
        errors.append("operator V7 drawdown kill must remain 0.15")

    architecture_directive = directives.get("architecture") if isinstance(directives.get("architecture"), dict) else {}
    if architecture_directive.get("legacy_runtime_supported") is not False:
        errors.append("operator directives must explicitly disable legacy runtime support")
    for key in ("single_execution_ledger", "single_runtime_owner", "single_broker_authority", "single_live_champion"):
        if architecture_directive.get(key) is not True:
            errors.append(f"operator directives must require {key}")

    schedulers = registry.get("schedulers")
    if not isinstance(schedulers, list):
        errors.append("scheduler registry does not contain a scheduler list")
        return errors, notes
    scheduler_ids = {str(raw.get("id")) for raw in schedulers if isinstance(raw, dict) and raw.get("id")}
    assignments = directives.get("scheduler_assignments")
    if not isinstance(assignments, dict):
        errors.append("operator directives must contain scheduler_assignments")
        assignments = {}
    missing_assignments = sorted(scheduler_ids.difference(assignments))
    extra_assignments = sorted(set(assignments).difference(scheduler_ids))
    if missing_assignments:
        errors.append("operator directives missing scheduler assignments: " + ", ".join(missing_assignments))
    if extra_assignments:
        errors.append("operator directives reference unknown schedulers: " + ", ".join(extra_assignments))

    priorities = directives.get("current_priority_order")
    if not isinstance(priorities, list) or len(priorities) < 4 or not all(str(item).strip() for item in priorities):
        errors.append("operator directives must provide a non-empty canonical V7 priority sequence")
    forbidden = directives.get("forbidden_regressions")
    forbidden_text = "\n".join(str(item).lower() for item in forbidden) if isinstance(forbidden, list) else ""
    if "retired runtime" not in forbidden_text and "legacy runtime" not in forbidden_text:
        errors.append("operator directives must explicitly forbid reintroducing retired runtime generations")
    if "fixed-dollar" not in forbidden_text:
        errors.append("operator directives must explicitly forbid restoring a binding fixed-dollar V7 cap")
    if "authenticated" not in forbidden_text and "real-money" not in forbidden_text:
        errors.append("operator directives must explicitly forbid authenticated/real-money execution")

    for rel in [str(item) for item in context.get("required_surfaces", [])]:
        if not (root / rel).exists():
            errors.append(f"required repository surface is missing: {rel}")

    ssh_text = ssh_path.read_text(encoding="utf-8")
    for required in (
        "Host polymarket",
        "HostName 100.104.183.109",
        "Port 22",
        "User enrico",
        "ServerAliveInterval 30",
        "ServerAliveCountMax 3",
        "RequestTTY yes",
        "RemoteCommand cd ~/polymarket && exec ${SHELL:-/bin/zsh} -l",
    ):
        if required not in ssh_text:
            errors.append(f"canonical SSH config is missing: {required}")
    if "BEGIN OPENSSH PRIVATE KEY" in ssh_text or "BEGIN RSA PRIVATE KEY" in ssh_text:
        errors.append("private key material must never appear in ops/ssh_config_polymarket")

    grafana = context.get("grafana") or {}
    if grafana.get("canonical_operator_url") != "http://mamma-portfolio.tail1bae85.ts.net":
        errors.append("canonical Grafana URL changed unexpectedly")
    if grafana.get("dashboard_uid") != "polymarket-v7-paper":
        errors.append("canonical Grafana dashboard UID must be polymarket-v7-paper")
    if grafana.get("dashboard_file") != "monitoring/grafana/dashboards/polymarket-v7.json":
        errors.append("canonical Grafana dashboard file must be the V7 dashboard")
    dashboard_path = root / str(grafana.get("dashboard_file") or "")
    if not dashboard_path.is_file():
        errors.append("canonical Grafana V7 dashboard file is missing")
    else:
        dashboard = load_json(dashboard_path)
        if dashboard.get("uid") != grafana.get("dashboard_uid"):
            errors.append("canonical Grafana dashboard UID does not match project context")

    for raw in schedulers:
        if not isinstance(raw, dict):
            continue
        scheduler_id = str(raw.get("id", "<unknown>"))
        workflow_rel = str(raw.get("workflow", ""))
        workflow = root / workflow_rel
        if not workflow.is_file():
            continue
        text = workflow.read_text(encoding="utf-8")
        if "sparse-checkout:" in text or "sparse-checkout-cone-mode:" in text:
            errors.append(f"{scheduler_id} narrows its repository checkout; full project visibility is required")
            continue
        local_checkout = "actions/checkout@v4" in text
        remote_checkout = any(marker in text for marker in ("cd ~/polymarket", 'cd "$HOME/polymarket"', "cd $HOME/polymarket"))
        if not local_checkout and not remote_checkout:
            errors.append(f"{scheduler_id} has no complete repository worktree available")
        else:
            assignment = str(assignments.get(scheduler_id) or "")
            notes.append(f"{scheduler_id}: {'runner checkout' if local_checkout else 'canonical remote checkout'}; directive={assignment}")
    return errors, notes


def render(errors: list[str], notes: list[str]) -> str:
    lines = [
        "# Scheduler project-context validation",
        "",
        f"- schedulers with verified full-worktree access and assignments: {len(notes)}",
        f"- errors: {len(errors)}",
        "- policy: every scheduler sees the complete repository, the canonical V7 architecture and the current explicit operator directive before narrowing to its bounded responsibility",
        "",
        "## Visibility and assignment",
    ]
    lines.extend(f"- {note}" for note in notes)
    if errors:
        lines.extend(["", "## Errors"])
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.extend(["", "All registered schedulers have complete repository visibility, a current operator assignment and consistent V7 runtime/Grafana context."])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate canonical V7 project context and explicit operator assignments for Polymarket schedulers")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="project-context-validation.md")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        errors, notes = validate(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors, notes = [str(exc)], []
    report = render(errors, notes)
    Path(args.output).write_text(report, encoding="utf-8")
    print(report, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
