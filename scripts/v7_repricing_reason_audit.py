#!/usr/bin/env python3
"""Summarize causal PM repricing by native decision reason.

This report is diagnostic only. It measures subsequent PM movement after a
decision/rejection and never treats midpoint movement as executable PnL.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
import statistics
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_repricing_reason_audit_v1"


def _finite(x: Any) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
        raise ValueError("nonfinite")
    return float(x)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[int, str, int], list[tuple[float, str]]] = defaultdict(list)
    invalid = 0
    for row in rows:
        try:
            if row.get("schema") != "polymarket_v7_native_repricing_label_v1":
                raise ValueError("schema")
            reason = row.get("origin_reason")
            direction = row.get("signal_direction")
            horizon = row.get("repricing_horizon_ms")
            asset, contract_horizon = row.get("asset"), row.get("horizon")
            market = str(row.get("market_id") or "")
            if type(reason) is not int or direction not in (-1, 1) or type(horizon) is not int:
                raise ValueError("identity")
            if not isinstance(asset, str) or not isinstance(contract_horizon, str) or not market:
                raise ValueError("context")
            delta = _finite(row.get("delta_probability"))
            groups[(reason, f"{asset}:{contract_horizon}", horizon)].append((direction * delta, market))
        except ValueError:
            invalid += 1

    out: dict[str, Any] = {}
    for (reason, context, horizon), sample in sorted(groups.items()):
        values = [x for x, _ in sample]
        key = f"{reason}|{context}|{horizon}"
        out[key] = {
            "reason": reason,
            "context": context,
            "repricing_horizon_ms": horizon,
            "n": len(values),
            "markets": len({m for _, m in sample}),
            "mean_shock_aligned_delta_probability": statistics.fmean(values),
            "median_shock_aligned_delta_probability": statistics.median(values),
            "positive_fraction": sum(v > 0 for v in values) / len(values),
        }
    return {
        "schema": SCHEMA,
        "groups": out,
        "invalid_rows": invalid,
        "paper_only": True,
        "execution_authority": False,
        "midpoint_not_executable_pnl": True,
        "interpretation": (
            "Positive shock-aligned delta means PM midpoint continued moving in the external-shock "
            "direction after the decision. It is not proof that a rejected order was executable or profitable."
        ),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    rows = [json.loads(line) for line in a.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = summarize(rows)
    if a.output.exists():
        raise SystemExit("refusing to overwrite repricing audit")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
                        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
