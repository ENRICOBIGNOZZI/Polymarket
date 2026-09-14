#!/usr/bin/env python3
"""Freeze an exact-SHA eight-hour PAPER Maker forward experiment.

This command does not start, stop, deploy, cancel or submit anything. It is a
fail-closed preregistration step executed only after the PAPER runtime is already
healthy on the exact approved repository SHA. It snapshots the economic protocol,
source-audit evaluators, toxicity analyzers, config and evidence baselines before
the window starts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import time
from typing import Any, Iterable

import v7_maker_forward_window_evaluator as evaluator

SCHEMA = evaluator.MANIFEST_SCHEMA
HORIZONS = ["100ms", "250ms", "500ms", "1s", "5s", "10s", "30s"]
WINDOW_MS = 8 * 60 * 60 * 1000


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


def git_output(repository_root: pathlib.Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repository_root, text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ValueError("repository:git_command") from exc


def validate_repository_state(
    repository_root: pathlib.Path, expected_sha: str,
) -> dict[str, Any]:
    head = git_output(repository_root, "rev-parse", "HEAD")
    if head != expected_sha:
        raise ValueError("repository:head_sha")
    dirty = git_output(
        repository_root, "status", "--porcelain", "--untracked-files=no"
    )
    if dirty:
        raise ValueError("repository:tracked_worktree_dirty")
    origin_main = git_output(
        repository_root, "rev-parse", "--verify", "refs/remotes/origin/main"
    )
    if origin_main != expected_sha:
        raise ValueError("repository:origin_main_sha")
    return {
        "head_sha": head,
        "origin_main_sha": origin_main,
        "tracked_worktree_clean": True,
    }


def require_paper_identity(
    value: dict[str, Any], expected_sha: str, name: str,
) -> None:
    if value.get("model_sha") != expected_sha:
        raise ValueError(f"{name}:sha")
    if value.get("paper_only") is not True:
        raise ValueError(f"{name}:paper_only")
    if value.get("authenticated_execution") is not False:
        raise ValueError(f"{name}:authenticated_execution")
    if value.get("real_order_submission") is not False:
        raise ValueError(f"{name}:real_order_submission")


def validate_runtime(
    run_root: pathlib.Path, expected_sha: str, now_ms: int,
) -> dict[str, Any]:
    deployed = (run_root / "control" / "deployed_sha").read_text(
        encoding="utf-8"
    ).strip()
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
    if (
        config.get("paper_only") is not True
        or config.get("authenticated_execution") is not False
        or config.get("real_order_submission") is not False
    ):
        raise ValueError("maker_config:paper_contract")
    selection = (
        config.get("market_selection")
        if isinstance(config.get("market_selection"), dict) else {}
    )
    recent = (
        selection.get("recent_flow")
        if isinstance(selection.get("recent_flow"), dict) else {}
    )
    anchor = (
        selection.get("settlement_anchor")
        if isinstance(selection.get("settlement_anchor"), dict) else {}
    )
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
        "path": str(path.relative_to(repository_root)),
        "sha256": sha256_file(path),
        "zero_flow_execution_fallback_enabled": False,
        "anchor_causal_flow_authority_enabled": True,
        "anchor_execution_authority_enabled": False,
        "rotation_min_projected_fill_probability": 0.004,
    }


def file_baseline(path: pathlib.Path, *, required: bool = False) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ValueError(f"evidence_baseline:missing:{path}")
        return {"exists": False, "bytes": 0}
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"evidence_baseline:not_regular_file:{path}")
    return {
        "exists": True,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def evidence_tree_baseline(paths: Iterable[pathlib.Path]) -> dict[str, Any]:
    files: list[pathlib.Path] = []
    roots = [path.resolve() for path in paths]
    if not roots:
        raise ValueError("book_evidence:missing_argument")
    for root in roots:
        if not root.exists() or root.is_symlink():
            raise ValueError(f"book_evidence:missing_or_symlink:{root}")
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            for pattern in ("*.json", "*.jsonl", "*.jsonl.gz"):
                files.extend(item for item in root.rglob(pattern) if item.is_file())
        else:
            raise ValueError(f"book_evidence:not_file_or_directory:{root}")
    unique = sorted(set(files))
    if not unique:
        raise ValueError("book_evidence:no_regular_files")
    digest = hashlib.sha256()
    total = 0
    entries = []
    for path in unique:
        if path.is_symlink():
            raise ValueError(f"book_evidence:symlink:{path}")
        size = path.stat().st_size
        sha = sha256_file(path)
        total += size
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha.encode("ascii"))
        digest.update(b"\n")
        entries.append({"path": str(path), "bytes": size, "sha256": sha})
    return {
        "exists": True,
        "files": len(entries),
        "bytes": total,
        "tree_sha256": digest.hexdigest(),
        "entries": entries,
    }


def build_manifest(
    *, expected_sha: str, start_ms: int,
    repository_proof: dict[str, Any], runtime_proof: dict[str, Any],
    policy_proof: dict[str, Any], frozen_artifacts: dict[str, Any],
    evidence_baselines: dict[str, Any], experiment_id: str,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "experiment_id": experiment_id,
        "code_sha": expected_sha,
        "window_start_ms": start_ms,
        "window_end_ms": start_ms + WINDOW_MS,
        "maximum_post_window_fill_ms": 60_000,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "automatic_promotion": False,
        "required_authority_basis": evaluator.REQUIRED_BASIS,
        "markout_horizons": HORIZONS,
        "evidence_sufficiency": {
            "minimum_independent_fill_clusters": 20,
            "minimum_filled_shares": 50.0,
        },
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
        "repository_preflight": repository_proof,
        "runtime_preflight": runtime_proof,
        "policy_preflight": policy_proof,
        "frozen_artifacts": frozen_artifacts,
        "evidence_baselines": evidence_baselines,
    }
    manifest["manifest_sha256"] = evaluator.canonical_hash(
        manifest, "manifest_sha256"
    )
    evaluator.validate_manifest(manifest)
    return manifest


def prepare(
    repository_root: pathlib.Path, run_root: pathlib.Path,
    output_root: pathlib.Path, expected_sha: str, *,
    book_evidence: Iterable[pathlib.Path], now_ms: int | None = None,
) -> dict[str, Any]:
    repository_root = repository_root.resolve()
    run_root = run_root.resolve()
    output_root = output_root.resolve()
    now_ms = int(time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    if not exact_sha(expected_sha):
        raise ValueError("expected_sha")
    repository_proof = validate_repository_state(repository_root, expected_sha)
    runtime_proof = validate_runtime(run_root, expected_sha, now_ms)
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

    experiment_id = f"btc-m5-maker-forward-{now_ms}-{expected_sha[:12]}"
    target = output_root / experiment_id
    if target.exists():
        raise ValueError("experiment:already_exists")
    frozen = target / "frozen"
    frozen.mkdir(parents=True)
    frozen_artifacts: dict[str, Any] = {}
    for source in sources:
        destination = frozen / source.name
        shutil.copy2(source, destination)
        frozen_artifacts[source.name] = {
            "sha256": sha256_file(destination),
            "bytes": destination.stat().st_size,
        }

    evidence_baselines = {
        "ledger_execution_jsonl": file_baseline(
            run_root / "ledger" / "execution.jsonl", required=True
        ),
        "selector_events_jsonl": file_baseline(
            run_root / "micro_maker" / "reward_selection.events.jsonl",
            required=True,
        ),
        "fillability_ws_jsonl": file_baseline(
            run_root / "micro_maker" / "fillability_ws.jsonl", required=True
        ),
        "causal_book_evidence": evidence_tree_baseline(book_evidence),
    }
    manifest = build_manifest(
        expected_sha=expected_sha,
        start_ms=now_ms,
        repository_proof=repository_proof,
        runtime_proof=runtime_proof,
        policy_proof=policy_proof,
        frozen_artifacts=frozen_artifacts,
        evidence_baselines=evidence_baselines,
        experiment_id=experiment_id,
    )
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (target / "README.txt").write_text(
        "PAPER-only frozen 8h Maker experiment. Do not inspect economic endpoints before window_end_ms.\n"
        f"code_sha={expected_sha}\nwindow_start_ms={manifest['window_start_ms']}\n"
        f"window_end_ms={manifest['window_end_ms']}\n"
        f"manifest_sha256={manifest['manifest_sha256']}\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root", type=pathlib.Path, default=pathlib.Path(".")
    )
    parser.add_argument(
        "--run-root", type=pathlib.Path, default=pathlib.Path("runs/paper_v7_live")
    )
    parser.add_argument(
        "--output-root", type=pathlib.Path,
        default=pathlib.Path("runs/paper_v7_experiments")
    )
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument(
        "--book-evidence", type=pathlib.Path, action="append", required=True,
        help="Causal book evidence file/directory; repeat for multiple roots.",
    )
    args = parser.parse_args()
    manifest = prepare(
        args.repository_root, args.run_root, args.output_root, args.expected_sha,
        book_evidence=args.book_evidence,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
