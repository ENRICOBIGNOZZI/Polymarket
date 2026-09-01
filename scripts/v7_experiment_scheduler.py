#!/usr/bin/env python3
"""Immutable, budget-bound receipts for pre-registered V7 experiments.

This scheduler is deliberately a slow-plane admission and lineage component:
it does not execute a supplied command, train a model, access private state,
or alter a champion. A worker reports its measured terminal attempt; the
scheduler validates resource ceilings and stores an immutable receipt. A failed
or stopped attempt may be resumed only with the same registered experiment,
code SHA, data manifest, and seed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_experiment_run"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
STATUSES = {"COMPLETED", "FAILED", "STOPPED"}
STOPPING = {"COMPLETED", "BUDGET_EXHAUSTED", "EXTERNAL_STOP", "FAILURE"}


class ExperimentSchedulerError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _registry_module() -> Any:
    name = "v7_experiment_registry_for_scheduler"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("v7_experiment_registry.py"))
    if spec is None or spec.loader is None:
        raise ExperimentSchedulerError("registry:unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ExperimentSchedulerError(f"{field}:invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExperimentSchedulerError(f"{field}:invalid") from exc
    if parsed.tzinfo is None:
        raise ExperimentSchedulerError(f"{field}:timezone")
    return parsed.astimezone(timezone.utc)


def _number(value: Any, field: str, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ExperimentSchedulerError(f"{field}:invalid")
    if integer and not isinstance(value, int):
        raise ExperimentSchedulerError(f"{field}:invalid")
    return value


def _hashes(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ExperimentSchedulerError(f"{field}:invalid")
    output: list[dict[str, str]] = []
    names: set[str] = set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"name", "sha256"}
                or not IDENTIFIER_RE.fullmatch(str(row.get("name")))
                or not SHA256_RE.fullmatch(str(row.get("sha256")))
                or row["name"] in names):
            raise ExperimentSchedulerError(f"{field}:invalid")
        names.add(row["name"])
        output.append({"name": row["name"], "sha256": row["sha256"]})
    if output != sorted(output, key=lambda row: row["name"]):
        raise ExperimentSchedulerError(f"{field}:not_sorted")
    return output


def experiment_sha256(experiment: Any) -> str:
    try:
        return digest(_registry_module().validate(experiment))
    except ValueError as exc:
        raise ExperimentSchedulerError(f"experiment:invalid:{exc}") from exc


def validate_run(value: Any, *, experiment: Any) -> dict[str, Any]:
    registry = _registry_module()
    try:
        experiment = registry.validate(experiment)
    except ValueError as exc:
        raise ExperimentSchedulerError(f"experiment:invalid:{exc}") from exc
    required = {
        "schema", "run_id", "attempt", "experiment_id", "experiment_sha256", "code_sha", "data_manifest",
        "random_seed", "resume_of_run_sha256", "started_at", "ended_at", "wall_time_seconds",
        "resource_usage", "cached_intermediates", "stopping_condition", "status", "failure_reason",
        "output_hashes", "run_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ExperimentSchedulerError("run:shape")
    if value.get("schema") != SCHEMA or not IDENTIFIER_RE.fullmatch(str(value.get("run_id"))):
        raise ExperimentSchedulerError("run:identity")
    if (not isinstance(value.get("attempt"), int) or isinstance(value.get("attempt"), bool)
            or value["attempt"] < 1 or value.get("experiment_id") != experiment["experiment_id"]
            or value.get("experiment_sha256") != experiment_sha256(experiment)
            or value.get("code_sha") != experiment["code_sha"] or value.get("data_manifest") != experiment["data_manifest"]
            or value.get("random_seed") != experiment["random_seed"]):
        raise ExperimentSchedulerError("run:identity")
    resume = value.get("resume_of_run_sha256")
    if resume is not None and (not isinstance(resume, str) or not SHA256_RE.fullmatch(resume)):
        raise ExperimentSchedulerError("run:resume_identity")
    started, ended = _timestamp(value.get("started_at"), "run:started_at"), _timestamp(value.get("ended_at"), "run:ended_at")
    wall = _number(value.get("wall_time_seconds"), "run:wall_time_seconds")
    if ended < started or abs((ended - started).total_seconds() - wall) > 0.001:
        raise ExperimentSchedulerError("run:wall_time_mismatch")
    usage = value.get("resource_usage")
    if not isinstance(usage, dict) or set(usage) != {"cpu_seconds", "gpu_seconds", "peak_memory_bytes"}:
        raise ExperimentSchedulerError("run:resource_usage")
    cpu = _number(usage.get("cpu_seconds"), "run:cpu_seconds")
    gpu = _number(usage.get("gpu_seconds"), "run:gpu_seconds")
    memory = _number(usage.get("peak_memory_bytes"), "run:peak_memory_bytes", integer=True)
    budget = experiment["compute_budget"]
    if (cpu > budget["maximum_cpu_hours"] * 3600 or gpu > budget["maximum_gpu_hours"] * 3600
            or memory > budget["maximum_memory_bytes"]):
        raise ExperimentSchedulerError("run:compute_budget_exceeded")
    _hashes(value.get("cached_intermediates"), "run:cached_intermediates")
    outputs = _hashes(value.get("output_hashes"), "run:output_hashes")
    status, stopping, failure = value.get("status"), value.get("stopping_condition"), value.get("failure_reason")
    if status not in STATUSES or stopping not in STOPPING:
        raise ExperimentSchedulerError("run:status")
    if status == "COMPLETED" and (stopping != "COMPLETED" or failure is not None or not outputs):
        raise ExperimentSchedulerError("run:completed_contract")
    if status == "FAILED" and (stopping != "FAILURE" or not isinstance(failure, str) or not failure.strip()):
        raise ExperimentSchedulerError("run:failed_contract")
    if status == "STOPPED" and (stopping not in {"BUDGET_EXHAUSTED", "EXTERNAL_STOP"} or failure is not None):
        raise ExperimentSchedulerError("run:stopped_contract")
    unhashed = dict(value)
    supplied = unhashed.pop("run_sha256")
    if not isinstance(supplied, str) or supplied != digest(unhashed):
        raise ExperimentSchedulerError("run:sha256")
    return value


def build_run(*, experiment: Any, attempt: int, started_at: str, ended_at: str,
              resource_usage: dict[str, Any], cached_intermediates: list[dict[str, str]],
              stopping_condition: str, status: str, failure_reason: str | None,
              output_hashes: list[dict[str, str]], resume_of_run_sha256: str | None = None) -> dict[str, Any]:
    registry = _registry_module()
    experiment = registry.validate(experiment)
    started, ended = _timestamp(started_at, "run:started_at"), _timestamp(ended_at, "run:ended_at")
    if ended < started:
        raise ExperimentSchedulerError("run:wall_time_mismatch")
    value: dict[str, Any] = {
        "schema": SCHEMA, "run_id": f"{experiment['experiment_id']}-run-{attempt:04d}", "attempt": attempt,
        "experiment_id": experiment["experiment_id"], "experiment_sha256": experiment_sha256(experiment),
        "code_sha": experiment["code_sha"], "data_manifest": experiment["data_manifest"],
        "random_seed": experiment["random_seed"], "resume_of_run_sha256": resume_of_run_sha256,
        "started_at": started.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ended_at": ended.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "wall_time_seconds": (ended - started).total_seconds(), "resource_usage": resource_usage,
        "cached_intermediates": cached_intermediates, "stopping_condition": stopping_condition,
        "status": status, "failure_reason": failure_reason, "output_hashes": output_hashes,
    }
    value["run_sha256"] = digest(value)
    return validate_run(value, experiment=experiment)


def run_directory(root: Path, experiment: dict[str, Any]) -> Path:
    return Path(root).resolve() / "experiments" / experiment["code_sha"] / experiment["experiment_id"] / "runs"


def _existing_runs(root: Path, experiment: dict[str, Any]) -> list[dict[str, Any]]:
    directory = run_directory(root, experiment)
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ExperimentSchedulerError("run_directory:invalid")
    rows = []
    for candidate in sorted(directory.glob("*.json")):
        if candidate.is_symlink():
            raise ExperimentSchedulerError("run_path:symlink")
        try:
            rows.append(validate_run(json.loads(candidate.read_text(encoding="utf-8")), experiment=experiment))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExperimentSchedulerError("run_path:unreadable") from exc
    if [row["attempt"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ExperimentSchedulerError("runs:attempt_sequence")
    return rows


def immutable_record(root: Path, value: Any, *, experiment: Any) -> Path:
    registry = _registry_module()
    experiment = registry.validate(experiment)
    run = validate_run(value, experiment=experiment)
    root = Path(root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise ExperimentSchedulerError("scheduler_root:invalid")
    existing = _existing_runs(root, experiment)
    if run["attempt"] <= len(existing):
        stored = existing[run["attempt"] - 1]
        if stored["run_sha256"] != run["run_sha256"]:
            raise ExperimentSchedulerError("runs:immutable_collision")
        return run_directory(root, experiment) / f"{run['attempt']:04d}.json"
    if run["attempt"] != len(existing) + 1:
        raise ExperimentSchedulerError("runs:attempt_sequence")
    if not existing and run["resume_of_run_sha256"] is not None:
        raise ExperimentSchedulerError("runs:unexpected_resume")
    if existing:
        previous = existing[-1]
        if previous["status"] == "COMPLETED":
            raise ExperimentSchedulerError("runs:completed_experiment_cannot_resume")
        if run["resume_of_run_sha256"] != previous["run_sha256"]:
            raise ExperimentSchedulerError("runs:resume_chain")
    directory = run_directory(root, experiment)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ExperimentSchedulerError("run_directory:invalid")
    output = directory / f"{run['attempt']:04d}.json"
    if output.is_symlink():
        raise ExperimentSchedulerError("run_path:symlink")
    payload = canonical_bytes(run) + b"\n"
    if output.exists():
        if not output.is_file() or output.read_bytes() != payload:
            raise ExperimentSchedulerError("runs:immutable_collision")
        return output
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o444)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output, follow_symlinks=False)
    except FileExistsError:
        if not output.is_file() or output.is_symlink() or output.read_bytes() != payload:
            raise ExperimentSchedulerError("runs:immutable_collision")
    except OSError as exc:
        raise ExperimentSchedulerError("runs:write_failed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return output


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentSchedulerError("input:unreadable") from exc
    if not isinstance(value, dict):
        raise ExperimentSchedulerError("input:invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--experiment", type=Path, required=True)
    validate.add_argument("--run", type=Path, required=True)
    record = commands.add_parser("record")
    record.add_argument("--root", type=Path, required=True)
    record.add_argument("--experiment", type=Path, required=True)
    record.add_argument("--run", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        experiment, run = _load_json(args.experiment), _load_json(args.run)
        validated = validate_run(run, experiment=experiment)
        if args.command == "record":
            output = immutable_record(args.root, validated, experiment=experiment)
            print(json.dumps({"path": str(output), "run_sha256": validated["run_sha256"]}, sort_keys=True))
        else:
            print(json.dumps({"valid": True, "run_sha256": validated["run_sha256"]}, sort_keys=True))
        return 0
    except ExperimentSchedulerError as exc:
        print(f"v7_experiment_scheduler: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
