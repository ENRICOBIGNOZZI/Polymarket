#!/usr/bin/env python3
"""Immutable V7 research-experiment registry.

An experiment is registered before it runs.  The registry stores one
content-stable specification per exact code SHA and experiment ID; it neither
trains models nor grants any execution authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_experiment_v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
STATUSES = {"REGISTERED", "RUNNING", "COMPLETED", "REJECTED", "ARCHIVED"}
REQUIRED = {
    "schema", "experiment_id", "hypothesis", "primary_metric", "secondary_metrics", "independent_unit",
    "universe_definition", "feature_cut", "label_definition", "cost_model_version", "train_period",
    "validation_period", "final_holdout_period", "purge", "embargo", "hyperparameter_space", "compute_budget",
    "stopping_rule", "multiplicity_family", "random_seed", "code_sha", "data_manifest", "status", "result",
}


class ExperimentRegistryError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _text(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentRegistryError(f"{field}:invalid")
    return value.strip()


def _period(field: str, value: Any) -> tuple[datetime, datetime]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ExperimentRegistryError(f"{field}:shape")
    parsed: list[datetime] = []
    for item in (value["start"], value["end"]):
        try:
            timestamp = datetime.fromisoformat(_text(field, item).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ExperimentRegistryError(f"{field}:timestamp") from exc
        if timestamp.tzinfo is None:
            raise ExperimentRegistryError(f"{field}:timezone")
        parsed.append(timestamp)
    if parsed[0] >= parsed[1]:
        raise ExperimentRegistryError(f"{field}:range")
    return parsed[0], parsed[1]


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != REQUIRED:
        raise ExperimentRegistryError("experiment:shape")
    if value["schema"] != SCHEMA or not IDENTIFIER_RE.fullmatch(str(value["experiment_id"])):
        raise ExperimentRegistryError("experiment:identity")
    for field in ("hypothesis", "primary_metric", "independent_unit", "universe_definition", "feature_cut",
                  "label_definition", "cost_model_version", "purge", "embargo", "stopping_rule", "multiplicity_family"):
        _text(field, value[field])
    if (not isinstance(value["secondary_metrics"], list) or not value["secondary_metrics"]
            or any(not isinstance(metric, str) or not metric.strip() for metric in value["secondary_metrics"])):
        raise ExperimentRegistryError("secondary_metrics:invalid")
    train_start, train_end = _period("train_period", value["train_period"])
    validation_start, validation_end = _period("validation_period", value["validation_period"])
    holdout_start, holdout_end = _period("final_holdout_period", value["final_holdout_period"])
    if train_end > validation_start or validation_end > holdout_start:
        raise ExperimentRegistryError("periods:not_chronological")
    if not isinstance(value["hyperparameter_space"], dict) or not isinstance(value["compute_budget"], dict):
        raise ExperimentRegistryError("research_spec:invalid")
    budget = value["compute_budget"]
    if set(budget) != {"maximum_cpu_hours", "maximum_gpu_hours", "maximum_memory_bytes"}:
        raise ExperimentRegistryError("compute_budget:shape")
    for field in ("maximum_cpu_hours", "maximum_gpu_hours", "maximum_memory_bytes"):
        if (isinstance(budget[field], bool) or not isinstance(budget[field], (int, float))
                or budget[field] < 0 or (field == "maximum_memory_bytes" and budget[field] < 1)):
            raise ExperimentRegistryError("compute_budget:invalid")
    if isinstance(value["random_seed"], bool) or not isinstance(value["random_seed"], int) or value["random_seed"] < 0:
        raise ExperimentRegistryError("random_seed:invalid")
    if not SHA_RE.fullmatch(str(value["code_sha"])) or not SHA256_RE.fullmatch(str(value["data_manifest"])):
        raise ExperimentRegistryError("provenance:invalid")
    if value["status"] not in STATUSES:
        raise ExperimentRegistryError("status:invalid")
    if value["status"] == "REGISTERED" and value["result"] is not None:
        raise ExperimentRegistryError("result:registered_experiment_must_be_unobserved")
    if value["status"] in {"COMPLETED", "REJECTED", "ARCHIVED"} and not isinstance(value["result"], dict):
        raise ExperimentRegistryError("result:terminal_experiment_requires_object")
    # Explicitly consume parsed periods to keep every range checked above.
    if holdout_end <= holdout_start or validation_end <= validation_start or train_end <= train_start:
        raise ExperimentRegistryError("periods:invalid")
    return value


def registry_path(root: Path, experiment: dict[str, Any]) -> Path:
    return Path(root) / "experiments" / experiment["code_sha"] / f"{experiment['experiment_id']}.json"


def immutable_register(root: Path, value: Any) -> Path:
    experiment = validate(value)
    root = Path(root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise ExperimentRegistryError("registry_root:invalid")
    output = registry_path(root, experiment)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink() or output.is_symlink():
        raise ExperimentRegistryError("registry_path:symlink")
    payload = canonical_bytes(experiment) + b"\n"
    if output.exists():
        if not output.is_file() or output.read_bytes() != payload:
            raise ExperimentRegistryError("registry:immutable_collision")
        return output
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o444)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output, follow_symlinks=False)
    except FileExistsError:
        if output.is_symlink() or not output.is_file() or output.read_bytes() != payload:
            raise ExperimentRegistryError("registry:immutable_collision")
    except OSError as exc:
        raise ExperimentRegistryError("registry:write_failed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return output


def _load(path: Path) -> dict[str, Any]:
    try:
        return validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentRegistryError("input:unreadable") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--input", type=Path, required=True)
    register.add_argument("--root", type=Path, required=True)
    check = commands.add_parser("validate")
    check.add_argument("input", type=Path)
    args = parser.parse_args(argv)
    try:
        value = _load(args.input)
        if args.command == "validate":
            print(json.dumps({"valid": True, "experiment_id": value["experiment_id"]}, sort_keys=True))
        else:
            output = immutable_register(args.root, value)
            print(json.dumps({"experiment_id": value["experiment_id"], "path": str(output)}, sort_keys=True))
        return 0
    except ExperimentRegistryError as exc:
        print(f"v7_experiment_registry: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
