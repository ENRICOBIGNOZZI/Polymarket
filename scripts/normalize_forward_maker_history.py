#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def normalize_run(run: dict[str, Any]) -> dict[str, Any] | None:
    aggregate = run.get("aggregate_by_policy")
    if isinstance(aggregate, dict) and aggregate:
        return run

    summaries = run.get("policy_summaries")
    if not isinstance(summaries, list):
        return None

    normalized: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        policy = str(summary.get("policy") or "").strip()
        if not policy:
            continue
        probes = max(0, integer(summary.get("probes")))
        pair_fills = max(0, integer(summary.get("pair_fills")))
        one_sided = max(0, integer(summary.get("one_sided")))
        normalized[policy] = {
            "probes": probes,
            "pair_fill_rate": pair_fills / probes if probes else 0.0,
            "one_sided_only_rate": one_sided / probes if probes else 0.0,
            "conservative_pnl_ex_rewards_usd": finite(summary.get("pnl_ex_rewards")),
        }

    if not normalized:
        return None

    output = dict(run)
    output["aggregate_by_policy"] = normalized
    return output


def empty_stats() -> dict[str, int]:
    return {
        "nonempty_rows": 0,
        "parsed_json_rows": 0,
        "dict_rows": 0,
        "supported_rows": 0,
        "unsupported_rows": 0,
        "malformed_rows": 0,
        "non_dict_rows": 0,
    }


def normalize_history(text: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    records: list[dict[str, Any]] = []
    stats = empty_stats()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        stats["nonempty_rows"] += 1
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            stats["malformed_rows"] += 1
            continue
        stats["parsed_json_rows"] += 1
        if not isinstance(value, dict):
            stats["non_dict_rows"] += 1
            continue
        stats["dict_rows"] += 1
        normalized = normalize_run(value)
        if normalized is None:
            stats["unsupported_rows"] += 1
            continue
        stats["supported_rows"] += 1
        records.append(normalized)
    return records, stats


def history_integrity_errors(stats: dict[str, int]) -> list[str]:
    errors: list[str] = []
    for key in ("malformed_rows", "non_dict_rows", "unsupported_rows"):
        count = int(stats.get(key, 0))
        if count:
            errors.append(f"{key}={count}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize persisted forward-maker history for Alpha Factory evaluation"
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = Path(args.input)
    target = Path(args.output)
    text = source.read_text(encoding="utf-8") if source.exists() else ""
    records, stats = normalize_history(text)
    errors = history_integrity_errors(stats)

    report = dict(stats)
    report["normalized_records"] = len(records)
    report["output"] = str(target)
    report["integrity_status"] = "fail_closed" if errors else "ok"
    report["integrity_errors"] = errors
    print(json.dumps(report, sort_keys=True))

    if errors:
        target.unlink(missing_ok=True)
        raise SystemExit(
            "forward-maker history integrity failure; refusing reduced evidence sample: "
            + ", ".join(errors)
        )

    if stats["nonempty_rows"] != len(records):
        target.unlink(missing_ok=True)
        raise SystemExit(
            "forward-maker history row accounting mismatch; refusing evidence normalization"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
