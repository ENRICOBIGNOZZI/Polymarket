#!/usr/bin/env python3
"""Bounded, exact-SHA supervisor for the one canonical V7 FULL-PAPER writer."""
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
MONITORING = ROOT / "monitoring"
if str(MONITORING) not in sys.path:
    sys.path.insert(0, str(MONITORING))

from v7_runtime_contract import (  # noqa: E402
    RECOVERABLE,
    SAFE,
    UNSAFE,
    assess_reconciliation,
    pid_alive,
    runtime_health,
)


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _git_head(root: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=5,
    ).strip()


class Supervisor:
    def __init__(self, args: argparse.Namespace, policy: dict[str, Any]) -> None:
        self.root = args.repository_root.resolve()
        self.run_root = args.run_root.resolve()
        self.expected_sha = args.expected_sha
        self.policy = policy
        service = policy["service"]
        self.command = list(args.command or service["command"])
        self.startup_grace = int(service["startup_grace_seconds"])
        self.health_interval = float(service["health_interval_seconds"])
        self.stale_seconds = int(service["runtime_stale_seconds"])
        self.termination_grace = float(service["termination_grace_seconds"])
        restart = service["restart"]
        self.restart_maximum = int(restart["maximum_attempts"])
        self.restart_window = int(restart["window_seconds"])
        self.backoff = [float(value) for value in restart["backoff_seconds"]]
        self.control = self.run_root / "control"
        self.status_path = self.control / "supervisor_status.json"
        self.restart_path = self.control / "supervisor_restarts.json"
        self.lock = self.control / "supervisor.lock"
        self.child: subprocess.Popen[bytes] | None = None
        self.stopping = False
        self.started_at = int(time.time())

    def validate_static_contract(self) -> None:
        if self.policy.get("version") != 7:
            raise RuntimeError("supervision policy is not V7")
        if self.policy.get("paper_only") is not True:
            raise RuntimeError("supervision policy is not PAPER-only")
        for key in ("authenticated_execution", "real_order_submission", "real_capital_at_risk"):
            if self.policy.get(key) is not False:
                raise RuntimeError(f"unsafe supervision policy: {key}")
        if _git_head(self.root) != self.expected_sha:
            raise RuntimeError("repository HEAD does not match exact expected SHA")
        runtime_config = _json(self.root / "config" / "paper_v7.json")
        v7 = runtime_config.get("v7") if isinstance(runtime_config.get("v7"), dict) else {}
        if runtime_config.get("engine_version") != 7 or runtime_config.get("paper_only") is not True:
            raise RuntimeError("canonical runtime config is not V7 PAPER")
        if v7.get("paper_only") is not True or v7.get("authenticated_execution") is not False or v7.get("real_order_submission") is not False:
            raise RuntimeError("canonical V7 execution authority is unsafe")
        if not self.command or self.command != list(self.policy["service"]["command"]):
            raise RuntimeError("service command must match the reviewed canonical command")

    def acquire(self) -> None:
        self.control.mkdir(parents=True, exist_ok=True)
        try:
            self.lock.mkdir()
        except FileExistsError:
            owner = _json(self.lock / "owner.json")
            if pid_alive(owner.get("pid")):
                raise RuntimeError(f"another canonical supervisor is active pid={owner.get('pid')}")
            # Only the two exact files owned by this lock protocol are removed.
            try:
                (self.lock / "owner.json").unlink()
            except FileNotFoundError:
                pass
            self.lock.rmdir()
            self.lock.mkdir()
        _atomic_json(
            self.lock / "owner.json",
            {
                "schema": "polymarket_v7_supervisor_lock_v1",
                "pid": os.getpid(),
                "expected_sha": self.expected_sha,
                "paper_only": True,
                "authenticated_execution": False,
                "started_at": self.started_at,
            },
        )

    def release(self) -> None:
        try:
            (self.lock / "owner.json").unlink()
            self.lock.rmdir()
        except (FileNotFoundError, OSError):
            pass

    def status(self, state: str, reasons: list[str] | tuple[str, ...] = ()) -> None:
        router = _json(self.run_root / "external_fair" / "paper_router_status.json")
        router_ready = bool(
            state == "running"
            and router.get("schema") == "polymarket_v7_external_fair_paper_router_v1"
            and router.get("code_sha") == self.expected_sha
            and router.get("state") == "RUNNING"
            and router.get("paper_only") is True
            and router.get("authenticated_execution") is False
            and router.get("real_order_submission") is False
            and router.get("execution_authority") == "PAPER_EXECUTION_OWNER"
            and router.get("order_submission_enabled") is True
            and router.get("killed") is False
            and not router.get("blocker")
            and int(time.time()) - int(router.get("timestamp") or 0) <= 15
        )
        _atomic_json(
            self.status_path,
            {
                "schema": "polymarket_v7_supervisor_status_v1",
                "timestamp": int(time.time()),
                "version": 7,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "expected_sha": self.expected_sha,
                "supervisor_pid": os.getpid(),
                "child_pid": self.child.pid if self.child and self.child.poll() is None else 0,
                "state": state,
                "readiness": "FULL_PAPER_RUNTIME" if router_ready else "CORE_RUNTIME_ONLY" if state == "running" else "NOT_READY",
                "p0_full_stack_ready": router_ready,
                "reasons": sorted(set(reasons)),
                "started_at": self.started_at,
                "restart_count_window": len(self._restart_times()),
            },
        )

    def _restart_times(self) -> list[int]:
        now = int(time.time())
        value = _json(self.restart_path)
        if (
            value.get("schema") != "polymarket_v7_supervisor_restarts_v1"
            or value.get("expected_sha") != self.expected_sha
        ):
            return []
        raw = value.get("timestamps") if isinstance(value.get("timestamps"), list) else []
        return sorted(int(item) for item in raw if isinstance(item, (int, float)) and now - int(item) <= self.restart_window)

    def record_restart(self) -> int:
        times = self._restart_times()
        times.append(int(time.time()))
        _atomic_json(
            self.restart_path,
            {
                "schema": "polymarket_v7_supervisor_restarts_v1",
                "expected_sha": self.expected_sha,
                "window_seconds": self.restart_window,
                "timestamps": times,
            },
        )
        return len(times)

    def reconcile(self) -> Any:
        result = assess_reconciliation(self.run_root, self.expected_sha, now=int(time.time()))
        self.status("reconciling" if result.may_start else "quarantined", result.reasons)
        if result.may_start:
            _atomic_json(
                self.control / "feed_lineage_epoch.json",
                {
                    "schema": "polymarket_v7_feed_lineage_epoch_v1",
                    "timestamp": int(time.time()),
                    "paper_only": True,
                    "authenticated_execution": False,
                    "model_sha": self.expected_sha,
                    "previous_lineage_invalidated": True,
                    "ledger_rows_reconciled": result.ledger_rows,
                    "paper_inventory_rebuild_required": result.paper_inventory_present,
                },
            )
        return result

    def stop_child(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + self.termination_grace
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def run(self) -> int:
        while not self.stopping:
            assessment = self.reconcile()
            if not assessment.may_start:
                return 78
            prior_restarts = self._restart_times()
            if len(prior_restarts) >= self.restart_maximum:
                self.status("restart_budget_exhausted", ["repeated_restart"])
                return 75

            environment = os.environ.copy()
            environment.update(
                {
                    "PM_V7_CONFIG": "config/paper_v7.json",
                    "PM_V7_RUN_ROOT": str(self.run_root),
                    "PM_V7_MODEL_SHA": self.expected_sha,
                    "PM_V7_AUTHENTICATED_EXECUTION": "0",
                    "PM_V7_REAL_ORDER_SUBMISSION": "0",
                }
            )
            self.child = subprocess.Popen(
                self.command,
                cwd=self.root,
                env=environment,
                start_new_session=True,
            )
            launched = time.monotonic()
            self.status("starting", assessment.reasons)
            failure: Any = None
            while not self.stopping and self.child.poll() is None:
                elapsed = time.monotonic() - launched
                # runtime_status may still describe the stopped incumbent for a
                # moment; do not mistake that stale file for the new child.
                if elapsed < min(2.0, float(self.startup_grace)):
                    self.status("starting", assessment.reasons)
                    time.sleep(min(self.health_interval, 0.25))
                    continue
                health = runtime_health(
                    self.run_root,
                    self.expected_sha,
                    now=int(time.time()),
                    stale_seconds=self.stale_seconds,
                )
                if health.classification == UNSAFE:
                    failure = health
                    self.status("quarantined", health.reasons)
                    self.stop_child()
                    return 78
                if elapsed >= self.startup_grace and health.classification == RECOVERABLE:
                    failure = health
                    self.status("unhealthy", health.reasons)
                    self.stop_child()
                    break
                if health.classification == SAFE:
                    self.status("running")
                time.sleep(self.health_interval)

            if self.stopping:
                break
            exit_code = self.child.poll()
            reasons = list(failure.reasons if failure else ())
            reasons.append(f"child_exit:{exit_code}")
            count = self.record_restart()
            if count >= self.restart_maximum:
                self.status("restart_budget_exhausted", reasons + ["repeated_restart"])
                return 75
            self.status("backoff", reasons)
            time.sleep(self.backoff[min(count - 1, len(self.backoff) - 1)])

        self.status("stopped")
        self.stop_child()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v7_live"))
    parser.add_argument("--policy", type=Path, default=ROOT / "config" / "v7_runtime_supervision.json")
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--reconcile-only", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    policy = _json(args.policy)
    supervisor = Supervisor(args, policy)
    try:
        supervisor.validate_static_contract()
        supervisor.acquire()
        if args.reconcile_only:
            result = supervisor.reconcile()
            print(json.dumps({"classification": result.classification, "reasons": result.reasons, "may_start": result.may_start}, sort_keys=True))
            return 0 if result.may_start else 78

        def stop(_signum: int, _frame: Any) -> None:
            supervisor.stopping = True
            supervisor.stop_child()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        return supervisor.run()
    except (OSError, RuntimeError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        supervisor.status("failed", [str(exc)])
        print(f"v7 supervisor: {exc}", file=sys.stderr)
        return 78
    finally:
        supervisor.stop_child()
        supervisor.release()


if __name__ == "__main__":
    raise SystemExit(main())
