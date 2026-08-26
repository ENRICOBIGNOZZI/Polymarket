#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_alpha_factory_data_health_audit_v1"

# These experiments infer execution economics directly from the canonical live-smoke
# snapshot. They are invalid when that snapshot's public trade tape is unhealthy.
LIVE_EXECUTION_EXPERIMENTS = {
    "execution_fillability_frontier",
    "b1_execution_cost_surface",
    "b2_clustered_dynamic_factor",
}


def recorder_health(live: dict[str, Any]) -> tuple[str, list[str]]:
    data_health = live.get("data_health") or {}
    recorder = data_health.get("trade_recorder") or {}
    status = str(recorder.get("status") or "missing").strip().lower()
    failures = recorder.get("failures") or []
    if not isinstance(failures, list):
        failures = [str(failures)]
    return status, [str(item) for item in failures if str(item)]


def experiment_ids(alpha_report: dict[str, Any] | None) -> set[str]:
    if not isinstance(alpha_report, dict):
        return set()
    rows = alpha_report.get("next_experiments") or []
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("experiment_id"))
        for row in rows
        if isinstance(row, dict) and row.get("experiment_id")
    }


def evaluate(live: dict[str, Any], alpha_report: dict[str, Any] | None = None) -> dict[str, Any]:
    status, failures = recorder_health(live)
    healthy = status == "healthy"
    scheduled = experiment_ids(alpha_report)
    contaminated = sorted(scheduled.intersection(LIVE_EXECUTION_EXPERIMENTS)) if not healthy else []
    walk = live.get("walk_forward") or {}
    oos = walk.get("oos") or {}
    return {
        "schema": SCHEMA,
        "git_sha": str(live.get("git_sha") or ""),
        "live_generated_ts": live.get("generated_ts"),
        "trade_recorder_status": status,
        "trade_recorder_failures": failures,
        "execution_evidence_usable": healthy,
        "alpha_factory_should_degrade": not healthy,
        "zero_oos_trades": int(float(oos.get("trades", 0) or 0)) == 0,
        "contaminated_live_execution_experiments": contaminated,
        "decision": "BLOCK_LIVE_EXECUTION_INFERENCE" if not healthy else "ALLOW_LIVE_EXECUTION_INFERENCE",
        "successor_contract": [
            "freshness and public-trade-tape health must both be true before live zero-fill/completion observations are economic evidence",
            "fresh-but-unhealthy live smoke must produce an explicit degraded-data-health state",
            "do not schedule live-derived fillability/B1/B2 execution experiments from an unhealthy canonical tape",
            "do not advance live-derived candidate evidence or consecutive-pass state from an unhealthy canonical tape",
            "independent workers may still contribute evidence only when their own data-health contract is explicitly healthy",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether Alpha Factory may use canonical live execution evidence")
    parser.add_argument("--live-smoke", type=Path, required=True)
    parser.add_argument("--alpha-report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    live = json.loads(args.live_smoke.read_text(encoding="utf-8"))
    report = None
    if args.alpha_report and args.alpha_report.exists():
        report = json.loads(args.alpha_report.read_text(encoding="utf-8"))
    result = evaluate(live, report)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["execution_evidence_usable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
