#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from pathlib import Path
from typing import Any


class ContractError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid_json:{path}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"json_not_object:{path}")
    return value


def finite_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError(f"invalid_number:{name}") from exc
    if not math.isfinite(number):
        raise ContractError(f"nonfinite_number:{name}")
    return number


def age_seconds(payload: dict[str, Any], path: Path, now: float) -> float:
    timestamp = payload.get("timestamp")
    if timestamp is None:
        try:
            timestamp = path.stat().st_mtime
        except OSError as exc:
            raise ContractError(f"missing_timestamp:{path}") from exc
    return max(0.0, now - finite_number(timestamp, f"timestamp:{path.name}"))


def locate_state_root(run_root: Path) -> Path:
    direct = run_root / "runtime_status.json"
    if direct.is_file():
        return run_root
    try:
        children = sorted(path for path in run_root.iterdir() if path.is_dir())
    except OSError as exc:
        raise ContractError(f"run_root_unreadable:{run_root}") from exc
    candidates = [path for path in children if (path / "runtime_status.json").is_file()]
    if len(candidates) != 1:
        raise ContractError(f"runtime_status_missing_or_ambiguous:{run_root}")
    return candidates[0]


def validate(manifest: Path, repository_root: Path, max_age: float) -> dict[str, Any]:
    now = time.time()
    champion = read_json(manifest)
    version = champion.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ContractError("invalid_champion_version")

    run_root_rel = Path(str(champion.get("run_root", "")))
    config_rel = Path(str(champion.get("config", "")))
    if run_root_rel.is_absolute() or ".." in run_root_rel.parts:
        raise ContractError("invalid_champion_run_root")
    if config_rel.is_absolute() or ".." in config_rel.parts:
        raise ContractError("invalid_champion_config")

    run_root = repository_root / run_root_rel
    config_path = repository_root / config_rel
    config = read_json(config_path)
    state_root = locate_state_root(run_root)

    runtime_path = state_root / "runtime_status.json"
    runtime = read_json(runtime_path)
    if runtime.get("version") != version:
        raise ContractError(f"runtime_version_mismatch:expected={version}:actual={runtime.get('version')}")
    if runtime.get("paper_only") is not True:
        raise ContractError("runtime_not_paper_only")
    if runtime.get("authenticated_execution") is True:
        raise ContractError("authenticated_execution_enabled")

    runtime_age = age_seconds(runtime, runtime_path, now)
    if runtime_age > max_age:
        raise ContractError(f"runtime_status_stale:{runtime_age:.3f}")

    required_runtime_numbers = (
        "starting_capital",
        "equity",
        "pnl",
        "drawdown",
        "live_units",
        "gross_exposure",
    )
    values = {key: finite_number(runtime.get(key), f"runtime.{key}") for key in required_runtime_numbers}
    if values["starting_capital"] <= 0:
        raise ContractError("starting_capital_not_positive")
    if values["drawdown"] < -1e-12:
        raise ContractError("negative_drawdown")
    max_drawdown = finite_number(config.get("max_drawdown", 0.15), "config.max_drawdown")
    if values["drawdown"] > max_drawdown + 1e-12:
        raise ContractError(
            f"drawdown_limit_breached:actual={values['drawdown']}:limit={max_drawdown}"
        )

    strategies = runtime.get("strategies")
    if not isinstance(strategies, dict) or not strategies:
        raise ContractError("runtime_strategies_missing")

    allocator_path = state_root / "allocator_status.json"
    allocator = read_json(allocator_path)
    if allocator.get("paper_only") is not True:
        raise ContractError("allocator_not_paper_only")
    if allocator.get("authenticated_execution") is True:
        raise ContractError("allocator_authenticated_execution_enabled")
    allocator_age = age_seconds(allocator, allocator_path, now)
    if allocator_age > max_age:
        raise ContractError(f"allocator_status_stale:{allocator_age:.3f}")
    expected = int(finite_number(allocator.get("models_expected"), "allocator.models_expected"))
    alive = int(finite_number(allocator.get("models_alive"), "allocator.models_alive"))
    if expected <= 0:
        raise ContractError("allocator_models_expected_not_positive")
    if alive != expected:
        raise ContractError(f"allocator_models_not_all_alive:{alive}/{expected}")

    strategy_path = state_root / "strategy_status.csv"
    try:
        with strategy_path.open(newline="", encoding="utf-8") as handle:
            rows = [dict(row) for row in csv.DictReader(handle) if row]
    except OSError as exc:
        raise ContractError(f"strategy_status_unreadable:{strategy_path}") from exc
    if len(rows) < expected:
        raise ContractError(f"strategy_status_too_few_rows:{len(rows)}/{expected}")
    for row in rows:
        name = str(row.get("name") or "unknown")
        if int(finite_number(row.get("alive"), f"strategy.{name}.alive")) != 1:
            raise ContractError(f"strategy_not_alive:{name}")
        if finite_number(row.get("status_age_seconds", 0.0), f"strategy.{name}.status_age_seconds") > max_age:
            raise ContractError(f"strategy_status_stale:{name}")

    return {
        "version": version,
        "run_root": str(run_root_rel),
        "state_root": str(state_root.relative_to(repository_root)),
        "runtime_age_seconds": runtime_age,
        "allocator_age_seconds": allocator_age,
        "models_alive": alive,
        "models_expected": expected,
        "drawdown": values["drawdown"],
        "max_drawdown": max_drawdown,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the version-neutral Polymarket PAPER runtime contract")
    parser.add_argument("--manifest", type=Path, default=Path("config/live_champion.json"))
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--max-age-seconds", type=float, default=180.0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not math.isfinite(args.max_age_seconds) or args.max_age_seconds <= 0:
        raise SystemExit("max-age-seconds must be a positive finite number")
    try:
        result = validate(args.manifest, args.repository_root, args.max_age_seconds)
    except ContractError as exc:
        print(f"runtime_contract_health=failed reason={exc}")
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            "runtime_contract_health=ok "
            + " ".join(f"{key}={value}" for key, value in result.items())
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
