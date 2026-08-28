#!/usr/bin/env python3
"""Create and validate immutable V7 research dataset manifests.

The manifest hashes source bytes rather than paths or mtimes.  Reusing an
existing output path with different content is rejected, so a dataset identity
cannot silently move underneath an economic report.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "polymarket_v7_dataset_manifest_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
POINT_IN_TIME_STATES = {"POINT_IN_TIME", "NOT_POINT_IN_TIME", "UNKNOWN"}


class ManifestError(ValueError):
    """The requested manifest is incomplete or internally inconsistent."""


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


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{field}:invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ManifestError(f"{field}:timezone_required")
    return parsed


def _open_text(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8", newline="")


def infer_row_count(path: Path) -> int:
    """Count records for supported text datasets; fail instead of guessing."""
    name = path.name.lower()
    inner = name[:-3] if name.endswith(".gz") else name
    if inner.endswith(".json"):
        with _open_text(path) as handle:
            value = json.load(handle)
        if not isinstance(value, list):
            raise ManifestError(f"source:{path}:json_root_must_be_array_for_row_count")
        return len(value)
    if inner.endswith(".csv") or inner.endswith(".tsv"):
        delimiter = "\t" if inner.endswith(".tsv") else ","
        with _open_text(path) as handle:
            rows = csv.reader(handle, delimiter=delimiter)
            count = sum(1 for _ in rows)
        return max(0, count - 1)
    if inner.endswith(".jsonl") or inner.endswith(".ndjson"):
        with _open_text(path) as handle:
            return sum(1 for line in handle if line.strip())
    raise ManifestError(f"source:{path}:row_count_required_for_unknown_format")


def _parse_row_counts(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        name, separator, raw_count = value.rpartition("=")
        if not separator or not name:
            raise ManifestError("row_count:expected_PATH=COUNT")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise ManifestError(f"row_count:{name}:invalid_integer") from exc
        if count < 0:
            raise ManifestError(f"row_count:{name}:negative")
        out[str(Path(name))] = count
    return out


def _display_path(path: Path, base: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def build_manifest(
    *,
    source_paths: list[Path],
    collector_sha: str,
    data_sources: list[str],
    point_in_time_status: str,
    start_timestamp: str,
    end_timestamp: str,
    receive_start_timestamp: str,
    receive_end_timestamp: str,
    markets: list[str],
    events: list[str],
    missing_data: list[str],
    known_gaps: list[str],
    row_counts: dict[str, int] | None = None,
    universe_snapshot: Path | None = None,
    dataset_id: str | None = None,
    base_path: Path | None = None,
) -> dict[str, Any]:
    if not GIT_SHA_RE.fullmatch(collector_sha):
        raise ManifestError("collector_sha:not_exact_git_sha")
    if point_in_time_status not in POINT_IN_TIME_STATES:
        raise ManifestError("point_in_time_status:invalid")
    if not source_paths:
        raise ManifestError("source_files:empty")
    if not data_sources or any(not value.strip() for value in data_sources):
        raise ManifestError("data_sources:empty")

    start = _parse_time(start_timestamp, "start_timestamp")
    end = _parse_time(end_timestamp, "end_timestamp")
    receive_start = _parse_time(receive_start_timestamp, "receive_start_timestamp")
    receive_end = _parse_time(receive_end_timestamp, "receive_end_timestamp")
    if start > end:
        raise ManifestError("timestamp_range:reversed")
    if receive_start > receive_end:
        raise ManifestError("receive_timestamp_coverage:reversed")

    base = (base_path or Path.cwd()).resolve()
    explicit_counts = row_counts or {}
    source_entries: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for raw_path in source_paths:
        path = raw_path.resolve()
        if path in seen:
            raise ManifestError(f"source:{raw_path}:duplicate")
        seen.add(path)
        if not path.is_file():
            raise ManifestError(f"source:{raw_path}:not_a_file")
        key_candidates = (str(raw_path), str(path), _display_path(path, base))
        explicit = next((explicit_counts[key] for key in key_candidates if key in explicit_counts), None)
        count = explicit if explicit is not None else infer_row_count(path)
        source_entries.append(
            {
                "path": _display_path(path, base),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "row_count": count,
            }
        )
    source_entries.sort(key=lambda row: row["path"])

    universe: dict[str, Any] | None = None
    if universe_snapshot is not None:
        if not universe_snapshot.is_file():
            raise ManifestError("universe_snapshot:not_a_file")
        universe = {
            "path": _display_path(universe_snapshot, base),
            "sha256": sha256_file(universe_snapshot),
        }
    if point_in_time_status == "POINT_IN_TIME" and universe is None:
        raise ManifestError("universe_snapshot:required_for_point_in_time_dataset")

    dataset_hash_input = {
        "sources": source_entries,
        "collector_sha": collector_sha,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "universe_snapshot": universe,
    }
    dataset_sha256 = hashlib.sha256(canonical_bytes(dataset_hash_input)).hexdigest()
    resolved_id = dataset_id or f"v7ds-{dataset_sha256[:20]}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", resolved_id):
        raise ManifestError("dataset_id:invalid")

    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "dataset_id": resolved_id,
        "dataset_sha256": dataset_sha256,
        "source_files": source_entries,
        "row_count": sum(int(row["row_count"]) for row in source_entries),
        "markets": sorted(set(markets)),
        "events": sorted(set(events)),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "receive_timestamp_coverage": {
            "start_timestamp": receive_start_timestamp,
            "end_timestamp": receive_end_timestamp,
        },
        "data_sources": sorted(set(data_sources)),
        "missing_data": sorted(set(missing_data)),
        "known_gaps": sorted(set(known_gaps)),
        "collector_sha": collector_sha,
        "point_in_time_status": point_in_time_status,
        "universe_snapshot": universe,
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    validate_manifest(manifest)
    return manifest


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("manifest:not_an_object")
    required = {
        "schema", "dataset_id", "dataset_sha256", "source_files", "row_count",
        "markets", "events", "start_timestamp", "end_timestamp",
        "receive_timestamp_coverage", "data_sources", "missing_data", "known_gaps",
        "collector_sha", "point_in_time_status", "universe_snapshot", "manifest_sha256",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise ManifestError("manifest:missing:" + ",".join(missing))
    if value["schema"] != SCHEMA:
        raise ManifestError("schema:unsupported")
    if not GIT_SHA_RE.fullmatch(str(value["collector_sha"])):
        raise ManifestError("collector_sha:not_exact_git_sha")
    if value["point_in_time_status"] not in POINT_IN_TIME_STATES:
        raise ManifestError("point_in_time_status:invalid")
    if not SHA256_RE.fullmatch(str(value["dataset_sha256"])):
        raise ManifestError("dataset_sha256:invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", str(value["dataset_id"])):
        raise ManifestError("dataset_id:invalid")
    if not isinstance(value["source_files"], list) or not value["source_files"]:
        raise ManifestError("source_files:empty")
    total = 0
    for source in value["source_files"]:
        if not isinstance(source, dict) or set(source) != {"path", "sha256", "size_bytes", "row_count"}:
            raise ManifestError("source_files:invalid_entry")
        if not source["path"] or not SHA256_RE.fullmatch(str(source["sha256"])):
            raise ManifestError("source_files:invalid_identity")
        if int(source["size_bytes"]) < 0 or int(source["row_count"]) < 0:
            raise ManifestError("source_files:negative_measure")
        total += int(source["row_count"])
    if total != int(value["row_count"]):
        raise ManifestError("row_count:mismatch")
    start = _parse_time(str(value["start_timestamp"]), "start_timestamp")
    end = _parse_time(str(value["end_timestamp"]), "end_timestamp")
    if start > end:
        raise ManifestError("timestamp_range:reversed")
    coverage = value["receive_timestamp_coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {"start_timestamp", "end_timestamp"}:
        raise ManifestError("receive_timestamp_coverage:invalid")
    receive_start = _parse_time(str(coverage["start_timestamp"]), "receive_start_timestamp")
    receive_end = _parse_time(str(coverage["end_timestamp"]), "receive_end_timestamp")
    if receive_start > receive_end:
        raise ManifestError("receive_timestamp_coverage:reversed")
    universe = value["universe_snapshot"]
    if value["point_in_time_status"] == "POINT_IN_TIME" and not isinstance(universe, dict):
        raise ManifestError("universe_snapshot:required_for_point_in_time_dataset")
    if universe is not None:
        if set(universe) != {"path", "sha256"} or not SHA256_RE.fullmatch(str(universe["sha256"])):
            raise ManifestError("universe_snapshot:invalid")
    for field in ("markets", "events", "data_sources", "missing_data", "known_gaps"):
        if not isinstance(value[field], list) or any(not isinstance(item, str) for item in value[field]):
            raise ManifestError(f"{field}:invalid")
    if not value["data_sources"]:
        raise ManifestError("data_sources:empty")
    dataset_hash_input = {
        "sources": value["source_files"],
        "collector_sha": value["collector_sha"],
        "start_timestamp": value["start_timestamp"],
        "end_timestamp": value["end_timestamp"],
        "universe_snapshot": universe,
    }
    if value["dataset_sha256"] != hashlib.sha256(canonical_bytes(dataset_hash_input)).hexdigest():
        raise ManifestError("dataset_sha256:mismatch")
    supplied_hash = str(value["manifest_sha256"])
    unhashed = dict(value)
    unhashed.pop("manifest_sha256")
    expected_hash = hashlib.sha256(canonical_bytes(unhashed)).hexdigest()
    if supplied_hash != expected_hash:
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


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create an immutable dataset manifest")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--source", action="append", type=Path, required=True)
    create.add_argument("--row-count", action="append", default=[], metavar="PATH=COUNT")
    create.add_argument("--collector-sha", required=True)
    create.add_argument("--data-source", action="append", required=True)
    create.add_argument("--point-in-time-status", choices=sorted(POINT_IN_TIME_STATES), required=True)
    create.add_argument("--universe-snapshot", type=Path)
    create.add_argument("--dataset-id")
    create.add_argument("--start-timestamp", required=True)
    create.add_argument("--end-timestamp", required=True)
    create.add_argument("--receive-start-timestamp", required=True)
    create.add_argument("--receive-end-timestamp", required=True)
    create.add_argument("--market", action="append", default=[])
    create.add_argument("--event", action="append", default=[])
    create.add_argument("--missing-data", action="append", default=[])
    create.add_argument("--known-gap", action="append", default=[])
    create.add_argument("--base-path", type=Path, default=Path.cwd())
    validate = subparsers.add_parser("validate", help="validate a dataset manifest")
    validate.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            value = validate_manifest(_load(args.manifest))
            print(json.dumps({"valid": True, "dataset_id": value["dataset_id"], "manifest_sha256": value["manifest_sha256"]}, sort_keys=True))
            return 0
        value = build_manifest(
            source_paths=args.source,
            collector_sha=args.collector_sha,
            data_sources=args.data_source,
            point_in_time_status=args.point_in_time_status,
            start_timestamp=args.start_timestamp,
            end_timestamp=args.end_timestamp,
            receive_start_timestamp=args.receive_start_timestamp,
            receive_end_timestamp=args.receive_end_timestamp,
            markets=args.market,
            events=args.event,
            missing_data=args.missing_data,
            known_gaps=args.known_gap,
            row_counts=_parse_row_counts(args.row_count),
            universe_snapshot=args.universe_snapshot,
            dataset_id=args.dataset_id,
            base_path=args.base_path,
        )
        immutable_write(args.output, value)
        print(json.dumps({"dataset_id": value["dataset_id"], "manifest": str(args.output), "manifest_sha256": value["manifest_sha256"]}, sort_keys=True))
        return 0
    except (ManifestError, OSError, json.JSONDecodeError) as exc:
        print(f"v7_dataset_manifest: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
