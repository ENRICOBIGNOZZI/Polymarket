#!/usr/bin/env python3
"""Validate and resolve the declarative V7 long-lived process manifest."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_process_manifest_v1"
AUTHORITY_KEYS = {
    "global_portfolio_coordinator", "capital_allocator", "risk_engine", "oms",
    "inventory", "ledger", "promotion", "runtime_identity",
}


class ProcessManifestError(ValueError):
    pass


def launcher_logs(text: str) -> list[str]:
    lines = text.splitlines()
    previous = 0
    logs: list[str] = []
    for index, line in enumerate(lines):
        if 'pids+=("$!")' not in line and 'v7_register_child "$!"' not in line:
            continue
        segment = "\n".join(lines[previous:index + 1])
        previous = index + 1
        found = re.findall(r"\$RUN_ROOT/([A-Za-z0-9_./${}-]+\.log)", segment)
        if not found:
            raise ProcessManifestError(f"launcher_child_log_missing:{index + 1}")
        logs.append(found[-1])
    return logs


def resolve(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("version") != 1
        or manifest.get("paper_only") is not True
        or manifest.get("authenticated_execution") is not False
        or manifest.get("real_order_submission") is not False
        or manifest.get("real_capital_at_risk") is not False
        or manifest.get("automatic_promotion") is not False
        or manifest.get("launcher") != "scripts/paper_v7_execution_loop.sh"
        or manifest.get("authority_registry") != "config/v7_authority_registry.json"
    ):
        raise ProcessManifestError("manifest_identity_or_safety")
    profiles = manifest.get("profiles")
    rows = manifest.get("processes")
    if not isinstance(profiles, dict) or not isinstance(rows, list) or len(rows) != 22:
        raise ProcessManifestError("profile_or_process_count")
    resolved: list[dict[str, Any]] = []
    ids: set[str] = set()
    declared_logs: set[str] = set()
    required_profile = {
        "owner_class", "restart_policy", "liveness_slo_seconds",
        "freshness_slo_seconds", "fault_domain", "authority_flags",
        "drain_behavior", "archival_behavior",
    }
    for row in rows:
        if not isinstance(row, dict) or not {
            "id", "executable", "arguments", "profile", "inputs", "outputs",
            "dependencies", "exact_sha_required", "config_identity_required",
        } <= set(row):
            raise ProcessManifestError("process_shape")
        process_id = row.get("id")
        profile = profiles.get(row.get("profile"))
        if not isinstance(process_id, str) or not process_id or process_id in ids:
            raise ProcessManifestError("process_id_unique")
        if not isinstance(profile, dict) or set(profile) != required_profile:
            raise ProcessManifestError(f"profile_shape:{process_id}")
        authorities = profile.get("authority_flags")
        if not isinstance(authorities, dict) or set(authorities) != AUTHORITY_KEYS or any(
            not isinstance(value, bool) for value in authorities.values()
        ):
            raise ProcessManifestError(f"authority_flags:{process_id}")
        authorities = dict(authorities)
        overrides = row.get("authority_overrides", {})
        if not isinstance(overrides, dict) or not set(overrides) <= AUTHORITY_KEYS or any(
            not isinstance(value, bool) for value in overrides.values()
        ):
            raise ProcessManifestError(f"authority_overrides:{process_id}")
        authorities.update(overrides)
        for field in ("arguments", "inputs", "outputs", "dependencies"):
            if not isinstance(row.get(field), list) or any(not isinstance(value, str) for value in row[field]):
                raise ProcessManifestError(f"{field}:{process_id}")
        if not row["outputs"] or row.get("exact_sha_required") is not True:
            raise ProcessManifestError(f"output_or_sha:{process_id}")
        if not isinstance(row.get("config_identity_required"), bool):
            raise ProcessManifestError(f"config_identity:{process_id}")
        if (
            isinstance(profile["liveness_slo_seconds"], bool)
            or not isinstance(profile["liveness_slo_seconds"], int)
            or profile["liveness_slo_seconds"] <= 0
            or isinstance(profile["freshness_slo_seconds"], bool)
            or not isinstance(profile["freshness_slo_seconds"], int)
            or profile["freshness_slo_seconds"] <= 0
        ):
            raise ProcessManifestError(f"slo:{process_id}")
        executable = row.get("executable")
        if not isinstance(executable, str) or not executable:
            raise ProcessManifestError(f"executable:{process_id}")
        if executable.startswith(("scripts/", "ops/")) and not (root / executable).is_file():
            raise ProcessManifestError(f"executable_missing:{process_id}")
        log = row.get("launcher_log")
        if log is not None:
            if not isinstance(log, str) or log in declared_logs or log not in row["outputs"]:
                raise ProcessManifestError(f"launcher_log:{process_id}")
            declared_logs.add(log)
        resolved.append({
            **row,
            "owner_class": profile["owner_class"],
            "restart_policy": profile["restart_policy"],
            "liveness_slo_seconds": profile["liveness_slo_seconds"],
            "freshness_slo_seconds": profile["freshness_slo_seconds"],
            "fault_domain": profile["fault_domain"],
            "authority_flags": authorities,
            "drain_behavior": profile["drain_behavior"],
            "archival_behavior": profile["archival_behavior"],
        })
        ids.add(process_id)
    for row in resolved:
        unknown = set(row["dependencies"]) - ids
        if unknown:
            raise ProcessManifestError(f"dependency_unknown:{row['id']}:{sorted(unknown)}")
        if row["owner_class"] == "LIVE_ALGORITHM_DATA_FEED" and (
            any(row["authority_flags"].values())
            or row["restart_policy"] != "FAIL_CLOSED_ENGINE_INPUT"
            or row["drain_behavior"] != "STOP_WITHOUT_INVENTORY"
        ):
            raise ProcessManifestError(f"feed_authority_violation:{row['id']}")
    dependencies = {row["id"]: set(row["dependencies"]) for row in resolved}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(process_id: str) -> None:
        if process_id in visiting:
            raise ProcessManifestError(f"dependency_cycle:{process_id}")
        if process_id in visited:
            return
        visiting.add(process_id)
        for dependency in dependencies[process_id]:
            visit(dependency)
        visiting.remove(process_id)
        visited.add(process_id)

    for process_id in dependencies:
        visit(process_id)
    authority_counts = {
        key: sum(row["authority_flags"][key] for row in resolved)
        for key in AUTHORITY_KEYS
    }
    expected_counts = {key: 1 for key in AUTHORITY_KEYS}
    expected_counts["promotion"] = 0
    if authority_counts != expected_counts:
        raise ProcessManifestError(f"long_lived_authority_counts:{authority_counts}")
    launcher = (root / manifest["launcher"]).read_text(encoding="utf-8")
    actual_logs = launcher_logs(launcher)
    if len(actual_logs) != 20 or len(set(actual_logs)) != 20 or set(actual_logs) != declared_logs:
        raise ProcessManifestError("launcher_manifest_parity")
    return {
        "schema": "polymarket_v7_process_manifest_validation_v1",
        "paper_only": True,
        "process_count": len(resolved),
        "launcher_child_count": len(actual_logs),
        "launcher_manifest_parity": True,
        "feed_zero_authority": True,
        "dependency_graph_acyclic": True,
        "authority_counts": authority_counts,
        "processes": resolved,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, default=Path("config/v7_process_manifest.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        root = args.repository_root.resolve()
        manifest = json.loads((root / args.manifest).read_text(encoding="utf-8"))
        result = resolve(root, manifest)
        summary = {key: result[key] for key in (
            "schema", "paper_only", "process_count", "launcher_child_count",
            "launcher_manifest_parity", "feed_zero_authority",
            "dependency_graph_acyclic", "authority_counts",
        )}
        rendered = json.dumps(result if args.output else summary, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    except (OSError, json.JSONDecodeError, ProcessManifestError) as exc:
        print(f"v7_process_manifest: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
