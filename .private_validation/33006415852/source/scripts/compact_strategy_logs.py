#!/usr/bin/env python3
"""Bound V5 paper-model diagnostic logs without touching durable fills or risk state."""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Sequence


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _enabled_strategies(config: dict[str, Any]) -> list[tuple[str, str]]:
    multi = config.get("multi_strategy")
    if not isinstance(multi, dict) or multi.get("paper_only") is not True:
        raise ValueError("multi_strategy.paper_only must be true")
    strategies = multi.get("strategies")
    if not isinstance(strategies, list):
        raise ValueError("multi_strategy.strategies must be a list")
    result: list[tuple[str, str]] = []
    for raw in strategies:
        if not isinstance(raw, dict) or not bool(raw.get("enabled", True)):
            continue
        name = str(raw.get("name", ""))
        expert = str(raw.get("expert", ""))
        if not name or not expert:
            raise ValueError("enabled strategy requires name and expert")
        result.append((name, expert))
    if not result:
        raise ValueError("no enabled V5 strategies")
    return result


def _retention(config: dict[str, Any]) -> tuple[int, int, int]:
    multi = config.get("multi_strategy")
    raw = multi.get("log_retention") if isinstance(multi, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("multi_strategy.log_retention is required")

    def positive_int(key: str) -> int:
        try:
            value = int(raw[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid log retention field: {key}") from exc
        if value <= 0:
            raise ValueError(f"log retention field must be positive: {key}")
        return value

    return (
        positive_int("signal_rows_per_model"),
        positive_int("arbitrage_rows_per_model"),
        positive_int("pca_history_rows_per_market"),
    )


def _engine_pids(run_root: Path, strategy_names: Iterable[str]) -> list[int]:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    markers = {
        name: str((run_root / "generated_configs" / f"{name}.json").resolve())
        for name in strategy_names
    }
    pids: set[int] = set()
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pieces = stripped.split(maxsplit=1)
        if len(pieces) != 2:
            continue
        try:
            pid = int(pieces[0])
        except ValueError:
            continue
        command = pieces[1]
        if "polymarket_engine" not in command:
            continue
        if any(marker in command for marker in markers.values()):
            pids.add(pid)
    return sorted(pids)


def _pause(pids: Sequence[int]) -> list[int]:
    paused: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGSTOP)
        except (ProcessLookupError, PermissionError):
            continue
        paused.append(pid)
    if paused:
        time.sleep(0.05)
    return paused


def _resume(pids: Sequence[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            pass


def _line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _atomic_replace_lines(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".compact.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        for line in lines:
            handle.write(line.rstrip("\n") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, path.stat().st_mode)
    except OSError:
        pass
    os.replace(temporary, path)


def _compact_tail(path: Path, max_rows: int) -> dict[str, int]:
    if not path.exists():
        return {"rows_before": 0, "rows_after": 0, "bytes_before": 0, "bytes_after": 0}
    bytes_before = path.stat().st_size
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        header = handle.readline()
        tail: deque[str] = deque(handle, maxlen=max_rows)
    rows_before = max(0, _line_count(path) - (1 if header else 0))
    rows_after = len(tail)
    if rows_before > max_rows:
        _atomic_replace_lines(path, [header, *tail] if header else list(tail))
    bytes_after = path.stat().st_size
    return {
        "rows_before": rows_before,
        "rows_after": rows_after,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


def _compact_history(path: Path, *, keep_per_market: int, retain: bool) -> dict[str, int]:
    if not path.exists():
        return {"rows_before": 0, "rows_after": 0, "bytes_before": 0, "bytes_after": 0}
    bytes_before = path.stat().st_size
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows_before = 0
        kept: dict[str, deque[list[str]]] = defaultdict(lambda: deque(maxlen=keep_per_market))
        for row in reader:
            if not row:
                continue
            rows_before += 1
            if retain and len(row) >= 3:
                kept[row[1]].append(row)

    output_rows: list[list[str]] = []
    if retain:
        for market_id in sorted(kept):
            output_rows.extend(kept[market_id])
    rows_after = len(output_rows)
    if rows_after != rows_before:
        temporary = path.with_suffix(path.suffix + ".compact.tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if header:
                writer.writerow(header)
            writer.writerows(output_rows)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, path.stat().st_mode)
        except OSError:
            pass
        os.replace(temporary, path)
    bytes_after = path.stat().st_size
    return {
        "rows_before": rows_before,
        "rows_after": rows_after,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


def compact(config_path: Path, run_root: Path, *, pause_processes: bool = True) -> dict[str, Any]:
    config = _read_json(config_path)
    strategies = _enabled_strategies(config)
    signal_rows, arbitrage_rows, history_rows = _retention(config)
    run_root.mkdir(parents=True, exist_ok=True)
    lock_path = run_root / ".log_compaction.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"timestamp": int(time.time()), "success": True, "skipped": "lock_busy"}

        pids = _engine_pids(run_root, (name for name, _ in strategies)) if pause_processes else []
        paused = _pause(pids) if pause_processes else []
        started = time.time()
        status: dict[str, Any] = {
            "timestamp": int(started),
            "success": False,
            "paused_pids": paused,
            "strategies": {},
        }
        try:
            total_before = 0
            total_after = 0
            for name, expert in strategies:
                directory = run_root / "strategies" / name
                model_status = {
                    "expert": expert,
                    "signals": _compact_tail(directory / "signals.csv", signal_rows),
                    "arbitrage": _compact_tail(directory / "arbitrage.csv", arbitrage_rows),
                    "history": _compact_history(
                        directory / "history.csv",
                        keep_per_market=history_rows,
                        retain=expert == "pca",
                    ),
                }
                status["strategies"][name] = model_status
                for file_status in model_status.values():
                    if not isinstance(file_status, dict):
                        continue
                    total_before += int(file_status.get("bytes_before", 0))
                    total_after += int(file_status.get("bytes_after", 0))
            status.update(
                {
                    "success": True,
                    "duration_seconds": time.time() - started,
                    "bytes_before": total_before,
                    "bytes_after": total_after,
                    "bytes_reclaimed": max(0, total_before - total_after),
                }
            )
        except Exception as exc:
            status["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _resume(paused)
            status["completed_timestamp"] = int(time.time())
            _atomic_json(run_root / "compaction_status.json", status)
        return status


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/paper_v5.json"))
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v5_live"))
    parser.add_argument("--no-pause", action="store_true", help="Only for offline tests or a stopped runtime")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = compact(args.config, args.run_root, pause_processes=not args.no_pause)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
