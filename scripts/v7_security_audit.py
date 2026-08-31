#!/usr/bin/env python3
"""Produce a redacted, fail-closed V7 pre-canary security audit.

This program deliberately has no credential inputs.  It combines the full-history
secret scanner with the checked-in zero-live-cap contract and records only scanner
fingerprints and locations.  Rotation, revocation, hosted-repository governance,
and any private operational controls remain explicitly unverified unless separate
external evidence is supplied to a review process.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_security_audit_v1"
LIVE_CAPS_PATH = "config/v7_live_caps_zero.json"
SCANNER_PATH = "scripts/v7_secret_scan.py"


class SecurityAuditError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _scanner() -> Any:
    name = "v7_secret_scan_for_security_audit"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("v7_secret_scan.py"))
    if spec is None or spec.loader is None:
        raise SecurityAuditError("secret_scanner_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _redacted_scan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("history_scanned") is not True:
        raise SecurityAuditError("secret_scan_history_required")
    findings = value.get("findings")
    count = value.get("finding_count")
    if not isinstance(findings, list) or isinstance(count, bool) or not isinstance(count, int) or count != len(findings):
        raise SecurityAuditError("secret_scan_shape")
    redacted: list[dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"kind", "location", "fingerprint"}:
            raise SecurityAuditError("secret_scan_finding_shape")
        if any(not isinstance(finding[key], str) or not finding[key] for key in finding):
            raise SecurityAuditError("secret_scan_finding_value")
        redacted.append({key: finding[key] for key in ("kind", "location", "fingerprint")})
    return {"finding_count": count, "findings": redacted}


def _zero_live_caps(path: Path) -> bool:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecurityAuditError("live_caps_unreadable") from exc
    if not isinstance(raw, dict) or raw.get("live_enabled") is not False:
        return False
    caps = [value for key, value in raw.items() if str(key).startswith("maximum_")]
    return bool(caps) and all(not isinstance(value, bool) and isinstance(value, int) and value == 0 for value in caps)


def audit(root: Path, *, now: datetime | None = None, secret_report: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    caps_path = root / LIVE_CAPS_PATH
    scanner_path = root / SCANNER_PATH
    if not caps_path.is_file() or not scanner_path.is_file():
        raise SecurityAuditError("required_security_source_missing")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise SecurityAuditError("audit_timestamp_timezone_required")
    scan = _redacted_scan(secret_report if secret_report is not None else _scanner().report(root, include_history=True))
    caps_zero = _zero_live_caps(caps_path)
    finding_free = scan["finding_count"] == 0
    # These controls cannot be proven from a public checkout.  Never infer them
    # from a green scanner or a safe checked-in default.
    external_controls = {
        "credential_rotation_and_revocation_evidence": "UNVERIFIED_EXTERNAL",
        "github_protection_and_signed_release_evidence": "UNVERIFIED_EXTERNAL",
        "private_operational_configuration_evidence": "UNVERIFIED_EXTERNAL",
    }
    reasons: list[str] = []
    if not finding_free:
        reasons.append("historical_or_worktree_secret_scan_findings")
    if not caps_zero:
        reasons.append("checked_in_live_caps_not_zero")
    reasons.extend(sorted(key for key, value in external_controls.items() if value != "VERIFIED"))
    return {
        "schema": SCHEMA,
        "audit_timestamp": now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "inputs": {
            "secret_scan": scan,
            "live_caps_sha256": _sha256(caps_path),
            "secret_scanner_sha256": _sha256(scanner_path),
        },
        "checked_in_live_caps_zero": caps_zero,
        "external_controls": external_controls,
        "reason_codes": reasons,
        "authenticated_execution_allowed": False,
        "state": "SECURITY_BLOCKED" if not finding_free or not caps_zero else "MORE_EVIDENCE_REQUIRED",
        "commands": [
            "python3 scripts/v7_secret_scan.py --repository-root . --history --fail-on-findings",
            "python3 scripts/v7_security_audit.py --repository-root .",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.repository_root)
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
