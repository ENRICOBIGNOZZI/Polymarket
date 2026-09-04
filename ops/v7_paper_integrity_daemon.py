#!/usr/bin/env python3
"""Persistent fail-closed integrity plane for the V7 simulated PAPER account."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MONITORING = ROOT / "monitoring"
for directory in (SCRIPTS, MONITORING):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from v7_binary_tape_retention import (  # noqa: E402
    _atomic_json,
    _json,
    archive_closed_binary_tapes,
)
from v7_paper_exploration_account import (  # noqa: E402
    _failure_status,
    reconcile_once,
)

SHA40 = re.compile(r"^[0-9a-f]{40}$")


def exact_sha(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value if SHA40.fullmatch(value) else ""


def git_head(root: Path) -> str:
    try:
        value = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return value if SHA40.fullmatch(value) else ""


def disk_status(path: Path, policy: dict[str, Any]) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    ratio = usage.free / usage.total if usage.total else 0.0
    critical_ratio = float(policy.get("critical_free_ratio") or 0.10)
    minimum_bytes = int(policy.get("minimum_free_bytes") or 5 * 1024**3)
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_ratio": ratio,
        "critical_free_ratio": critical_ratio,
        "minimum_free_bytes": minimum_bytes,
        "healthy": ratio >= critical_ratio and usage.free >= minimum_bytes,
    }


def failure_retention(
    run_root: Path, model_sha: str, exc: BaseException
) -> dict[str, Any]:
    value = {
        "schema": "polymarket_v7_binary_tape_retention_status_v1",
        "timestamp": int(time.time()),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "expected_sha": model_sha,
        "candidate_count": 0,
        "archived": [],
        "skipped": [],
        "failures": [{"path": "", "reason": str(exc)[:500]}],
        "reclaimed_bytes": 0,
        "complete": False,
        "dry_run": False,
    }
    _atomic_json(
        run_root / "control" / "binary_tape_retention_status.json", value
    )
    return value


def run_once(
    repository_root: Path,
    run_root: Path,
    deployed_sha_path: Path,
    retention_config: Path,
    *,
    previous_retention: dict[str, Any] | None = None,
    run_retention: bool,
) -> dict[str, Any]:
    deployed_sha = exact_sha(deployed_sha_path)
    head = git_head(repository_root)
    current = int(time.time())
    blockers: list[str] = []
    if not deployed_sha:
        blockers.append("DEPLOYED_SHA_UNAVAILABLE")
    if not head:
        blockers.append("REPOSITORY_HEAD_UNAVAILABLE")
    if deployed_sha and head and deployed_sha != head:
        blockers.append("DEPLOYED_SHA_REPOSITORY_HEAD_MISMATCH")

    account: dict[str, Any]
    if blockers:
        account = _failure_status(
            run_root, deployed_sha or "0" * 40,
            RuntimeError(blockers[0]),
        )
    else:
        try:
            account = reconcile_once(run_root, deployed_sha)
        except Exception as exc:
            account = _failure_status(run_root, deployed_sha, exc)
    if account.get("complete") is not True:
        blockers.append("PAPER_ACCOUNT_RECONCILIATION_INCOMPLETE")

    config = _json(retention_config)
    disk_policy = (
        config.get("disk") if isinstance(config.get("disk"), dict) else {}
    )
    binary_policy = (
        config.get("binary_tapes")
        if isinstance(config.get("binary_tapes"), dict)
        else {}
    )
    retention = previous_retention if isinstance(previous_retention, dict) else {}
    if run_retention and deployed_sha and head == deployed_sha:
        try:
            retention = archive_closed_binary_tapes(
                run_root,
                binary_policy,
                deployed_sha,
                dry_run=False,
            )
            _atomic_json(
                run_root / "control" / "binary_tape_retention_status.json",
                retention,
            )
        except Exception as exc:
            retention = failure_retention(run_root, deployed_sha, exc)
    if retention.get("complete") is not True:
        blockers.append("BINARY_TAPE_RETENTION_INCOMPLETE")
    retention_timestamp = int(retention.get("timestamp") or 0)
    if current - retention_timestamp > 180:
        blockers.append("BINARY_TAPE_RETENTION_STALE")

    disk = disk_status(run_root, disk_policy)
    if not disk["healthy"]:
        blockers.append("DISK_CAPACITY_BELOW_FAIL_CLOSED_THRESHOLD")
    blockers = sorted(set(blockers))
    status = {
        "schema": "polymarket_v7_paper_integrity_status_v1",
        "timestamp": current,
        "version": 7,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "repository_head": head,
        "deployed_sha": deployed_sha,
        "single_execution_owner": True,
        "account_ledger_writer_authority": False,
        "account_spool_producer_only": True,
        "state": "OPERATIONAL" if not blockers else "BLOCKED",
        "complete": not blockers,
        "blockers": blockers,
        "paper_account": account,
        "binary_tape_retention": retention,
        "disk": disk,
    }
    _atomic_json(
        run_root / "control" / "paper_integrity_status.json", status
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument(
        "--run-root", type=Path, default=Path("runs/paper_v7_live")
    )
    parser.add_argument("--deployed-sha", type=Path)
    parser.add_argument(
        "--retention-config",
        type=Path,
        default=ROOT / "config" / "v7_data_retention.json",
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--retention-interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    repository_root = args.repository_root.resolve()
    run_root = args.run_root.resolve()
    deployed_sha = (
        args.deployed_sha.resolve()
        if args.deployed_sha
        else run_root / "control" / "deployed_sha"
    )
    previous_retention: dict[str, Any] = {}
    last_retention = 0.0
    while True:
        monotonic = time.monotonic()
        due = monotonic - last_retention >= max(5.0, args.retention_interval)
        status = run_once(
            repository_root,
            run_root,
            deployed_sha,
            args.retention_config.resolve(),
            previous_retention=previous_retention,
            run_retention=due,
        )
        if due:
            previous_retention = status["binary_tape_retention"]
            last_retention = monotonic
        print(json.dumps({
            "timestamp": status["timestamp"],
            "state": status["state"],
            "complete": status["complete"],
            "blockers": status["blockers"],
            "free_ratio": status["disk"]["free_ratio"],
        }, sort_keys=True), flush=True)
        if args.once:
            return 0 if status["complete"] else 2
        time.sleep(max(0.25, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
