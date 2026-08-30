#!/usr/bin/env python3
"""Rotate the PAPER Maker process cohort only through a proven flat handoff.

The C++ maker and its two evidence observers consume one immutable selection per
process generation. This supervisor follows the continuously refreshed
candidate selection without ever discarding hypothetical inventory or live
PAPER orders: it freezes new risk, waits for a fresh flat state, atomically
promotes the candidate, and restarts all three consumers as one cohort.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any


ALLOWED_SOURCES = {
    "public_clob_rewards",
    "adaptive_universe_fallback",
    "adaptive_universe_recent_flow",
}
# The owning shell grants its children a five-second bounded shutdown window.
# Finish the nested cohort before that deadline so it can never orphan a WS
# process group during a whole-runtime cutover.
TERMINATION_GRACE_SECONDS = 3.0


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def validate_selection(value: dict[str, Any], model_sha: str) -> list[dict[str, Any]]:
    markets = value.get("markets")
    capacity = int(value.get("resource_capacity_markets") or 0)
    if (
        value.get("schema") != "polymarket_v7_maker_reward_selection_v1"
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("model_sha") != model_sha
        or value.get("source") not in ALLOWED_SOURCES
        or not isinstance(markets, list)
        or not markets
        or int(value.get("selected_count") or 0) != len(markets)
        or capacity <= 0
        or len(markets) > capacity
    ):
        raise ValueError("maker_rotation_selection_invalid")
    keys = ("condition_id", "market_id", "yes_token", "no_token")
    for row in markets:
        if (
            not isinstance(row, dict)
            or not all(str(row.get(key) or "") for key in keys)
            or str(row.get("yes_token")) == str(row.get("no_token"))
        ):
            raise ValueError("maker_rotation_market_invalid")
    return markets


def membership_sha256(value: dict[str, Any]) -> str:
    rows = validate_selection(value, str(value.get("model_sha") or ""))
    membership = sorted(
        (str(row["condition_id"]), str(row["yes_token"]), str(row["no_token"]))
        for row in rows
    )
    encoded = json.dumps(membership, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def safe_membership_sha256(value: dict[str, Any]) -> str:
    try:
        return membership_sha256(value)
    except (KeyError, TypeError, ValueError):
        return ""


def inventory_flat_state(
    value: dict[str, Any],
    model_sha: str,
    *,
    newer_than_ms: int = 0,
) -> bool:
    if (
        value.get("schema") != "polymarket_v7_professional_maker_state_v2"
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("model_sha") != model_sha
        or int(value.get("timestamp_ms") or 0) < newer_than_ms
    ):
        return False
    inventory = value.get("inventory")
    if not isinstance(inventory, dict):
        return False
    for row in inventory.values():
        if not isinstance(row, dict):
            return False
        for key in ("yes_shares", "no_shares", "yes_cost", "no_cost"):
            try:
                if abs(float(row.get(key) or 0.0)) > 1e-9:
                    return False
            except (TypeError, ValueError):
                return False
    return True


def flat_state(
    value: dict[str, Any],
    model_sha: str,
    *,
    newer_than_ms: int = 0,
    require_frozen: bool = False,
) -> bool:
    if not inventory_flat_state(
        value, model_sha, newer_than_ms=newer_than_ms
    ):
        return False
    return (
        int(value.get("active_order_count") or 0) == 0
        and value.get("flat_restart_safe") is True
        and (not require_frozen or value.get("new_risk_frozen") is True)
    )


class CohortSupervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_root = args.run_root
        self.control = self.run_root / "control"
        self.selection = args.selection
        self.candidate = args.candidate
        self.state = self.run_root / "micro_maker" / "state.json"
        self.drain = self.control / "MAKER_ROTATION_DRAIN"
        self.status = self.run_root / "micro_maker" / "rotation_status.json"
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.logs: list[Any] = []
        self.stop_requested = False
        self.rotation_count = 0

    def write_status(self, state: str, **extra: Any) -> None:
        runtime = read_json(self.selection)
        candidate = read_json(self.candidate)
        payload: dict[str, Any] = {
            "schema": "polymarket_v7_maker_cohort_rotation_status_v1",
            "timestamp_ms": time.time_ns() // 1_000_000,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": self.args.model_sha,
            "state": state,
            "rotation_count": self.rotation_count,
            "runtime_membership_sha256": (
                safe_membership_sha256(runtime) if runtime else ""
            ),
            "candidate_membership_sha256": (
                safe_membership_sha256(candidate) if candidate else ""
            ),
            "cohort_pids": {name: process.pid for name, process in self.processes.items()},
        }
        payload.update(extra)
        atomic_json(self.status, payload)

    def command_specs(self) -> dict[str, tuple[list[str], Path, dict[str, str]]]:
        base_env = os.environ.copy()
        maker_env = dict(base_env)
        maker_env["PM_V7_WS_JSON_ARENA_MAX_BYTES"] = str(self.args.maker_arena_bytes)
        observer_env = dict(base_env)
        observer_env["PM_V7_WS_JSON_ARENA_MAX_BYTES"] = str(self.args.observer_arena_bytes)
        fillability_env = dict(base_env)
        fillability_env["PM_V7_WS_JSON_ARENA_MAX_BYTES"] = str(
            self.args.fillability_arena_bytes
        )
        return {
            "maker": ([
                str(self.args.maker_runtime),
                "--config", str(self.args.config),
                "--maker-policy", str(self.args.maker_policy),
                "--run-root", str(self.run_root),
                "--selection", str(self.selection),
                "--model", str(self.args.model),
                "--model-sha", self.args.model_sha,
            ], self.run_root / "micro_maker" / "runtime.log", maker_env),
            "markout": ([
                str(self.args.markout_observer),
                "--config", str(self.args.config),
                "--run-root", str(self.run_root),
                "--selection", str(self.selection),
                "--model-sha", self.args.model_sha,
            ], self.run_root / "micro_maker" / "markout_observer.log", observer_env),
            "fillability": ([
                str(self.args.fillability_observer),
                "--config", str(self.args.config),
                "--run-root", str(self.run_root),
                "--selection", str(self.selection),
                "--model-sha", self.args.model_sha,
            ], self.run_root / "micro_maker" / "fillability_observer.log", fillability_env),
        }

    def start_cohort(self) -> None:
        validate_selection(read_json(self.selection), self.args.model_sha)
        self.processes.clear()
        self.logs.clear()
        for name, (command, log_path, environment) in self.command_specs().items():
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("ab", buffering=0)
            self.logs.append(log)
            self.processes[name] = subprocess.Popen(
                command,
                cwd=self.args.repository_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def stop_cohort(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline and any(
            process.poll() is None for process in self.processes.values()
        ):
            time.sleep(0.05)
        for process in self.processes.values():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        self.processes.clear()
        for log in self.logs:
            log.close()
        self.logs.clear()

    def cohort_healthy(self) -> bool:
        return len(self.processes) == 3 and all(
            process.poll() is None for process in self.processes.values()
        )

    def pending_candidate(self) -> dict[str, Any] | None:
        runtime = read_json(self.selection)
        candidate = read_json(self.candidate)
        try:
            validate_selection(runtime, self.args.model_sha)
            validate_selection(candidate, self.args.model_sha)
        except ValueError:
            return None
        return candidate if membership_sha256(runtime) != membership_sha256(candidate) else None

    def rotate_if_safe(self, candidate: dict[str, Any]) -> None:
        now_ms = time.time_ns() // 1_000_000
        if not inventory_flat_state(
            read_json(self.state), self.args.model_sha,
            newer_than_ms=now_ms - 5_000,
        ):
            self.write_status("PENDING_NONFLAT")
            return
        requested_ms = time.time_ns() // 1_000_000
        atomic_json(self.drain, {
            "schema": "polymarket_v7_maker_rotation_drain_v1",
            "timestamp_ms": requested_ms,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": self.args.model_sha,
            "candidate_membership_sha256": membership_sha256(candidate),
        })
        self.write_status("DRAINING")
        deadline = time.monotonic() + self.args.drain_timeout_seconds
        while not self.stop_requested and time.monotonic() < deadline:
            if not self.cohort_healthy():
                raise RuntimeError("maker_cohort_died_during_rotation_drain")
            if flat_state(
                read_json(self.state), self.args.model_sha,
                newer_than_ms=requested_ms, require_frozen=True,
            ):
                latest = read_json(self.candidate)
                validate_selection(latest, self.args.model_sha)
                self.stop_cohort()
                # Re-read after the maker's final state write. A late PAPER fill
                # must abort promotion and restart the old cohort unchanged.
                if not flat_state(
                    read_json(self.state), self.args.model_sha, require_frozen=True
                ):
                    self.drain.unlink(missing_ok=True)
                    self.start_cohort()
                    self.write_status("PENDING_NONFLAT")
                    return
                atomic_json(self.selection, latest)
                self.rotation_count += 1
                self.drain.unlink(missing_ok=True)
                self.start_cohort()
                self.write_status("RUNNING", last_rotation_ms=time.time_ns() // 1_000_000)
                return
            time.sleep(0.1)
        self.drain.unlink(missing_ok=True)
        self.write_status("PENDING_DRAIN_TIMEOUT")

    def run(self) -> int:
        self.control.mkdir(parents=True, exist_ok=True)
        self.drain.unlink(missing_ok=True)
        try:
            self.start_cohort()
            self.write_status("RUNNING")
            while not self.stop_requested and not (self.control / "KILL").exists():
                if not self.cohort_healthy():
                    self.write_status("FAILED", reason="cohort_process_exit")
                    return 2
                candidate = self.pending_candidate()
                if candidate is not None:
                    self.rotate_if_safe(candidate)
                else:
                    self.write_status("RUNNING")
                time.sleep(self.args.poll_seconds)
            return 0
        finally:
            self.drain.unlink(missing_ok=True)
            self.stop_cohort()
            if self.stop_requested or (self.control / "KILL").exists():
                self.write_status("STOPPED")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--maker-policy", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--maker-runtime", type=Path, required=True)
    parser.add_argument("--markout-observer", type=Path, required=True)
    parser.add_argument("--fillability-observer", type=Path, required=True)
    parser.add_argument("--maker-arena-bytes", type=int, required=True)
    parser.add_argument("--observer-arena-bytes", type=int, required=True)
    parser.add_argument("--fillability-arena-bytes", type=int, required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--drain-timeout-seconds", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    supervisor = CohortSupervisor(args)

    def stop(_signum: int, _frame: Any) -> None:
        supervisor.stop_requested = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
