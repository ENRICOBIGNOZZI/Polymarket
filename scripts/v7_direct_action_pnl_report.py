#!/usr/bin/env python3
"""Build a compact time-series report from a Direct Action research result."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_direct_action_pnl_over_time_v1"


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def iso_from_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1e9, tz=timezone.utc).isoformat()


def maximum_drawdown(values: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst


def rolling(values: list[float], width: int) -> list[float | None]:
    output = []
    for index in range(len(values)):
        start = max(0, index + 1 - width)
        window = values[start:index + 1]
        output.append(sum(window) / len(window) if window else None)
    return output


def bucket_key(decision_ns: int, seconds: int) -> int:
    width = seconds * 1_000_000_000
    return decision_ns // width * width


def build(result: dict[str, Any], *, rolling_trades: int = 10,
          bucket_seconds: int = 3600) -> dict[str, Any]:
    diagnostics = [
        row for row in (result.get("diagnostic_selected_outcomes") or [])
        if isinstance(row, dict) and row.get("action", "TRADE") == "TRADE"
    ]
    selected_total = int(
        ((result.get("summary") or {}).get("selected_trades") or 0)
    )
    complete = len(diagnostics) == selected_total

    rows = sorted(
        diagnostics,
        key=lambda row: (
            int(row.get("decision_ns") or 0),
            str(row.get("market_id") or ""),
        ),
    )

    cumulative_observed = 0.0
    cumulative_worst = 0.0
    observed_step = []
    robust_step = []
    realized_values = []
    hit_values = []
    buckets: dict[int, dict[str, Any]] = {}
    by_asset: dict[str, dict[str, float | int]] = {}
    by_horizon: dict[str, dict[str, float | int]] = {}
    by_side: dict[str, dict[str, float | int]] = {}
    time_rows = []

    def cell(store, key):
        return store.setdefault(str(key), {
            "selected": 0, "observed": 0, "censored": 0,
            "positive": 0, "zero": 0, "negative": 0,
            "observed_pnl": 0.0, "worst_case_pnl": 0.0,
        })

    for index, row in enumerate(rows, 1):
        decision_ns = int(row.get("decision_ns") or 0)
        realized = row.get("realized_pnl")
        observed = realized is not None and finite(realized)
        if observed:
            realized = float(realized)
            cumulative_observed += realized
            cumulative_worst += realized
            realized_values.append(realized)
            hit_values.append(1.0 if realized > 0 else 0.0)
            step_observed = realized
            step_worst = realized
        else:
            worst = row.get("censored_worst_case_pnl")
            step_observed = 0.0
            step_worst = float(worst) if finite(worst) else 0.0
            cumulative_worst += step_worst

        observed_step.append(step_observed)
        robust_step.append(step_worst)
        bucket = bucket_key(decision_ns, bucket_seconds)
        b = buckets.setdefault(bucket, {
            "start_ns": bucket, "selected": 0, "observed": 0,
            "censored": 0, "observed_pnl": 0.0, "worst_case_pnl": 0.0,
        })
        b["selected"] += 1
        b["observed"] += int(observed)
        b["censored"] += int(not observed)
        b["observed_pnl"] += step_observed
        b["worst_case_pnl"] += step_worst

        for store, key in (
            (by_asset, row.get("asset") or "UNKNOWN"),
            (by_horizon, row.get("exit_horizon_ms") or "UNKNOWN"),
            (by_side, row.get("side") or "UNKNOWN"),
        ):
            c = cell(store, key)
            c["selected"] += 1
            c["observed"] += int(observed)
            c["censored"] += int(not observed)
            c["observed_pnl"] += step_observed
            c["worst_case_pnl"] += step_worst
            if observed:
                if realized > 0:
                    c["positive"] += 1
                elif realized < 0:
                    c["negative"] += 1
                else:
                    c["zero"] += 1

        regret = row.get("cash_pnl_policy_regret")
        same_regret = (
            regret.get("same_horizon_cash_regret")
            if isinstance(regret, dict) else None
        )
        all_regret = (
            regret.get("all_horizon_cash_regret")
            if isinstance(regret, dict) else None
        )
        time_rows.append({
            "trade_index": index,
            "decision_ns": decision_ns,
            "decision_time_utc": iso_from_ns(decision_ns) if decision_ns > 0 else None,
            "market_id": row.get("market_id"),
            "asset": row.get("asset"),
            "side": row.get("side"),
            "size": row.get("size"),
            "notional": row.get("notional"),
            "exit_horizon_ms": row.get("exit_horizon_ms"),
            "latency_ms": row.get("latency_ms"),
            "effective_action_age_ms": row.get("effective_action_age_ms"),
            "observed": observed,
            "realized_pnl": realized if observed else None,
            "censored_worst_case_pnl": (
                row.get("censored_worst_case_pnl") if not observed else None
            ),
            "cumulative_observed_pnl": cumulative_observed,
            "cumulative_worst_case_pnl": cumulative_worst,
            "policy_utility": row.get("policy_utility"),
            "predicted_total_net_cash_pnl": row.get(
                "predicted_total_net_cash_pnl"),
            "same_horizon_cash_regret": same_regret,
            "all_horizon_cash_regret": all_regret,
        })

    rolling_pnl = rolling(observed_step, rolling_trades)
    rolling_robust = rolling(robust_step, rolling_trades)
    for index, row in enumerate(time_rows):
        row["rolling_observed_pnl_per_selected_trade"] = rolling_pnl[index]
        row["rolling_worst_case_pnl_per_selected_trade"] = rolling_robust[index]

    for values in (by_asset, by_horizon, by_side):
        for c in values.values():
            c["observed_fraction"] = (
                c["observed"] / c["selected"] if c["selected"] else None)
            c["mean_observed_pnl"] = (
                c["observed_pnl"] / c["observed"] if c["observed"] else None)
            c["hit_rate_observed"] = (
                c["positive"] / c["observed"] if c["observed"] else None)

    bucket_rows = []
    cumulative_bucket_observed = 0.0
    cumulative_bucket_worst = 0.0
    for key in sorted(buckets):
        row = buckets[key]
        cumulative_bucket_observed += row["observed_pnl"]
        cumulative_bucket_worst += row["worst_case_pnl"]
        bucket_rows.append({
            **row,
            "start_time_utc": iso_from_ns(key),
            "observed_fraction": (
                row["observed"] / row["selected"] if row["selected"] else None),
            "cumulative_observed_pnl": cumulative_bucket_observed,
            "cumulative_worst_case_pnl": cumulative_bucket_worst,
        })

    observed_curve = [float(row["cumulative_observed_pnl"]) for row in time_rows]
    worst_curve = [float(row["cumulative_worst_case_pnl"]) for row in time_rows]
    regret_same = [
        float(row["same_horizon_cash_regret"]) for row in time_rows
        if finite(row.get("same_horizon_cash_regret"))
    ]
    regret_all = [
        float(row["all_horizon_cash_regret"]) for row in time_rows
        if finite(row.get("all_horizon_cash_regret"))
    ]

    return {
        "schema": SCHEMA,
        "paper_only": result.get("paper_only") is True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "source_schema": result.get("schema"),
        "source_data_sha256": result.get("data_sha256"),
        "diagnostics_complete": complete,
        "selected_trades_reported": len(rows),
        "selected_trades_expected": selected_total,
        "coverage_warning": (
            None if complete
            else "TIME_SERIES_IS_BOUNDED_DIAGNOSTIC_SUBSET_NOT_FULL_POLICY_PATH"
        ),
        "bucket_seconds": bucket_seconds,
        "rolling_selected_trades": rolling_trades,
        "summary": {
            "observed_selected_trades": sum(
                row["observed"] for row in time_rows),
            "censored_selected_trades": sum(
                not row["observed"] for row in time_rows),
            "total_observed_pnl": cumulative_observed,
            "total_worst_case_pnl_lower_bound": cumulative_worst,
            "maximum_drawdown_observed_path": maximum_drawdown(observed_curve),
            "maximum_drawdown_worst_case_path": maximum_drawdown(worst_curve),
            "observed_hit_rate": (
                sum(hit_values) / len(hit_values) if hit_values else None),
            "same_horizon_regret_total": (
                sum(regret_same) if regret_same else None),
            "same_horizon_regret_mean": (
                sum(regret_same) / len(regret_same) if regret_same else None),
            "all_horizon_regret_total": (
                sum(regret_all) if regret_all else None),
            "all_horizon_regret_mean": (
                sum(regret_all) / len(regret_all) if regret_all else None),
        },
        "by_asset": by_asset,
        "by_exit_horizon_ms": by_horizon,
        "by_side": by_side,
        "pnl_by_time_bucket": bucket_rows,
        "trade_path": time_rows,
    }


def render_svg(report: dict[str, Any], path: Path) -> None:
    rows = report.get("trade_path") or []
    width, height = 1100, 520
    margin = 70
    if not rows:
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="520">'
            '<text x="40" y="60">No selected trades</text></svg>\n',
            encoding="utf-8")
        return

    obs = [float(row["cumulative_observed_pnl"]) for row in rows]
    worst = [float(row["cumulative_worst_case_pnl"]) for row in rows]
    values = obs + worst + [0.0]
    low, high = min(values), max(values)
    span = max(1e-9, high - low)
    xspan = max(1, len(rows) - 1)

    def xy(index, value):
        x = margin + (width - 2 * margin) * index / xspan
        y = height - margin - (height - 2 * margin) * (value - low) / span
        return x, y

    def polyline(values):
        return " ".join(
            f"{xy(index, value)[0]:.1f},{xy(index, value)[1]:.1f}"
            for index, value in enumerate(values)
        )

    zero_y = xy(0, 0.0)[1]
    start = html.escape(str(rows[0].get("decision_time_utc") or ""))
    end = html.escape(str(rows[-1].get("decision_time_utc") or ""))
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
<rect x="0" y="0" width="{width}" height="{height}" fill="white"/>
<text x="{margin}" y="30" font-size="20">Direct Action OOS cumulative PnL</text>
<line x1="{margin}" y1="{zero_y:.1f}" x2="{width-margin}" y2="{zero_y:.1f}" stroke="#777" stroke-width="1"/>
<polyline fill="none" stroke="#111" stroke-width="3" points="{polyline(obs)}"/>
<polyline fill="none" stroke="#777" stroke-width="2" stroke-dasharray="7 5" points="{polyline(worst)}"/>
<text x="{margin}" y="{height-25}" font-size="12">{start}</text>
<text x="{width-margin-220}" y="{height-25}" font-size="12">{end}</text>
<text x="{margin}" y="52" font-size="12">solid = observed-only cumulative PnL; dashed = censored worst-case lower bound</text>
<text x="10" y="{margin}" font-size="12">high={high:.4f}</text>
<text x="10" y="{height-margin}" font-size="12">low={low:.4f}</text>
</svg>
"""
    path.write_text(svg, encoding="utf-8")


def write_csv(report: dict[str, Any], path: Path) -> None:
    rows = report.get("trade_path") or []
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-svg", type=Path, required=True)
    parser.add_argument("--rolling-trades", type=int, default=10)
    parser.add_argument("--bucket-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    value = json.loads(args.input.read_text(encoding="utf-8"))
    report = build(
        value,
        rolling_trades=args.rolling_trades,
        bucket_seconds=args.bucket_seconds,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_csv(report, args.output_csv)
    render_svg(report, args.output_svg)
    print(json.dumps(report["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
