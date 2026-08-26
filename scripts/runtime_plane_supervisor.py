#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path


def terminate(process: subprocess.Popen[object] | None, signum: int = signal.SIGTERM) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def start(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.Popen[object]:
    return subprocess.Popen(command, env=env, close_fds=True, start_new_session=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervise the canonical V7 PAPER champion and separate read-only fast-arb shadow plane")
    parser.add_argument("--champion-manifest", type=Path, default=Path("config/live_champion.json"))
    parser.add_argument("--fast-run-dir", type=Path, default=Path("runs/live-fast-shadow"))
    parser.add_argument("--fast-duration-seconds", type=int, default=3600)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / args.champion_manifest).read_text(encoding="utf-8"))
    if int(manifest.get("version") or 0) != 7:
        raise SystemExit("runtime supervisor accepts only canonical V7")
    if manifest.get("paper_only") is not True:
        raise SystemExit("runtime supervisor requires PAPER-only champion")
    if manifest.get("authenticated_execution") is not False:
        raise SystemExit("runtime supervisor requires authenticated execution disabled")
    if manifest.get("loop") != "scripts/paper_v7_loop.sh":
        raise SystemExit("runtime supervisor requires canonical V7 loop")
    if manifest.get("config") != "config/paper_v7.json":
        raise SystemExit("runtime supervisor requires canonical V7 config")
    if manifest.get("run_root") != "runs/paper_v7_live":
        raise SystemExit("runtime supervisor requires canonical V7 run root")

    loop = root / str(manifest["loop"])
    config = root / str(manifest["config"])
    run_root = root / str(manifest["run_root"])
    if not loop.is_file() or not config.is_file():
        raise SystemExit("live champion manifest points to missing V7 loop/config")

    args.fast_run_dir.mkdir(parents=True, exist_ok=True)
    champion_env = os.environ.copy()
    champion_env["POLYMARKET_RUNTIME_PARENT_PID"] = str(os.getpid())
    champion_command = ["bash", str(loop), str(config), str(run_root)]
    fast_command = [
        str(root / "build" / "polymarket_fast_arb_shadow"),
        "--config", str(config),
        "--policy", str(root / "config" / "fast_arb_policy.json"),
        "--relations", str(root / "config" / "fast_arb_relations.csv"),
        "--external-signals", str(root / "data" / "external_signals.csv"),
        "--run-dir", str(args.fast_run_dir),
        "--markets", "600",
        "--min-liquidity", "100",
        "--shard-size", "200",
        "--snapshot-refresh-seconds", "20",
        "--duration-seconds", str(max(60, int(args.fast_duration_seconds))),
        "--recycle-seconds", "0",
    ]

    champion: subprocess.Popen[object] | None = None
    fast: subprocess.Popen[object] | None = None
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        terminate(fast)
        terminate(champion)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, stop)

    champion = start(champion_command, env=champion_env)
    fast = start(fast_command)
    try:
        while not stopping:
            champion_code = champion.poll()
            if champion_code is not None:
                terminate(fast)
                return champion_code if champion_code != 0 else 1
            if fast.poll() is not None:
                time.sleep(2)
                if stopping:
                    break
                fast = start(fast_command)
            time.sleep(1)
    finally:
        terminate(fast)
        terminate(champion)
        deadline = time.time() + 5
        for process in (fast, champion):
            if process is None or process.poll() is not None:
                continue
            try:
                process.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                terminate(process, signal.SIGKILL)
                process.wait(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
