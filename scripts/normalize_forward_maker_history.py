#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

SUPPORTED_SESSION_SCHEMA = "polymarket_forward_maker_session_summary_v1"


def required_int(value: Any, field: str) -> int:
    try:
        out = int(float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid integer {field}: {value!r}") from exc
    if out < 0:
        raise ValueError(f"negative integer {field}: {out}")
    return out


def required_finite(value: Any, field: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid finite {field}: {value!r}") from exc
    if not math.isfinite(out):
        raise ValueError(f"non-finite {field}: {value!r}")
    return out


def validate_aggregate_policy(policy: str, metrics: dict[str, Any]) -> None:
    probes = required_int(metrics.get("probes"), f"{policy}.probes")
    pair_rate = required_finite(metrics.get("pair_fill_rate"), f"{policy}.pair_fill_rate")
    one_sided_rate = required_finite(
        metrics.get("one_sided_only_rate"), f"{policy}.one_sided_only_rate"
    )
    required_finite(
        metrics.get("conservative_pnl_ex_rewards_usd"),
        f"{policy}.conservative_pnl_ex_rewards_usd",
    )
    if not 0.0 <= pair_rate <= 1.0:
        raise ValueError(f"pair_fill_rate outside [0,1] for policy {policy}")
    if not 0.0 <= one_sided_rate <= 1.0:
        raise ValueError(f"one_sided_only_rate outside [0,1] for policy {policy}")
    if probes == 0 and (pair_rate != 0.0 or one_sided_rate != 0.0):
        raise ValueError(f"nonzero fill rate with zero probes for policy {policy}")


def normalize_run(run: dict[str, Any]) -> dict[str, Any] | None:
    aggregate = run.get("aggregate_by_policy")
    if aggregate is not None:
        if not isinstance(aggregate, dict) or not aggregate:
            raise ValueError("aggregate_by_policy must be a non-empty object")
        for policy, metrics in aggregate.items():
            policy_name = str(policy).strip()
            if not policy_name or not isinstance(metrics, dict):
                raise ValueError("aggregate_by_policy contains an invalid policy row")
            validate_aggregate_policy(policy_name, metrics)
        return run

    summaries = run.get("policy_summaries")
    if summaries is None:
        return None
    if run.get("schema") not in (None, SUPPORTED_SESSION_SCHEMA):
        raise ValueError(f"unsupported forward-maker schema: {run.get('schema')!r}")
    if not isinstance(summaries, list) or not summaries:
        raise ValueError("policy_summaries must be a non-empty list")

    normalized: dict[str, dict[str, Any]] = {}
    for index, summary in enumerate(summaries):
        if not isinstance(summary, dict):
            raise ValueError(f"policy_summaries[{index}] must be an object")
        policy = str(summary.get("policy") or "").strip()
        if not policy:
            raise ValueError(f"policy_summaries[{index}] is missing policy")
        if policy in normalized:
            raise ValueError(f"duplicate policy in one session: {policy}")
        probes = required_int(summary.get("probes"), f"{policy}.probes")
        pair_fills = required_int(summary.get("pair_fills"), f"{policy}.pair_fills")
        one_sided = required_int(summary.get("one_sided"), f"{policy}.one_sided")
        if pair_fills > probes or one_sided > probes:
            raise ValueError(f"fill counts exceed probes for policy {policy}")
        normalized[policy] = {
            "probes": probes,
            "pair_fill_rate": pair_fills / probes if probes else 0.0,
            "one_sided_only_rate": one_sided / probes if probes else 0.0,
            "conservative_pnl_ex_rewards_usd": required_finite(
                summary.get("pnl_ex_rewards"), f"{policy}.pnl_ex_rewards"
            ),
        }

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
        "invalid_supported_rows": 0,
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
        try:
            normalized = normalize_run(value)
        except ValueError:
            stats["invalid_supported_rows"] += 1
            continue
        if normalized is None:
            stats["unsupported_rows"] += 1
            continue
        stats["supported_rows"] += 1
        records.append(normalized)
    return records, stats


def history_integrity_errors(stats: dict[str, int]) -> list[str]:
    errors: list[str] = []
    for key in (
        "malformed_rows",
        "non_dict_rows",
        "unsupported_rows",
        "invalid_supported_rows",
    ):
        count = int(stats.get(key, 0))
        if count:
            errors.append(f"{key}={count}")
    if stats.get("nonempty_rows", 0) != stats.get("supported_rows", 0):
        errors.append(
            "row_accounting="
            f"{stats.get('supported_rows', 0)}/{stats.get('nonempty_rows', 0)}"
        )
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

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
