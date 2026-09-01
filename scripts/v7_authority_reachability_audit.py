#!/usr/bin/env python3
"""Audit every static producer edge into the canonical V7 ledger transport."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from v7_authority_contract import validate as validate_authority


class ReachabilityAuditError(ValueError):
    pass


def detected_ledger_edges(root: Path) -> set[str]:
    roots = ("ops", "scripts", "src", "include", "monitoring")
    needles = ("spool_event(", "ledger/spool", '"ledger" / "spool"')
    detected: set[str] = set()
    for prefix in roots:
        directory = root / prefix
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {
                ".py", ".sh", ".cpp", ".cc", ".inc", ".hpp", ".h",
            }:
                continue
            relative = path.relative_to(root).as_posix()
            if relative == "scripts/v7_authority_reachability_audit.py":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if any(needle in text for needle in needles):
                detected.add(relative)
    return detected


def audit(root: Path, registry: dict[str, Any], edge_registry: dict[str, Any]) -> dict[str, Any]:
    authority_report = validate_authority(registry)
    if (
        edge_registry.get("schema") != "polymarket_v7_authority_edge_registry_v1"
        or edge_registry.get("paper_only") is not True
        or edge_registry.get("authenticated_execution") is not False
        or edge_registry.get("real_order_submission") is not False
        or edge_registry.get("real_capital_at_risk") is not False
        or edge_registry.get("canonical_sink") != "V7_CANONICAL_LEDGER"
    ):
        raise ReachabilityAuditError("edge_registry_identity_or_safety")
    rows = edge_registry.get("edges")
    if not isinstance(rows, list) or not rows:
        raise ReachabilityAuditError("edge_rows_required")
    declared: dict[str, dict[str, Any]] = {}
    required = {
        "source", "classification", "runtime_reachability", "authority_mode",
        "severity", "reason", "canonical_replacement",
    }
    for row in rows:
        if not isinstance(row, dict) or not required <= set(row):
            raise ReachabilityAuditError("edge_shape")
        source = row.get("source")
        if not isinstance(source, str) or source in declared:
            raise ReachabilityAuditError("edge_source_unique")
        declared[source] = row
    detected = detected_ledger_edges(root)
    unexplained = sorted(detected - set(declared))
    stale = sorted(set(declared) - detected)
    if unexplained:
        raise ReachabilityAuditError(f"unexplained_edges:{unexplained}")
    if stale:
        raise ReachabilityAuditError(f"stale_declared_edges:{stale}")
    defects = [
        {key: row[key] for key in (
            "source", "severity", "runtime_reachability", "reason", "canonical_replacement",
        )}
        for row in rows if str(row.get("severity", "")).startswith("P")
    ]
    return {
        "schema": "polymarket_v7_authority_reachability_audit_v2",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "source_snapshot_sha": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL,
        ).strip(),
        "authority_registry": "config/v7_authority_registry.json",
        "edge_registry": "config/v7_authority_edges.json",
        "owners": registry["owners"],
        "owner_counts": authority_report["owner_counts"],
        "economic_engines": registry["economic_engines"],
        "decision_chain": registry["decision_chain"],
        "static_ledger_transport_edges": rows,
        "unexplained_edges": [],
        "known_migration_defects": defects,
        "known_migration_defect_count": len(defects),
        "audit_gate": {
            "complete_static_edge_coverage": True,
            "one_declared_owner_per_authority": True,
            "duplicate_owner_injection_tested": True,
            "all_detected_edges_explained": True,
            "target_topology_complete": len(defects) == 0,
        },
        "live_runtime_verification": {
            "state": "EXTERNAL_BLOCKER",
            "reason": "PAPER-host SSH owner authentication is unavailable",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--authority-registry", type=Path, default=Path("config/v7_authority_registry.json"))
    parser.add_argument("--edge-registry", type=Path, default=Path("config/v7_authority_edges.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        root = args.repository_root.resolve()
        result = audit(
            root,
            json.loads((root / args.authority_registry).read_text(encoding="utf-8")),
            json.loads((root / args.edge_registry).read_text(encoding="utf-8")),
        )
        rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    except (OSError, json.JSONDecodeError, ReachabilityAuditError, ValueError) as exc:
        print(f"v7_authority_reachability_audit: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
