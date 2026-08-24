#!/usr/bin/env python3
"""Censor forward markouts whose requested horizon was not actually observed."""
from __future__ import annotations

import argparse
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
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
    print(
        "forward_probe_finalize"
        f" cleared_60={summary['cleared_60']}"
        f" cleared_300={summary['cleared_300']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
