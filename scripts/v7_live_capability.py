#!/usr/bin/env python3
"""Validate the minimal PAPER-only and pre-canary security boundary.

This module has no signing key, identity, exact-SHA binding,
or order-submission capability.  It only verifies that checked-in configuration
cannot spend capital and that the repository has no detectable historical
credential before a future authenticated transport is connected.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


class LiveCapabilityError(ValueError):
    pass


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LiveCapabilityError(f"{field}:invalid_integer")
    return value


def validate_checked_in_config(config: dict[str, Any]) -> None:
    """Require the checked-in runtime to remain unable to trade live."""
    if config.get("paper_only") is not True:
        raise LiveCapabilityError("config:paper_only_required")
    v7 = config.get("v7")
    if not isinstance(v7, dict) or v7.get("paper_only") is not True:
        raise LiveCapabilityError("config:v7_paper_only_required")
    if v7.get("authenticated_execution") is not False or v7.get("real_order_submission") is not False:
        raise LiveCapabilityError("config:authenticated_execution_forbidden")
    gate = v7.get("live_capability")
    if not isinstance(gate, dict) or gate.get("live_enabled") is not False:
        raise LiveCapabilityError("config:live_capability_not_fail_closed")
    for field in ("max_order", "max_exposure", "max_daily_loss"):
        if _nonnegative_integer(gate.get(field), f"config:{field}") != 0:
            raise LiveCapabilityError(f"config:{field}_must_be_zero")
    security = v7.get("pre_canary_security")
    if not isinstance(security, dict) or security != {
            "full_history_secret_scan_required": True,
            "findings_must_equal": 0,
            "remediation_evidence_required": True}:
        raise LiveCapabilityError("config:pre_canary_security_contract")


def validate_pre_canary_security(repository_root: Path) -> dict[str, Any]:
    """Require a fresh redacted full-history secret scan for a future canary."""
    scanner_path = Path(__file__).with_name("v7_secret_scan.py")
    spec = importlib.util.spec_from_file_location("v7_secret_scan_pre_canary", scanner_path)
    if spec is None or spec.loader is None:
        raise LiveCapabilityError("pre_canary_security:scanner_unavailable")
    scanner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scanner
    spec.loader.exec_module(scanner)
    result = scanner.report(Path(repository_root), include_history=True)
    if (result.get("history_scanned") is not True or result.get("finding_count") != 0
            or result.get("safe_for_authenticated_execution") is not True):
        raise LiveCapabilityError("pre_canary_security:secret_scan_not_clean")
    return result


def validate_pre_canary(config_path: Path, *, repository_root: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    validate_checked_in_config(config)
    return validate_pre_canary_security(repository_root)


def security_summary(scan: dict[str, Any]) -> dict[str, Any]:
    if scan.get("safe_for_authenticated_execution") is not True:
        raise LiveCapabilityError("security_summary:scan_not_clean")
    return {
        "schema": "polymarket_v7_pre_canary_security_summary_v1",
        "paper_only": True,
        "full_history_secret_scan_clean": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        rendered = json.dumps(security_summary(validate_pre_canary(
            args.config, repository_root=args.repository_root)), sort_keys=True, indent=2) + "\n"
    except (OSError, json.JSONDecodeError, LiveCapabilityError) as exc:
        rendered = json.dumps({"schema": "polymarket_v7_pre_canary_security_summary_v1",
                               "state": "PRE_CANARY_BLOCKED", "reason": str(exc)}, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 1
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
