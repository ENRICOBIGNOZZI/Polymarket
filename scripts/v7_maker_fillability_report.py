#!/usr/bin/env python3
"""Generate human-readable exact-SHA V7 maker fillability evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"
if str(MONITORING) not in sys.path:
    sys.path.insert(0, str(MONITORING))
from v7_maker_fillability_exact import summarize_best_available_fillability


def _pct(num: float, den: float) -> str:
    return "n/a" if den <= 0 else f"{100.0 * num / den:.1f}%"


def _table(rows: list[dict[str, Any]], key: str) -> list[str]:
    out = [f"| {key} | orders | reachable | pess opp | fills | mean rest ms | near miss |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for row in rows[:20]:
        out.append(
            f"| {row.get(key,'UNKNOWN')} | {row.get('orders',0)} | {row.get('trade_reachable',0)} | "
            f"{row.get('fill_opportunities',0)} | {row.get('filled_orders',0)} | "
            f"{float(row.get('mean_rest_ms',0)):.1f} | {float(row.get('mean_near_miss_ratio',0)):.3f} |"
        )
    return out


def _funnel(lines: list[str], funnel: dict[str, Any], *, exact: bool = False) -> None:
    orders = int(funnel.get("orders", 0))
    lines.extend(["| Stage | Count | Rate / orders |", "|---|---:|---:|"])
    stages = [
        ("Orders", "orders"), ("Effective", "orders_effective"), ("Rested", "orders_rested"),
        ("Trade reachable", "trade_reachable"), ("Lower queue depleted", "lower_queue_depleted"),
        ("Expected queue depleted", "expected_queue_depleted"), ("Pessimistic queue depleted", "pessimistic_queue_depleted"),
        ("Lower fill opportunities", "fill_opportunity_lower"), ("Expected fill opportunities", "fill_opportunity_expected"),
        ("Pessimistic fill opportunities", "fill_opportunity_pessimistic"), ("Partial fills", "partial_fills"),
        ("Full fills", "full_fills"),
    ]
    if exact:
        stages.append(("Cancelled before observed future flow", "cancelled_before_observed_future_flow"))
    else:
        stages.extend([("No flow while resting / cancelled", "cancelled_before_flow"), ("Priority resets", "priority_resets")])
    for label, key in stages:
        value = int(funnel.get(key, 0))
        lines.append(f"| {label} | {value} | {_pct(value, orders)} |")


def render_markdown(report: dict[str, Any]) -> str:
    funnel = report.get("funnel") or {}
    lines = [
        "# V7 Maker Fillability Cold-Start Report",
        "",
        f"- runtime SHA: `{report.get('runtime_sha') or 'UNKNOWN'}`",
        f"- exact SHA: `{report.get('exact_sha_ok')}`",
        f"- policy hash: `{report.get('policy_hash')}`",
        f"- historical/coarse root cause: **{report.get('root_cause')}**",
        f"- simulator bug suspected: **{report.get('simulator_bug_suspected')}**",
        f"- next experiment: **{report.get('next_experiment')}**",
        "",
        "## Historical/coarse funnel",
        "",
    ]
    _funnel(lines, funnel)

    lines.extend(["", "## Why zero fills?", ""])
    reasons = report.get("zero_fill_reasons") or {}
    if reasons:
        for reason, count in sorted(reasons.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"- `{reason}`: {count}")
    else:
        lines.append("- no zero-fill classifications")

    lines.extend(["", "## Actions", ""] + _table(report.get("actions") or [], "action"))
    lines.extend(["", "## Markets", ""] + _table(report.get("markets") or [], "market_id"))
    lines.extend(["", "## Lifetime buckets", ""] + _table(report.get("lifetimes") or [], "lifetime_bucket"))

    lines.extend(["", "## Closest historical/coarse zero-fill orders", ""])
    for row in (report.get("near_misses") or [])[:10]:
        lines.extend([
            f"### {row.get('order_id')}",
            f"- market: `{row.get('market_id')}`; side/action: `{row.get('side')}/{row.get('action')}`",
            f"- resting: {float(row.get('resting_time_ms',0)):.1f} ms; distance from touch: {row.get('distance_from_touch_ticks')}",
            f"- queue lower/expected/upper: {float(row.get('queue_ahead_lower',0)):.3f} / {float(row.get('queue_ahead_expected',0)):.3f} / {float(row.get('queue_ahead_upper',0)):.3f}",
            f"- aggressive volume: {float(row.get('aggressive_volume',0)):.3f}; near-miss: {float(row.get('near_miss_ratio',0)):.3f}",
            f"- lower/expected/pess fill opportunity: {row.get('fill_opportunity_lower')} / {row.get('fill_opportunity_expected')} / {row.get('fill_opportunity_pessimistic')}",
            f"- classification: **{row.get('fillability_classification')}**",
            "",
        ])

    exact = report.get("forward_exact_ws") if isinstance(report.get("forward_exact_ws"), dict) else {}
    lines.extend(["", "## Forward exact-WS evidence", ""])
    if not exact.get("present"):
        lines.append("No complete forward order life is yet covered by the independent exact public-WS observer.")
    else:
        lines.extend([
            f"- evidence complete: `{exact.get('evidence_complete')}`",
            f"- root cause: **{exact.get('root_cause')}**",
            f"- simulator status: **{exact.get('simulator_bug_suspected')}**",
            f"- next experiment: **{exact.get('next_experiment')}**",
            "",
        ])
        _funnel(lines, exact.get("funnel") or {}, exact=True)
        lines.extend(["", "### Exact-WS zero-fill reasons", ""])
        for reason, count in sorted((exact.get("zero_fill_reasons") or {}).items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"- `{reason}`: {count}")
        lines.extend(["", "### Exact-WS closest orders", ""])
        for row in (exact.get("near_misses") or [])[:10]:
            lines.append(
                f"- `{row.get('order_id')}` market `{row.get('market_id')}`: "
                f"near-miss={float(row.get('near_miss_ratio',0)):.3f}, "
                f"pess_fill={float(row.get('counterfactual_fill_pessimistic',0)):.3f}, "
                f"class={row.get('fillability_classification')}"
            )

    quality = report.get("quality") or {}
    lines.extend([
        "",
        "## Evidence caveat",
        "",
        f"Historical REST evidence has `{quality.get('timestamp_resolution_ms',1000)} ms` exchange timestamp resolution and is conservative/coarse. "
        "Forward exact-WS evidence conserves every public print across own orders, but it is an independent WS connection; a simulator-bug claim still requires same-feed deterministic replay. No simulator relaxation is performed.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v7_live"))
    parser.add_argument("--policy", type=Path, default=Path("config/v7_professional_market_maker.json"))
    parser.add_argument("--model-sha")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-orders", type=Path)
    parser.add_argument("--output-md", type=Path)
    args = parser.parse_args()
    runtime = {}
    try:
        runtime = json.loads((args.run_root / "control" / "runtime_status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    model_sha = args.model_sha or str(runtime.get("model_sha") or "") or None
    report = summarize_best_available_fillability(
        args.run_root / "ledger" / "execution.jsonl",
        args.run_root / "trade_tape.csv",
        args.policy,
        model_sha=model_sha,
    )
    markdown = render_markdown(report)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.output_orders:
        args.output_orders.parent.mkdir(parents=True, exist_ok=True)
        with args.output_orders.open("w", encoding="utf-8") as handle:
            for row in report.get("orders") or []:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(markdown + "\n", encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
