#!/usr/bin/env python3
"""Archive and independently recheck an official V7 platform-contract snapshot.

This is a read-only evidence boundary.  It never fetches a URL, uses a signer,
or opens an authenticated venue connection: a separately retrieved snapshot
and the exact official-document bytes are supplied as local inputs.  Their
immutable, exact-SHA artifact pointers make a later drift decision reproducible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import v7_artifact_store as artifact_store
import v7_platform_drift_monitor as monitor


SCHEMA = "polymarket_v7_platform_contract_archive_v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
ARTIFACT_KEYS = {"schema_version", "exact_code_sha", "run_id", "name", "location", "sha256", "historical_non_authoritative"}


class PlatformArchiveError(ValueError):
    pass


def _load_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformArchiveError(code) from exc
    if not isinstance(value, dict):
        raise PlatformArchiveError(code)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _document_arguments(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        url, separator, filename = value.partition("=")
        if not separator or not url or not filename or url in result:
            raise PlatformArchiveError("source_document_argument")
        result[url] = Path(filename)
    return result


def _pointer_path(root: Path, pointer: Any, *, exact_code_sha: str, run_id: str, name: str) -> Path:
    if not isinstance(pointer, dict) or set(pointer) != ARTIFACT_KEYS:
        raise PlatformArchiveError("artifact_pointer_shape")
    expected = f"artifacts/by_sha/{exact_code_sha}/{run_id}/{name}"
    if (pointer.get("schema_version") != 1 or pointer.get("exact_code_sha") != exact_code_sha
            or pointer.get("run_id") != run_id or pointer.get("name") != name
            or pointer.get("location") != expected or pointer.get("historical_non_authoritative") is not False
            or not isinstance(pointer.get("sha256"), str) or not SHA256.fullmatch(pointer["sha256"])):
        raise PlatformArchiveError("artifact_pointer_identity")
    relative = PurePosixPath(expected)
    path = root
    for component in relative.parts:
        path = path / component
        if path.is_symlink():
            raise PlatformArchiveError("artifact_pointer_symlink")
    if not path.is_file() or _sha256(path) != pointer["sha256"]:
        raise PlatformArchiveError("artifact_pointer_hash")
    return path


def _artifact(root: Path, source: Path, *, exact_code_sha: str, run_id: str, name: str) -> dict[str, Any]:
    try:
        return artifact_store.store(root, source, exact_code_sha=exact_code_sha, run_id=run_id, name=name)
    except artifact_store.ArtifactStoreError as exc:
        raise PlatformArchiveError(f"artifact_store:{exc}") from exc


def archive(root: Path, registry_path: Path, snapshot_path: Path, *, exact_code_sha: str,
            run_id: str, source_documents: dict[str, Path], now: datetime | None = None) -> dict[str, Any]:
    """Store one complete platform snapshot and its official source bytes immutably."""
    if not SHA40.fullmatch(exact_code_sha):
        raise PlatformArchiveError("exact_code_sha")
    root = Path(root).resolve()
    registry = monitor.load(registry_path)
    monitor.validate_registry(registry)
    expected_sources = set(registry["official_sources"])
    if set(source_documents) != expected_sources:
        raise PlatformArchiveError("official_source_coverage")
    try:
        report = monitor.report(registry, Path(snapshot_path), now=now)
    except monitor.DriftError as exc:
        raise PlatformArchiveError(f"snapshot_invalid:{exc}") from exc
    snapshot = _artifact(root, Path(snapshot_path), exact_code_sha=exact_code_sha, run_id=run_id,
                         name="platform_snapshot.json")
    documents: list[dict[str, Any]] = []
    for index, source_url in enumerate(sorted(expected_sources)):
        source = Path(source_documents[source_url])
        pointer = _artifact(root, source, exact_code_sha=exact_code_sha, run_id=run_id,
                            name=f"platform_source_{index:02d}.bin")
        documents.append({"source_url": source_url, "artifact": pointer})
    manifest = {"schema": SCHEMA, "exact_code_sha": exact_code_sha, "run_id": run_id,
                "registry_sha256": _sha256(registry_path), "snapshot": snapshot,
                "source_documents": documents, "monitor_report": report}
    staging = root / ".platform-contract-archive-manifest.json"
    try:
        staging.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        pointer = _artifact(root, staging, exact_code_sha=exact_code_sha, run_id=run_id,
                            name="platform_contract_archive.json")
    finally:
        staging.unlink(missing_ok=True)
    return {"archive": pointer, "monitor_report": report}


def verify(root: Path, registry_path: Path, manifest_path: Path, *, archive_sha256: str,
           now: datetime | None = None) -> dict[str, Any]:
    """Verify archive bytes, source coverage, and the original drift decision."""
    root = Path(root).resolve()
    registry = monitor.load(registry_path)
    monitor.validate_registry(registry)
    manifest_path = Path(manifest_path)
    if not SHA256.fullmatch(archive_sha256) or manifest_path.is_symlink() or not manifest_path.is_file():
        raise PlatformArchiveError("manifest_pointer")
    if _sha256(manifest_path) != archive_sha256:
        raise PlatformArchiveError("manifest_hash")
    manifest = _load_json(manifest_path, "manifest_unreadable")
    required = {"schema", "exact_code_sha", "run_id", "registry_sha256", "snapshot", "source_documents", "monitor_report"}
    if set(manifest) != required or manifest.get("schema") != SCHEMA or not isinstance(manifest.get("run_id"), str):
        raise PlatformArchiveError("manifest_shape")
    exact_code_sha = manifest.get("exact_code_sha")
    if not isinstance(exact_code_sha, str) or not SHA40.fullmatch(exact_code_sha) or manifest.get("registry_sha256") != _sha256(registry_path):
        raise PlatformArchiveError("manifest_identity")
    expected_manifest = root / "artifacts" / "by_sha" / exact_code_sha / manifest["run_id"] / "platform_contract_archive.json"
    current = root
    for component in expected_manifest.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            raise PlatformArchiveError("manifest_pointer_symlink")
    if manifest_path.resolve() != expected_manifest.resolve():
        raise PlatformArchiveError("manifest_location")
    snapshot_path = _pointer_path(root, manifest["snapshot"], exact_code_sha=exact_code_sha,
                                  run_id=manifest["run_id"], name="platform_snapshot.json")
    documents = manifest.get("source_documents")
    if not isinstance(documents, list) or len(documents) != len(registry["official_sources"]):
        raise PlatformArchiveError("manifest_source_documents")
    expected_sources = sorted(registry["official_sources"])
    observed_sources: list[str] = []
    for index, row in enumerate(documents):
        if not isinstance(row, dict) or set(row) != {"source_url", "artifact"} or row.get("source_url") != expected_sources[index]:
            raise PlatformArchiveError("manifest_source_document")
        _pointer_path(root, row["artifact"], exact_code_sha=exact_code_sha, run_id=manifest["run_id"],
                      name=f"platform_source_{index:02d}.bin")
        observed_sources.append(row["source_url"])
    try:
        report = monitor.report(registry, snapshot_path, now=now)
    except monitor.DriftError as exc:
        raise PlatformArchiveError(f"snapshot_invalid:{exc}") from exc
    if report != manifest["monitor_report"]:
        raise PlatformArchiveError("monitor_report_mismatch")
    return {"schema": SCHEMA, "exact_code_sha": exact_code_sha, "run_id": manifest["run_id"],
            "source_count": len(observed_sources), "status": report["status"],
            "required_execution_mode": report["required_execution_mode"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    archive_parser = subparsers.add_parser("archive")
    archive_parser.add_argument("--root", type=Path, default=Path("."))
    archive_parser.add_argument("--registry", type=Path, default=Path("config/v7_platform_contract.json"))
    archive_parser.add_argument("--snapshot", type=Path, required=True)
    archive_parser.add_argument("--exact-code-sha", required=True)
    archive_parser.add_argument("--run-id", required=True)
    archive_parser.add_argument("--source-document", action="append", default=[], metavar="URL=PATH")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--root", type=Path, default=Path("."))
    verify_parser.add_argument("--registry", type=Path, default=Path("config/v7_platform_contract.json"))
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--archive-sha256", required=True)
    args = parser.parse_args()
    if args.command == "archive":
        value = archive(args.root, args.registry, args.snapshot, exact_code_sha=args.exact_code_sha,
                        run_id=args.run_id, source_documents=_document_arguments(args.source_document))
    else:
        value = verify(args.root, args.registry, args.manifest, archive_sha256=args.archive_sha256)
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
