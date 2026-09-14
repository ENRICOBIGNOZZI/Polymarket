#!/usr/bin/env python3
"""Freeze exactly one preregistered Maker window for one successful PAPER deploy.

This helper owns no execution authority. It verifies the exact deployed SHA,
waits for required evidence, claims one deploy-run marker, and invokes the
fail-closed forward-window freezer. Interrupted attempts remain FREEZING.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import v7_maker_forward_window_evaluator as evaluator
import v7_prepare_maker_forward_window as freezer

SCHEMA = "polymarket_v7_maker_forward_deploy_freeze_v1"
MARKER_DIR = ".deploy_freeze"


def exact_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def marker_paths(output_root: pathlib.Path, deploy_run_id: str) -> tuple[pathlib.Path, pathlib.Path]:
    marker_dir = output_root / MARKER_DIR
    return marker_dir / f"{deploy_run_id}.json", marker_dir / f"{deploy_run_id}.manifest.json"


def validate_manifest(
    manifest: dict[str, Any], *, expected_sha: str, experiment_id: str | None = None,
) -> dict[str, Any]:
    evaluator.validate_manifest(manifest)
    if manifest.get("code_sha") != expected_sha:
        raise ValueError("manifest:code_sha")
    if experiment_id is not None and manifest.get("experiment_id") != experiment_id:
        raise ValueError("manifest:experiment_id")
    if manifest.get("paper_only") is not True:
        raise ValueError("manifest:paper_only")
    if manifest.get("authenticated_execution") is not False:
        raise ValueError("manifest:authenticated_execution")
    if manifest.get("real_order_submission") is not False:
        raise ValueError("manifest:real_order_submission")
    if manifest.get("automatic_promotion") is not False:
        raise ValueError("manifest:automatic_promotion")
    start = int(manifest["window_start_ms"])
    if int(manifest["window_end_ms"]) - start != freezer.WINDOW_MS:
        raise ValueError("manifest:window")
    capture = manifest.get("evidence_baselines", {}).get("capture", {})
    protocol = manifest.get("freeze_protocol", {})
    completed = int(capture.get("completed_ms") or 0)
    prepared = int(protocol.get("manifest_prepared_ms") or 0)
    lead = int(protocol.get("window_start_lead_ms") or 0)
    if completed <= 0 or completed > prepared or prepared >= start:
        raise ValueError("manifest:freeze_ordering")
    if lead != freezer.FREEZE_LEAD_MS or start - prepared != lead:
        raise ValueError("manifest:freeze_lead")
    if capture.get("hash_semantics") != freezer.PREFIX_HASH_SEMANTICS:
        raise ValueError("manifest:hash_semantics")
    return manifest


def existing_result(
    marker: pathlib.Path, stable_manifest: pathlib.Path, *, expected_sha: str, deploy_run_id: str,
) -> dict[str, Any] | None:
    if not marker.exists():
        return None
    state = read_json(marker)
    if state.get("deploy_run_id") != deploy_run_id or state.get("code_sha") != expected_sha:
        raise ValueError("marker:identity")
    if state.get("state") != "FROZEN":
        raise ValueError("marker:incomplete_prior_attempt")
    if not stable_manifest.is_file() or stable_manifest.is_symlink():
        raise ValueError("marker:stable_manifest_missing")
    manifest = validate_manifest(read_json(stable_manifest), expected_sha=expected_sha,
                                 experiment_id=str(state.get("experiment_id") or ""))
    if state.get("manifest_sha256") != manifest.get("manifest_sha256"):
        raise ValueError("marker:manifest_hash")
    return {
        "schema": SCHEMA, "state": "ALREADY_FROZEN_IDEMPOTENT", "deploy_run_id": deploy_run_id,
        "code_sha": expected_sha, "experiment_id": manifest["experiment_id"],
        "manifest_sha256": manifest["manifest_sha256"], "window_start_ms": manifest["window_start_ms"],
        "window_end_ms": manifest["window_end_ms"], "stable_manifest_path": str(stable_manifest),
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "automatic_promotion": False,
    }


def required_evidence_paths(run_root: pathlib.Path) -> tuple[pathlib.Path, ...]:
    return (
        run_root / "ledger" / "execution.jsonl",
        run_root / "micro_maker" / "reward_selection.events.jsonl",
        run_root / "micro_maker" / "fillability_ws.jsonl",
        run_root / "micro_maker" / "book_observations" / "current.jsonl",
    )


def evidence_ready(path: pathlib.Path, *, allow_empty: bool = False) -> bool:
    return (
        path.is_file() and not path.is_symlink()
        and (allow_empty or path.stat().st_size > 0)
    )


def evidence_surfaces_ready(run_root: pathlib.Path) -> bool:
    ledger, selector, fillability, books = required_evidence_paths(run_root)
    return (
        evidence_ready(ledger, allow_empty=True)
        and evidence_ready(selector)
        and evidence_ready(fillability)
        and evidence_ready(books)
    )


def wait_for_evidence(run_root: pathlib.Path, *, timeout_seconds: float, poll_seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    paths = required_evidence_paths(run_root)
    while True:
        if evidence_surfaces_ready(run_root):
            return
        if time.monotonic() >= deadline:
            ledger, selector, fillability, books = paths
            missing = []
            if not evidence_ready(ledger, allow_empty=True): missing.append(str(ledger))
            for path in (selector, fillability, books):
                if not evidence_ready(path): missing.append(str(path))
            raise ValueError("evidence:not_ready:" + ",".join(missing))
        time.sleep(max(0.01, poll_seconds))


def preflight(repository_root: pathlib.Path, run_root: pathlib.Path,
              expected_sha: str, *, now_ms: int) -> dict[str, Any]:
    repository = freezer.validate_repository_state(repository_root, expected_sha)
    runtime = freezer.validate_runtime(run_root, expected_sha, now_ms)
    policy = freezer.validate_policy_config(repository_root)
    if not evidence_surfaces_ready(run_root):
        raise ValueError("evidence:preflight_not_ready")
    book_root = run_root / "micro_maker" / "book_observations"
    if not book_root.is_dir() or book_root.is_symlink():
        raise ValueError("evidence:book_root")
    return {"repository": repository, "runtime": runtime, "policy": policy,
            "evidence_surfaces_ready": True}


def claim_marker(marker: pathlib.Path, *, expected_sha: str, deploy_run_id: str, now_ms: int) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA, "state": "FREEZING", "code_sha": expected_sha,
        "deploy_run_id": deploy_run_id, "created_at_ms": now_ms,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "automatic_promotion": False,
    }
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("marker:claim_race") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())


def finalize(
    marker: pathlib.Path, stable_manifest: pathlib.Path,
    manifest_path: pathlib.Path, manifest: dict[str, Any], *,
    expected_sha: str, deploy_run_id: str, now_ms: int,
) -> dict[str, Any]:
    validate_manifest(manifest, expected_sha=expected_sha)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("manifest:source_missing")
    disk_manifest = validate_manifest(read_json(manifest_path), expected_sha=expected_sha,
                                      experiment_id=str(manifest["experiment_id"]))
    if evaluator.canonical_json(disk_manifest) != evaluator.canonical_json(manifest):
        raise ValueError("manifest:disk_memory_mismatch")
    stable_manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary = stable_manifest.with_name(stable_manifest.name + f".tmp.{os.getpid()}")
    shutil.copy2(manifest_path, temporary); os.replace(temporary, stable_manifest)
    stable = validate_manifest(read_json(stable_manifest), expected_sha=expected_sha,
                               experiment_id=str(manifest["experiment_id"]))
    if stable.get("manifest_sha256") != manifest.get("manifest_sha256"):
        raise ValueError("manifest:stable_hash")
    final_marker = {
        "schema": SCHEMA, "state": "FROZEN", "code_sha": expected_sha,
        "deploy_run_id": deploy_run_id, "experiment_id": manifest["experiment_id"],
        "manifest_sha256": manifest["manifest_sha256"], "window_start_ms": manifest["window_start_ms"],
        "window_end_ms": manifest["window_end_ms"], "frozen_at_ms": now_ms,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "automatic_promotion": False,
    }
    atomic_json(marker, final_marker)
    return {
        "schema": SCHEMA, "state": "FROZEN", "deploy_run_id": deploy_run_id,
        "code_sha": expected_sha, "experiment_id": manifest["experiment_id"],
        "manifest_sha256": manifest["manifest_sha256"], "window_start_ms": manifest["window_start_ms"],
        "window_end_ms": manifest["window_end_ms"], "stable_manifest_path": str(stable_manifest),
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "automatic_promotion": False,
    }


def freeze_after_deploy(
    repository_root: pathlib.Path, run_root: pathlib.Path,
    output_root: pathlib.Path, *, expected_sha: str, deploy_run_id: str,
    wait_seconds: float = 180.0, poll_seconds: float = 2.0,
) -> dict[str, Any]:
    repository_root = repository_root.resolve(); run_root = run_root.resolve(); output_root = output_root.resolve()
    deploy_run_id = str(deploy_run_id).strip()
    if not exact_sha(expected_sha):
        raise ValueError("expected_sha")
    if not deploy_run_id or not deploy_run_id.isdigit():
        raise ValueError("deploy_run_id")
    marker, stable_manifest = marker_paths(output_root, deploy_run_id)
    existing = existing_result(marker, stable_manifest, expected_sha=expected_sha, deploy_run_id=deploy_run_id)
    if existing is not None:
        preflight(repository_root, run_root, expected_sha, now_ms=time.time_ns() // 1_000_000)
        return existing
    wait_for_evidence(run_root, timeout_seconds=wait_seconds, poll_seconds=poll_seconds)
    preflight_ms = time.time_ns() // 1_000_000
    preflight(repository_root, run_root, expected_sha, now_ms=preflight_ms)
    claim_marker(marker, expected_sha=expected_sha, deploy_run_id=deploy_run_id, now_ms=preflight_ms)
    manifest = freezer.prepare(
        repository_root, run_root, output_root, expected_sha,
        book_evidence=[run_root / "micro_maker" / "book_observations"],
    )
    manifest_path = output_root / str(manifest["experiment_id"]) / "manifest.json"
    return finalize(marker, stable_manifest, manifest_path, manifest,
                    expected_sha=expected_sha, deploy_run_id=deploy_run_id,
                    now_ms=time.time_ns() // 1_000_000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--run-root", type=pathlib.Path, default=pathlib.Path("runs/paper_v7_live"))
    parser.add_argument("--output-root", type=pathlib.Path, default=pathlib.Path("runs/paper_v7_experiments"))
    parser.add_argument("--expected-sha", required=True); parser.add_argument("--deploy-run-id", required=True)
    parser.add_argument("--wait-seconds", type=float, default=180.0); parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    result = freeze_after_deploy(args.repository_root, args.run_root, args.output_root,
        expected_sha=args.expected_sha, deploy_run_id=args.deploy_run_id,
        wait_seconds=args.wait_seconds, poll_seconds=args.poll_seconds)
    print(json.dumps(result, indent=2, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
