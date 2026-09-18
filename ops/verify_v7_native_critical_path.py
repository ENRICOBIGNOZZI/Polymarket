#!/usr/bin/env python3
"""Fail-closed deployment gate for the V7 native crypto critical path.

This is a control-plane verifier. It is never part of trigger-to-admission.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EXPECTED_ENGINE = "CRYPTO_SETTLEMENT_ENGINE"
CRITICAL_OWNER_KEYS = {
    "capital_allocator",
    "global_portfolio_coordinator",
    "inventory",
    "oms",
    "risk_engine",
}
INTERPRETED_SUFFIXES = (".py", ".sh", ".bash", ".zsh")
INTERPRETED_PREFIXES = ("scripts/", "launcher_", "python", "bash", "sh ", "zsh")
REQUIRED_FORBIDDEN = {
    "PYTHON_INTERPRETER",
    "FILESYSTEM_POLLING",
    "SYNCHRONOUS_REST_MARKET_DATA",
    "DATABASE_IO",
    "PROCESS_SPAWN",
    "CROSS_PROCESS_JSON_IPC",
    "SYNCHRONOUS_TELEMETRY",
    "UNBOUNDED_ALLOCATION",
    "FIXED_SLEEP_POLLING",
}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be a JSON object")
    return value


def _is_interpreted(executable: str) -> bool:
    token = executable.strip().lower()
    return token.endswith(INTERPRETED_SUFFIXES) or token.startswith(INTERPRETED_PREFIXES)


def validate(policy: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if policy.get("schema") != "polymarket_v7_native_critical_path_policy_v1":
        errors.append("native critical-path policy schema mismatch")
    if policy.get("engine") != EXPECTED_ENGINE:
        errors.append(f"canonical engine must be {EXPECTED_ENGINE}")
    if policy.get("critical_path_language") != "C++":
        errors.append("critical path must be C++")
    if not policy.get("event_driven_required"):
        errors.append("event-driven critical path must be required")
    if not policy.get("bounded_queues_required"):
        errors.append("bounded queues must be required")
    if not policy.get("fail_closed_on_gap_or_overflow"):
        errors.append("gap/overflow handling must be fail-closed")
    for key in (
        "single_execution_owner_required",
        "single_risk_owner_required",
        "single_oms_owner_required",
        "single_inventory_owner_required",
    ):
        if policy.get(key) is not True:
            errors.append(f"policy must require {key}")

    forbidden = set(policy.get("forbidden_hot_path_dependencies") or [])
    missing_forbidden = sorted(REQUIRED_FORBIDDEN - forbidden)
    if missing_forbidden:
        errors.append("policy missing forbidden hot-path dependencies: " + ",".join(missing_forbidden))

    processes = manifest.get("processes")
    if not isinstance(processes, list):
        errors.append("process manifest must contain a processes array")
        return errors

    deployed = [p for p in processes if isinstance(p, dict) and p.get("london_deployed") is True]
    hot = [p for p in deployed if p.get("runtime_class") == "HOT_PATH"]
    if len(hot) != 1:
        ids = ",".join(str(p.get("id", "?")) for p in hot) or "none"
        errors.append(f"exactly one London-deployed HOT_PATH process required; found {len(hot)}: {ids}")
        return errors

    engine = hot[0]
    engine_id = str(engine.get("id") or "")
    executable = str(engine.get("executable") or "")
    if not engine_id:
        errors.append("native HOT_PATH process requires an id")
    if not executable:
        errors.append("native HOT_PATH process requires an executable")
    elif _is_interpreted(executable):
        errors.append(f"HOT_PATH executable must be native C++, not interpreted/shell: {executable}")

    dependencies = engine.get("dependencies") or []
    if dependencies:
        errors.append(
            "single-process HOT_PATH cannot depend on other runtime processes: "
            + ",".join(str(x) for x in dependencies)
        )

    owners = engine.get("authority_overrides") or {}
    missing_owners = sorted(k for k in CRITICAL_OWNER_KEYS if owners.get(k) is not True)
    if missing_owners:
        errors.append("native HOT_PATH must explicitly own portfolio/risk/capital/OMS/inventory chain: " + ",".join(missing_owners))

    for process in deployed:
        if process is engine:
            continue
        overrides = process.get("authority_overrides") or {}
        duplicate = sorted(k for k in CRITICAL_OWNER_KEYS if overrides.get(k) is True)
        if duplicate:
            errors.append(
                f"parallel authority in London process {process.get('id', '?')}: " + ",".join(duplicate)
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        policy = _load(args.policy)
        manifest = _load(args.manifest)
        errors = validate(policy, manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors = [str(exc)]

    receipt = {
        "schema": "polymarket_v7_native_cutover_gate_v1",
        "engine": EXPECTED_ENGINE,
        "ready": not errors,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(receipt, sort_keys=True))
    elif errors:
        for error in errors:
            print(f"native_cutover_gate: {error}", file=sys.stderr)
    else:
        print("native_cutover_gate=ready")
    return 0 if not errors else 78


if __name__ == "__main__":
    raise SystemExit(main())
