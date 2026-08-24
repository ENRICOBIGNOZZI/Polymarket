#!/usr/bin/env python3
"""Bounded, auditable continuous alpha-research controller.

It compares a fixed challenger set with contemporaneous B1/B2 champions,
requires executable-cost screening and separate chronological OOS evidence,
and never changes production directly. Promotion is draft-PR only.
"""
from __future__ import annotations

import argparse
import json
import shlex
import time
from pathlib import Path
from typing import Any, Callable

from alpha_research_model import (
    SCHEMA, Candidate, ConfigError, atomic_write_json, atomic_write_text,
    load_config, scanner_command, select_challengers,
)
from alpha_research_metrics import (
    promotion_gate, read_json_object, resolve_report_path, screen_candidate,
    summarize_scan,
)
from alpha_research_report import append_history, render_markdown, run_scanner

def run_cycle(
    config: dict[str, Any],
    run_root: Path,
    build_dir: Path,
    paper_config: Path,
    now: int,
    execute: bool,
    source_sha: str = "",
    runner: Callable[[list[str], Path, Path, int], dict[str, Any]] = run_scanner,
) -> dict[str, Any]:
    cycle_index, selected = select_challengers(config, now)
    champions: dict[str, Candidate] = config["_champions"]
    candidates = [champions["B1"], champions["B2"], *selected]
    cycle_dir = run_root / "alpha_research" / "cycles" / f"{now}-{cycle_index}"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    timeout_seconds = int(config.get("scanner_timeout_seconds", 1200))
    notional_cap = float(config.get("screen", {}).get("per_opportunity_notional_cap", 250.0))

    scans: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        csv_path = cycle_dir / f"{candidate.candidate_id}.csv"
        stdout_path = cycle_dir / f"{candidate.candidate_id}.stdout.log"
        stderr_path = cycle_dir / f"{candidate.candidate_id}.stderr.log"
        command = scanner_command(candidate, build_dir, paper_config, csv_path)
        execution = {"returncode": None, "timed_out": False, "duration_seconds": 0.0}
        if execute:
            execution = runner(command, stdout_path, stderr_path, timeout_seconds)
        metrics = summarize_scan(csv_path, candidate.family, notional_cap)
        scans[candidate.candidate_id] = {
            "candidate": candidate,
            "csv": str(csv_path),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "command": command,
            "command_display": shlex.join(command),
            "execution": execution,
            "metrics": metrics,
        }

    tested_count = max(1, len(config["_challengers"]))
    items: list[dict[str, Any]] = []
    promotion_ready = 0
    for candidate in candidates:
        scan = scans[candidate.candidate_id]
        metrics = scan["metrics"]
        execution = scan["execution"]
        stage = "champion_reference" if candidate.champion else "scanner_error"
        screen_ok = candidate.champion
        screen_failures: list[str] = []
        screen_evidence = {
            "candidate_score": float(metrics["diversified_screen_score"]),
            "champion_score": float(metrics["diversified_screen_score"]),
            "absolute_improvement": 0.0,
            "improvement_ratio": 1.0,
        }
        promotion_failures: list[str] = []
        promotion_evidence: dict[str, Any] = {}

        if execute and execution.get("returncode") != 0:
            stage = "scanner_error"
            screen_failures = ["scanner_failed"]
        elif not candidate.champion:
            champion_scan = scans[champions[candidate.family].candidate_id]
            if execute and champion_scan["execution"].get("returncode") != 0:
                stage = "scanner_error"
                screen_failures = ["champion_scanner_failed"]
                items.append({
                    "id": candidate.candidate_id,
                    "family": candidate.family,
                    "champion": candidate.champion,
                    "hypothesis": candidate.hypothesis,
                    "params": candidate.params,
                    "execution_min_edge": candidate.execution_min_edge,
                    "stage": stage,
                    "screen_pass": False,
                    "screen_failures": screen_failures,
                    "screen": screen_evidence,
                    "promotion_failures": [],
                    "promotion": {},
                    "metrics": metrics,
                    "command": scan["command"],
                    "command_display": scan["command_display"],
                    "execution": execution,
                    "artifacts": {
                        "scanner_csv": scan["csv"],
                        "stdout": scan["stdout"],
                        "stderr": scan["stderr"],
                    },
                })
                continue
            champion_metrics = champion_scan["metrics"]
            screen_ok, screen_failures, screen_evidence = screen_candidate(
                metrics, champion_metrics, config.get("screen", {})
            )
            if not screen_ok:
                stage = "screen_rejected"
            else:
                challenger_oos_path = resolve_report_path(candidate.oos_report, run_root)
                champion_oos_path = resolve_report_path(champions[candidate.family].oos_report, run_root)
                promotion_ok, promotion_failures, promotion_evidence = promotion_gate(
                    read_json_object(challenger_oos_path),
                    read_json_object(champion_oos_path),
                    config.get("promotion", {}),
                    tested_count,
                )
                if promotion_ok:
                    stage = "promotion_ready"
                    promotion_ready += 1
                elif "missing_challenger_oos" in promotion_failures:
                    stage = "shadow_required"
                else:
                    stage = "oos_rejected"

        items.append({
            "id": candidate.candidate_id,
            "family": candidate.family,
            "champion": candidate.champion,
            "hypothesis": candidate.hypothesis,
            "params": candidate.params,
            "execution_min_edge": candidate.execution_min_edge,
            "stage": stage,
            "screen_pass": screen_ok,
            "screen_failures": screen_failures,
            "screen": screen_evidence,
            "promotion_failures": promotion_failures,
            "promotion": promotion_evidence,
            "metrics": metrics,
            "command": scan["command"],
            "command_display": scan["command_display"],
            "execution": execution,
            "artifacts": {
                "scanner_csv": scan["csv"],
                "stdout": scan["stdout"],
                "stderr": scan["stderr"],
            },
        })

    return {
        "schema": SCHEMA,
        "started_ts": now,
        "source_sha": source_sha,
        "cycle_index": cycle_index,
        "cadence_seconds": config["cadence_seconds"],
        "selected_challengers": [c.candidate_id for c in selected],
        "tested_challengers": tested_count,
        "production_modified": False,
        "promotion_requires_pull_request": True,
        "promotion_ready_count": promotion_ready,
        "candidates": items,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("config/alpha_research.json"))
    ap.add_argument("--run-root", type=Path, default=Path("runs/paper_v4_live"))
    ap.add_argument("--build-dir", type=Path, default=Path("build"))
    ap.add_argument("--paper-config", type=Path, default=Path("config/paper_v4.json"))
    ap.add_argument("--now", type=int, default=None)
    ap.add_argument("--execute", action="store_true", help="run the fixed scanner commands")
    ap.add_argument("--source-sha", default="", help="validated source revision used for this cycle")
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()

    config = load_config(args.config)
    now = int(time.time()) if args.now is None else args.now
    report = run_cycle(config, args.run_root, args.build_dir, args.paper_config, now, args.execute, args.source_sha)

    latest_json = args.output or args.run_root / "alpha_research" / "latest.json"
    latest_md = latest_json.with_suffix(".md")
    atomic_write_json(latest_json, report)
    atomic_write_text(latest_md, render_markdown(report))
    append_history(args.run_root / "alpha_research" / "history.jsonl", report)
    print(json.dumps(report, indent=2, sort_keys=True))

    scanner_errors = sum(item["stage"] == "scanner_error" for item in report["candidates"])
    return 1 if args.execute and scanner_errors == len(report["candidates"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
