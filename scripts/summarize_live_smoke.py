#!/usr/bin/env python3
"""Summarize one exact-SHA V7 PAPER validation run.

The snapshot is deliberately a thin, lossless summary of canonical V7 runtime,
strategy and execution-evidence state. It does not reconstruct retired B1/B2/B3
pipelines, infer synthetic walk-forward results, or weaken economic gates.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_public_live_smoke_v1"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            return [dict(row) for row in csv.DictReader(handle) if row]
    except (OSError, csv.Error):
        return []


def mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def summarize(run_root: Path, *, git_sha: str, run_id: str, now: int | None = None) -> dict[str, Any]:
    now_ts = int(time.time()) if now is None else int(now)
    runtime_path = run_root / "runtime_status.json"
    strategy_path = run_root / "strategy_status.csv"
    evidence_path = run_root / "v7_execution_evidence.json"
    action_path = run_root / "action_report.json"
    proxy_path = run_root / "market_proxy_status.json"
    runtime = read_json(runtime_path)
    execution = read_json(evidence_path)
    action = read_json(action_path)
    proxy = read_json(proxy_path)
    strategy_rows = read_csv(strategy_path)

    strategies = runtime.get("strategies") if isinstance(runtime.get("strategies"), dict) else {}
    total_fills = sum(integer(row.get("fills")) for row in strategies.values() if isinstance(row, dict))
    if not total_fills:
        total_fills = sum(integer(row.get("fills")) for row in strategy_rows)

    models = execution.get("models") if isinstance(execution.get("models"), dict) else {}
    eligible_models = [name for name, row in models.items() if isinstance(row, dict) and row.get("paper_eligible") is True]
    insufficient_models = [name for name, row in models.items() if isinstance(row, dict) and row.get("paper_eligible") is not True]

    runtime_ts = integer(runtime.get("timestamp"), mtime(runtime_path))
    evidence_ts = integer(execution.get("generated_ts"), mtime(evidence_path))
    proxy_ts = integer(proxy.get("timestamp"), mtime(proxy_path))

    return {
        "schema": SCHEMA,
        "generated_ts": now_ts,
        "git_sha": git_sha,
        "run_id": str(run_id),
        "paper_only": True,
        "authenticated_execution": False,
        "runtime": {
            "present": bool(runtime),
            "version": integer(runtime.get("version")),
            "timestamp": runtime_ts,
            "age_seconds": max(0, now_ts - runtime_ts) if runtime_ts else None,
            "paper_only": runtime.get("paper_only") is True,
            "authenticated_execution": runtime.get("authenticated_execution") is True,
            "equity_usd": finite(runtime.get("equity")),
            "pnl_usd": finite(runtime.get("pnl")),
            "realized_pnl_usd": finite(runtime.get("realized_pnl")),
            "drawdown": finite(runtime.get("drawdown")),
            "killed": bool(runtime.get("killed")),
            "gross_exposure_usd": finite(runtime.get("gross_exposure")),
            "reserved_cash_usd": finite(runtime.get("reserved_cash")),
            "live_units": integer(runtime.get("live_units")),
            "total_fills": total_fills,
            "strategy_count": len(strategies) if strategies else len(strategy_rows),
            "strategies": strategies,
        },
        "execution_evidence": {
            "present": bool(execution),
            "schema": execution.get("schema"),
            "generated_ts": evidence_ts,
            "age_seconds": max(0, now_ts - evidence_ts) if evidence_ts else None,
            "evidence_id": execution.get("evidence_id"),
            "summary": execution.get("summary") if isinstance(execution.get("summary"), dict) else {},
            "eligible_models": sorted(eligible_models),
            "insufficient_models": sorted(insufficient_models),
            "models": models,
        },
        "action_report": action,
        "data_health": {
            "market_proxy_present": bool(proxy),
            "market_proxy_timestamp": proxy_ts,
            "market_proxy_age_seconds": max(0, now_ts - proxy_ts) if proxy_ts else None,
            "market_proxy_source": proxy.get("source"),
            "market_proxy_markets": integer(proxy.get("markets")),
            "market_proxy_failures": integer(proxy.get("failures")),
            "runtime_execution_staleness_seconds": finite(runtime.get("execution_staleness")),
        },
        "source_files": {
            "runtime_status": str(runtime_path),
            "strategy_status": str(strategy_path),
            "execution_evidence": str(evidence_path),
            "action_report": str(action_path),
            "market_proxy_status": str(proxy_path),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize canonical V7 PAPER validation state")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--trade-lookback-seconds", type=int, default=900, help="retained CLI compatibility; V7 snapshot uses canonical runtime/evidence state")
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()
    report = summarize(args.run_root, git_sha=args.git_sha, run_id=args.run_id, now=args.now)
    atomic_json(args.output, report)
    print(json.dumps({
        "schema": report["schema"],
        "runtime_present": report["runtime"]["present"],
        "runtime_total_fills": report["runtime"]["total_fills"],
        "execution_evidence_present": report["execution_evidence"]["present"],
        "execution_evidence_eligible_models": len(report["execution_evidence"]["eligible_models"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
