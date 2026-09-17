#!/usr/bin/env python3
"""Read-only cross-asset PAPER risk report for frozen multi-crypto forwards."""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from v7_lead_lag_replay import ReplayError

SCHEMA = "polymarket_v7_multi_crypto_risk_report_v1"
FORWARD_SCHEMA = "polymarket_v7_multi_crypto_forward_report_v1"


def _number(value: Any, reason: str) -> float:
    if isinstance(value, bool):
        raise ReplayError(reason)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReplayError(reason) from exc
    if not math.isfinite(number):
        raise ReplayError(reason)
    return number


def _corr(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    ml = sum(left) / len(left); mr = sum(right) / len(right)
    dl = [x - ml for x in left]; dr = [x - mr for x in right]
    vl = sum(x * x for x in dl); vr = sum(x * x for x in dr)
    if vl <= 0.0 or vr <= 0.0:
        return None
    return sum(x * y for x, y in zip(dl, dr)) / math.sqrt(vl * vr)


def _validate_report(report: dict[str, Any]) -> tuple[str, str]:
    if (not isinstance(report, dict) or report.get("schema") != FORWARD_SCHEMA
            or report.get("paper_only") is not True
            or report.get("authenticated_execution") is not False
            or report.get("real_order_submission") is not False
            or report.get("real_capital_at_risk") is not False
            or report.get("automatic_promotion") is not False
            or report.get("entry_authority") is not False):
        raise ReplayError("FORWARD_REPORT_CONTRACT_INVALID")
    asset = str(report.get("asset") or "")
    horizon = str(report.get("horizon") or "")
    if not asset or not horizon:
        raise ReplayError("FORWARD_REPORT_COHORT_IDENTITY_MISSING")
    if not isinstance(report.get("market_rows"), list):
        raise ReplayError("FORWARD_REPORT_MARKET_ROWS_MISSING")
    return asset, horizon


def build(reports: list[dict[str, Any]], *, block_ns: int,
          minimum_common_blocks: int, shrinkage: float) -> dict[str, Any]:
    if type(block_ns) is not int or block_ns <= 0:
        raise ReplayError("BLOCK_NS_INVALID")
    if type(minimum_common_blocks) is not int or minimum_common_blocks < 2:
        raise ReplayError("MINIMUM_COMMON_BLOCKS_INVALID")
    shrink = _number(shrinkage, "SHRINKAGE_INVALID")
    if not 0.0 <= shrink <= 1.0:
        raise ReplayError("SHRINKAGE_INVALID")

    cohort_blocks: dict[str, dict[int, float]] = {}
    pending_counts: dict[str, int] = {}
    shock_rows: dict[str, list[tuple[str, float | None]]] = defaultdict(list)
    cohort_meta: dict[str, dict[str, str]] = {}
    pending_total = 0

    for report in reports:
        asset, horizon = _validate_report(report)
        cohort = f"{asset}:{horizon}"
        if cohort in cohort_blocks:
            raise ReplayError("DUPLICATE_COHORT")
        cohort_meta[cohort] = {"asset": asset, "horizon": horizon}
        blocks: dict[int, float] = defaultdict(float)
        pending = 0
        for row in report["market_rows"]:
            if not isinstance(row, dict):
                raise ReplayError("FORWARD_MARKET_ROW_INVALID")
            stamp_ms = row.get("authorized_ms")
            if type(stamp_ms) is not int or stamp_ms <= 0:
                raise ReplayError("FORWARD_MARKET_CLOCK_INVALID")
            resolved = row.get("resolved") is True
            pnl_raw = row.get("pnl")
            pnl = None if pnl_raw is None else _number(pnl_raw, "FORWARD_MARKET_PNL_INVALID")
            if resolved and pnl is None:
                raise ReplayError("RESOLVED_MARKET_PNL_MISSING")
            if not resolved:
                pending += 1; pending_total += 1
            else:
                block = (stamp_ms * 1_000_000) // block_ns
                blocks[block] += pnl
            shock = str(row.get("parent_shock_id") or "")
            if shock:
                shock_rows[shock].append((cohort, pnl if resolved else None))
        cohort_blocks[cohort] = dict(blocks)
        if pending:
            pending_counts[cohort] = pending

    cohorts = sorted(cohort_blocks)
    pairs: list[dict[str, Any]] = []
    all_pairs_estimable = len(cohorts) >= 2
    for i, left_name in enumerate(cohorts):
        for right_name in cohorts[i + 1:]:
            left = cohort_blocks[left_name]; right = cohort_blocks[right_name]
            common = sorted(set(left) & set(right))
            raw: float | None = None
            shrunk: float | None = None
            if len(common) >= minimum_common_blocks:
                raw = _corr([left[k] for k in common], [right[k] for k in common])
                if raw is not None:
                    shrunk = (1.0 - shrink) * raw
            if raw is None:
                all_pairs_estimable = False
            pairs.append({
                "left": left_name, "right": right_name,
                "common_blocks": len(common),
                "minimum_common_blocks": minimum_common_blocks,
                "raw_correlation": raw, "shrunk_correlation": shrunk,
                "shrinkage_to_zero": shrink,
            })

    shock_summary: list[dict[str, Any]] = []
    maximum_simultaneous = 0
    for shock_id, rows in sorted(shock_rows.items()):
        active = sorted({cohort for cohort, _ in rows})
        maximum_simultaneous = max(maximum_simultaneous, len(active))
        values = [pnl for _, pnl in rows]
        observed = None if any(value is None for value in values) else sum(values)  # type: ignore[arg-type]
        shock_summary.append({
            "parent_shock_id": shock_id,
            "cohorts": active,
            "cohort_count": len(active),
            "observed_portfolio_pnl": observed,
            "complete": observed is not None,
        })

    status = "ESTIMATED" if pending_total == 0 and all_pairs_estimable else "INSUFFICIENT_EVIDENCE"
    return {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "automatic_promotion": False,
        "entry_authority": False,
        "status": status,
        "block_ns": block_ns,
        "minimum_common_blocks": minimum_common_blocks,
        "shrinkage_to_zero": shrink,
        "cohorts": cohort_meta,
        "pending_markets": pending_total,
        "pending_market_counts": dict(sorted(pending_counts.items())),
        "pairwise_block_correlations": pairs,
        "parent_shock_stress": {
            "maximum_simultaneous_cohorts": maximum_simultaneous,
            "shocks": shock_summary,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forward-report", type=Path, action="append", required=True)
    parser.add_argument("--block-ns", type=int, required=True)
    parser.add_argument("--minimum-common-blocks", type=int, required=True)
    parser.add_argument("--shrinkage", type=float, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.forward_report]
    result = build(reports, block_ns=args.block_ns,
                   minimum_common_blocks=args.minimum_common_blocks,
                   shrinkage=args.shrinkage)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_suffix(args.output.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(args.output)
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
