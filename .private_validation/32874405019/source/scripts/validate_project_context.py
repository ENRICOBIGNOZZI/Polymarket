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
    if context.get("schema_version") != 1:
        errors.append("config/project_context.json schema_version must equal 1")
    if context.get("repository") != "ENRICOBIGNOZZI/Polymarket":
        errors.append("project context repository must be ENRICOBIGNOZZI/Polymarket")
    if context.get("canonical_branch") != "main":
        errors.append("canonical branch must be main")
    if context.get("deployment_ref") != "paper-validated":
        errors.append("deployment ref must be paper-validated")

    policy = context.get("context_policy") or {}
    if policy.get("scope") != "entire_repository":
        errors.append("scheduler context scope must be entire_repository")
    if policy.get("require_full_worktree_inventory") is not True:
        errors.append("full worktree inventory must be required")

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
    if grafana.get("dashboard_uid") != "polymarket-multi-strategy-v5":
        errors.append("canonical Grafana dashboard must be polymarket-multi-strategy-v5")

    schedulers = registry.get("schedulers")
    if not isinstance(schedulers, list):
        errors.append("scheduler registry does not contain a scheduler list")
        return errors, notes

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
            notes.append(f"{scheduler_id}: {'runner checkout' if local_checkout else 'canonical remote checkout'}")
    return errors, notes


def render(errors: list[str], notes: list[str]) -> str:
    lines = [
        "# Scheduler project-context validation",
        "",
        f"- schedulers with verified full-worktree access: {len(notes)}",
        f"- errors: {len(errors)}",
        "- policy: every scheduler sees a complete project worktree before narrowing to its bounded responsibility",
        "",
        "## Visibility",
    ]
    lines.extend(f"- {note}" for note in notes)
    if errors:
        lines.extend(["", "## Errors"])
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.extend(["", "All registered schedulers have complete repository visibility and canonical runtime/Grafana context is consistent."])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate project-wide context availability for Polymarket schedulers")
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
