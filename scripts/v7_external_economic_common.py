#!/usr/bin/env python3
"""Shared, read-only primitives for External Fair economic evidence.

The helpers in this module never submit orders and never write to the canonical
ledger.  They only read immutable JSONL evidence, deduplicate exact records and
build reproducible hashes for downstream audit artifacts.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Iterator


COUNTERFACTUAL_SCHEMA = "polymarket_v7_external_fair_counterfactual_v1"


def finite(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                   allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def jsonl_rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                yield line_number, {"__malformed__": True}
                continue
            yield line_number, row if isinstance(row, dict) else {"__malformed__": True}


def discover_counterfactual_tapes(inputs: Iterable[Path]) -> list[Path]:
    paths: set[Path] = set()
    for raw in inputs:
        source = Path(raw)
        if source.is_file():
            paths.add(source.resolve())
        elif source.is_dir():
            paths.update(
                path.resolve()
                for path in source.glob("**/external_fair/counterfactuals.jsonl")
                if path.is_file()
            )
    return sorted(paths)


def load_counterfactual_evidence(
    inputs: Iterable[Path],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load exact records and fail closed on malformed/conflicting evidence."""
    paths = discover_counterfactual_tapes(inputs)
    unique: dict[str, dict[str, Any]] = {}
    locations: dict[str, list[dict[str, Any]]] = {}
    malformed = missing_ids = duplicates = conflicts = 0
    raw_records = 0
    manifests: list[dict[str, Any]] = []
    for path in paths:
        file_rows = 0
        for line_number, row in jsonl_rows(path):
            raw_records += 1
            file_rows += 1
            if row.get("__malformed__"):
                malformed += 1
                continue
            record_id = str(row.get("record_id") or "")
            if not record_id:
                missing_ids += 1
                continue
            locations.setdefault(record_id, []).append({
                "path": str(path), "line": line_number,
                "model_sha": str(row.get("model_sha") or ""),
            })
            prior = unique.get(record_id)
            if prior is None:
                unique[record_id] = row
            else:
                duplicates += 1
                if prior != row:
                    conflicts += 1
        manifests.append({
            "path": str(path),
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
            "jsonl_rows": file_rows,
        })
    rows = sorted(unique.values(), key=lambda row: (
        int(finite(row.get("timestamp_ms"), 0.0) or 0),
        str(row.get("record_id") or ""),
    ))
    quality = {
        "tape_files": len(paths),
        "raw_records": raw_records,
        "unique_records": len(rows),
        "duplicates_removed": duplicates,
        "conflicting_record_ids": conflicts,
        "malformed_records": malformed,
        "records_missing_record_id": missing_ids,
        "input_manifests": manifests,
        "fail_closed": bool(conflicts or malformed or missing_ids),
    }
    if conflicts:
        quality["conflict_locations"] = {
            record_id: values for record_id, values in locations.items()
            if len({entry["model_sha"] for entry in values}) > 1
        }
    return rows, quality


def group_trade_lifecycles(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Join candidate/fill/markout/final records by immutable lifecycle IDs."""
    lifecycles: dict[str, dict[str, Any]] = {}
    candidate_to_fill: dict[str, str] = {}
    for row in rows:
        kind = str(row.get("event_type") or "")
        fill_id = str(row.get("fill_id") or "")
        counterfactual_id = str(row.get("counterfactual_id") or "")
        if kind == "VIRTUAL_FILL" and fill_id:
            candidate_to_fill[counterfactual_id] = fill_id
            lifecycle = lifecycles.setdefault(fill_id, {
                "fill_id": fill_id, "candidate": None, "fill": None,
                "markouts": [], "final": None,
            })
            lifecycle["fill"] = row
    for row in rows:
        kind = str(row.get("event_type") or "")
        fill_id = str(row.get("fill_id") or "")
        if not fill_id:
            fill_id = candidate_to_fill.get(str(row.get("counterfactual_id") or ""), "")
        if not fill_id:
            continue
        lifecycle = lifecycles.setdefault(fill_id, {
            "fill_id": fill_id, "candidate": None, "fill": None,
            "markouts": [], "final": None,
        })
        if kind == "CANDIDATE":
            lifecycle["candidate"] = row
        elif kind == "VIRTUAL_FILL":
            lifecycle["fill"] = row
        elif kind == "VIRTUAL_MARKOUT":
            lifecycle["markouts"].append(row)
        elif kind == "VIRTUAL_FINAL":
            lifecycle["final"] = row
    for lifecycle in lifecycles.values():
        lifecycle["markouts"].sort(key=lambda row: (
            int(finite(row.get("receive_ts_ms"), finite(row.get("timestamp_ms"), 0.0)) or 0),
            str(row.get("record_id") or ""),
        ))
    return lifecycles


def lineage_state(lifecycle: dict[str, Any], current_sha: str) -> dict[str, Any]:
    events = [
        lifecycle.get("candidate"), lifecycle.get("fill"),
        *lifecycle.get("markouts", []), lifecycle.get("final"),
    ]
    shas = sorted({
        str(row.get("model_sha") or "") for row in events if isinstance(row, dict)
    } - {""})
    fill = lifecycle.get("fill") if isinstance(lifecycle.get("fill"), dict) else {}
    final = lifecycle.get("final") if isinstance(lifecycle.get("final"), dict) else {}
    fill_sha = str(fill.get("model_sha") or "")
    final_sha = str(final.get("model_sha") or "")
    if not fill or not final:
        state = "INCOMPLETE"
    elif len(shas) > 1:
        state = "MIXED_SHA"
    elif shas == [current_sha]:
        state = "EXACT_SHA"
    else:
        state = "HISTORICAL"
    return {
        "state": state,
        "event_shas": shas,
        "entry_sha": fill_sha or None,
        "terminal_sha": final_sha or None,
        "entry_terminal_sha_match": bool(fill_sha and fill_sha == final_sha),
        "current_sha": current_sha,
    }


def nearest_prior_forecast(
    rows: Iterable[dict[str, Any]], market_id: str, timestamp_ms: int,
) -> dict[str, Any] | None:
    candidates = [
        row for row in rows
        if row.get("event_type") == "FORECAST"
        and str(row.get("market_id") or "") == market_id
        and int(finite(row.get("timestamp_ms"), 0.0) or 0) <= timestamp_ms
    ]
    return max(
        candidates,
        key=lambda row: int(finite(row.get("timestamp_ms"), 0.0) or 0),
        default=None,
    )
