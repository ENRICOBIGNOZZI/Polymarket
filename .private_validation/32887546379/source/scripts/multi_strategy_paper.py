#!/usr/bin/env python3
"""Run independent Polymarket paper books and aggregate them at capital level."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
PROTECTED_OVERRIDES = {
    "gamma_url",
    "clob_url",
    "run_dir",
    "starting_capital",
    "expert_weights",
    "external_signals_file",
    "scan_only",
}


def _now() -> int:
    return int(time.time())


def _float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _atomic_csv(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})
    os.replace(temporary, path)


def _append_csv(path: Path, fields: Sequence[str], row: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


def _count_fills(path: Path) -> tuple[int, int, int, int]:
    total = buys = sells = settles = 0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                total += 1
                action = str(row.get("action", "")).upper()
                buys += int(action == "BUY")
                sells += int(action == "SELL")
                settles += int(action == "SETTLE")
    except OSError:
        pass
    return total, buys, sells, settles


def _open_cost_basis(path: Path) -> float:
    total = 0.0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                total += max(0.0, _float(row.get("cost_basis")))
    except OSError:
        pass
    return total


@dataclass(frozen=True)
class StrategySpec:
    name: str
    expert: str
    capital_fraction: float
    enabled: bool = True
    overrides: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagerSpec:
    starting_capital: float
    reserve_fraction: float
    global_max_drawdown: float
    global_max_gross_fraction: float
    status_interval_seconds: float
    restart_backoff_seconds: float
    strategies: tuple[StrategySpec, ...]
    expert_names: tuple[str, ...]


@dataclass
class ChildRuntime:
    spec: StrategySpec
    starting_capital: float
    run_dir: Path
    config_path: Path
    process: subprocess.Popen[str] | None = None
    log_handle: Any = None
    restarts: int = 0
    restart_after: float = 0.0
    last_error: str = ""

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


def load_manager_spec(config: Mapping[str, Any]) -> ManagerSpec:
    starting = _float(config.get("starting_capital"))
    if starting <= 0.0:
        raise ValueError("starting_capital must be positive")
    multi = config.get("multi_strategy")
    if not isinstance(multi, dict):
        raise ValueError("multi_strategy object is required")
    if _int(multi.get("schema_version"), -1) != SCHEMA_VERSION:
        raise ValueError(f"multi_strategy.schema_version must be {SCHEMA_VERSION}")
    if multi.get("paper_only") is not True:
        raise ValueError("multi_strategy.paper_only must be true")

    reserve = _float(multi.get("reserve_fraction"), -1.0)
    if not 0.0 <= reserve < 1.0:
        raise ValueError("reserve_fraction must be in [0, 1)")
    global_drawdown = _float(multi.get("global_max_drawdown"), _float(config.get("max_drawdown"), 0.15))
    if not 0.0 < global_drawdown < 1.0:
        raise ValueError("global_max_drawdown must be in (0, 1)")
    global_gross = _float(multi.get("global_max_gross_fraction"), 0.35)
    if not 0.0 < global_gross <= 1.0:
        raise ValueError("global_max_gross_fraction must be in (0, 1]")
    status_interval = _float(multi.get("status_interval_seconds"), 5.0)
    restart_backoff = _float(multi.get("restart_backoff_seconds"), 2.0)
    if status_interval <= 0.0 or restart_backoff < 0.0:
        raise ValueError("invalid manager timing parameters")

    weights = config.get("expert_weights")
    if not isinstance(weights, dict) or not weights:
        raise ValueError("expert_weights must enumerate supported experts")
    expert_names = tuple(str(key) for key in weights)
    if any(abs(_float(value)) > 1e-12 for value in weights.values()):
        raise ValueError("V5 parent expert_weights must all be zero (fail-closed parent config)")

    raw_strategies = multi.get("strategies")
    if not isinstance(raw_strategies, list) or not raw_strategies:
        raise ValueError("multi_strategy.strategies must be a non-empty list")
    strategies: list[StrategySpec] = []
    seen: set[str] = set()
    for raw in raw_strategies:
        if not isinstance(raw, dict):
            raise ValueError("every strategy must be an object")
        name = str(raw.get("name", ""))
        expert = str(raw.get("expert", ""))
        enabled = bool(raw.get("enabled", True))
        fraction = _float(raw.get("capital_fraction"), -1.0)
        overrides = raw.get("overrides", {})
        if not NAME_RE.fullmatch(name) or name in seen:
            raise ValueError(f"invalid or duplicate strategy name: {name!r}")
        seen.add(name)
        if expert not in expert_names:
            raise ValueError(f"strategy {name} references unknown expert {expert!r}")
        if enabled and fraction <= 0.0:
            raise ValueError(f"enabled strategy {name} needs positive capital_fraction")
        if not isinstance(overrides, dict):
            raise ValueError(f"strategy {name} overrides must be an object")
        protected = sorted(PROTECTED_OVERRIDES.intersection(overrides))
        if protected:
            raise ValueError(f"strategy {name} overrides protected keys: {', '.join(protected)}")
        child_drawdown = _float(overrides.get("max_drawdown"), global_drawdown)
        if not 0.0 < child_drawdown <= global_drawdown + 1e-12:
            raise ValueError(f"strategy {name} max_drawdown exceeds the global limit")
        child_gross = _float(overrides.get("max_gross_fraction"), _float(config.get("max_gross_fraction"), 0.25))
        if not 0.0 < child_gross <= 1.0:
            raise ValueError(f"strategy {name} max_gross_fraction must be in (0, 1]")
        strategies.append(StrategySpec(name, expert, fraction, enabled, dict(overrides)))

    enabled = [item for item in strategies if item.enabled]
    allocated = sum(item.capital_fraction for item in enabled)
    if abs(allocated + reserve - 1.0) > 1e-9:
        raise ValueError("enabled capital fractions plus reserve_fraction must equal one")
    weighted_gross = sum(
        item.capital_fraction
        * _float(item.overrides.get("max_gross_fraction"), _float(config.get("max_gross_fraction"), 0.25))
        for item in enabled
    )
    if weighted_gross > global_gross + 1e-12:
        raise ValueError("weighted child gross caps exceed global_max_gross_fraction")

    return ManagerSpec(
        starting,
        reserve,
        global_drawdown,
        global_gross,
        status_interval,
        restart_backoff,
        tuple(strategies),
        expert_names,
    )


def build_child_config(
    base: Mapping[str, Any], manager: ManagerSpec, strategy: StrategySpec, run_dir: Path, *, scan_only: bool = False
) -> dict[str, Any]:
    child = {key: value for key, value in base.items() if key != "multi_strategy"}
    child.update(dict(strategy.overrides))
    child["starting_capital"] = manager.starting_capital * strategy.capital_fraction
    child["run_dir"] = str(run_dir)
    child["scan_only"] = bool(scan_only)
    child["max_drawdown"] = min(_float(child.get("max_drawdown"), manager.global_max_drawdown), manager.global_max_drawdown)
    child["expert_weights"] = {
        name: (1.0 if name == strategy.expert else 0.0) for name in manager.expert_names
    }
    return child


def _read_oos(path: Path) -> dict[str, object]:
    report = _read_json(path) or {}
    oos = report.get("oos") if isinstance(report.get("oos"), dict) else {}
    stress = report.get("oos_cost_stress") if isinstance(report.get("oos_cost_stress"), dict) else {}
    return {
        "trades": _int(oos.get("trades")),
        "net_pnl": _float(oos.get("net_pnl")),
        "stressed_net_pnl": _float(stress.get("net_pnl")),
        "max_drawdown": _float(oos.get("max_drawdown")),
        "bootstrap_pvalue": _float(report.get("bootstrap_one_sided_pvalue"), 1.0),
        "eligible_for_tiny_pilot": bool(report.get("eligible_for_tiny_pilot", False)),
        "production_threshold": _float(report.get("production_threshold")),
    }


class MultiStrategyManager:
    STATUS_FIELDS = (
        "timestamp", "name", "expert", "capital_fraction", "starting_capital", "cash", "equity", "pnl",
        "realized_pnl", "peak_equity", "drawdown", "gross_exposure", "open_positions", "killed", "alive",
        "status_age_seconds", "restarts", "fills", "buy_fills", "sell_fills", "settle_fills", "last_error",
    )
    EVENT_FIELDS = ("timestamp", "strategy", "expert", "event", "restart_count", "detail")

    def __init__(
        self,
        config_path: Path,
        run_root: Path,
        engine_path: Path,
        *,
        markets: int | None = None,
        min_liquidity: float | None = None,
        selected: set[str] | None = None,
        scan_only: bool = False,
    ) -> None:
        raw = _read_json(config_path)
        if raw is None:
            raise ValueError(f"cannot read JSON config: {config_path}")
        self.base_config = raw
        self.spec = load_manager_spec(raw)
        self.run_root = run_root
        self.engine_path = engine_path
        self.markets = markets
        self.min_liquidity = min_liquidity
        self.scan_only = scan_only
        known = {item.name for item in self.spec.strategies if item.enabled}
        if selected is not None and selected.difference(known):
            raise ValueError("unknown or disabled strategies: " + ", ".join(sorted(selected.difference(known))))
        self.selected = selected
        self.stop_requested = False
        self.state_path = run_root / "allocator_state.json"
        state = _read_json(self.state_path) or {}
        self.peak_equity = max(self.spec.starting_capital, _float(state.get("peak_equity"), self.spec.starting_capital))
        self.killed = bool(state.get("killed", False))
        self.children: dict[str, ChildRuntime] = {}
        self._prepare_children()

    def _prepare_children(self) -> None:
        generated = self.run_root / "generated_configs"
        generated.mkdir(parents=True, exist_ok=True)
        for strategy in self.spec.strategies:
            if not strategy.enabled or (self.selected is not None and strategy.name not in self.selected):
                continue
            run_dir = self.run_root / "strategies" / strategy.name
            run_dir.mkdir(parents=True, exist_ok=True)
            config_path = generated / f"{strategy.name}.json"
            _atomic_json(config_path, build_child_config(self.base_config, self.spec, strategy, run_dir, scan_only=self.scan_only))
            self.children[strategy.name] = ChildRuntime(
                strategy,
                self.spec.starting_capital * strategy.capital_fraction,
                run_dir,
                config_path,
            )

    def validate(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "paper_only": True,
            "starting_capital": self.spec.starting_capital,
            "reserve_fraction": self.spec.reserve_fraction,
            "global_max_drawdown": self.spec.global_max_drawdown,
            "global_max_gross_fraction": self.spec.global_max_gross_fraction,
            "strategies": [
                {
                    "name": child.spec.name,
                    "expert": child.spec.expert,
                    "capital_fraction": child.spec.capital_fraction,
                    "starting_capital": child.starting_capital,
                    "config": str(child.config_path),
                    "run_dir": str(child.run_dir),
                }
                for child in self.children.values()
            ],
        }

    def _command(self, child: ChildRuntime, *, once: bool = False) -> list[str]:
        command = [str(self.engine_path), "--config", str(child.config_path), "--once" if once else "--loop"]
        command.append("--scan-only" if self.scan_only else "--paper")
        command += ["--run-dir", str(child.run_dir)]
        if self.markets is not None:
            command += ["--markets", str(self.markets)]
        if self.min_liquidity is not None:
            command += ["--min-liquidity", str(self.min_liquidity)]
        return command

    def _event(self, child: ChildRuntime, event: str, detail: str = "") -> None:
        _append_csv(
            self.run_root / "allocator_events.csv",
            self.EVENT_FIELDS,
            {
                "timestamp": _now(), "strategy": child.spec.name, "expert": child.spec.expert,
                "event": event, "restart_count": child.restarts, "detail": detail,
            },
        )

    def _start(self, child: ChildRuntime) -> None:
        if self.killed or self.stop_requested or child.alive():
            return
        child.log_handle = (child.run_dir / "engine.log").open("a", encoding="utf-8", buffering=1)
        try:
            child.process = subprocess.Popen(
                self._command(child),
                cwd=str(Path.cwd()),
                stdout=child.log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            child.last_error = ""
            self._event(child, "start" if child.restarts == 0 else "restart")
        except OSError as exc:
            child.last_error = str(exc)
            child.restart_after = time.time() + self.spec.restart_backoff_seconds
            child.log_handle.close()
            child.log_handle = None
            self._event(child, "start_failed", child.last_error)

    def _stop(self, child: ChildRuntime, event: str) -> None:
        had_runtime = child.process is not None or child.log_handle is not None
        if child.process is not None and child.process.poll() is None:
            try:
                os.killpg(child.process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                child.process.terminate()
            try:
                child.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    child.process.kill()
                child.process.wait(timeout=5)
        child.process = None
        if child.log_handle is not None:
            child.log_handle.close()
            child.log_handle = None
        if had_runtime:
            self._event(child, event)

    def stop_all(self, event: str) -> None:
        for child in self.children.values():
            self._stop(child, event)

    def _row(self, child: ChildRuntime, now: int) -> dict[str, object]:
        status_path = child.run_dir / "status.json"
        status = _read_json(status_path) or {}
        try:
            age = max(0.0, now - status_path.stat().st_mtime)
        except OSError:
            age = 1e12
        equity = _float(status.get("equity"), child.starting_capital)
        cash = _float(status.get("cash"), child.starting_capital)
        peak = max(child.starting_capital, _float(status.get("peak_equity"), child.starting_capital))
        gross = max(0.0, _float(status.get("gross_exposure")))
        positions = max(0, _int(status.get("open_positions")))
        drawdown = max(0.0, _float(status.get("drawdown"), 1.0 - equity / peak if peak > 0 else 0.0))
        fills, buys, sells, settles = _count_fills(child.run_dir / "fills.csv")
        realized = cash - child.starting_capital + _open_cost_basis(child.run_dir / "broker_state.csv")
        return {
            "timestamp": now, "name": child.spec.name, "expert": child.spec.expert,
            "capital_fraction": child.spec.capital_fraction, "starting_capital": child.starting_capital,
            "cash": cash, "equity": equity, "pnl": equity - child.starting_capital,
            "realized_pnl": realized, "peak_equity": peak, "drawdown": drawdown,
            "gross_exposure": gross, "open_positions": positions,
            "killed": int(bool(status.get("killed", False))), "alive": int(child.alive()),
            "status_age_seconds": age, "restarts": child.restarts, "fills": fills,
            "buy_fills": buys, "sell_fills": sells, "settle_fills": settles,
            "last_error": child.last_error,
        }

    def publish(self) -> dict[str, object]:
        now = _now()
        rows = [self._row(child, now) for child in self.children.values()]
        selected_fraction = sum(child.spec.capital_fraction for child in self.children.values())
        inactive_fraction = max(0.0, 1.0 - self.spec.reserve_fraction - selected_fraction)
        reserve = self.spec.starting_capital * (self.spec.reserve_fraction + inactive_fraction)
        equity = reserve + sum(_float(row["equity"]) for row in rows)
        cash = reserve + sum(_float(row["cash"]) for row in rows)
        gross = sum(_float(row["gross_exposure"]) for row in rows)
        positions = sum(_int(row["open_positions"]) for row in rows)
        realized = sum(_float(row["realized_pnl"]) for row in rows)
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = max(0.0, 1.0 - equity / self.peak_equity) if self.peak_equity > 0.0 else 0.0
        if drawdown + 1e-12 >= self.spec.global_max_drawdown:
            self.killed = True
        canonical = {
            "schema_version": 1,
            "timestamp": now,
            "mode": "paper-multi-strategy-v5",
            "paper_only": True,
            "equity": equity,
            "pnl": equity - self.spec.starting_capital,
            "cash": cash,
            "peak_equity": self.peak_equity,
            "drawdown": drawdown,
            "killed": self.killed,
            "live_units": positions,
            "reserved_cash": reserve,
            "gross_exposure": gross,
            "realized_pnl": realized,
            "execution_imbalance": 0.0,
            "execution_staleness": max((_float(row["status_age_seconds"], 1e12) for row in rows), default=0.0),
            "models_alive": sum(_int(row["alive"]) for row in rows),
            "models_expected": len(rows),
            "oos": _read_oos(self.run_root / "walk_forward.json"),
        }
        allocator = {
            **canonical,
            "starting_capital": self.spec.starting_capital,
            "reserve_fraction": self.spec.reserve_fraction + inactive_fraction,
            "global_max_drawdown": self.spec.global_max_drawdown,
            "global_max_gross_fraction": self.spec.global_max_gross_fraction,
            "global_gross_fraction": gross / self.spec.starting_capital,
            "strategies": rows,
        }
        _atomic_csv(self.run_root / "strategy_status.csv", self.STATUS_FIELDS, rows)
        _atomic_json(self.run_root / "runtime_status.json", canonical)
        _atomic_json(self.run_root / "allocator_status.json", allocator)
        _atomic_json(self.state_path, {"timestamp": now, "peak_equity": self.peak_equity, "killed": self.killed})
        return allocator

    def run_once(self) -> int:
        if not self.engine_path.exists():
            raise FileNotFoundError(f"engine binary not found: {self.engine_path}")
        failures = 0
        for child in self.children.values():
            with (child.run_dir / "engine.log").open("a", encoding="utf-8") as log:
                result = subprocess.run(
                    self._command(child, once=True), cwd=str(Path.cwd()), stdout=log,
                    stderr=subprocess.STDOUT, text=True, check=False,
                )
            if result.returncode:
                failures += 1
                child.last_error = f"once_rc={result.returncode}"
                self._event(child, "once_failed", child.last_error)
            else:
                self._event(child, "once_complete")
        self.publish()
        return int(failures > 0)

    def run_forever(self) -> int:
        if not self.engine_path.exists():
            raise FileNotFoundError(f"engine binary not found: {self.engine_path}")

        def request_stop(signum: int, _frame: object) -> None:
            self.stop_requested = True
            _append_csv(
                self.run_root / "allocator_events.csv", self.EVENT_FIELDS,
                {"timestamp": _now(), "strategy": "allocator", "expert": "", "event": "signal", "restart_count": 0, "detail": signum},
            )

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        global_kill_applied = False
        try:
            self.publish()
            while not self.stop_requested:
                if self.killed:
                    if not global_kill_applied:
                        self.stop_all("global_kill")
                        global_kill_applied = True
                else:
                    for child in self.children.values():
                        if child.alive():
                            continue
                        if child.process is not None:
                            child.last_error = f"exit_rc={child.process.poll()}"
                            child.process = None
                            if child.log_handle is not None:
                                child.log_handle.close()
                                child.log_handle = None
                            child.restarts += 1
                            child.restart_after = time.time() + self.spec.restart_backoff_seconds
                            self._event(child, "exit", child.last_error)
                        if time.time() >= child.restart_after:
                            self._start(child)
                status = self.publish()
                if bool(status.get("killed")) and not global_kill_applied:
                    self.stop_all("global_kill")
                    global_kill_applied = True
                time.sleep(self.spec.status_interval_seconds)
        finally:
            self.stop_all("shutdown")
            self.publish()
        return 0


def _selection(value: str | None) -> set[str] | None:
    if value is None or not value.strip():
        return None
    return {item.strip() for item in value.split(",") if item.strip()}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/paper_v5.json"))
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v5_live"))
    parser.add_argument("--engine", type=Path, default=Path("build/polymarket_engine"))
    parser.add_argument("--markets", type=int, default=None)
    parser.add_argument("--min-liquidity", type=float, default=None)
    parser.add_argument("--strategy", default=None, help="Optional comma-separated strategy names")
    parser.add_argument("--scan-only", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--validate-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manager = MultiStrategyManager(
            args.config,
            args.run_root,
            args.engine,
            markets=args.markets,
            min_liquidity=args.min_liquidity,
            selected=_selection(args.strategy),
            scan_only=args.scan_only,
        )
        manifest = manager.validate()
        _atomic_json(args.run_root / "allocator_manifest.json", manifest)
        print(json.dumps(manifest, sort_keys=True), flush=True)
        if args.validate_only:
            manager.publish()
            return 0
        return manager.run_once() if args.once else manager.run_forever()
    except (OSError, ValueError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
