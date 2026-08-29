#!/usr/bin/env python3
"""Censor forward markouts whose requested horizon was not actually observed."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any


def finalize(payload: dict[str, Any]) -> dict[str, int]:
    cleared_60 = 0
    cleared_300 = 0
    for result in payload.get("results", []):
        if not isinstance(result, dict):
            continue
        quote_end = result.get("quote_end_ts")
        try:
            quote_end_ts = int(quote_end)
        except (TypeError, ValueError):
            quote_end_ts = 0
        for side in ("yes", "no"):
            leg = result.get(side)
            if not isinstance(leg, dict):
                continue
            first = leg.get("first_fill_ts")
            try:
                first_ts = int(first)
            except (TypeError, ValueError):
                first_ts = 0
            if first_ts <= 0 or quote_end_ts < first_ts + 60:
                if leg.get("markout_60_bid_per_share") is not None:
                    cleared_60 += 1
                leg["markout_60_bid_per_share"] = None
            if first_ts <= 0 or quote_end_ts < first_ts + 300:
                if leg.get("markout_300_bid_per_share") is not None:
                    cleared_300 += 1
                leg["markout_300_bid_per_share"] = None
    method = payload.setdefault("method", {})
    if isinstance(method, dict):
        method["markout_censoring"] = (
            "60s/300s markouts are null unless a book snapshot at or after the exact horizon was observed"
        )
    return {"cleared_60": cleared_60, "cleared_300": cleared_300}


def result_key(value: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(value.get("market_id") or ""),
        str(value.get("condition_id") or ""),
        str(value.get("policy") or ""),
    )


def csv_value(value: Any) -> str:
    return "" if value is None else f"{float(value):.12g}"


def synchronize_csv(path: Path, payload: dict[str, Any]) -> int:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("forward probe CSV has no header")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    required = {
        "market_id",
        "condition_id",
        "policy",
        "yes_markout_60",
        "no_markout_60",
        "yes_markout_300",
        "no_markout_300",
    }
    missing = sorted(required.difference(fieldnames))
    if missing:
        raise ValueError(f"forward probe CSV missing columns: {', '.join(missing)}")

    lookup: dict[tuple[str, str, str], dict[str, Any]] = {}
    for result in payload.get("results", []):
        if isinstance(result, dict):
            lookup[result_key(result)] = result

    updated = 0
    for row in rows:
        result = lookup.get(result_key(row))
        if result is None:
            continue
        yes = result.get("yes") if isinstance(result.get("yes"), dict) else {}
        no = result.get("no") if isinstance(result.get("no"), dict) else {}
        row["yes_markout_60"] = csv_value(yes.get("markout_60_bid_per_share"))
        row["no_markout_60"] = csv_value(no.get("markout_60_bid_per_share"))
        row["yes_markout_300"] = csv_value(yes.get("markout_300_bid_per_share"))
        row["no_markout_300"] = csv_value(no.get("markout_300_bid_per_share"))
        updated += 1

    temporary = path.with_suffix(path.suffix + ".finalize.tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--csv", type=Path, default=None)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("forward probe JSON must be an object")
    summary = finalize(payload)
    temporary = args.input.with_suffix(args.input.suffix + ".finalize.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.input)
    finally:
        temporary.unlink(missing_ok=True)
    csv_rows = synchronize_csv(args.csv, payload) if args.csv is not None else 0
    print(
        "forward_probe_finalize"
        f" cleared_60={summary['cleared_60']}"
        f" cleared_300={summary['cleared_300']}"
        f" csv_rows={csv_rows}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
