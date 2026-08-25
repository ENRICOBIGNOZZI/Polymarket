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


def normalize_history(text: str) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    parsed_dicts = 0
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        parsed_dicts += 1
        normalized = normalize_run(value)
        if normalized is not None:
            records.append(normalized)
    return records, parsed_dicts


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
    records, parsed_dicts = normalize_history(text)

    if parsed_dicts and not records:
        raise SystemExit(
            "forward-maker history contained JSON records but none matched a supported evidence schema"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "input_json_records": parsed_dicts,
                "normalized_records": len(records),
                "output": str(target),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
