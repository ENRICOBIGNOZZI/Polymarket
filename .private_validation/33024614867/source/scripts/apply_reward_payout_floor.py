#!/usr/bin/env python3

import argparse
import csv
import math
import os
import sys
from pathlib import Path


def finite_float(row: dict[str, str], key: str) -> float:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {key}: {row.get(key)!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"non-finite {key}: {value}")
    return value


def apply_floor(path: Path, minimum_daily_payout_usd: float) -> dict[str, float | int]:
    if not math.isfinite(minimum_daily_payout_usd) or minimum_daily_payout_usd < 0.0:
        raise ValueError("minimum daily payout must be finite and non-negative")

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("reward CSV has no header")
        rows = list(reader)
        fieldnames = list(reader.fieldnames)

    required = {
        "estimated_native_daily_value",
        "capital_charge_daily",
        "adverse_budget_daily",
        "conservative_daily_score",
    }
    missing = sorted(required.difference(fieldnames))
    if missing:
        raise ValueError(f"reward CSV missing columns: {', '.join(missing)}")

    added = [
        "minimum_daily_payout_usd",
        "standalone_payable_native_daily_value",
        "payout_shortfall_usd",
        "conditional_conservative_daily_score",
    ]
    for name in added:
        if name not in fieldnames:
            fieldnames.append(name)

    conditional_positive = 0
    standalone_positive = 0
    best_conditional = -math.inf
    best_standalone = -math.inf

    for row in rows:
        estimated = max(0.0, finite_float(row, "estimated_native_daily_value"))
        capital = max(0.0, finite_float(row, "capital_charge_daily"))
        adverse = max(0.0, finite_float(row, "adverse_budget_daily"))
        conditional = estimated - capital - adverse
        payable = estimated if estimated + 1e-12 >= minimum_daily_payout_usd else 0.0
        shortfall = max(0.0, minimum_daily_payout_usd - estimated)
        standalone = payable - capital - adverse

        row["minimum_daily_payout_usd"] = f"{minimum_daily_payout_usd:.12g}"
        row["standalone_payable_native_daily_value"] = f"{payable:.12g}"
        row["payout_shortfall_usd"] = f"{shortfall:.12g}"
        row["conditional_conservative_daily_score"] = f"{conditional:.12g}"
        row["conservative_daily_score"] = f"{standalone:.12g}"

        conditional_positive += conditional > 0.0
        standalone_positive += standalone > 0.0
        best_conditional = max(best_conditional, conditional)
        best_standalone = max(best_standalone, standalone)

    rows.sort(key=lambda row: finite_float(row, "conservative_daily_score"), reverse=True)

    temporary = path.with_name(path.name + ".payout-floor.tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()

    if not rows:
        best_conditional = 0.0
        best_standalone = 0.0
    return {
        "rows": len(rows),
        "conditional_positive": conditional_positive,
        "standalone_positive": standalone_positive,
        "best_conditional": best_conditional,
        "best_standalone": best_standalone,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply Polymarket's daily minimum liquidity-reward payout to per-market "
            "shadow economics. The venue threshold is account/day aggregate; this is "
            "therefore a conservative standalone screen, while the pre-floor score is preserved."
        )
    )
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--minimum-daily-payout-usd", type=float, default=1.0)
    args = parser.parse_args()

    try:
        summary = apply_floor(args.csv, args.minimum_daily_payout_usd)
    except Exception as exc:
        # The scanner writes the pre-floor score to this path. If validation or
        # rewriting fails, remove that optimistic artifact rather than allowing
        # monitoring to publish it as if the payout floor had been applied.
        try:
            args.csv.unlink(missing_ok=True)
        except OSError as unlink_exc:
            print(
                f"reward_payout_floor_cleanup_failed csv={args.csv} error={unlink_exc}",
                file=sys.stderr,
            )
        print(f"reward_payout_floor_failed csv={args.csv} error={exc}", file=sys.stderr)
        raise

    print(
        "reward_payout_floor"
        f" rows={summary['rows']}"
        f" floor_usd={args.minimum_daily_payout_usd:.12g}"
        f" conditional_positive={summary['conditional_positive']}"
        f" standalone_positive={summary['standalone_positive']}"
        f" best_conditional={summary['best_conditional']:.12g}"
        f" best_standalone={summary['best_standalone']:.12g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
