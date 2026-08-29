#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Job:
    name: str
    cadence_seconds: int
    command: tuple[str, ...]
    log_path: Path


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified V7 multi-frequency research/shadow scheduler")
    parser.add_argument("--paper-config", type=Path, default=Path("config/paper_v7.json"))
    parser.add_argument("--frequency-config", type=Path, default=Path("config/v7_frequency_matrix.json"))
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v7_live"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    paper = json.loads(args.paper_config.read_text(encoding="utf-8"))
    freq = json.loads(args.frequency_config.read_text(encoding="utf-8"))
    cadence = freq["research_recalibration_seconds"]
    root = args.run_root
    shadow = root / "shadow"
    shadow.mkdir(parents=True, exist_ok=True)
    gamma = str(paper.get("gamma_url", "https://gamma-api.polymarket.com"))
    clob = str(paper.get("clob_url", "https://clob.polymarket.com"))

    ranking_state = shadow / "ranking_state.json"
    jobs = [
        Job(
            "pca_stat_arb",
            int(cadence["pca_stat_arb"]),
            (
                sys.executable, "scripts/v7_pca_stat_arb_research.py",
                "--config", "config/research_v7_pca_stat_arb.json",
                "--paper-config", str(args.paper_config),
                "--output-json", str(shadow / "pca_stat_arb.json"),
            ),
            shadow / "pca_stat_arb.log",
        ),
        Job(
            "local_factor_30m",
            int(cadence["local_factor_30m"]),
            (
                sys.executable, "scripts/v7_local_factor_research.py",
                "--config", "config/research_v7_local_factor.json",
                "--paper-config", str(args.paper_config),
                "--output-json", str(shadow / "local_factor_30m.json"),
            ),
            shadow / "local_factor_30m.log",
        ),
        Job(
            "local_factor_60m",
            int(cadence["local_factor_60m"]),
            (
                sys.executable, "scripts/v7_local_factor_research.py",
                "--config", "config/research_v7_local_factor_60m.json",
                "--paper-config", str(args.paper_config),
                "--output-json", str(shadow / "local_factor_60m.json"),
            ),
            shadow / "local_factor_60m.log",
        ),
        Job(
            "cross_sectional_rank",
            int(cadence["cross_sectional_rank"]),
            (
                sys.executable, "scripts/v7_cross_sectional_rank_forward_multifreq.py",
                "--config", "config/research_v7_cross_sectional_rank.json",
                "--state-in", str(ranking_state),
                "--state-out", str(ranking_state),
                "--output-json", str(shadow / "cross_sectional_rank.json"),
                "--gamma-url", gamma,
                "--clob-url", clob,
                "--market-limit", str(int(paper.get("market_limit", 1000))),
            ),
            shadow / "cross_sectional_rank.log",
        ),
        Job(
            "hf_frequency_probe",
            300,
            (
                sys.executable, "scripts/v7_hf_frequency_probe.py",
                "--frequency-config", str(args.frequency_config),
                "--trade-tape", str(root / "execution" / "trade_tape.csv"),
                "--maker-orders", str(root / "execution" / "maker" / "maker_orders.csv"),
                "--output-json", str(shadow / "hf_frequency_probe.json"),
            ),
            shadow / "hf_frequency_probe.log",
        ),
    ]

    stopped = False
    active: dict[str, tuple[subprocess.Popen[bytes], Any]] = {}
    last_started = {job.name: 0 for job in jobs}
    status_path = shadow / "scheduler_status.json"

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def poll_finished() -> None:
        for name, (process, handle) in list(active.items()):
            code = process.poll()
            if code is None:
                continue
            handle.close()
            del active[name]

    while not stopped:
        poll_finished()
        now = int(time.time())
        for job in jobs:
            if job.name in active:
                continue
            if not args.once and now - last_started[job.name] < job.cadence_seconds:
                continue
            job.log_path.parent.mkdir(parents=True, exist_ok=True)
            handle = job.log_path.open("ab", buffering=0)
            process = subprocess.Popen(job.command, stdout=handle, stderr=subprocess.STDOUT, close_fds=True)
            active[job.name] = (process, handle)
            last_started[job.name] = now
            if len(active) >= 2:
                break

        atomic_json(
            status_path,
            {
                "timestamp": now,
                "paper_only": True,
                "authenticated_execution": False,
                "active_jobs": {name: process.pid for name, (process, _handle) in active.items()},
                "last_started": last_started,
                "frequency_matrix": freq,
            },
        )
        if args.once:
            while active:
                time.sleep(1)
                poll_finished()
            break
        time.sleep(5)

    for process, handle in active.values():
        if process.poll() is None:
            process.terminate()
    deadline = time.time() + 5
    for process, handle in active.values():
        try:
            process.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
