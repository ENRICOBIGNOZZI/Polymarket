#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass
class EngineSnapshot:
    name: str
    available: bool
    healthy: bool
    fresh: bool
    killed: bool
    equity: float
    pnl: float
    timestamp: int
    reason: str


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def number(value: Any, fallback: float = 0.0) -> float:
    try:
        if isinstance(value, bool):
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def integer(value: Any, fallback: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return fallback
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def nested_number(data: dict[str, Any], keys: Iterable[str], fallback: float) -> float:
    for key in keys:
        if key in data:
            result = number(data[key], float("nan"))
            if result == result:
                return result
    return fallback


def latest_csv_row(path: Path) -> dict[str, str] | None:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = csv.DictReader(handle)
            latest: dict[str, str] | None = None
            for row in rows:
                latest = dict(row)
            return latest
    except OSError:
        return None


def alpha_fallback_status(run_root: Path, stale_after: int, baseline: float) -> EngineSnapshot:
    now = int(time.time())
    supervisor = latest_csv_row(run_root / "runtime_supervisor.csv")
    equity_row = latest_csv_row(run_root / "multileg_equity.csv")
    available = supervisor is not None or equity_row is not None
    timestamp = 0
    healthy = False
    killed = False
    equity = baseline
    reasons: list[str] = []
    if supervisor is not None:
        timestamp = integer(supervisor.get("timestamp"), 0)
        recorder_alive = integer(supervisor.get("recorder_alive"), 0) == 1
        broker_alive = integer(supervisor.get("broker_alive"), 0) == 1
        healthy = recorder_alive and broker_alive
        if not recorder_alive:
            reasons.append("recorder_down")
        if not broker_alive:
            reasons.append("broker_down")
    if equity_row is not None:
        timestamp = max(timestamp, integer(equity_row.get("timestamp") or equity_row.get("ts"), 0))
        for key in ("equity", "paper_equity", "current_equity", "net_liquidation"):
            if key in equity_row and equity_row[key] not in (None, ""):
                equity = number(equity_row[key], baseline)
                break
        killed = str(equity_row.get("killed", "0")).lower() in {"1", "true", "yes"}
    fresh = timestamp > 0 and now - timestamp <= stale_after
    if not fresh:
        reasons.append("status_stale_or_missing")
    if killed:
        reasons.append("engine_killed")
    return EngineSnapshot(
        name="alpha",
        available=available,
        healthy=available and healthy and fresh and not killed,
        fresh=fresh,
        killed=killed,
        equity=equity,
        pnl=equity - baseline,
        timestamp=timestamp,
        reason=";".join(reasons) or "ok",
    )


def canonical_snapshot(
    name: str,
    status_path: Path,
    stale_after: int,
    baseline: float,
    require_explicit_healthy: bool,
) -> EngineSnapshot | None:
    data = read_json(status_path)
    if data is None:
        return None
    now = int(time.time())
    try:
        file_timestamp = int(status_path.stat().st_mtime)
    except OSError:
        file_timestamp = 0
    timestamp = integer(data.get("timestamp"), file_timestamp)
    fresh = timestamp > 0 and now - timestamp <= stale_after
    killed = bool(data.get("killed", False))
    equity = nested_number(data, ("equity", "paper_equity", "current_equity"), baseline)
    pnl = nested_number(data, ("pnl", "realized_pnl"), equity - baseline)
    explicit_healthy = data.get("healthy")
    healthy_flag = bool(explicit_healthy) if explicit_healthy is not None else not require_explicit_healthy
    reasons: list[str] = []
    if not healthy_flag:
        reasons.append("reported_unhealthy")
    if not fresh:
        reasons.append("status_stale")
    if killed:
        reasons.append("engine_killed")
    error = str(data.get("error", "")).strip()
    if error:
        reasons.append(error[:500])
    return EngineSnapshot(
        name=name,
        available=True,
        healthy=healthy_flag and fresh and not killed,
        fresh=fresh,
        killed=killed,
        equity=equity,
        pnl=pnl,
        timestamp=timestamp,
        reason=";".join(reasons) or "ok",
    )


def load_config(path: Path) -> dict[str, Any]:
    data = read_json(path)
    if data is None:
        raise SystemExit(f"invalid supervisor config: {path}")
    if integer(data.get("schema_version"), 0) != 1:
        raise SystemExit("unsupported supervisor config schema")
    return data


def build_snapshot(config: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    now = int(time.time())
    starting_capital = number(config.get("starting_capital"), 10_000.0)
    max_drawdown = number(config.get("global_max_drawdown"), 0.15)
    stale_after = integer(config.get("stale_after_seconds"), 90)
    bootstrap_grace = integer(config.get("cross_bootstrap_grace_seconds"), 180)
    alpha_fraction = number(config.get("alpha_allocation_fraction"), 0.75)
    cross_fraction = number(config.get("cross_venue_allocation_fraction"), 0.25)
    if alpha_fraction < 0 or cross_fraction < 0 or alpha_fraction + cross_fraction > 1.0 + 1e-12:
        raise RuntimeError("invalid portfolio allocation fractions")

    alpha_baseline = number(config.get("alpha_engine_baseline_equity"), starting_capital)
    cross_baseline = number(
        config.get("cross_venue_engine_baseline_equity"), starting_capital * cross_fraction
    )
    alpha_status_path = Path(str(config.get("alpha_status", "runs/paper_v4_live/runtime_status.json")))
    alpha_run_root = Path(str(config.get("alpha_run_root", "runs/paper_v4_live")))
    cross_status_path = Path(str(config.get("cross_venue_status", "runs/cross_venue/runtime_status.json")))

    alpha = canonical_snapshot("alpha", alpha_status_path, stale_after, alpha_baseline, False)
    if alpha is None:
        alpha = alpha_fallback_status(alpha_run_root, stale_after, alpha_baseline)
    cross = canonical_snapshot("cross_venue", cross_status_path, stale_after, cross_baseline, True)
    if cross is None:
        cross = EngineSnapshot(
            name="cross_venue",
            available=False,
            healthy=False,
            fresh=False,
            killed=False,
            equity=cross_baseline,
            pnl=0.0,
            timestamp=0,
            reason="status_missing",
        )

    global_equity = starting_capital + alpha.pnl + cross.pnl
    previous_peak = number(state.get("peak_equity"), starting_capital)
    peak_equity = max(previous_peak, global_equity)
    drawdown = 0.0 if peak_equity <= 0 else max(0.0, 1.0 - global_equity / peak_equity)
    manual_kill_path = Path(str(config.get("manual_kill_file", "runs/supervisor/KILL")))
    previous_kill = bool(state.get("global_kill", False))
    global_kill = previous_kill or manual_kill_path.exists() or drawdown >= max_drawdown

    started_at = integer(state.get("started_at"), now)
    cross_bootstrap = not cross.available and now - started_at <= bootstrap_grace
    require_alpha_for_cross = bool(config.get("require_alpha_health_for_cross", False))

    alpha_allowed = not global_kill and alpha.healthy
    cross_health_ok = cross.healthy or cross_bootstrap
    cross_allowed = not global_kill and cross_health_ok and (alpha.healthy or not require_alpha_for_cross)

    alpha_capital = max(0.0, global_equity * alpha_fraction)
    cross_capital = max(0.0, global_equity * cross_fraction)
    cross_max_bundle = min(
        number(config.get("cross_venue_max_bundle_usd"), 25.0),
        max(0.0, cross_capital * number(config.get("cross_venue_max_bundle_fraction"), 0.02)),
    )
    if cross_max_bundle <= 0 and cross_capital > 0:
        cross_max_bundle = min(25.0, cross_capital)

    limits = {
        "schema_version": 1,
        "timestamp": now,
        "global_kill": global_kill,
        "starting_capital": starting_capital,
        "global_equity": global_equity,
        "peak_equity": peak_equity,
        "drawdown": drawdown,
        "max_drawdown": max_drawdown,
        "engines": {
            "alpha": {
                "capital_limit_usd": alpha_capital,
                "new_exposure_allowed": alpha_allowed,
                "reason": "ok" if alpha_allowed else ("global_kill" if global_kill else alpha.reason),
            },
            "cross_venue": {
                "capital_limit_usd": cross_capital,
                "max_bundle_usd": cross_max_bundle,
                "new_exposure_allowed": cross_allowed,
                "bootstrap": cross_bootstrap,
                "reason": "ok" if cross_allowed else ("global_kill" if global_kill else cross.reason),
            },
        },
    }
    status = {
        **limits,
        "healthy": not global_kill and (alpha.healthy or not bool(config.get("alpha_required", True)))
        and (cross.healthy or cross_bootstrap or not bool(config.get("cross_venue_required", True))),
        "snapshots": {
            "alpha": alpha.__dict__,
            "cross_venue": cross.__dict__,
        },
    }
    new_state = {
        "schema_version": 1,
        "started_at": started_at,
        "timestamp": now,
        "peak_equity": peak_equity,
        "global_kill": global_kill,
    }
    return {"limits": limits, "status": status}, new_state


def run_once(config_path: Path) -> int:
    config = load_config(config_path)
    state_path = Path(str(config.get("state_file", "runs/supervisor/state.json")))
    limits_path = Path(str(config.get("limits_file", "runs/supervisor/capital_limits.json")))
    status_path = Path(str(config.get("status_file", "runs/supervisor/runtime_status.json")))
    state = read_json(state_path) or {"schema_version": 1, "started_at": int(time.time())}
    result, new_state = build_snapshot(config, state)
    atomic_write_json(limits_path, result["limits"])
    atomic_write_json(status_path, result["status"])
    atomic_write_json(state_path, new_state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capital and risk supervisor for independent trading engines")
    parser.add_argument("--config", default="config/portfolio_supervisor.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config)
    if not args.loop:
        return run_once(config_path)

    config = load_config(config_path)
    interval = max(1, integer(config.get("interval_seconds"), 5))
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    while not stop:
        try:
            run_once(config_path)
        except Exception as exc:  # fail closed while keeping supervisor observable
            status_path = Path(str(config.get("status_file", "runs/supervisor/runtime_status.json")))
            limits_path = Path(str(config.get("limits_file", "runs/supervisor/capital_limits.json")))
            now = int(time.time())
            atomic_write_json(
                limits_path,
                {
                    "schema_version": 1,
                    "timestamp": now,
                    "global_kill": True,
                    "engines": {
                        "alpha": {"capital_limit_usd": 0.0, "new_exposure_allowed": False, "reason": str(exc)},
                        "cross_venue": {
                            "capital_limit_usd": 0.0,
                            "max_bundle_usd": 0.0,
                            "new_exposure_allowed": False,
                            "reason": str(exc),
                        },
                    },
                },
            )
            atomic_write_json(
                status_path,
                {"schema_version": 1, "timestamp": now, "healthy": False, "global_kill": True, "error": str(exc)},
            )
        for _ in range(interval * 10):
            if stop:
                break
            time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
