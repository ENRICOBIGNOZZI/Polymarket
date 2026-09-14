#!/usr/bin/env python3
"""Freeze an exact-SHA eight-hour PAPER Maker forward experiment.

This command owns no execution authority. It snapshots the research protocol and
an exact byte prefix of all append-only evidence before the window begins. The
manifest is published atomically with a deterministic lead before window start.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import time
from typing import Any, Iterable

import v7_maker_forward_window_evaluator as evaluator

SCHEMA = evaluator.MANIFEST_SCHEMA
HORIZONS = ["100ms", "250ms", "500ms", "1s", "5s", "10s", "30s"]
WINDOW_MS = 8 * 60 * 60 * 1000
FREEZE_LEAD_MS = 5_000
PREFIX_HASH_SEMANTICS = "SHA256_EXACT_PREFIX_AT_RECORDED_BYTE_COUNT"


def wall_ms() -> int:
    return time.time_ns() // 1_000_000


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def exact_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_prefix(path: pathlib.Path, byte_count: int) -> str:
    if byte_count < 0:
        raise ValueError("evidence_baseline:negative_prefix")
    digest = hashlib.sha256()
    remaining = int(byte_count)
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(1 << 20, remaining))
            if not chunk:
                raise ValueError(f"evidence_baseline:shrunk_during_hash:{path}")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def git_output(repository_root: pathlib.Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repository_root, text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ValueError("repository:git_command") from exc


def validate_repository_state(repository_root: pathlib.Path, expected_sha: str) -> dict[str, Any]:
    head = git_output(repository_root, "rev-parse", "HEAD")
    if head != expected_sha:
        raise ValueError("repository:head_sha")
    dirty = git_output(repository_root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise ValueError("repository:tracked_worktree_dirty")
    origin_main = git_output(repository_root, "rev-parse", "--verify", "refs/remotes/origin/main")
    if origin_main != expected_sha:
        raise ValueError("repository:origin_main_sha")
    return {"head_sha": head, "origin_main_sha": origin_main, "tracked_worktree_clean": True}


def require_paper_identity(value: dict[str, Any], expected_sha: str, name: str) -> None:
    if value.get("model_sha") != expected_sha:
        raise ValueError(f"{name}:sha")
    if value.get("paper_only") is not True:
        raise ValueError(f"{name}:paper_only")
    if value.get("authenticated_execution") is not False:
        raise ValueError(f"{name}:authenticated_execution")
    if value.get("real_order_submission") is not False:
        raise ValueError(f"{name}:real_order_submission")


def validate_runtime(run_root: pathlib.Path, expected_sha: str, now_ms: int) -> dict[str, Any]:
    deployed = (run_root / "control" / "deployed_sha").read_text(encoding="utf-8").strip()
    if deployed != expected_sha:
        raise ValueError("runtime:deployed_sha")
    runtime = read_json(run_root / "control" / "runtime_status.json")
    require_paper_identity(runtime, expected_sha, "runtime")
    if runtime.get("version") != 7 or runtime.get("state") != "running":
        raise ValueError("runtime:not_running_v7")
    if runtime.get("killed") is not False:
        raise ValueError("runtime:killed")
    if runtime.get("economic_new_risk_ready") is not False:
        raise ValueError("runtime:unexpected_real_risk_readiness")
    runtime_ms = int(runtime.get("timestamp") or 0) * 1000
    if runtime_ms <= 0 or now_ms - runtime_ms > 180_000 or runtime_ms > now_ms + 5_000:
        raise ValueError("runtime:stale")
    selection = read_json(run_root / "micro_maker" / "reward_selection.json")
    require_paper_identity(selection, expected_sha, "selection")
    selection_ms = int(selection.get("timestamp_ms") or 0)
    if selection_ms <= 0 or now_ms - selection_ms > 5_000 or selection_ms > now_ms + 5_000:
        raise ValueError("selection:stale")
    observer = read_json(run_root / "micro_maker" / "fillability_ws_status.json")
    require_paper_identity(observer, expected_sha, "fillability_observer")
    observer_ms = int(observer.get("timestamp_ms") or 0)
    if observer.get("state") != "running" or observer.get("evidence_complete") is not True:
        raise ValueError("fillability_observer:not_complete_running")
    if int(observer.get("dropped_events") or 0) != 0 or int(observer.get("decoder_failures") or 0) != 0:
        raise ValueError("fillability_observer:evidence_loss")
    if observer_ms <= 0 or now_ms - observer_ms > 5_000 or observer_ms > now_ms + 5_000:
        raise ValueError("fillability_observer:stale")
    if not str(observer.get("observer_session_id") or ""):
        raise ValueError("fillability_observer:missing_session")
    if int(observer.get("connection_epoch") or 0) <= 0:
        raise ValueError("fillability_observer:missing_epoch")
    return {
        "runtime_status_timestamp": int(runtime.get("timestamp") or 0),
        "selection_timestamp_ms": selection_ms,
        "fillability_observer_timestamp_ms": observer_ms,
        "observer_session_id": str(observer.get("observer_session_id") or ""),
        "connection_epoch": int(observer.get("connection_epoch") or 0),
    }


def validate_policy_config(repository_root: pathlib.Path) -> dict[str, Any]:
    path = repository_root / "config" / "v7_professional_market_maker.json"
    config = read_json(path)
    if config.get("paper_only") is not True or config.get("authenticated_execution") is not False or config.get("real_order_submission") is not False:
        raise ValueError("maker_config:paper_contract")
    selection = config.get("market_selection") if isinstance(config.get("market_selection"), dict) else {}
    recent = selection.get("recent_flow") if isinstance(selection.get("recent_flow"), dict) else {}
    anchor = selection.get("settlement_anchor") if isinstance(selection.get("settlement_anchor"), dict) else {}
    if recent.get("enabled") is not True:
        raise ValueError("maker_config:recent_flow_disabled")
    if recent.get("zero_flow_execution_fallback_enabled") is not False:
        raise ValueError("maker_config:zero_flow_fallback")
    if anchor.get("enabled") is not True or anchor.get("causal_flow_authority_enabled") is not True:
        raise ValueError("maker_config:anchor_flow_disabled")
    if anchor.get("execution_authority_enabled") is not False:
        raise ValueError("maker_config:anchor_cold_start_authority")
    if anchor.get("paper_exploration_only") is not True or anchor.get("real_money_authority") is not False:
        raise ValueError("maker_config:anchor_authority_contract")
    if float(recent.get("rotation_min_projected_fill_probability") or -1) != 0.004:
        raise ValueError("maker_config:projected_fill_gate_changed")
    return {
        "path": str(path.relative_to(repository_root)), "sha256": sha256_file(path),
        "zero_flow_execution_fallback_enabled": False,
        "anchor_causal_flow_authority_enabled": True,
        "anchor_execution_authority_enabled": False,
        "rotation_min_projected_fill_probability": 0.004,
    }


def prefix_baseline(path: pathlib.Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ValueError(f"evidence_baseline:missing:{path}")
        return {"exists": False, "bytes": 0}
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"evidence_baseline:not_regular_file:{path}")
    before = path.stat()
    size = int(before.st_size)
    if required and size <= 0:
        raise ValueError(f"evidence_baseline:empty:{path}")
    digest = sha256_prefix(path, size)
    after = path.stat()
    if before.st_dev != after.st_dev or before.st_ino != after.st_ino:
        raise ValueError(f"evidence_baseline:replaced_during_hash:{path}")
    if after.st_size < size:
        raise ValueError(f"evidence_baseline:shrunk_during_hash:{path}")
    return {
        "exists": True, "bytes": size, "sha256": digest,
        "hash_semantics": PREFIX_HASH_SEMANTICS,
        "post_hash_bytes": int(after.st_size),
    }


def file_baseline(path: pathlib.Path, *, required: bool = False) -> dict[str, Any]:
    return prefix_baseline(path, required=required)


def evidence_tree_baseline(paths: Iterable[pathlib.Path]) -> dict[str, Any]:
    raw_roots = [pathlib.Path(path) for path in paths]
    if not raw_roots:
        raise ValueError("book_evidence:missing_argument")
    roots: list[pathlib.Path] = []
    for raw in raw_roots:
        if not raw.exists() or raw.is_symlink():
            raise ValueError(f"book_evidence:missing_or_symlink:{raw}")
        roots.append(raw.resolve())
    files: set[pathlib.Path] = set()
    for root in roots:
        if root.is_file():
            files.add(root)
        elif root.is_dir():
            for pattern in ("*.json", "*.jsonl", "*.jsonl.gz"):
                files.update(item for item in root.rglob(pattern) if item.is_file())
        else:
            raise ValueError(f"book_evidence:not_file_or_directory:{root}")
    unique = sorted(files)
    if not unique:
        raise ValueError("book_evidence:no_regular_files")
    tree = hashlib.sha256()
    total = 0
    entries: list[dict[str, Any]] = []
    for path in unique:
        baseline = prefix_baseline(path, required=False)
        size = int(baseline["bytes"])
        total += size
        tree.update(str(path).encode("utf-8")); tree.update(b"\0")
        tree.update(str(size).encode("ascii")); tree.update(b"\0")
        tree.update(str(baseline["sha256"]).encode("ascii")); tree.update(b"\n")
        entries.append({"path": str(path), **baseline})
    if total <= 0:
        raise ValueError("book_evidence:empty")
    return {
        "exists": True, "files": len(entries), "bytes": total,
        "tree_sha256": tree.hexdigest(), "hash_semantics": PREFIX_HASH_SEMANTICS,
        "entries": entries,
    }


def build_manifest(
    *, expected_sha: str, start_ms: int,
    repository_proof: dict[str, Any], runtime_proof: dict[str, Any],
    policy_proof: dict[str, Any], frozen_artifacts: dict[str, Any],
    evidence_baselines: dict[str, Any], experiment_id: str,
    prepared_ms: int | None = None,
) -> dict[str, Any]:
    prepared = int(start_ms - FREEZE_LEAD_MS if prepared_ms is None else prepared_ms)
    manifest: dict[str, Any] = {
        "schema": SCHEMA, "experiment_id": experiment_id, "code_sha": expected_sha,
        "window_start_ms": start_ms, "window_end_ms": start_ms + WINDOW_MS,
        "maximum_post_window_fill_ms": 60_000,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "automatic_promotion": False,
        "required_authority_basis": evaluator.REQUIRED_BASIS,
        "markout_horizons": HORIZONS,
        "evidence_sufficiency": {"minimum_independent_fill_clusters": 20, "minimum_filled_shares": 50.0},
        "single_window_positive_requirements": {
            "share_weighted_250ms_markout_strictly_positive": True,
            "cluster_bootstrap_95pct_lower_bound_strictly_positive": True,
            "canonical_final_pnl_per_filled_share_strictly_positive": True,
        },
        "cumulative_real_money_research_gate": {
            "minimum_independent_fill_clusters": 100,
            "minimum_positive_forward_windows": 3,
            "automatic_promotion": False,
        },
        "freeze_protocol": {
            "manifest_prepared_ms": prepared,
            "window_start_lead_ms": FREEZE_LEAD_MS,
            "window_start_rule": "MANIFEST_ATOMICALLY_PUBLISHED_BEFORE_WINDOW_START",
            "evidence_hash_semantics": PREFIX_HASH_SEMANTICS,
        },
        "repository_preflight": repository_proof,
        "runtime_preflight": runtime_proof,
        "policy_preflight": policy_proof,
        "frozen_artifacts": frozen_artifacts,
        "evidence_baselines": evidence_baselines,
    }
    manifest["manifest_sha256"] = evaluator.canonical_hash(manifest, "manifest_sha256")
    evaluator.validate_manifest(manifest)
    return manifest


def _write_fsync(path: pathlib.Path, content: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(content); handle.flush(); os.fsync(handle.fileno())


def prepare(
    repository_root: pathlib.Path, run_root: pathlib.Path,
    output_root: pathlib.Path, expected_sha: str, *,
    book_evidence: Iterable[pathlib.Path], now_ms: int | None = None,
) -> dict[str, Any]:
    repository_root = repository_root.resolve(); run_root = run_root.resolve(); output_root = output_root.resolve()
    fixed_clock = int(now_ms) if now_ms is not None else None
    clock = (lambda: fixed_clock) if fixed_clock is not None else wall_ms
    preflight_ms = int(clock())
    if not exact_sha(expected_sha):
        raise ValueError("expected_sha")
    repository_before = validate_repository_state(repository_root, expected_sha)
    runtime_before = validate_runtime(run_root, expected_sha, preflight_ms)
    policy_proof = validate_policy_config(repository_root)
    sources = [
        repository_root / "scripts" / "v7_maker_forward_window_evaluator.py",
        repository_root / "scripts" / "v7_maker_forward_window_evaluator_v2.py",
        repository_root / "scripts" / "v7_maker_fill_conditioned_toxicity.py",
        repository_root / "scripts" / "v7_maker_decision_time_toxicity.py",
        repository_root / "config" / "v7_professional_market_maker.json",
    ]
    for path in sources:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"frozen_artifact:not_regular:{path.name}")
    baseline_started_ms = int(clock())
    evidence_baselines = {
        "ledger_execution_jsonl": file_baseline(run_root / "ledger" / "execution.jsonl", required=True),
        "selector_events_jsonl": file_baseline(run_root / "micro_maker" / "reward_selection.events.jsonl", required=True),
        "fillability_ws_jsonl": file_baseline(run_root / "micro_maker" / "fillability_ws.jsonl", required=True),
        "causal_book_evidence": evidence_tree_baseline(book_evidence),
    }
    baseline_completed_ms = int(clock())
    evidence_baselines["capture"] = {
        "started_ms": baseline_started_ms, "completed_ms": baseline_completed_ms,
        "hash_semantics": PREFIX_HASH_SEMANTICS,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_root / f".maker-forward-freeze-{expected_sha[:12]}-{os.getpid()}-{time.time_ns()}"
    if temporary.exists():
        raise ValueError("experiment:temporary_exists")
    temporary.mkdir()
    try:
        frozen = temporary / "frozen"; frozen.mkdir(); frozen_artifacts: dict[str, Any] = {}
        for source in sources:
            destination = frozen / source.name; shutil.copy2(source, destination)
            frozen_artifacts[source.name] = {"sha256": sha256_file(destination), "bytes": destination.stat().st_size}
        before_window_check_ms = int(clock())
        repository_after = validate_repository_state(repository_root, expected_sha)
        runtime_after = validate_runtime(run_root, expected_sha, before_window_check_ms)
        repository_proof = {"before_baseline": repository_before, "before_window": repository_after}
        runtime_proof = {"before_baseline": runtime_before, "before_window": runtime_after}
        prepared_ms = int(clock()); start_ms = prepared_ms + FREEZE_LEAD_MS
        experiment_id = f"btc-m5-maker-forward-{start_ms}-{expected_sha[:12]}"; target = output_root / experiment_id
        if target.exists():
            raise ValueError("experiment:already_exists")
        manifest = build_manifest(
            expected_sha=expected_sha, start_ms=start_ms, prepared_ms=prepared_ms,
            repository_proof=repository_proof, runtime_proof=runtime_proof,
            policy_proof=policy_proof, frozen_artifacts=frozen_artifacts,
            evidence_baselines=evidence_baselines, experiment_id=experiment_id,
        )
        _write_fsync(temporary / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        _write_fsync(
            temporary / "README.txt",
            "PAPER-only frozen 8h Maker experiment. Do not inspect economic endpoints before window_end_ms.\n"
            f"code_sha={expected_sha}\nwindow_start_ms={manifest['window_start_ms']}\n"
            f"window_end_ms={manifest['window_end_ms']}\nmanifest_sha256={manifest['manifest_sha256']}\n",
        )
        if int(clock()) >= start_ms:
            raise ValueError("experiment:freeze_missed_window_start")
        os.replace(temporary, target)
        if int(clock()) >= start_ms:
            shutil.rmtree(target, ignore_errors=True)
            raise ValueError("experiment:publish_missed_window_start")
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--run-root", type=pathlib.Path, default=pathlib.Path("runs/paper_v7_live"))
    parser.add_argument("--output-root", type=pathlib.Path, default=pathlib.Path("runs/paper_v7_experiments"))
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--book-evidence", type=pathlib.Path, action="append", required=True,
                        help="Causal book evidence file/directory; repeat for multiple roots.")
    args = parser.parse_args()
    manifest = prepare(args.repository_root, args.run_root, args.output_root, args.expected_sha,
                       book_evidence=args.book_evidence)
    print(json.dumps(manifest, indent=2, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
