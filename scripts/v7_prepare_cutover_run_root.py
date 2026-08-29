#!/usr/bin/env python3
"""Atomically preserve a prior V7 PAPER run before an exact-SHA cutover."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

SHA40 = re.compile(r"^[0-9a-f]{40}$")


class CutoverArchiveError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def pid_alive(value: object) -> bool:
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


def git_is_ancestor(repository_root: Path, older: str, newer: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repository_root), "merge-base", "--is-ancestor", older, newer],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def validate_ledger(
    path: Path,
    repository_root: Path,
    target_sha: str,
    ancestor_check: Callable[[Path, str, str], bool],
) -> tuple[int, str, dict[str, int]]:
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return 0, hashlib.sha256(b"").hexdigest(), {}
    digest = hashlib.sha256()
    rows = 0
    model_sha_counts: dict[str, int] = {}
    ancestry: dict[str, bool] = {}
    with handle:
        for number, raw in enumerate(handle, start=1):
            digest.update(raw)
            if not raw.endswith(b"\n"):
                raise CutoverArchiveError("ledger_incomplete_tail")
            if not raw.strip():
                continue
            rows += 1
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CutoverArchiveError(f"ledger_invalid_json:{number}") from exc
            if not isinstance(value, dict):
                raise CutoverArchiveError(f"ledger_invalid_record:{number}")
            if value.get("paper_only") is not True or value.get("authenticated_execution") is not False:
                raise CutoverArchiveError(f"ledger_unsafe_record:{number}")
            model_sha = str(value.get("model_sha") or "")
            if not SHA40.fullmatch(model_sha):
                raise CutoverArchiveError(f"ledger_sha_invalid:{number}")
            if model_sha not in ancestry:
                ancestry[model_sha] = ancestor_check(repository_root, model_sha, target_sha)
            if not ancestry[model_sha]:
                raise CutoverArchiveError(f"ledger_sha_not_ancestor:{number}")
            model_sha_counts[model_sha] = model_sha_counts.get(model_sha, 0) + 1
    return rows, digest.hexdigest(), dict(sorted(model_sha_counts.items()))


def prepare(
    run_root: Path,
    archive_root: Path,
    repository_root: Path,
    target_sha: str,
    *,
    now: int | None = None,
    ancestor_check: Callable[[Path, str, str], bool] = git_is_ancestor,
) -> dict:
    if not SHA40.fullmatch(target_sha):
        raise CutoverArchiveError("target_sha_invalid")
    run_root = run_root.resolve()
    archive_root = archive_root.resolve()
    repository_root = repository_root.resolve()
    if not run_root.exists():
        run_root.mkdir(parents=True)
        return {"state": "NEW_RUN_ROOT", "target_sha": target_sha, "archived": False}

    runtime = read_json(run_root / "control/runtime_status.json")
    supervisor = read_json(run_root / "control/supervisor_status.json")
    deployed_path = run_root / "control/deployed_sha"
    deployed_sha = deployed_path.read_text(encoding="utf-8").strip() if deployed_path.is_file() else ""
    runtime_sha = str(runtime.get("model_sha") or "")

    if not runtime and not deployed_sha:
        lineage = read_json(run_root / "control/cutover_lineage.json")
        if not any(run_root.iterdir()):
            return {"state": "NEW_RUN_ROOT", "target_sha": target_sha, "archived": False}
        if (
            lineage.get("schema") == "polymarket_v7_cutover_lineage_v1"
            and lineage.get("target_sha") == target_sha
            and lineage.get("paper_only") is True
            and lineage.get("authenticated_execution") is False
            and lineage.get("real_order_submission") is False
            and not (run_root / "ledger/execution.jsonl").exists()
        ):
            return {"state": "PREPARED_RUN_ROOT", "target_sha": target_sha, "archived": False}
        raise CutoverArchiveError("previous_runtime_sha_missing_or_invalid")

    if deployed_sha and not SHA40.fullmatch(deployed_sha):
        raise CutoverArchiveError("previous_deployed_sha_invalid")
    if runtime and not SHA40.fullmatch(runtime_sha):
        raise CutoverArchiveError("previous_runtime_sha_missing_or_invalid")
    if pid_alive(runtime.get("pid")) or pid_alive(supervisor.get("supervisor_pid")):
        raise CutoverArchiveError("prior_runtime_or_supervisor_still_alive")

    previous_sha = deployed_sha or runtime_sha
    if not SHA40.fullmatch(previous_sha):
        raise CutoverArchiveError("previous_runtime_sha_missing_or_invalid")
    runtime_checkout_drift = bool(
        deployed_sha and runtime_sha and runtime_sha == target_sha and runtime_sha != deployed_sha
    )
    if deployed_sha and runtime_sha and runtime_sha != deployed_sha and not runtime_checkout_drift:
        raise CutoverArchiveError("previous_deployed_runtime_sha_mismatch")
    if previous_sha == target_sha:
        _, _, model_sha_counts = validate_ledger(
            run_root / "ledger/execution.jsonl", repository_root, target_sha, ancestor_check,
        )
        if any(model_sha != target_sha for model_sha in model_sha_counts):
            raise CutoverArchiveError("same_sha_ledger_mismatch")
        return {"state": "SAME_SHA_RECOVERY", "target_sha": target_sha, "archived": False}
    if not ancestor_check(repository_root, previous_sha, target_sha):
        raise CutoverArchiveError("previous_runtime_not_ancestor_of_target")
    if runtime.get("paper_only") is not True or runtime.get("authenticated_execution") is not False or runtime.get("real_order_submission") is not False:
        raise CutoverArchiveError("prior_runtime_safety_contract_invalid")

    portfolio = read_json(run_root / "control/portfolio_state.json")
    if portfolio:
        if portfolio.get("paper_only") is not True or portfolio.get("authenticated_execution") is not False:
            raise CutoverArchiveError("prior_portfolio_safety_contract_invalid")
        if portfolio.get("killed") is True:
            raise CutoverArchiveError("prior_portfolio_killed")
        try:
            drawdown = float(portfolio.get("drawdown") or 0.0)
            maximum = float(portfolio.get("max_drawdown") or 0.15)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CutoverArchiveError("prior_portfolio_drawdown_invalid") from exc
        if drawdown >= maximum:
            raise CutoverArchiveError("prior_portfolio_drawdown_limit")

    ledger_rows, ledger_sha256, ledger_model_sha_counts = validate_ledger(
        run_root / "ledger/execution.jsonl", repository_root, target_sha, ancestor_check,
    )
    archived_at = int(now if now is not None else time.time())
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / f"cutover-{previous_sha}-{archived_at}-{os.getpid()}"
    if destination.exists():
        raise CutoverArchiveError("archive_destination_exists")
    os.replace(run_root, destination)
    run_root.mkdir(parents=True)
    control = run_root / "control"
    control.mkdir()
    receipt = {
        "schema": "polymarket_v7_cutover_lineage_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "previous_runtime_sha": previous_sha,
        "target_sha": target_sha,
        "archived_at": archived_at,
        "archive_path": str(destination),
        "ledger_rows": ledger_rows,
        "ledger_sha256": ledger_sha256,
        "ledger_model_sha_counts": ledger_model_sha_counts,
        "runtime_checkout_drift_detected": runtime_checkout_drift,
    }
    temporary = control / f"cutover_lineage.json.tmp.{os.getpid()}"
    temporary.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, control / "cutover_lineage.json")
    return {"state": "ARCHIVED_PRIOR_SHA", "archived": True, **receipt}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--target-sha", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.run_root, args.archive_root, args.repository_root, args.target_sha), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
