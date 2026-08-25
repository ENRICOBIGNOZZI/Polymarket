#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

FIELDS = ("market_key", "q_yes", "confidence", "source", "timestamp")
APPROVED_STATE = "APPROVED_FOR_INTEGRATION"


def finite(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def iter_rows(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
    except (OSError, EOFError):
        return


def evidence_provenance(report: dict[str, Any]) -> tuple[str, str] | None:
    evidence = report.get("alpha_factory_evidence")
    if not isinstance(evidence, dict):
        return None
    if evidence.get("integration_evidence_pass") is not True:
        return None
    if evidence.get("gate_pass_before_fdr") is not True:
        return None
    if str(evidence.get("evidence_state") or "") != APPROVED_STATE:
        return None
    if evidence.get("terminal_calibration_pass") is not True:
        return None
    if list(evidence.get("critical_failures") or []):
        return None

    metrics = evidence.get("metrics") or {}
    if not isinstance(metrics, dict):
        return None
    if integer(metrics.get("oos_predictions")) <= 0 or integer(metrics.get("trades")) <= 0:
        return None
    if finite(metrics.get("net_pnl_per_share"), 0.0) <= 0.0:
        return None
    if finite(metrics.get("two_x_cost_stressed_pnl_per_share"), 0.0) <= 0.0:
        return None

    candidate_id = str(evidence.get("candidate_id") or "")
    parts = candidate_id.split(":")
    if len(parts) != 4 or parts[0] != "external" or parts[2] != "external_probability":
        return None
    source = parts[1].strip()
    if not source:
        return None
    digest = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return source, f"{candidate_id}@{digest}"


def candidate(
    row: dict[str, Any],
    *,
    approved_source: str,
    provenance: str,
    now: int,
    max_age_seconds: int,
    min_confidence: float,
    min_mapping_score: float,
) -> dict[str, Any] | None:
    if str(row.get("source") or "").strip() != approved_source:
        return None
    if str(row.get("feature_name") or "").strip() != "external_probability":
        return None

    q_yes = finite(row.get("q_external"))
    confidence = finite(row.get("confidence"))
    mapping_score = finite(row.get("mapping_score"), 1.0)
    timestamp = integer(row.get("observed_ts") or row.get("retrieved_ts"))
    market_key = str(row.get("market_id") or row.get("condition_id") or row.get("slug") or "").strip()
    if not market_key or not (0.0 < q_yes < 1.0):
        return None
    if not (min_confidence <= confidence <= 1.0):
        return None
    if not (min_mapping_score <= mapping_score <= 1.0) or timestamp <= 0:
        return None
    age = now - timestamp
    if age < -300 or age > max_age_seconds:
        return None

    effective_confidence = max(0.0, min(1.0, confidence * mapping_score))
    if effective_confidence < min_confidence:
        return None
    return {
        "market_key": market_key,
        "q_yes": f"{q_yes:.12g}",
        "confidence": f"{effective_confidence:.12g}",
        "source": provenance,
        "timestamp": str(timestamp),
        "_timestamp": timestamp,
        "_confidence": effective_confidence,
    }


def materialize(
    rows: Iterable[dict[str, Any]],
    *,
    report: dict[str, Any],
    now: int,
    max_age_seconds: int,
    min_confidence: float = 0.10,
    min_mapping_score: float = 0.70,
) -> list[dict[str, Any]]:
    approval = evidence_provenance(report)
    if approval is None:
        return []
    approved_source, provenance = approval

    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = candidate(
            row,
            approved_source=approved_source,
            provenance=provenance,
            now=now,
            max_age_seconds=max_age_seconds,
            min_confidence=min_confidence,
            min_mapping_score=min_mapping_score,
        )
        if value is None:
            continue
        key = value["market_key"]
        current = latest.get(key)
        if current is None or (value["_timestamp"], value["_confidence"]) > (
            current["_timestamp"], current["_confidence"]
        ):
            latest[key] = value
    return [latest[key] for key in sorted(latest)]


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDS})
        temporary = Path(handle.name)
    os.replace(temporary, path)
    if path.stat().st_size <= 0:
        raise RuntimeError("external paper feed write produced an empty file")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize evidence-approved direct external probabilities for paper V5"
    )
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=21600)
    parser.add_argument("--min-confidence", type=float, default=0.10)
    parser.add_argument("--min-mapping-score", type=float, default=0.70)
    parser.add_argument("--now", type=int, default=0)
    args = parser.parse_args()

    if args.max_age_seconds <= 0:
        raise SystemExit("max-age-seconds must be positive")
    if not 0.0 <= args.min_confidence <= 1.0:
        raise SystemExit("min-confidence must be in [0,1]")
    if not 0.0 <= args.min_mapping_score <= 1.0:
        raise SystemExit("min-mapping-score must be in [0,1]")

    now = args.now or int(time.time())
    rows: list[dict[str, Any]] = []
    for raw_path in args.input:
        rows.extend(iter_rows(Path(raw_path)))
    report = read_json(Path(args.report))
    output = materialize(
        rows,
        report=report,
        now=now,
        max_age_seconds=args.max_age_seconds,
        min_confidence=args.min_confidence,
        min_mapping_score=args.min_mapping_score,
    )
    write_csv(Path(args.output), output)
    approval = evidence_provenance(report)
    state = "approved" if approval is not None else "abstain_unapproved"
    print(f"external_paper_feed_state={state} rows={len(output)} inputs={len(rows)} now={now}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
