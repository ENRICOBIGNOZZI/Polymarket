#!/usr/bin/env python3
"""Research-only V6 diagnostic adapter for the Polymarket Alpha Factory.

This module interprets V6 live-smoke telemetry without changing the live
champion, model thresholds, portfolio risk, deployment, or execution. It is a
bounded bridge until the main Alpha Factory natively understands V6 sleeves.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


_KV = re.compile(r"([A-Za-z0-9_]+)=([^\s]+)")


def _number(text: str) -> int | float | str:
    try:
        if any(ch in text for ch in ".eE"):
            return float(text)
        return int(text)
    except (TypeError, ValueError):
        return text


def parse_tick(line: str) -> dict[str, Any]:
    """Parse a whitespace key=value runtime tick; malformed fields are ignored."""
    if not isinstance(line, str):
        return {}
    return {key: _number(value.rstrip(",")) for key, value in _KV.findall(line)}


def last_tick(logs: dict[str, Any], name: str) -> dict[str, Any]:
    rows = logs.get(name) if isinstance(logs, dict) else None
    if not isinstance(rows, list):
        return {}
    for row in reversed(rows):
        if isinstance(row, str) and row.strip():
            return parse_tick(row)
    return {}


def is_v6_live(live: dict[str, Any]) -> bool:
    if str(live.get("run_root") or "") == "paper_v6_live":
        return True
    info = ((live.get("metrics") or {}).get("polymarket_runtime_info") or {})
    labels = str(info.get("labels") or "") if isinstance(info, dict) else ""
    return 'adapter="v6"' in labels or 'version="v6"' in labels


def build_v6_diagnostics(live: dict[str, Any]) -> dict[str, Any]:
    """Extract V6 execution/data state from a public live-smoke payload.

    The function fails closed: missing fields are reported as unavailable rather
    than converted into inferred fills, signals, or alpha.
    """
    if not is_v6_live(live):
        return {"detected": False}

    logs = live.get("logs") or {}
    maker = last_tick(logs, "maker")
    multileg = last_tick(logs, "multileg")
    recorder_health = ((live.get("data_health") or {}).get("trade_recorder") or {})
    recorder_fields = recorder_health.get("fields") or {} if isinstance(recorder_health, dict) else {}
    metrics = live.get("metrics") or {}

    signals = int(maker.get("signals", 0) or 0)
    posted = int(maker.get("posted", 0) or 0)
    resting = int(maker.get("resting", 0) or 0)
    positions = int(maker.get("positions", 0) or 0)
    recorder_status = str(recorder_health.get("status") or "unknown") if isinstance(recorder_health, dict) else "unknown"
    trade_age = recorder_fields.get("trade_age_seconds")
    if trade_age is None and isinstance(recorder_health, dict):
        last_trade_ts = recorder_fields.get("last_trade_ts")
        generated_ts = live.get("generated_ts")
        if isinstance(last_trade_ts, (int, float)) and isinstance(generated_ts, (int, float)):
            trade_age = max(0, int(generated_ts) - int(last_trade_ts))

    zero_fill_with_fresh_data = (
        recorder_status == "healthy"
        and posted > 0
        and resting > 0
        and positions == 0
    )

    return {
        "detected": True,
        "git_sha": str(live.get("git_sha") or ""),
        "generated_ts": live.get("generated_ts"),
        "micro_maker": {
            "markets": int(maker.get("markets", 0) or 0),
            "signals": signals,
            "posted": posted,
            "resting": resting,
            "positions": positions,
            "reserved_usd": float(maker.get("reserved", 0.0) or 0.0),
            "zero_fill_with_fresh_data": zero_fill_with_fresh_data,
        },
        "multileg": {
            "bundles": int(multileg.get("bundles", 0) or 0),
            "resting": int(multileg.get("resting", 0) or 0),
            "complete": int(multileg.get("complete", 0) or 0),
            "aborting": int(multileg.get("aborting", 0) or 0),
            "closed": int(multileg.get("closed", 0) or 0),
            "unwound": int(multileg.get("unwound", 0) or 0),
            "trades_processed": int(multileg.get("trades_processed", 0) or 0),
        },
        "trade_recorder": {
            "status": recorder_status,
            "markets": int(recorder_fields.get("markets", 0) or 0),
            "new_trades": int(recorder_fields.get("new_trades", 0) or 0),
            "errors": int(recorder_fields.get("errors", 0) or 0),
            "trade_age_seconds": trade_age,
        },
        "portfolio": {
            "equity_usd": float(metrics.get("polymarket_runtime_equity_usd", 0.0) or 0.0),
            "gross_exposure_usd": float(metrics.get("polymarket_runtime_gross_exposure_usd", 0.0) or 0.0),
            "reserved_cash_usd": float(metrics.get("polymarket_runtime_reserved_cash_usd", 0.0) or 0.0),
            "realized_pnl_usd": float(metrics.get("polymarket_runtime_realized_pnl_usd_total", 0.0) or 0.0),
            "oos_trades": int(metrics.get("polymarket_runtime_oos_trades", 0.0) or 0),
        },
    }


def recommend_v6_experiments(diag: dict[str, Any]) -> list[dict[str, Any]]:
    """Return non-duplicative V6-specific research handoffs."""
    if not diag.get("detected"):
        return []
    out: list[dict[str, Any]] = []
    maker = diag.get("micro_maker") or {}
    recorder = diag.get("trade_recorder") or {}
    multileg = diag.get("multileg") or {}

    if maker.get("zero_fill_with_fresh_data"):
        out.append({
            "experiment_id": "v6_micro_queue_fillability",
            "priority": 1,
            "owner": "HF research / forward-maker worker",
            "hypothesis": "Fresh trade data plus resting maker orders and zero positions identifies queue/fillability, not signal admission, as the immediate bottleneck.",
            "evidence": f"signals={maker.get('signals', 0)} posted={maker.get('posted', 0)} resting={maker.get('resting', 0)} positions=0 recorder={recorder.get('status')}",
            "success_metric": "candidate-specific queue depletion/fill hazard, completed fills, and 60s/300s markout positive after 1x/1.5x/2x execution stress",
            "do_not_do": "do not lower taker edge below executable costs merely to manufacture fills",
        })

    if int(multileg.get("bundles", 0) or 0) == 0 and int(multileg.get("trades_processed", 0) or 0) > 0:
        out.append({
            "experiment_id": "v6_rv_admission_attribution",
            "priority": 3,
            "owner": "LF research",
            "hypothesis": "The V6 RV path is observing market trades but currently emits no bundles; attribution must separate no statistical signal, relation rejection, and negative post-cost edge.",
            "evidence": f"trades_processed={multileg.get('trades_processed', 0)} bundles=0",
            "success_metric": "per-stage funnel for local-factor/graph relations with raw forecast edge, hedge cost wedge, final executable edge, and rejection reason on identical chronological rows",
            "do_not_do": "do not relax unit-root/FDR or relation semantics before the post-cost attribution is known",
        })

    return sorted(out, key=lambda item: (item["priority"], item["experiment_id"]))


def build_report(live: dict[str, Any]) -> dict[str, Any]:
    diag = build_v6_diagnostics(live)
    return {
        "schema": "polymarket_alpha_factory_v6_diagnostics_v1",
        "research_only": True,
        "authenticated_execution": False,
        "direct_champion_mutation": False,
        "diagnostics": diag,
        "next_experiments": recommend_v6_experiments(diag),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-smoke", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        live = json.loads(args.live_smoke.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        live = {}
    report = build_report(live)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
