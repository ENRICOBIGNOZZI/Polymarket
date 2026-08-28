#!/usr/bin/env python3
"""Create and validate canonical immutable identities for economic V7 runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "polymarket_v7_run_manifest_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^v7-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")


class ManifestError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repository_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository_root), *arguments], check=False,
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise ManifestError("git:" + ":".join(arguments) + ":failed")
    return result.stdout.strip()


def repository_identity(repository_root: Path) -> str:
    sha = _git(repository_root, "rev-parse", "HEAD")
    if not GIT_SHA_RE.fullmatch(sha):
        raise ManifestError("code_sha:not_exact_git_sha")
    if _git(repository_root, "status", "--porcelain", "--untracked-files=no"):
        raise ManifestError("repository:tracked_worktree_dirty")
    return sha


def _iso_time(value: str | None) -> tuple[str, datetime]:
    if value is None:
        parsed = datetime.now(timezone.utc)
        return parsed.isoformat(timespec="seconds").replace("+00:00", "Z"), parsed
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError("start_time:invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ManifestError("start_time:timezone_required")
    return value, parsed


def _artifact(path: Path, base: Path) -> dict[str, str]:
    if not path.is_file():
        raise ManifestError(f"artifact:{path}:not_a_file")
    resolved = path.resolve()
    try:
        display = resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        display = resolved.as_posix()
    return {"path": display, "sha256": sha256_file(path)}


def _named_artifacts(values: Iterable[str], base: Path) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        name, separator, reference = value.partition("=")
        if not separator or not name or not reference:
            raise ManifestError("model:expected_NAME=PATH_OR_SHA256")
        if name in seen:
            raise ManifestError(f"model:{name}:duplicate")
        seen.add(name)
        if SHA256_RE.fullmatch(reference):
            out.append({"name": name, "path": "", "sha256": reference})
        else:
            artifact = _artifact(Path(reference), base)
            out.append({"name": name, **artifact})
    return sorted(out, key=lambda row: row["name"])


def _checked_manifest(path: Path, expected_schema: str, base: Path) -> dict[str, str]:
    artifact = _artifact(path, base)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"artifact:{path}:invalid_json") from exc
    if not isinstance(value, dict) or value.get("schema") != expected_schema:
        raise ManifestError(f"artifact:{path}:wrong_schema")
    supplied = str(value.get("manifest_sha256") or "")
    unhashed = dict(value)
    unhashed.pop("manifest_sha256", None)
    if not SHA256_RE.fullmatch(supplied) or supplied != hashlib.sha256(canonical_bytes(unhashed)).hexdigest():
        raise ManifestError(f"artifact:{path}:invalid_manifest_hash")
    return {**artifact, "manifest_sha256": supplied}


def _hash_or_file(value: str, field: str, base: Path) -> dict[str, str]:
    if SHA256_RE.fullmatch(value):
        return {"path": "", "sha256": value}
    try:
        return _artifact(Path(value), base)
    except ManifestError as exc:
        raise ManifestError(f"{field}:invalid_reference") from exc


def build_manifest(
    *,
    code_sha: str,
    paper_validated_sha: str,
    config: Path,
    strategy_registry: Path,
    models: list[str],
    dataset_manifests: list[Path],
    universe_snapshot: Path,
    fee_schedule_version: str,
    execution_model_version: str,
    contract_mapping: str,
    oracle_mapping: str,
    build_manifest: Path,
    start_time: str | None,
    host: str,
    repository_root: Path,
    run_id: str | None = None,
) -> dict[str, Any]:
    if not GIT_SHA_RE.fullmatch(code_sha):
        raise ManifestError("code_sha:not_exact_git_sha")
    if not GIT_SHA_RE.fullmatch(paper_validated_sha):
        raise ManifestError("paper_validated_sha:not_exact_git_sha")
    if not fee_schedule_version.strip():
        raise ManifestError("fee_schedule_version:empty")
    if not execution_model_version.strip():
        raise ManifestError("execution_model_version:empty")
    if not host.strip():
        raise ManifestError("host:empty")
    base = repository_root.resolve()
    model_entries = _named_artifacts(models, base)
    if not model_entries:
        raise ManifestError("models:empty")
    datasets = [
        _checked_manifest(path, "polymarket_v7_dataset_manifest_v1", base)
        for path in dataset_manifests
    ]
    if not datasets:
        raise ManifestError("dataset_manifests:empty")
    datasets.sort(key=lambda row: row["path"])
    build = _checked_manifest(build_manifest, "polymarket_v7_build_manifest_v1", base)
    build_value = json.loads(build_manifest.read_text(encoding="utf-8"))
    if build_value.get("code_sha") != code_sha:
        raise ManifestError("build_manifest:code_sha_mismatch")
    binaries = build_value.get("binaries")
    if not isinstance(binaries, list) or not binaries:
        raise ManifestError("build_manifest:binaries_empty")

    start_text, start_parsed = _iso_time(start_time)
    config_artifact = _artifact(config, base)
    registry_artifact = _artifact(strategy_registry, base)
    universe_artifact = _artifact(universe_snapshot, base)
    contract_artifact = _hash_or_file(contract_mapping, "contract_mapping", base)
    oracle_artifact = _hash_or_file(oracle_mapping, "oracle_mapping", base)
    dataset_manifest_sha = hashlib.sha256(
        canonical_bytes([row["manifest_sha256"] for row in datasets])
    ).hexdigest()
    model_sha = hashlib.sha256(
        canonical_bytes([{"name": row["name"], "sha256": row["sha256"]} for row in model_entries])
    ).hexdigest()
    binary_sha = hashlib.sha256(canonical_bytes(binaries)).hexdigest()

    identity = {
        "code_sha": code_sha,
        "paper_validated_sha": paper_validated_sha,
        "config_sha": config_artifact["sha256"],
        "strategy_registry_sha": registry_artifact["sha256"],
        "model_sha": model_sha,
        "dataset_manifest_sha": dataset_manifest_sha,
        "universe_snapshot_sha": universe_artifact["sha256"],
        "fee_schedule_version": fee_schedule_version,
        "execution_model_version": execution_model_version,
        "contract_mapping_sha": contract_artifact["sha256"],
        "oracle_mapping_sha": oracle_artifact["sha256"],
        "binary_sha": binary_sha,
        "build_manifest_sha": build["manifest_sha256"],
    }
    identity_digest = hashlib.sha256(canonical_bytes(identity)).hexdigest()
    generated_run_id = f"v7-{start_parsed.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{identity_digest[:12]}"
    resolved_run_id = run_id or generated_run_id
    if not RUN_ID_RE.fullmatch(resolved_run_id):
        raise ManifestError("run_id:invalid")
    if resolved_run_id != generated_run_id:
        raise ManifestError("run_id:not_canonical_for_identity_and_start_time")

    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": resolved_run_id,
        "code_sha": code_sha,
        "paper_validated_sha": paper_validated_sha,
        "config": config_artifact,
        "config_sha": config_artifact["sha256"],
        "strategy_registry": registry_artifact,
        "strategy_registry_sha": registry_artifact["sha256"],
        "models": model_entries,
        "model_sha": model_sha,
        "dataset_manifests": datasets,
        "dataset_manifest_sha": dataset_manifest_sha,
        "universe_snapshot": universe_artifact,
        "universe_snapshot_sha": universe_artifact["sha256"],
        "fee_schedule_version": fee_schedule_version,
        "execution_model_version": execution_model_version,
        "contract_mapping": contract_artifact,
        "contract_mapping_sha": contract_artifact["sha256"],
        "oracle_mapping": oracle_artifact,
        "oracle_mapping_sha": oracle_artifact["sha256"],
        "build_manifest": build,
        "build_manifest_sha": build["manifest_sha256"],
        "binaries": binaries,
        "binary_sha": binary_sha,
        "start_time": start_text,
        "host": host,
        "paper_status": {
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    validate_manifest(manifest)
    return manifest


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("manifest:not_an_object")
    required = {
        "schema", "run_id", "code_sha", "paper_validated_sha", "config", "config_sha",
        "strategy_registry", "strategy_registry_sha", "models", "model_sha",
        "dataset_manifests", "dataset_manifest_sha", "universe_snapshot",
        "universe_snapshot_sha", "fee_schedule_version", "execution_model_version",
        "contract_mapping", "contract_mapping_sha", "oracle_mapping", "oracle_mapping_sha",
        "build_manifest", "build_manifest_sha", "binaries", "binary_sha", "start_time",
        "host", "paper_status", "manifest_sha256",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise ManifestError("manifest:missing:" + ",".join(missing))
    if value["schema"] != SCHEMA:
        raise ManifestError("schema:unsupported")
    if not RUN_ID_RE.fullmatch(str(value["run_id"])):
        raise ManifestError("run_id:invalid")
    for field in ("code_sha", "paper_validated_sha"):
        if not GIT_SHA_RE.fullmatch(str(value[field])):
            raise ManifestError(f"{field}:not_exact_git_sha")
    for field in (
        "config_sha", "strategy_registry_sha", "model_sha", "dataset_manifest_sha",
        "universe_snapshot_sha", "contract_mapping_sha", "oracle_mapping_sha",
        "build_manifest_sha", "binary_sha",
    ):
        if not SHA256_RE.fullmatch(str(value[field])):
            raise ManifestError(f"{field}:invalid")
    if not str(value["fee_schedule_version"]).strip():
        raise ManifestError("fee_schedule_version:empty")
    if not str(value["execution_model_version"]).strip():
        raise ManifestError("execution_model_version:empty")
    if not str(value["host"]).strip():
        raise ManifestError("host:empty")
    for field, sha_field in (
        ("config", "config_sha"), ("strategy_registry", "strategy_registry_sha"),
        ("universe_snapshot", "universe_snapshot_sha"),
        ("contract_mapping", "contract_mapping_sha"), ("oracle_mapping", "oracle_mapping_sha"),
    ):
        artifact = value[field]
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise ManifestError(f"{field}:invalid")
        if artifact["sha256"] != value[sha_field]:
            raise ManifestError(f"{field}:hash_mismatch")
    models = value["models"]
    if not isinstance(models, list) or not models:
        raise ManifestError("models:empty")
    for row in models:
        if not isinstance(row, dict) or set(row) != {"name", "path", "sha256"}:
            raise ManifestError("models:invalid_entry")
        if not row["name"] or not SHA256_RE.fullmatch(str(row["sha256"])):
            raise ManifestError("models:invalid_identity")
    expected_model = hashlib.sha256(canonical_bytes([
        {"name": row["name"], "sha256": row["sha256"]} for row in models
    ])).hexdigest()
    if expected_model != value["model_sha"]:
        raise ManifestError("model_sha:mismatch")
    datasets = value["dataset_manifests"]
    if not isinstance(datasets, list) or not datasets:
        raise ManifestError("dataset_manifests:empty")
    for row in datasets:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "manifest_sha256"}:
            raise ManifestError("dataset_manifests:invalid_entry")
        if not row["path"] or not SHA256_RE.fullmatch(str(row["sha256"])) or not SHA256_RE.fullmatch(str(row["manifest_sha256"])):
            raise ManifestError("dataset_manifests:invalid_identity")
    expected_datasets = hashlib.sha256(canonical_bytes([
        row["manifest_sha256"] for row in datasets
    ])).hexdigest()
    if expected_datasets != value["dataset_manifest_sha"]:
        raise ManifestError("dataset_manifest_sha:mismatch")
    build = value["build_manifest"]
    if not isinstance(build, dict) or set(build) != {"path", "sha256", "manifest_sha256"}:
        raise ManifestError("build_manifest:invalid")
    if build["manifest_sha256"] != value["build_manifest_sha"]:
        raise ManifestError("build_manifest_sha:mismatch")
    binaries = value["binaries"]
    if not isinstance(binaries, list) or not binaries:
        raise ManifestError("binaries:empty")
    if hashlib.sha256(canonical_bytes(binaries)).hexdigest() != value["binary_sha"]:
        raise ManifestError("binary_sha:mismatch")
    paper = value["paper_status"]
    if paper != {
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
    }:
        raise ManifestError("paper_status:not_fail_closed")
    _, start_parsed = _iso_time(str(value["start_time"]))
    identity = {
        "code_sha": value["code_sha"],
        "paper_validated_sha": value["paper_validated_sha"],
        "config_sha": value["config_sha"],
        "strategy_registry_sha": value["strategy_registry_sha"],
        "model_sha": value["model_sha"],
        "dataset_manifest_sha": value["dataset_manifest_sha"],
        "universe_snapshot_sha": value["universe_snapshot_sha"],
        "fee_schedule_version": value["fee_schedule_version"],
        "execution_model_version": value["execution_model_version"],
        "contract_mapping_sha": value["contract_mapping_sha"],
        "oracle_mapping_sha": value["oracle_mapping_sha"],
        "binary_sha": value["binary_sha"],
        "build_manifest_sha": value["build_manifest_sha"],
    }
    identity_digest = hashlib.sha256(canonical_bytes(identity)).hexdigest()
    expected_run_id = f"v7-{start_parsed.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{identity_digest[:12]}"
    if value["run_id"] != expected_run_id:
        raise ManifestError("run_id:identity_mismatch")
    supplied_hash = str(value["manifest_sha256"])
    unhashed = dict(value)
    unhashed.pop("manifest_sha256")
    if supplied_hash != hashlib.sha256(canonical_bytes(unhashed)).hexdigest():
        raise ManifestError("manifest_sha256:mismatch")
    return value


def immutable_write(path: Path, value: dict[str, Any]) -> None:
    payload = canonical_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ManifestError("output:immutable_path_collision")
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="write runs/<run_id>/manifest.json")
    create.add_argument("--repository-root", type=Path, default=Path.cwd())
    create.add_argument("--runs-root", type=Path, default=Path("runs"))
    create.add_argument("--output", type=Path)
    create.add_argument("--code-sha")
    create.add_argument("--paper-validated-sha", required=True)
    create.add_argument("--config", type=Path, required=True)
    create.add_argument("--strategy-registry", type=Path, required=True)
    create.add_argument("--model", action="append", required=True, metavar="NAME=PATH_OR_SHA256")
    create.add_argument("--dataset-manifest", action="append", type=Path, required=True)
    create.add_argument("--universe-snapshot", type=Path, required=True)
    create.add_argument("--fee-schedule-version", required=True)
    create.add_argument("--execution-model-version", required=True)
    create.add_argument("--contract-mapping", required=True, metavar="PATH_OR_SHA256")
    create.add_argument("--oracle-mapping", required=True, metavar="PATH_OR_SHA256")
    create.add_argument("--build-manifest", type=Path, required=True)
    create.add_argument("--start-time")
    create.add_argument("--host", default=socket.getfqdn())
    create.add_argument("--run-id")
    validate = subparsers.add_parser("validate", help="validate a canonical run manifest")
    validate.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            value = validate_manifest(json.loads(args.manifest.read_text(encoding="utf-8")))
            print(json.dumps({"valid": True, "run_id": value["run_id"], "manifest_sha256": value["manifest_sha256"]}, sort_keys=True))
            return 0
        code_sha = args.code_sha or repository_identity(args.repository_root)
        value = build_manifest(
            code_sha=code_sha,
            paper_validated_sha=args.paper_validated_sha,
            config=args.config,
            strategy_registry=args.strategy_registry,
            models=args.model,
            dataset_manifests=args.dataset_manifest,
            universe_snapshot=args.universe_snapshot,
            fee_schedule_version=args.fee_schedule_version,
            execution_model_version=args.execution_model_version,
            contract_mapping=args.contract_mapping,
            oracle_mapping=args.oracle_mapping,
            build_manifest=args.build_manifest,
            start_time=args.start_time,
            host=args.host,
            repository_root=args.repository_root,
            run_id=args.run_id,
        )
        output = args.output or args.runs_root / value["run_id"] / "manifest.json"
        if output.name != "manifest.json" or output.parent.name != value["run_id"]:
            raise ManifestError("output:must_be_runs_RUN_ID_manifest_json")
        immutable_write(output, value)
        print(json.dumps({"run_id": value["run_id"], "manifest": str(output), "manifest_sha256": value["manifest_sha256"]}, sort_keys=True))
        return 0
    except (ManifestError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"v7_run_manifest: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
