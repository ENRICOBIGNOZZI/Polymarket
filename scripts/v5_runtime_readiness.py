#!/usr/bin/env python3
"""Validate V5 paper-runtime readiness without confusing long model ticks with dead processes."""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from pathlib import Path
from typing import Any

EXPECTED_MODELS = {"micro", "pca", "graph", "semantic", "external"}
START_EVENTS = {"start", "restart"}


class ReadinessError(RuntimeError):
    pass


def _csv_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError as exc:
        raise ReadinessError(f"cannot read {path}: {exc}") from exc


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReadinessError(f"{path} must contain a JSON object")
    return value


def _number(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ReadinessError(f"invalid numeric {label}: {value!r}") from exc
    if not math.isfinite(result):
        raise ReadinessError(f"non-finite {label}: {value!r}")
    return result


def evaluate(
    run_root: Path,
    *,
    now: float | None = None,
    supervisor_max_age: float = 60.0,
    allocator_max_age: float = 30.0,
    model_output_max_age: float = 120.0,
    startup_grace: float = 600.0,
) -> dict[str, object]:
    now = time.time() if now is None else float(now)
    supervisor_rows = _csv_rows(run_root / "runtime_supervisor.csv")
    if not supervisor_rows:
        raise ReadinessError("runtime supervisor has no rows")
    supervisor = supervisor_rows[-1]
    if supervisor.get("recorder_alive") != "1":
        raise ReadinessError("trade recorder is not alive")
    if supervisor.get("broker_alive") != "1":
        raise ReadinessError("multi-leg broker is not alive")
    if supervisor.get("allocator_alive") != "1":
        raise ReadinessError("allocator is not alive")
    supervisor_age = max(0.0, now - _number(supervisor.get("timestamp"), "supervisor timestamp"))
    if supervisor_age > supervisor_max_age:
        raise ReadinessError(f"runtime supervisor heartbeat is stale: {supervisor_age:.1f}s")

    allocator = _json_object(run_root / "allocator_status.json")
    strategy_rows = _csv_rows(run_root / "strategy_status.csv")
    if allocator.get("paper_only") is not True:
        raise ReadinessError("allocator is not paper-only")
    if int(_number(allocator.get("models_expected", 0), "models_expected")) != 5:
        raise ReadinessError("allocator does not expect all five models")
    if int(_number(allocator.get("models_alive", 0), "models_alive")) != 5:
        raise ReadinessError("not all five model processes are alive")
    allocator_age = max(0.0, now - _number(allocator.get("timestamp"), "allocator timestamp"))
    if allocator_age > allocator_max_age:
        raise ReadinessError(f"allocator heartbeat is stale: {allocator_age:.1f}s")

    names = {row.get("name", "") for row in strategy_rows}
    if names != EXPECTED_MODELS:
        raise ReadinessError(f"unexpected strategy set: {sorted(names)!r}")
    if any(row.get("alive") != "1" for row in strategy_rows):
        raise ReadinessError("strategy_status reports a dead model process")
    total = _number(allocator.get("reserve_fraction", 0.0), "reserve_fraction") + sum(
        _number(row.get("capital_fraction"), f"{row.get('name')} capital_fraction") for row in strategy_rows
    )
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ReadinessError(f"capital fractions plus reserve do not sum to one: {total}")

    latest_start: dict[str, float] = {}
    events_path = run_root / "allocator_events.csv"
    if events_path.exists():
        for event in _csv_rows(events_path):
            name = event.get("strategy", "")
            if name not in EXPECTED_MODELS or event.get("event") not in START_EVENTS:
                continue
            ts = _number(event.get("timestamp"), f"{name} start timestamp")
            latest_start[name] = max(ts, latest_start.get(name, float("-inf")))

    fresh_models: list[str] = []
    startup_models: list[str] = []
    for row in strategy_rows:
        name = row["name"]
        output_age = max(0.0, _number(row.get("status_age_seconds"), f"{name} status_age_seconds"))
        if output_age <= model_output_max_age:
            fresh_models.append(name)
            continue
        started = latest_start.get(name)
        if started is None:
            raise ReadinessError(f"{name} output is stale ({output_age:.1f}s) and no current start event exists")
        start_age = now - started
        if start_age < -5.0 or start_age > startup_grace:
            raise ReadinessError(
                f"{name} output is stale ({output_age:.1f}s) beyond startup grace; "
                f"latest start age={start_age:.1f}s"
            )
        startup_models.append(name)

    return {
        "ready": True,
        "supervisor_age_seconds": supervisor_age,
        "allocator_age_seconds": allocator_age,
        "fresh_models": sorted(fresh_models),
        "startup_grace_models": sorted(startup_models),
        "models_alive": 5,
        "paper_only": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--supervisor-max-age", type=float, default=60.0)
    parser.add_argument("--allocator-max-age", type=float, default=30.0)
    parser.add_argument("--model-output-max-age", type=float, default=120.0)
    parser.add_argument("--startup-grace", type=float, default=600.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = evaluate(
            args.run_root,
            supervisor_max_age=args.supervisor_max_age,
            allocator_max_age=args.allocator_max_age,
            model_output_max_age=args.model_output_max_age,
            startup_grace=args.startup_grace,
        )
    except ReadinessError as exc:
        print(f"not-ready: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())