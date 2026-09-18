#!/usr/bin/env python3
"""Best-effort bootstrap reconciliation of legacy PAPER native claims.

Runs before the current run's canonical writer starts. For each inactive source
ledger, it starts exactly one temporary writer, attempts public settlement with
a one-second resolution window, then stops that writer. Unresolved or failed
legacy claims remain reserved by the registry; this tool never releases capital.
"""
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

from v7_legacy_native_claims import validate_registry


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def existing_ledger_writers() -> list[int]:
    out: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for child in proc.iterdir():
        if not child.name.isdigit() or int(child.name) == os.getpid():
            continue
        try:
            command = (child / "cmdline").read_bytes().replace(b"\x00", b" ")
        except OSError:
            continue
        if b"v7_ledger_spool.py" in command:
            out.append(int(child.name))
    return sorted(out)


def groups(registry: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for source in registry["sources"]:
        by_sha: dict[str, list[str]] = {}
        for claim in source["claims"]:
            by_sha.setdefault(str(claim["model_sha"]), []).append(str(claim["market_id"]))
        for sha, markets in sorted(by_sha.items()):
            output.append({
                "source_run_root": str(source["source_run_root"]),
                "model_sha": sha,
                "markets": sorted(markets),
            })
    return output


def reconcile(
    registry: dict[str, Any], *, repository_root: Path, target_sha: str,
    python: str = sys.executable,
) -> dict[str, Any]:
    validate_registry(registry, target_sha=target_sha)
    active = existing_ledger_writers()
    report: dict[str, Any] = {
        "schema": "polymarket_v7_legacy_native_reconciliation_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "target_sha": target_sha,
        "registry_sha256": registry["registry_sha256"],
        "initial_claim_microdollars": registry["total_claim_microdollars"],
        "groups": [],
        "writer_pids_seen_before_start": active,
        "state": "COMPLETE",
    }
    if active:
        report["state"] = "SKIPPED_EXISTING_LEDGER_WRITER"
        return report

    writer_script = repository_root / "scripts/v7_ledger_spool.py"
    settler_script = repository_root / "scripts/v7_native_paper_settlement.py"
    if not writer_script.is_file() or not settler_script.is_file():
        raise ValueError("legacy_reconcile_runtime_scripts_missing")

    for group in groups(registry):
        root = Path(group["source_run_root"])
        sha = group["model_sha"]
        result = {**group, "settlements": [], "writer_started": False}
        log_path = root / "legacy_reconciliation_writer.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab", buffering=0) as log:
            writer = subprocess.Popen(
                [python, str(writer_script), "--run-root", str(root),
                 "--model-sha", sha, "--writer-id", f"legacy-reconcile:{target_sha[:12]}",
                 "--loop", "--interval", "0.1"],
                cwd=repository_root, stdout=log, stderr=subprocess.STDOUT,
            )
            time.sleep(0.25)
            if writer.poll() is not None:
                result["writer_rc"] = writer.returncode
                result["state"] = "WRITER_START_FAILED_CLAIM_RETAINED"
                report["groups"].append(result)
                continue
            result["writer_started"] = True
            try:
                for market in group["markets"]:
                    try:
                        completed = subprocess.run(
                            [python, str(settler_script), "--run-root", str(root),
                             "--model-sha", sha, "--market-id", market,
                             "--timeout-seconds", "1"],
                            cwd=repository_root, check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=15,
                        )
                        rc = int(completed.returncode)
                        state = "SETTLED_OR_ALREADY_CLOSED" if rc == 0 else (
                            "RESOLUTION_PENDING_CLAIM_RETAINED" if rc == 79
                            else "ERROR_CLAIM_RETAINED"
                        )
                        result["settlements"].append({
                            "market_id": market, "returncode": rc, "state": state,
                        })
                    except subprocess.TimeoutExpired:
                        result["settlements"].append({
                            "market_id": market, "returncode": None,
                            "state": "SETTLEMENT_PROCESS_TIMEOUT_CLAIM_RETAINED",
                        })
            finally:
                writer.send_signal(signal.SIGTERM)
                try:
                    writer.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    writer.kill(); writer.wait(timeout=5)
            result["writer_rc"] = writer.returncode
            result["state"] = "DONE"
        report["groups"].append(result)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    registry = read_json(args.registry)
    report = reconcile(
        registry, repository_root=args.repository_root.resolve(),
        target_sha=args.target_sha, python=args.python,
    )
    atomic_json(args.output, report)
    print(json.dumps({"state": report["state"], "groups": len(report["groups"]),
                      "initial_claim_microdollars": report["initial_claim_microdollars"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
