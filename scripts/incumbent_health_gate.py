#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def parse_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in values:
            values[key] = value
    return values


def validate(
    main_sha: str,
    validated_sha: str,
    deploy_enabled: bool,
    server_health: Path | None,
) -> tuple[list[str], dict[str, str]]:
    errors: list[str] = []
    state: dict[str, str] = {
        "main": main_sha,
        "paper_validated": validated_sha,
        "deploy_enabled": "true" if deploy_enabled else "false",
    }

    if not SHA_RE.fullmatch(main_sha):
        errors.append("main SHA is invalid")
    if not SHA_RE.fullmatch(validated_sha):
        errors.append("paper-validated SHA is invalid")
    if main_sha != validated_sha:
        errors.append("main and paper-validated are not equal")

    if not deploy_enabled:
        state["deployment_gate"] = "disabled"
        return errors, state

    state["deployment_gate"] = "required"
    if server_health is None:
        errors.append("server health evidence is required when deployment is enabled")
        return errors, state
    if not server_health.is_file():
        errors.append(f"server health evidence does not exist: {server_health}")
        return errors, state

    values = parse_key_values(server_health)
    for key in ("head", "origin_main", "paper_validated", "recorder_alive", "broker_alive"):
        if key not in values:
            errors.append(f"server health evidence is missing {key}")
        else:
            state[f"server_{key}"] = values[key]

    for key in ("head", "origin_main", "paper_validated"):
        value = values.get(key)
        if value is not None and value != main_sha:
            errors.append(f"server {key}={value} does not match main={main_sha}")

    for key in ("recorder_alive", "broker_alive"):
        value = values.get(key)
        if value is not None and value != "1":
            errors.append(f"server {key} is {value}, expected 1")

    return errors, state


def render(state: dict[str, str], errors: list[str]) -> str:
    lines = ["# Incumbent champion gate", ""]
    for key in sorted(state):
        lines.append(f"- {key}: `{state[key]}`")
    lines.extend(["", "## Decision"])
    if errors:
        lines.append("BLOCKED: incumbent validation/deployment/health is incomplete.")
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.append("PASS: incumbent is fully validated and, when enabled, deployed and healthy.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Require a fully validated and healthy incumbent before model integration"
    )
    parser.add_argument("--main-sha", required=True)
    parser.add_argument("--validated-sha", required=True)
    parser.add_argument("--deploy-enabled", choices=("true", "false"), required=True)
    parser.add_argument("--server-health")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    health_path = Path(args.server_health) if args.server_health else None
    errors, state = validate(
        args.main_sha,
        args.validated_sha,
        args.deploy_enabled == "true",
        health_path,
    )
    report = render(state, errors)
    Path(args.output).write_text(report, encoding="utf-8")
    print(report, end="")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
