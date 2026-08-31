#!/usr/bin/env python3
"""Pure runtime/restart contracts for the canonical V7 FULL-PAPER service.

The module never starts a process and never mutates the ledger.  It gives the
native supervisor one deterministic answer: safe to start, recoverable, or an
unsafe state which requires an operator.  Keeping this logic pure makes crash,
clock, disk and duplicate-writer behavior straightforward to chaos-test.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SAFE = "SAFE"
RECOVERABLE = "RECOVERABLE"
UNSAFE = "UNSAFE_OPERATOR_REQUIRED"
MAKER_SELECTOR_OPERATIONAL_STATES = {
    "OPERATIONAL_REWARDED",
    "OPERATIONAL_FALLBACK",
    "OPERATIONAL_RECENT_FLOW",
    "OPERATIONAL_BILATERAL_FLOW",
}
MAKER_ROTATION_OPERATIONAL_STATES = {
    "RUNNING",
    "RUNNING_CELL_REFRESHED",
    "RUNNING_DIRECTIONAL_DRAIN",
    "DRAINING",
    "PENDING_NONFLAT",
    "PENDING_DRAIN_TIMEOUT",
    "PENDING_CONFIRMATION",
    "PENDING_COOLDOWN",
    "PAUSED_NO_FRESH_FLOW",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def pid_alive(value: Any) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError, ProcessLookupError):
        return False
    return True


@dataclass(frozen=True)
class Assessment:
    classification: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    ledger_rows: int = 0
    paper_inventory_present: bool = False

    @property
    def may_start(self) -> bool:
        return self.classification in {SAFE, RECOVERABLE}


def _ledger_contract(path: Path, expected_sha: str) -> tuple[list[str], int]:
    if not path.exists():
        return [], 0
    reasons: list[str] = []
    rows = 0
    try:
        raw = path.read_bytes()
    except OSError:
        return ["ledger_unavailable"], 0
    if raw and not raw.endswith(b"\n"):
        reasons.append("ledger_incomplete_tail")
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        rows += 1
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            reasons.append(f"ledger_invalid_json:{number}")
            continue
        if not isinstance(value, dict):
            reasons.append(f"ledger_invalid_record:{number}")
            continue
        if value.get("paper_only") is not True or value.get("authenticated_execution") is not False:
            reasons.append(f"ledger_unsafe_record:{number}")
        if value.get("model_sha") != expected_sha:
            reasons.append(f"ledger_sha_mismatch:{number}")
    return reasons, rows


def _paper_inventory(portfolio: dict[str, Any]) -> bool:
    sleeves = portfolio.get("sleeves")
    if not isinstance(sleeves, dict):
        return False
    for row in sleeves.values():
        if not isinstance(row, dict):
            continue
        for key in ("open_positions", "live_units", "pending_orders", "inventory"):
            try:
                if abs(float(row.get(key) or 0.0)) > 0.0:
                    return True
            except (TypeError, ValueError, OverflowError):
                return True
    return False


def assess_reconciliation(
    run_root: Path,
    expected_sha: str,
    *,
    now: int,
    maximum_clock_skew_seconds: int = 5,
) -> Assessment:
    """Assess an existing run root before any V7 process is started.

    A recoverable process crash may restart after ledger/account reconciliation.
    Safety ambiguity, a live writer, SHA drift, a kill-switch state, or corrupt
    evidence is never auto-restarted.
    """
    run_root = Path(run_root)
    if not SHA40.fullmatch(expected_sha):
        return Assessment(UNSAFE, ("expected_sha_invalid",))

    unsafe: list[str] = []
    recoverable: list[str] = []
    runtime = read_json(run_root / "control" / "runtime_status.json")
    portfolio = read_json(run_root / "control" / "portfolio_state.json")
    kill = read_json(run_root / "control" / "KILL")
    lock_pid_path = run_root / "control" / "runtime.lock" / "pid"

    if lock_pid_path.exists():
        try:
            lock_pid: Any = lock_pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            lock_pid = 0
        if pid_alive(lock_pid):
            unsafe.append("duplicate_writer_live")
        else:
            recoverable.append("stale_runtime_lock")

    if runtime:
        if runtime.get("version") != 7:
            unsafe.append("runtime_version_not_v7")
        if runtime.get("paper_only") is not True:
            unsafe.append("runtime_not_paper_only")
        if runtime.get("authenticated_execution") is not False or runtime.get("real_order_submission") is not False:
            unsafe.append("runtime_execution_authority_unsafe")
        if runtime.get("model_sha") != expected_sha:
            unsafe.append("runtime_sha_mismatch")
        try:
            timestamp = int(runtime.get("timestamp") or 0)
        except (TypeError, ValueError, OverflowError):
            timestamp = 0
        if timestamp > now + maximum_clock_skew_seconds:
            unsafe.append("runtime_clock_in_future")
        if runtime.get("killed") is True:
            if kill.get("schema") == "polymarket_v7_runtime_failure_v1" and kill.get("paper_only") is True and kill.get("authenticated_execution") is False:
                recoverable.append("recoverable_process_crash")
            else:
                unsafe.append("runtime_killed")

    inventory_present = _paper_inventory(portfolio)
    if portfolio:
        if portfolio.get("paper_only") is not True or portfolio.get("authenticated_execution") is not False:
            unsafe.append("paper_inventory_contract_unsafe")
        if portfolio.get("killed") is True:
            unsafe.append("portfolio_killed")
        try:
            drawdown = float(portfolio.get("drawdown") or 0.0)
            maximum = float(portfolio.get("max_drawdown") or 0.15)
        except (TypeError, ValueError, OverflowError):
            drawdown, maximum = math.inf, 0.15
        if not math.isfinite(drawdown) or drawdown >= maximum:
            unsafe.append("portfolio_drawdown_limit")
        if inventory_present:
            recoverable.append("paper_inventory_rebuild_required")

    ledger_reasons, rows = _ledger_contract(run_root / "ledger" / "execution.jsonl", expected_sha)
    unsafe.extend(ledger_reasons)

    if kill:
        schema = str(kill.get("schema") or "")
        if kill.get("paper_only") is not True or kill.get("authenticated_execution") is not False:
            unsafe.append("kill_marker_contract_unsafe")
        elif schema == "polymarket_v7_runtime_failure_v1":
            recoverable.append("recoverable_process_crash")
        else:
            unsafe.append("unsafe_kill_marker")

    if unsafe:
        return Assessment(UNSAFE, tuple(sorted(set(unsafe + recoverable))), rows, inventory_present)
    if recoverable:
        return Assessment(RECOVERABLE, tuple(sorted(set(recoverable))), rows, inventory_present)
    return Assessment(SAFE, (), rows, inventory_present)


def failure_action(policy: dict[str, Any], failure: str) -> dict[str, Any]:
    domains = policy.get("failure_domains") if isinstance(policy.get("failure_domains"), dict) else {}
    value = domains.get(failure)
    if not isinstance(value, dict):
        return {"scope": "unknown", "action": "quarantine", "critical": True}
    return {
        "scope": str(value.get("scope") or "unknown"),
        "action": str(value.get("action") or "quarantine"),
        "critical": value.get("critical") is True,
    }


def runtime_health(run_root: Path, expected_sha: str, *, now: int, stale_seconds: int) -> Assessment:
    run_root = Path(run_root)
    runtime = read_json(run_root / "control" / "runtime_status.json")
    if not runtime:
        return Assessment(RECOVERABLE, ("runtime_status_missing",))
    unsafe: list[str] = []
    recoverable: list[str] = []
    if runtime.get("version") != 7 or runtime.get("model_sha") != expected_sha:
        unsafe.append("runtime_identity_drift")
    if runtime.get("paper_only") is not True or runtime.get("authenticated_execution") is not False or runtime.get("real_order_submission") is not False:
        unsafe.append("runtime_execution_authority_unsafe")
    if runtime.get("killed") is True:
        unsafe.append("runtime_killed")
    if not pid_alive(runtime.get("pid")):
        recoverable.append("runtime_pid_dead")
    try:
        age = now - int(runtime.get("timestamp") or 0)
    except (TypeError, ValueError, OverflowError):
        age = stale_seconds + 1
    if age < -5:
        unsafe.append("runtime_clock_in_future")
    elif age > stale_seconds:
        recoverable.append("runtime_status_stale")
    if runtime.get("state") == "running":
        selector = read_json(run_root / "micro_maker" / "selector_status.json")
        rotation = read_json(run_root / "micro_maker" / "rotation_status.json")
        maker = read_json(run_root / "micro_maker" / "status.json")
        if selector:
            if (
                selector.get("paper_only") is not True
                or selector.get("authenticated_execution") is not False
                or selector.get("real_order_submission") is not False
            ):
                unsafe.append("maker_selector_execution_authority_unsafe")
            if selector.get("model_sha") != expected_sha:
                unsafe.append("maker_selector_identity_drift")
            if (
                selector.get("ready") is not True
                or selector.get("state") not in MAKER_SELECTOR_OPERATIONAL_STATES
            ):
                recoverable.append("maker_selector_not_ready")
            try:
                selector_age = now * 1000 - int(selector.get("timestamp_ms") or 0)
            except (TypeError, ValueError, OverflowError):
                selector_age = stale_seconds * 1000 + 1
            if selector_age < -5_000:
                unsafe.append("maker_selector_clock_in_future")
            # The slow selector refreshes every 60s after a bounded network
            # attempt; its heartbeat contract is intentionally slower than the
            # 1s maker/runtime state loop.
            elif selector_age > max(120, stale_seconds * 4) * 1000:
                recoverable.append("maker_selector_stale")
        else:
            recoverable.append("maker_selector_status_missing")
        if rotation:
            if (
                rotation.get("paper_only") is not True
                or rotation.get("authenticated_execution") is not False
                or rotation.get("real_order_submission") is not False
            ):
                unsafe.append("maker_cohort_execution_authority_unsafe")
            if rotation.get("model_sha") != expected_sha:
                unsafe.append("maker_cohort_identity_drift")
            if rotation.get("state") not in MAKER_ROTATION_OPERATIONAL_STATES:
                recoverable.append("maker_cohort_not_ready")
            try:
                rotation_age = now * 1000 - int(rotation.get("timestamp_ms") or 0)
            except (TypeError, ValueError, OverflowError):
                rotation_age = stale_seconds * 1000 + 1
            if rotation_age < -5_000:
                unsafe.append("maker_cohort_clock_in_future")
            elif rotation_age > stale_seconds * 1000:
                recoverable.append("maker_cohort_status_stale")
        else:
            recoverable.append("maker_cohort_status_missing")
        if maker:
            if maker.get("paper_only") is not True or maker.get("authenticated_execution") is not False:
                unsafe.append("professional_maker_execution_authority_unsafe")
            if maker.get("model_sha") != expected_sha:
                unsafe.append("professional_maker_identity_drift")
            if maker.get("killed") is True:
                recoverable.append("professional_maker_killed")
            if maker.get("source") in {None, "", "not_started"}:
                recoverable.append("professional_maker_not_started")
            try:
                maker_age = now * 1000 - int(maker.get("timestamp_ms") or 0)
            except (TypeError, ValueError, OverflowError):
                maker_age = stale_seconds * 1000 + 1
            if maker_age < -5_000:
                unsafe.append("professional_maker_clock_in_future")
            elif maker_age > stale_seconds * 1000:
                recoverable.append("professional_maker_status_stale")
        else:
            recoverable.append("professional_maker_status_missing")
    if unsafe:
        return Assessment(UNSAFE, tuple(sorted(set(unsafe + recoverable))))
    if recoverable:
        return Assessment(RECOVERABLE, tuple(sorted(set(recoverable))))
    return Assessment(SAFE)
