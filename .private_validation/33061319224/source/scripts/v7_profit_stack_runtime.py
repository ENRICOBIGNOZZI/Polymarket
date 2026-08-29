#!/usr/bin/env python3
"""Canonical V7 PAPER profit-stack runtime.

One process owns the V7 execution ledger.  The public trade recorder is a data
producer only; External Intelligence is materialized fail-closed; the complete-
set Maker is currently the only executable sleeve.  LF/PCA/Ranking remain
candidate-only until their point-in-time/survivorship contracts become valid.

There is no authenticated-order code path in this runtime.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_canonical_economics as economics
import v7_complete_set_maker as maker
import v7_execution_ledger as ledger
import v7_external_bridge as external

SCHEMA = "polymarket_v7_profit_stack_runtime_v1"
_STOP = False


class RuntimeContractError(ValueError):
    pass


def _stop(_signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{time.time_ns()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def repository_sha() -> str:
    value = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=10).strip().lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise RuntimeContractError("runtime git SHA is invalid")
    expected = os.environ.get("POLYMARKET_EXPECTED_MODEL_SHA", "").strip().lower()
    if expected and expected != value:
        raise RuntimeContractError(f"runtime SHA {value} != expected validated SHA {expected}")
    return value


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeContractError("runtime config must be an object")
    if value.get("schema") != "polymarket_v7_profit_stack_v1" or int(value.get("version") or 0) != 7:
        raise RuntimeContractError("runtime config is not V7 profit stack")
    if value.get("paper_only") is not True or value.get("authenticated_execution") is not False:
        raise RuntimeContractError("runtime violates PAPER/authenticated-execution boundary")
    maker_cfg = value.get("maker")
    if not isinstance(maker_cfg, dict) or maker_cfg.get("enabled") is not True:
        raise RuntimeContractError("complete-set Maker must be the explicit executable V7 sleeve")
    candidate_models = value.get("candidate_models")
    if not isinstance(candidate_models, dict):
        raise RuntimeContractError("candidate model contract missing")
    for family in ("local_factor", "pca", "ranking"):
        row = candidate_models.get(family)
        if not isinstance(row, dict) or row.get("execution_enabled") is not False:
            raise RuntimeContractError(f"{family} cannot execute before point-in-time evidence matures")
    recorder = value.get("trade_recorder")
    if not isinstance(recorder, dict):
        raise RuntimeContractError("trade recorder config missing")
    if int(recorder.get("market_limit") or 0) != 1000 or float(recorder.get("minimum_liquidity_usd") or 0.0) < 2.0:
        raise RuntimeContractError("trade recorder does not match V7 market/liquidity authority")
    return value


def rooted(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeContractError(f"runtime path must remain repository-relative: {relative}")
    return ROOT / path


def start_trade_recorder(cfg: dict[str, Any], run_root: Path) -> subprocess.Popen[str]:
    row = cfg["trade_recorder"]
    binary = ROOT / "build" / "polymarket_trade_recorder"
    if not binary.is_file():
        raise RuntimeContractError(f"trade recorder binary missing: {binary}")
    config_path = rooted(str(row["config"]))
    if not config_path.is_file():
        raise RuntimeContractError(f"trade recorder config missing: {config_path}")
    command = [
        str(binary),
        "--config", str(config_path),
        "--run-dir", str(run_root),
        "--markets", str(int(row["market_limit"])),
        "--batch", str(int(row.get("batch_size") or 100)),
        "--min-liquidity", str(float(row["minimum_liquidity_usd"])),
        "--lookback-seconds", str(int(row.get("lookback_seconds") or 300)),
        "--interval", str(int(row.get("interval_seconds") or 10)),
        "--loop",
    ]
    return subprocess.Popen(command, cwd=ROOT, text=True)


def refresh_external(cfg: dict[str, Any], run_root: Path) -> dict[str, Any]:
    row = cfg.get("external") or {}
    output = rooted(str(row.get("output") or "data/external_signals.csv"))
    status_path = run_root / "external_bridge_status.json"
    now = int(time.time())
    external.atomic_write(output, external.EMPTY_FEED)
    initializing = {
        "schema": external.SCHEMA,
        "timestamp": now,
        "report_age_seconds": -1,
        "integration_evidence_pass": False,
        "approved_candidate_id": "",
        "approved_horizon_seconds": 0,
        "materialized_signals": 0,
        "failures": ["bridge_incomplete"],
        "paper_only": True,
        "authenticated_execution": False,
    }
    external.atomic_write(status_path, json.dumps(initializing, indent=2, sort_keys=True) + "\n")
    if row.get("enabled") is not True:
        initializing["failures"] = ["external_disabled"]
        external.atomic_write(status_path, json.dumps(initializing, indent=2, sort_keys=True) + "\n")
        return initializing
    try:
        report = json.loads(external.fetch_text(external.DEFAULT_REPORT))
        if not isinstance(report, dict):
            raise ValueError("external report is not an object")
        signals = external.fetch_text(external.DEFAULT_SIGNALS)
        payload, status = external.materialize(
            report,
            signals,
            now=now,
            max_age_seconds=max(1, int(row.get("maximum_age_seconds") or 7200)),
            min_confidence=max(0.0, min(1.0, float(row.get("minimum_confidence") or 0.35))),
        )
    except Exception as exc:
        status = dict(initializing)
        status["failures"] = [f"bridge_io:{type(exc).__name__}:{exc}"]
        external.atomic_write(status_path, json.dumps(status, indent=2, sort_keys=True) + "\n")
        return status
    external.atomic_write(output, payload)
    external.atomic_write(status_path, json.dumps(status, indent=2, sort_keys=True) + "\n")
    return status


def write_economics(ledger_path: Path, run_root: Path, model_sha: str) -> dict[str, Any]:
    report = economics.assess(ledger_path, expected_model_sha=model_sha, family=maker.STRATEGY)
    atomic_json(run_root / "canonical_economics_maker.json", report)
    return report


def runtime_status(
    *,
    model_sha: str,
    recorder: subprocess.Popen[str],
    cycles: int,
    maker_report: dict[str, Any] | None,
    external_status: dict[str, Any] | None,
    economics_report: dict[str, Any] | None,
    last_error: str | None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "timestamp": int(time.time()),
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "single_ledger_writer": True,
        "executable_sleeves": [maker.STRATEGY],
        "candidate_only_sleeves": ["local_factor", "pca", "ranking", "external"],
        "cycles": cycles,
        "trade_recorder_pid": recorder.pid,
        "trade_recorder_alive": recorder.poll() is None,
        "maker": maker_report,
        "external": external_status,
        "economics": economics_report,
        "last_error": last_error,
    }


def run(config_path: Path, run_root: Path) -> int:
    cfg = load_config(config_path)
    model_sha = repository_sha()
    run_root.mkdir(parents=True, exist_ok=True)
    maker_cfg_path = rooted(str(cfg["maker"]["config"]))
    maker_cfg = maker.load_config(maker_cfg_path)
    tape_path = run_root / "trade_tape.csv"
    ledger_path = ledger.canonical_ledger_path(run_root)
    recorder = start_trade_recorder(cfg, run_root)
    cycle_interval = max(0.1, float(cfg.get("cycle_interval_seconds") or 2.0))
    external_interval = max(60.0, float(cfg.get("external_refresh_seconds") or 300.0))
    economics_interval = max(5.0, float(cfg.get("economics_interval_seconds") or 30.0))
    last_external = -math.inf if False else 0.0
    last_economics = 0.0
    external_status: dict[str, Any] | None = None
    economics_report: dict[str, Any] | None = None
    maker_report: dict[str, Any] | None = None
    cycles = 0
    last_error: str | None = None

    try:
        with ledger.CanonicalLedgerWriter(ledger_path, writer_id="v7-profit-stack", model_sha=model_sha) as writer:
            while not _STOP:
                if recorder.poll() is not None:
                    raise RuntimeContractError(f"trade recorder exited unexpectedly rc={recorder.returncode}")
                started = time.monotonic()
                try:
                    if time.monotonic() - last_external >= external_interval or external_status is None:
                        external_status = refresh_external(cfg, run_root)
                        last_external = time.monotonic()
                    maker_report = maker.run_cycle(
                        maker_cfg,
                        run_dir=run_root / "maker",
                        trade_tape=tape_path,
                        model_sha=model_sha,
                        writer=writer,
                    )
                    cycles += 1
                    if time.monotonic() - last_economics >= economics_interval:
                        economics_report = write_economics(ledger_path, run_root, model_sha)
                        last_economics = time.monotonic()
                    last_error = None
                except Exception as exc:
                    last_error = f"{type(exc).__name__}:{exc}"
                atomic_json(
                    run_root / "runtime_status.json",
                    runtime_status(
                        model_sha=model_sha,
                        recorder=recorder,
                        cycles=cycles,
                        maker_report=maker_report,
                        external_status=external_status,
                        economics_report=economics_report,
                        last_error=last_error,
                    ),
                )
                delay = cycle_interval - (time.monotonic() - started)
                if delay > 0 and not _STOP:
                    time.sleep(delay)
    finally:
        if recorder.poll() is None:
            recorder.terminate()
            try:
                recorder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                recorder.kill()
                recorder.wait(timeout=5)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("run_root", type=Path)
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return run(args.config, args.run_root)


if __name__ == "__main__":
    raise SystemExit(main())
