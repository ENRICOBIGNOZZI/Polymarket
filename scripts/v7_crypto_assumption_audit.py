#!/usr/bin/env python3
"""Classify every checked-in BTC-specific V7 occurrence for crypto migration."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_crypto_assumption_audit_v1"
PATTERN = re.compile(
    r"BTC_SETTLEMENT_ENGINE|BTC_5M|BTC_15M|BTC_4H|BTCUSDT|BTCUSD|BTC_|btc_|Bitcoin|\bBTC\b"
)


def _classification(path: str, match: str) -> tuple[str, str]:
    if path == "scripts/v7_crypto_assumption_audit.py":
        return "A_BTC_SPECIFIC", "audit vocabulary"
    if path == "AGENT_DIRECTIVE_V7_UNIFICATION_AND_LEGACY_ERADICATION.md":
        return "D_DOCUMENTATION", "historical directive"
    if match == "BTC_SETTLEMENT_ENGINE":
        return "E_STALE_LEGACY", "retired engine identity"
    if path.startswith("tests/"):
        return "C_FIXTURE_TEST", "explicit BTC regression fixture"
    if path == "README.md" or path.startswith("docs/"):
        return "D_DOCUMENTATION", "documented BTC reference context"
    if path.startswith("artifacts/"):
        return "E_STALE_LEGACY", "immutable forensic or generated evidence"
    if path in {"config/operator_directives.json", "config/v7_external_fair_rule_approvals.json"}:
        return "A_BTC_SPECIFIC", "explicit BTC policy or verified rule approval"
    btc_adapters = (
        "config/v7_crypto_", "config/v7_external_", "scripts/paper_v7_execution_loop.sh",
        "scripts/v7_crypto_", "scripts/v7_external_", "scripts/v7_contract_registry.py",
        "scripts/v7_rtds_", "scripts/v7_binance_", "scripts/v7_coinbase_",
        "scripts/v7_deribit_", "include/pm/v7_external_", "src/v7_external_",
    )
    if path.startswith(btc_adapters):
        return "A_BTC_SPECIFIC", "typed asset, BTC model, BTC source adapter, or reference rollout context"
    if path in {"schemas/v7/opportunity_envelope.schema.json", "scripts/v7_opportunity.py"}:
        return "A_BTC_SPECIFIC", "typed supported-asset enumeration"
    return "B_CRYPTO_GENERIC_HARDCODED", "requires economic-owner review"


def audit(repository_root: Path) -> dict[str, Any]:
    root = repository_root.resolve()
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root,
    ).decode("utf-8").split("\0")
    rows: list[dict[str, Any]] = []
    for relative in sorted(path for path in tracked if path):
        path = root / relative
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(lines, 1):
            for match in PATTERN.finditer(line):
                category, rationale = _classification(relative, match.group(0))
                rows.append({
                    "path": relative, "line": line_number, "token": match.group(0),
                    "category": category, "rationale": rationale,
                })
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    operational_stale = [
        row for row in rows
        if row["category"] == "E_STALE_LEGACY" and not row["path"].startswith("artifacts/")
    ]
    return {
        "schema": SCHEMA,
        "patterns": PATTERN.pattern,
        "occurrence_count": len(rows),
        "counts": dict(sorted(counts.items())),
        "operational_stale_count": len(operational_stale),
        "passed": not operational_stale,
        "occurrences": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.repository_root)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "schema", "occurrence_count", "counts", "operational_stale_count", "passed",
    )}, sort_keys=True))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
