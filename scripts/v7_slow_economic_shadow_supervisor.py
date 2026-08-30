#!/usr/bin/env python3
"""Always-on exact-SHA supervisor for Ranking, PCA and Local Factor research.

The children are periodic, frozen research evaluations with no capital, OMS or
ledger-writer authority.  The supervisor itself stays alive, publishes a fresh
manifest, preserves every prior report, and exposes failures as real blockers
instead of silently treating an absent scheduled workflow as evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_slow_economic_shadow_manifest_v1"
STATUS_SCHEMA = "polymarket_v7_slow_economic_shadow_status_v1"
FAMILIES = ("ranking", "pca", "local_factor")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class Supervisor:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.repository = args.repository_root.resolve()
        self.run_root = args.run_root.resolve()
        self.control = self.run_root / "control"
        self.manifest_path = self.control / "slow_research_shadow_manifest.json"
        self.cycle = 0
        scope = load_json(args.scope)
        configured = tuple(scope.get("always_on_economic_shadow_families") or [])
        if set(configured) != set(FAMILIES):
            raise RuntimeError("slow_economic_shadow_scope_not_exact")
        if (scope.get("paper_only") is not True
                or scope.get("authenticated_execution") is not False
                or scope.get("real_order_submission") is not False):
            raise RuntimeError("slow_economic_shadow_scope_unsafe")
        self.statuses: dict[str, dict[str, Any]] = {
            family: self.base_status(family, "WAITING_FOR_FIRST_CYCLE")
            for family in FAMILIES
        }

    def base_status(self, family: str, state: str) -> dict[str, Any]:
        return {
            "schema": STATUS_SCHEMA, "version": 7, "family": family,
            "timestamp": int(time.time()), "model_sha": self.args.model_sha,
            "paper_only": True, "research_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "has_capital": False, "has_oms_authority": False,
            "has_ledger_writer_authority": False, "automatic_promotion": False,
            "process_state": state, "economic_authority": "SHADOW_ZERO_CAPITAL",
        }

    def paths(self, family: str) -> tuple[Path, Path, Path]:
        root = self.run_root / "shadow" / family
        return root / "report.json", root / "opportunities.csv", root / "worker.log"

    def command(self, family: str) -> list[str]:
        report, opportunities, _log = self.paths(family)
        python = sys.executable
        if family == "ranking":
            return [
                python, "scripts/v7_cross_sectional_rank.py",
                "--config", "config/research_v7_cross_sectional_rank_frozen.json",
                "--market-limit", str(self.args.market_limit),
                "--output-json", str(report),
                "--output-shadow-intents", str(opportunities),
            ]
        if family == "pca":
            return [
                python, "scripts/v7_pca_stat_arb_research.py",
                "--config", "config/research_v7_pca_stat_arb.json",
                "--paper-config", "config/research_v7_market_data.json",
                "--market-limit", str(self.args.market_limit),
                "--bootstrap-reps", str(self.args.bootstrap_reps),
                "--output-json", str(report),
            ]
        return [
            python, "scripts/v7_local_factor_research.py",
            "--config", "config/research_v7_local_factor.json",
            "--paper-config", "config/research_v7_market_data.json",
            "--bootstrap-reps", str(self.args.bootstrap_reps),
            "--output-json", str(report),
        ]

    def publish(self) -> None:
        now = int(time.time())
        for family, status in self.statuses.items():
            status["timestamp"] = now
            atomic_json(self.run_root / "shadow" / family / "status.json", status)
        atomic_json(self.manifest_path, {
            "schema": SCHEMA, "version": 7, "timestamp": now,
            "model_sha": self.args.model_sha, "supervisor_pid": os.getpid(),
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "research_only": True,
            "families": self.statuses, "family_count": len(self.statuses),
            "always_on": True, "cycle": self.cycle,
            "automatic_promotion": False, "single_execution_owner_preserved": True,
        })

    def run_cycle(self) -> None:
        self.cycle += 1
        started = int(time.time())
        children: dict[str, tuple[subprocess.Popen[Any], Any]] = {}
        for family in FAMILIES:
            report, opportunities, log = self.paths(family)
            report.parent.mkdir(parents=True, exist_ok=True)
            handle = log.open("ab")
            process = subprocess.Popen(
                self.command(family), cwd=self.repository,
                stdout=handle, stderr=subprocess.STDOUT,
            )
            children[family] = (process, handle)
            self.statuses[family] = {
                **self.base_status(family, "RUNNING_EVALUATION"),
                "worker_pid": process.pid, "cycle": self.cycle,
                "cycle_started_at": started, "report_path": str(report),
                "opportunity_path": str(opportunities) if family == "ranking" else "",
                "blocker": "",
            }
        self.publish()
        deadline = time.monotonic() + max(1, self.args.worker_timeout_seconds)
        while children and not (self.run_root / "control" / "KILL").exists():
            for family, (process, handle) in list(children.items()):
                code = process.poll()
                if code is None and time.monotonic() < deadline:
                    continue
                timed_out = code is None
                if timed_out:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    code = process.returncode
                handle.close()
                report, opportunities, _log = self.paths(family)
                valid_report = report.is_file() and report.stat().st_size > 0
                state = "ACTIVE_SHADOW_EVIDENCE" if code == 0 and valid_report else "BLOCKED_RUNTIME"
                blocker = "" if state == "ACTIVE_SHADOW_EVIDENCE" else (
                    "WORKER_TIMEOUT" if timed_out else f"WORKER_EXIT_{code}"
                )
                self.statuses[family] = {
                    **self.base_status(family, state), "cycle": self.cycle,
                    "cycle_started_at": started, "cycle_completed_at": int(time.time()),
                    "worker_exit_code": code, "report_path": str(report),
                    "report_present": valid_report,
                    "opportunity_path": str(opportunities) if family == "ranking" else "",
                    "opportunity_tape_present": opportunities.is_file() if family == "ranking" else False,
                    "economic_loop_state": (
                        "FORWARD_SHADOW_REPORT_COLLECTING" if valid_report
                        else "NO_CAUSAL_REPORT"
                    ),
                    "blocker": blocker,
                }
                children.pop(family)
            self.publish()
            if children:
                time.sleep(min(5.0, self.args.heartbeat_seconds))
        for process, handle in children.values():
            if process.poll() is None:
                process.terminate()
            handle.close()
        self.publish()

    def run(self) -> None:
        self.publish()
        while not (self.run_root / "control" / "KILL").exists():
            self.run_cycle()
            if self.args.once:
                return
            wake = time.monotonic() + max(60, self.args.interval_seconds)
            while time.monotonic() < wake and not (self.run_root / "control" / "KILL").exists():
                self.publish()
                time.sleep(min(self.args.heartbeat_seconds, max(0.1, wake - time.monotonic())))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--interval-seconds", type=int, default=7200)
    parser.add_argument("--heartbeat-seconds", type=float, default=5.0)
    parser.add_argument("--worker-timeout-seconds", type=int, default=1800)
    parser.add_argument("--market-limit", type=int, default=500)
    parser.add_argument("--bootstrap-reps", type=int, default=200)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise RuntimeError("exact_model_sha_required")
    Supervisor(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
