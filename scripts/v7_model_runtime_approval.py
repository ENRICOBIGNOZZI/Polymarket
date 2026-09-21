#!/usr/bin/env python3
"""Fail-closed identity and approval checks for PAPER runtime model activation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any

MANIFEST_SCHEMA = "polymarket_v7_runtime_artifact_bundle_v1"
APPROVAL_SCHEMA = "polymarket_v7_model_runtime_approval_v1"
MAKER_SCHEMA = "polymarket_v7_maker_execution_model_v1"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
APPROVAL_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

# These fields identify provenance or the fit window, not the executable mapping.
# Everything else remains in the economic hash, intentionally conservatively.
MAKER_PROVENANCE_FIELDS = {
    "version",
    "generated_ts_ms",
    "model_sha",
    "code_sha",
    "training_source_model_shas",
    "training_window",
    "validation_window",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode()


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _safe_regular(path: Path, maximum_bytes: int = 32 * 1024 * 1024) -> Path:
    path = path.resolve()
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"unsafe or missing file:{path}")
    size = path.stat().st_size
    if size <= 0 or size > maximum_bytes:
        raise ValueError(f"file outside size bound:{path}")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    path = _safe_regular(path, 32 * 1024 * 1024)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required:{path}")
    return value


def maker_economic_hash(model: dict[str, Any]) -> str:
    if (
        model.get("schema") != MAKER_SCHEMA
        or model.get("paper_only") is not True
        or model.get("authenticated_execution") is not False
        or model.get("real_order_submission") is not False
    ):
        raise ValueError("maker model contract invalid")
    payload = {
        key: value for key, value in model.items()
        if key not in MAKER_PROVENANCE_FIELDS
    }
    return _sha256_bytes(_canonical(payload))


def economic_identity(manifest_path: Path, artifact_root: Path) -> dict[str, Any]:
    root = artifact_root.resolve()
    manifest = _load_json(manifest_path)
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("paper_only") is not True
        or manifest.get("authenticated_execution") is not False
        or manifest.get("real_order_submission") is not False
        or manifest.get("runtime_training") is not False
    ):
        raise ValueError("runtime artifact manifest contract invalid")

    maker_meta = manifest.get("maker_execution_model") or {}
    maker_rel = maker_meta.get("path")
    maker_digest = maker_meta.get("sha256")
    if not isinstance(maker_rel, str) or not maker_rel:
        raise ValueError("maker artifact path missing")
    maker_path = (root / maker_rel).resolve()
    if root != maker_path.parent and root not in maker_path.parents:
        raise ValueError("maker artifact escapes bundle")
    maker_path = _safe_regular(maker_path)
    if not isinstance(maker_digest, str) or _sha256_file(maker_path) != maker_digest:
        raise ValueError("maker artifact hash mismatch")
    maker = _load_json(maker_path)
    maker_hash = maker_economic_hash(maker)

    rich_meta = manifest.get("rich_research_model") or {}
    rich_state = str(rich_meta.get("state") or "UNAVAILABLE")
    rich_model_hash = None
    if rich_state == "AVAILABLE":
        rich_rel = rich_meta.get("path")
        rich_digest = rich_meta.get("sha256")
        if not isinstance(rich_rel, str) or not rich_rel:
            raise ValueError("rich artifact path missing")
        rich_path = (root / rich_rel).resolve()
        if root != rich_path.parent and root not in rich_path.parents:
            raise ValueError("rich artifact escapes bundle")
        rich_path = _safe_regular(rich_path)
        if not isinstance(rich_digest, str) or _sha256_file(rich_path) != rich_digest:
            raise ValueError("rich artifact hash mismatch")
        rich = _load_json(rich_path)
        rich_model_hash = rich.get("model_hash")
        if not isinstance(rich_model_hash, str) or not SHA256.fullmatch(rich_model_hash):
            # Fall back to exact artifact bytes only when the artifact does not
            # expose its own stable model identity.
            rich_model_hash = _sha256_file(rich_path)
    elif rich_state != "UNAVAILABLE":
        raise ValueError("unknown rich model state")

    payload = {
        "maker_economic_sha256": maker_hash,
        "rich_state": rich_state,
        "rich_model_hash": rich_model_hash,
    }
    return {
        **payload,
        "economic_model_identity_sha256": _sha256_bytes(_canonical(payload)),
    }


def validate_approval(
    approval_path: Path,
    manifest_path: Path,
    artifact_root: Path,
    reviewed_report: Path,
    expected_target_sha: str,
) -> dict[str, Any]:
    if not SHA40.fullmatch(expected_target_sha):
        raise ValueError("exact target SHA required")
    approval = _load_json(approval_path)
    manifest = _load_json(manifest_path)
    identity = economic_identity(manifest_path, artifact_root)
    if (
        approval.get("schema") != APPROVAL_SCHEMA
        or approval.get("version") != 1
        or approval.get("approved") is not True
        or approval.get("approval_scope") != "PAPER_RUNTIME_NEW_MODEL_ACTIVATION"
        or approval.get("explicit_user_approval_required") is not True
        or approval.get("review_backtest_before_approval") is not True
        or approval.get("automatic_promotion") is not False
        or approval.get("target_model_sha") != expected_target_sha
        or manifest.get("target_model_sha") != expected_target_sha
        or approval.get("bundle_id") != manifest.get("bundle_id")
        or approval.get("economic_model_identity_sha256")
            != identity["economic_model_identity_sha256"]
    ):
        raise ValueError("model runtime approval contract invalid")

    approval_id = approval.get("approval_id")
    if not isinstance(approval_id, str) or not APPROVAL_ID.fullmatch(approval_id):
        raise ValueError("approval id invalid")
    report_rel = approval.get("backtest_report_path")
    if not isinstance(report_rel, str) or not report_rel:
        raise ValueError("reviewed backtest path missing")
    expected_report_hash = approval.get("backtest_report_sha256")
    if (
        not isinstance(expected_report_hash, str)
        or not SHA256.fullmatch(expected_report_hash)
    ):
        raise ValueError("reviewed backtest hash invalid")
    report = _safe_regular(reviewed_report)
    actual_report_hash = _sha256_file(report)
    if actual_report_hash != expected_report_hash:
        raise ValueError("reviewed backtest bytes do not match approval")

    return {
        "approval_id": approval_id,
        "target_model_sha": expected_target_sha,
        "bundle_id": manifest.get("bundle_id"),
        "backtest_report_path": report_rel,
        "backtest_report_sha256": actual_report_hash,
        **identity,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--reviewed-report", type=Path)
    parser.add_argument("--target-sha")
    args = parser.parse_args(argv)

    if args.approval is None:
        print(json.dumps(
            economic_identity(args.manifest, args.artifact_root),
            sort_keys=True,
        ))
        return 0
    if args.reviewed_report is None or args.target_sha is None:
        parser.error("--approval requires --reviewed-report and --target-sha")
    value = validate_approval(
        args.approval,
        args.manifest,
        args.artifact_root,
        args.reviewed_report,
        args.target_sha,
    )
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
