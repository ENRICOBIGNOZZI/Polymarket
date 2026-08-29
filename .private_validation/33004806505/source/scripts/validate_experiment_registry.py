#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from research_common import integer, safe_relative_script

VALID_STATUSES = {"active", "planned", "paused", "retired"}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def forbidden_token(command: list[str], forbidden: set[str]) -> str | None:
    for token in command:
        for item in forbidden:
            if token == item or token.startswith(item + "="):
                return item
    return None


def validate(root: Path, control: dict[str, Any], registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if control.get("paper_only") is not True:
        errors.append("autonomous research must remain paper_only")
    if control.get("allow_authenticated_execution") is not False:
        errors.append("authenticated execution must remain disabled")
    if control.get("allow_direct_champion_mutation") is not False:
        errors.append("direct champion mutation must remain disabled")
    if registry.get("schema") != "polymarket_experiment_registry_v1":
        errors.append("unexpected experiment registry schema")

    dispatcher = control.get("dispatcher") if isinstance(control.get("dispatcher"), dict) else {}
    allowed_interpreters = {str(x) for x in dispatcher.get("allowed_interpreters") or []}
    allowed_scripts = {str(x) for x in dispatcher.get("allowed_scripts") or []}
    forbidden = {str(x) for x in dispatcher.get("forbidden_argument_tokens") or []}
    maximum_timeout = max(1, integer(dispatcher.get("maximum_timeout_seconds"), 1800))

    experiments = registry.get("experiments")
    if not isinstance(experiments, list):
        return errors + ["experiments must be a list"]

    seen_ids: set[str] = set()
    for index, raw in enumerate(experiments):
        if not isinstance(raw, dict):
            errors.append(f"experiment[{index}] must be an object")
            continue
        experiment_id = str(raw.get("experiment_id") or "").strip()
        status = str(raw.get("status") or "").strip()
        if not experiment_id:
            errors.append(f"experiment[{index}] has no experiment_id")
        elif experiment_id in seen_ids:
            errors.append(f"duplicate experiment_id: {experiment_id}")
        seen_ids.add(experiment_id)
        if status not in VALID_STATUSES:
            errors.append(f"{experiment_id or index}: invalid status {status!r}")
        cadence = integer(raw.get("cadence_seconds"), 0)
        timeout = integer(raw.get("timeout_seconds"), 0)
        if cadence <= 0:
            errors.append(f"{experiment_id or index}: cadence_seconds must be positive")
        if timeout <= 0 or timeout > maximum_timeout:
            errors.append(
                f"{experiment_id or index}: timeout_seconds must be in [1, {maximum_timeout}]"
            )

        command_raw = raw.get("command")
        command = [str(x) for x in command_raw] if isinstance(command_raw, list) else []
        if status != "active":
            continue
        if len(command) < 2:
            errors.append(f"{experiment_id}: active experiment requires an executable command")
            continue
        interpreter, script = command[0], command[1]
        if interpreter not in allowed_interpreters:
            errors.append(f"{experiment_id}: interpreter {interpreter!r} is not allowlisted")
        if not safe_relative_script(script):
            errors.append(f"{experiment_id}: unsafe script path {script!r}")
        elif script not in allowed_scripts:
            errors.append(f"{experiment_id}: script {script!r} is not allowlisted")
        elif not (root / script).is_file():
            errors.append(f"{experiment_id}: active script is missing: {script}")
        forbidden_arg = forbidden_token(command[2:], forbidden)
        if forbidden_arg:
            errors.append(f"{experiment_id}: forbidden argument token {forbidden_arg}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate bounded autonomous research experiments")
    parser.add_argument("--root", default=".")
    parser.add_argument("--control", default="config/autonomous_research.json")
    parser.add_argument("--registry", default="config/experiment_registry.json")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        control = load_json(root / args.control)
        registry = load_json(root / args.registry)
        errors = validate(root, control, registry)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors = [str(exc)]
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Autonomous experiment registry is fail-closed and valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
