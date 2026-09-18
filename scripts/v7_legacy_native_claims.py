#!/usr/bin/env python3
"""Account-level PAPER reserve for unresolved native claims from older run roots.

This is cold-plane accounting only. It never creates orders and never imports
old fills into the current ledger. The current manager subtracts the resulting
claim from its own engine envelope until the source ledger contains a canonical
FINAL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from v7_native_risk_policy import unsettled_exposure

SCHEMA = "polymarket_v7_legacy_native_claims_v1"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
RUN_PREFIX = "paper_v7_london_"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _records(path: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not path.is_file():
        return grouped
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"legacy_ledger_invalid_json:{path}:{number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"legacy_ledger_invalid_record:{path}:{number}")
            if row.get("event_type") not in {"FILL", "FINAL"}:
                continue
            if row.get("strategy") != "CRYPTO_SETTLEMENT_ENGINE":
                continue
            if row.get("paper_only") is not True or row.get("authenticated_execution") is not False:
                raise ValueError(f"legacy_ledger_unsafe_record:{path}:{number}")
            sha = str(row.get("model_sha") or "")
            if not SHA40.fullmatch(sha):
                raise ValueError(f"legacy_ledger_sha_invalid:{path}:{number}")
            grouped.setdefault(sha, []).append(row)
    return grouped


def _candidate_roots(parent: Path, current: Path) -> list[Path]:
    roots: set[Path] = set()
    if not parent.is_dir():
        return []
    for child in parent.iterdir():
        if child.resolve() == current:
            continue
        if child.is_dir() and child.name.startswith(RUN_PREFIX) and not child.name.endswith("_archives"):
            roots.add(child.resolve())
        elif child.is_dir() and child.name.endswith("_archives"):
            for archived in child.iterdir():
                if archived.is_dir() and (archived / "ledger/execution.jsonl").is_file():
                    roots.add(archived.resolve())
    return sorted(roots)


def build_registry(*, current_run_root: Path, target_sha: str, scan_parent: Path) -> dict[str, Any]:
    if not SHA40.fullmatch(target_sha):
        raise ValueError("legacy_target_sha_invalid")
    current = current_run_root.resolve()
    sources: list[dict[str, Any]] = []
    total = 0
    for root in _candidate_roots(scan_parent.resolve(), current):
        ledger = root / "ledger/execution.jsonl"
        if not ledger.is_file():
            continue
        grouped = _records(ledger)
        source_claims: list[dict[str, Any]] = []
        source_total = 0
        for sha, rows in sorted(grouped.items()):
            closed: set[str] = set()
            unresolved_fills: list[dict[str, Any]] = []
            for row in rows:
                market = str(row.get("market_id") or "")
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                is_native_final = (
                    row.get("event_type") == "FINAL"
                    and metadata.get("native_market_settlement_id") == f"native-settlement:{market}"
                )
                if is_native_final:
                    if market in closed:
                        raise ValueError(f"legacy_duplicate_native_final:{root}:{market}")
                    closed.add(market)
                    continue
                if row.get("event_type") == "FILL":
                    if market in closed:
                        raise ValueError(f"legacy_fill_after_native_final:{root}:{market}")
                    unresolved_fills.append(row)
            unresolved_fills = [
                row for row in unresolved_fills
                if str(row.get("market_id") or "") not in closed
            ]
            exposure = unsettled_exposure(unresolved_fills, sha)
            for market, claim in sorted(exposure["unsettled_market_claims_microdollars"].items()):
                if not isinstance(claim, int) or isinstance(claim, bool) or claim <= 0:
                    raise ValueError("legacy_claim_invalid")
                source_claims.append({
                    "model_sha": sha,
                    "market_id": market,
                    "claim_microdollars": claim,
                })
                source_total += claim
        if not source_claims:
            continue
        sources.append({
            "source_run_root": str(root),
            "ledger_path": str(ledger),
            "ledger_sha256": _sha256(ledger),
            "claims": source_claims,
            "total_claim_microdollars": source_total,
        })
        total += source_total
    body = {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "target_sha": target_sha,
        "current_run_root": str(current),
        "total_claim_microdollars": total,
        "sources": sources,
        "release_policy": "SOURCE_CANONICAL_FINAL_OBSERVED_THEN_RESCAN",
        "automatic_capital_increase": False,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {**body, "registry_sha256": hashlib.sha256(encoded).hexdigest()}


def validate_registry(value: dict[str, Any], *, target_sha: str) -> dict[str, Any]:
    if (
        value.get("schema") != SCHEMA
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("execution_authority") is not False
        or value.get("target_sha") != target_sha
        or value.get("automatic_capital_increase") is not False
        or value.get("release_policy") != "SOURCE_CANONICAL_FINAL_OBSERVED_THEN_RESCAN"
    ):
        raise ValueError("legacy_claim_registry_contract")
    sources = value.get("sources")
    total = value.get("total_claim_microdollars")
    if not isinstance(sources, list) or not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("legacy_claim_registry_shape")
    computed = 0
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("legacy_claim_source_shape")
        ledger_hash = str(source.get("ledger_sha256") or "")
        claims = source.get("claims")
        source_total = source.get("total_claim_microdollars")
        if len(ledger_hash) != 64 or any(ch not in "0123456789abcdef" for ch in ledger_hash):
            raise ValueError("legacy_claim_ledger_hash")
        if not isinstance(claims, list) or not isinstance(source_total, int) or isinstance(source_total, bool):
            raise ValueError("legacy_claim_source_shape")
        subtotal = 0
        seen: set[tuple[str, str]] = set()
        for claim in claims:
            if not isinstance(claim, dict):
                raise ValueError("legacy_claim_shape")
            sha = str(claim.get("model_sha") or "")
            market = str(claim.get("market_id") or "")
            amount = claim.get("claim_microdollars")
            if (
                not SHA40.fullmatch(sha)
                or not market
                or not isinstance(amount, int)
                or isinstance(amount, bool)
                or amount <= 0
                or (sha, market) in seen
            ):
                raise ValueError("legacy_claim_invalid")
            seen.add((sha, market))
            subtotal += amount
        if subtotal != source_total:
            raise ValueError("legacy_claim_source_total_mismatch")
        computed += subtotal
    if computed != total:
        raise ValueError("legacy_claim_total_mismatch")
    body = {k: v for k, v in value.items() if k != "registry_sha256"}
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    if value.get("registry_sha256") != expected:
        raise ValueError("legacy_claim_registry_hash_mismatch")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-run-root", type=Path, required=True)
    parser.add_argument("--scan-parent", type=Path, required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registry = build_registry(
        current_run_root=args.current_run_root,
        target_sha=args.target_sha,
        scan_parent=args.scan_parent,
    )
    validate_registry(registry, target_sha=args.target_sha)
    atomic_json(args.output, registry)
    print(json.dumps({
        "schema": registry["schema"],
        "total_claim_microdollars": registry["total_claim_microdollars"],
        "source_count": len(registry["sources"]),
        "registry_sha256": registry["registry_sha256"],
        "output": str(args.output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
