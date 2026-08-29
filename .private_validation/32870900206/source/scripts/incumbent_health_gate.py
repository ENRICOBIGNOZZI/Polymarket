#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import time
from datetime import datetime
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


def parse_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def validate(
    main_sha: str,
    validated_sha: str,
    deploy_enabled: bool,
    server_health: Path | None,
    max_age_seconds: int,
    now_epoch: float,
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
    state["max_health_age_seconds"] = str(max_age_seconds)
    if server_health is None:
        errors.append("server health evidence is required when deployment is enabled")
        return errors, state
    if not server_health.is_file():
        errors.append(f"server health evidence does not exist: {server_health}")
        return errors, state

    values = parse_key_values(server_health)
    required = (
        "timestamp",
        "head",
        "origin_main",
        "paper_validated",
        "recorder_alive",
        "broker_alive",
    )
    for key in required:
        if key not in values:
            errors.append(f"server health evidence is missing {key}")
        else:
            state[f"server_{key}"] = values[key]

    timestamp = values.get("timestamp")
    if timestamp is not None:
        try:
            health_epoch = parse_timestamp(timestamp)
        except ValueError:
            errors.append(f"server health timestamp is invalid: {timestamp}")
        else:
            age = int(now_epoch - health_epoch)
            state["server_health_age_seconds"] = str(age)
            if age < -300:
                errors.append(f"server health timestamp is {abs(age)} seconds in the future")
            elif age > max_age_seconds:
                errors.append(
                    f"server health evidence is stale: age {age}s exceeds {max_age_seconds}s"
                )

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
    parser.add_argument("--max-age-seconds", type=int, default=7200)
    parser.add_argument("--now-epoch", type=float, default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.max_age_seconds <= 0:
        raise SystemExit("--max-age-seconds must be positive")

    health_path = Path(args.server_health) if args.server_health else None
    errors, state = validate(
        args.main_sha,
        args.validated_sha,
        args.deploy_enabled == "true",
        health_path,
        args.max_age_seconds,
        time.time() if args.now_epoch is None else args.now_epoch,
    )
    report = render(state, errors)
    Path(args.output).write_text(report, encoding="utf-8")
    print(report, end="")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
