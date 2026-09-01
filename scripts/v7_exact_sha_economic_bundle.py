#!/usr/bin/env python3
"""Create a sealed exact-SHA External Fair economic-truth bundle.

The bundle partitions current, historical and mixed-SHA evidence.  A dirty
worktree or a historical runtime can still be audited, but can never be labelled
as current exact-SHA economic evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
from typing import Any, Iterable

try:
    from v7_execution_latency_distribution import build_latency_report
    from v7_external_economic_common import (
        atomic_json, canonical_sha256, file_sha256, load_counterfactual_evidence,
    )
    from v7_external_loss_attribution import build_attribution
    from v7_external_policy_replay import build_replay
except ModuleNotFoundError:
    from scripts.v7_execution_latency_distribution import build_latency_report
    from scripts.v7_external_economic_common import (
        atomic_json, canonical_sha256, file_sha256, load_counterfactual_evidence,
    )
    from scripts.v7_external_loss_attribution import build_attribution
    from scripts.v7_external_policy_replay import build_replay


SCHEMA = "polymarket_v7_exact_sha_economic_bundle_v1"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL,
    ).strip()


def repository_identity(repo: Path) -> dict[str, Any]:
    head = _git(repo, "rev-parse", "HEAD")
    porcelain = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    changes = [line for line in porcelain.splitlines() if line]
    return {
        "head": head,
        "dirty": bool(changes),
        "worktree_change_count": len(changes),
        "worktree_change_digest": canonical_sha256(changes),
        "exact_committed_code_identity": not changes,
    }


def _json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _file_identity(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    value = _json(path)
    return {
        "path": str(path.resolve()),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
        "canonical_json_sha256": canonical_sha256(value) if value else None,
    }


def seal(bundle: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(bundle)
    sealed.pop("bundle_sha256", None)
    sealed["bundle_sha256"] = canonical_sha256(sealed)
    return sealed


def verify_seal(bundle: dict[str, Any]) -> bool:
    claimed = str(bundle.get("bundle_sha256") or "")
    unsigned = dict(bundle)
    unsigned.pop("bundle_sha256", None)
    return len(claimed) == 64 and claimed == canonical_sha256(unsigned)


def build_bundle(
    *, rows: list[dict[str, Any]], quality: dict[str, Any], repository: dict[str, Any],
    config: dict[str, Any] | None = None, config_identity: dict[str, Any] | None = None,
    model_artifacts: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    head = str(repository["head"])
    config = config if isinstance(config, dict) else {}
    policy = config.get("taker") if isinstance(config.get("taker"), dict) else {}
    latency_risk = policy.get("latency_risk_per_second", 0.001)
    try:
        latency_risk = max(0.0, float(latency_risk))
    except (TypeError, ValueError, OverflowError):
        latency_risk = 0.001
    attribution = build_attribution(rows, quality, head, policy)
    latency = build_latency_report(rows, quality, head)
    replay = build_replay(
        rows, quality, head, latency_risk_per_second=latency_risk,
    )
    lineage_counts = attribution["summary"]["lineage_states"]
    exact_terminal = int(attribution["summary"]["exact_sha_terminal_trades"])
    terminal = int(attribution["summary"]["terminal_trades"])
    if quality.get("fail_closed"):
        state = "INVALID_SOURCE_EVIDENCE"
    elif repository.get("dirty"):
        state = "DIRTY_WORKTREE_HISTORICAL_AUDIT_ONLY"
    elif exact_terminal > 0:
        state = "EXACT_SHA_EVIDENCE_AVAILABLE_WITH_PARTITIONED_HISTORY"
    else:
        state = "HISTORICAL_ONLY_NO_CURRENT_SHA_TERMINAL_EVIDENCE"
    row_policy_hashes = sorted({str(row.get("policy_sha256") or "") for row in rows} - {""})
    row_model_versions = sorted({str(row.get("model_version") or "") for row in rows} - {""})
    row_execution_shas = sorted({str(row.get("model_sha") or "") for row in rows} - {""})
    bundle: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at_unix_ms": int(time.time() * 1000),
        "evidence_state": state,
        "repository": repository,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "safety": {
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "automatic_promotion": False,
        },
        "identity": {
            "config": config_identity,
            "configured_policy_sha256": canonical_sha256(config) if config else None,
            "observed_policy_sha256": row_policy_hashes,
            "observed_model_versions": row_model_versions,
            "observed_execution_shas": row_execution_shas,
            "model_artifacts": list(model_artifacts),
            "raw_tapes": quality.get("input_manifests", []),
        },
        "source_quality": quality,
        "lineage_partition": {
            "terminal_trades": terminal,
            "exact_sha_terminal_trades": exact_terminal,
            "historical_terminal_trades": int(lineage_counts.get("HISTORICAL", 0)),
            "mixed_sha_terminal_trades": int(lineage_counts.get("MIXED_SHA", 0)),
            "incomplete_lifecycles": int(lineage_counts.get("INCOMPLETE", 0)),
            "historical_artifacts_may_be_presented_as_current": False,
        },
        "loss_attribution": attribution,
        "execution_latency_distribution": latency,
        "policy_replay": replay,
        "economic_claims": {
            "current_head_profitability_proven": False,
            "real_money_readiness_proven": False,
            "shadow_pnl_is_real_pnl": False,
            "exact_sha_status": state,
        },
        "verification": {
            "pnl_reconstructible_from_trade_rows": True,
            "missing_causal_fields_explicit": True,
            "raw_tapes_content_addressed": True,
            "bundle_is_signable": True,
            "signature_present": False,
        },
    }
    return seal(bundle)


def generate(
    inputs: Iterable[Path], repo: Path, output: Path, config_path: Path | None = None,
    model_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    rows, quality = load_counterfactual_evidence(inputs)
    config = _json(config_path)
    model_identities = [
        identity for identity in (_file_identity(path) for path in model_paths)
        if identity is not None
    ]
    bundle = build_bundle(
        rows=rows,
        quality=quality,
        repository=repository_identity(repo),
        config=config,
        config_identity=_file_identity(config_path),
        model_artifacts=model_identities,
    )
    atomic_json(output, bundle)
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, default=[])
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, default=Path("config/v7_external_fair.json"))
    parser.add_argument("--model-artifact", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify is not None:
        value = _json(args.verify)
        print("valid" if verify_seal(value) else "invalid")
        return 0 if verify_seal(value) else 2
    if not args.input or args.output is None:
        parser.error("--input and --output are required unless --verify is used")
    bundle = generate(
        args.input, args.repo.resolve(), args.output, args.config, args.model_artifact,
    )
    print(json.dumps({
        "evidence_state": bundle["evidence_state"],
        "bundle_sha256": bundle["bundle_sha256"],
        "terminal_trades": bundle["lineage_partition"]["terminal_trades"],
    }, indent=2, sort_keys=True))
    return 2 if bundle["source_quality"]["fail_closed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
