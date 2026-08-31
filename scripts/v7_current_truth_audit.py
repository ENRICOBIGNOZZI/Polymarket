#!/usr/bin/env python3
"""Regenerate the redacted V7 current-truth audit from the local checkout.

The audit is deliberately read-only. It records hashes and redacted secret-scan
findings, never matched credential values or raw private account evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SOURCE_PATHS = (
    "config/paper_v7.json",
    "config/v7_execution_modes.json",
    "config/v7_live_caps_zero.json",
    "config/v7_platform_contract.json",
    "scripts/v7_real_pnl_verifier.py",
    "scripts/v7_security_audit.py",
)


class CurrentTruthError(ValueError):
    pass


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secret_scanner() -> Any:
    name = "v7_secret_scan_for_current_truth"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("v7_secret_scan.py"))
    if spec is None or spec.loader is None:
        raise CurrentTruthError("secret_scanner_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _security_auditor() -> Any:
    name = "v7_security_audit_for_current_truth"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("v7_security_audit.py"))
    if spec is None or spec.loader is None:
        raise CurrentTruthError("security_auditor_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def audit(root: Path, *, now: datetime | None = None, secret_report: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    missing = [relative for relative in SOURCE_PATHS if not (root / relative).is_file()]
    if missing:
        raise CurrentTruthError(f"source_missing:{missing[0]}")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise CurrentTruthError("audit_timestamp_timezone_required")
    head = _git(root, "rev-parse", "HEAD")
    if head is None:
        raise CurrentTruthError("git_head_unavailable")
    origin_main = _git(root, "rev-parse", "origin/main")
    status = _git(root, "status", "--porcelain")
    secrets = secret_report if secret_report is not None else _secret_scanner().report(root, include_history=True)
    if not isinstance(secrets, dict) or not isinstance(secrets.get("finding_count"), int):
        raise CurrentTruthError("secret_report_invalid")
    try:
        security_audit = _security_auditor().audit(root, now=now, secret_report=secrets)
    except ValueError as exc:
        raise CurrentTruthError(f"security_audit_invalid:{exc}") from exc
    paper = json.loads((root / "config/paper_v7.json").read_text(encoding="utf-8"))
    v7 = paper.get("v7") if isinstance(paper, dict) else None
    if not isinstance(v7, dict):
        raise CurrentTruthError("paper_v7_invalid")
    source_hashes = {relative: _sha256(root / relative) for relative in SOURCE_PATHS}
    return {
        "schema_version": 1,
        "kind": "V7_CURRENT_TRUTH_AUDIT",
        "audit_timestamp": now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "audit_anchor_sha": "3138c84a361533ae9519ecaa4013dff3f6d77c54",
        "starting_head_sha": head,
        "origin_main_sha": origin_main,
        "working_tree_clean": status == "",
        "execution_mode": paper.get("execution_mode"),
        "live_authority": {
            "paper_only": paper.get("paper_only") is True,
            "authenticated_execution": v7.get("authenticated_execution") is True,
            "real_order_submission": v7.get("real_order_submission") is True,
            "checked_in_live_caps_zero": all(limit == 0 for key, limit in json.loads(
                (root / "config/v7_live_caps_zero.json").read_text(encoding="utf-8")
            ).items() if str(key).startswith("maximum_")),
        },
        "claims": {
            "IMPLEMENTATION_COMPLETE": False, "TECHNICAL_VALIDATION_COMPLETE": False,
            "EVIDENCE_COLLECTION_ACTIVE": False, "LIVE_CANARY_COMPLETE": False,
            "ECONOMIC_EVIDENCE_SUFFICIENT": False, "REAL_PNL_VERIFIED": False,
            "WORLD_CLASS_CANDIDATE": False, "MORE_EVIDENCE_REQUIRED": True,
            "PROFITABILITY_NOT_TESTABLE": True,
        },
        "security": {
            "history_secret_scan_findings": secrets["finding_count"],
            "secret_value_recorded": False,
            "safe_for_authenticated_execution": security_audit["authenticated_execution_allowed"],
            "audit_state": security_audit["state"],
            "external_controls": security_audit["external_controls"],
        },
        "limitations": [
            "Credential rotation/revocation evidence is unavailable.",
            "No authenticated account, wallet, CLOB private-state, Polygon, or real-order evidence was accessed.",
            "GitHub protection and Actions retention cannot be verified from this checkout.",
            "This audit records only redacted secret-scan fingerprints and locations.",
        ],
        "commands": [
            "git rev-parse HEAD origin/main", "git status --porcelain",
            "python3 scripts/v7_secret_scan.py --repository-root . --history --fail-on-findings",
        ],
        "source_paths": list(SOURCE_PATHS), "source_sha256": source_hashes,
    }


def markdown(value: dict[str, Any]) -> str:
    return "\n".join((
        "# V7 current-truth audit", "",
        f"Audit timestamp: `{value['audit_timestamp']}`  ",
        f"Audit anchor: `{value['audit_anchor_sha']}`  ",
        f"Current HEAD: `{value['starting_head_sha']}`", "",
        "This is a redacted evidence record, not a readiness claim.", "",
        "## Honest claim state", "",
        "```text", *(f"{key} = {str(flag).upper()}" for key, flag in value["claims"].items()), "```", "",
        "## Security", "",
        f"Historical redacting scan findings: `{value['security']['history_secret_scan_findings']}`.",
        f"Security audit state: `{value['security']['audit_state']}`.",
        "No secret values are included in this artifact.", "",
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--json-output", type=Path, default=Path("artifacts/v7_world_class/current_truth.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("docs/v7_world_class/current_truth.md"))
    args = parser.parse_args()
    value = audit(args.root)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown_output.write_text(markdown(value), encoding="utf-8")
    print(json.dumps({"kind": value["kind"], "audit_timestamp": value["audit_timestamp"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
