#!/usr/bin/env python3
"""Exact PAPER PnL attribution for the unified crypto execution stack.

The accounting identity is non-negotiable: every terminal dollar is assigned to
one named component.  Evidence that is not present in the canonical row is not
reconstructed or guessed; the residual is assigned to ``other`` and reported as
unattributed economic mass.  This makes the report useful immediately while
creating a precise incentive for producers to emit richer causal attribution.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "polymarket_v7_crypto_pnl_attribution_v1"
COMPONENTS = (
    "settlement_alpha",
    "spread_capture",
    "rebates_rewards",
    "adverse_selection",
    "fees",
    "slippage",
    "latency",
    "inventory",
    "unwind",
    "other",
)
TERMINAL_EVENTS = {"FINAL", "VIRTUAL_FINAL", "INVENTORY_MERGE"}


class AttributionError(ValueError):
    pass


def finite(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def realized_pnl(row: dict[str, Any]) -> float | None:
    for name in (
        "final_pnl", "realized_pnl", "counterfactual_pnl", "realized_cashflow",
        "pnl_usd",
    ):
        value = finite(row.get(name))
        if value is not None:
            return value
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for name in ("final_pnl", "realized_pnl", "counterfactual_pnl", "pnl_usd"):
        value = finite(metadata.get(name))
        if value is not None:
            return value
    return None


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    value = metadata.get("pnl_attribution")
    if value is None:
        value = row.get("pnl_attribution")
    return value if isinstance(value, dict) else {}


def attribute_terminal(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict) or str(row.get("event_type") or "").upper() not in TERMINAL_EVENTS:
        return None
    if row.get("paper_only") is not True or row.get("authenticated_execution") is not False:
        return None
    total = realized_pnl(row)
    if total is None:
        return None
    payload = _payload(row)
    unknown = sorted(set(payload) - set(COMPONENTS))
    if unknown:
        raise AttributionError("unknown_components:" + ",".join(unknown))
    components = {name: 0.0 for name in COMPONENTS}
    for name in COMPONENTS:
        if name == "other":
            continue
        if name in payload:
            value = finite(payload[name])
            if value is None:
                raise AttributionError(f"nonfinite_component:{name}")
            components[name] = value
    explicit_other = finite(payload.get("other"), 0.0) or 0.0
    known = sum(components[name] for name in COMPONENTS if name != "other")
    # The residual is authoritative accounting, not an economic guess.  If a
    # producer supplies an explicit ``other`` value we preserve it only insofar
    # as the final identity still closes; the remaining residual is also other.
    components["other"] = explicit_other + (total - known - explicit_other)
    reconstructed = sum(components.values())
    if abs(reconstructed - total) > 1e-9 * max(1.0, abs(total)):
        raise AttributionError("pnl_identity_failed")
    return {
        "record_id": str(row.get("record_id") or ""),
        "strategy": str(row.get("strategy") or "UNKNOWN").upper(),
        "market_id": str(row.get("market_id") or "UNKNOWN"),
        "event_id": str(row.get("event_id") or "UNKNOWN"),
        "action": str(row.get("intended_action") or (_payload(row).get("action") or "UNKNOWN")).upper(),
        "realized_pnl": total,
        "components": components,
        "fully_attributed": abs(components["other"]) <= 1e-12,
    }


def _add(target: dict[str, float], components: dict[str, float]) -> None:
    for name in COMPONENTS:
        target[name] = target.get(name, 0.0) + float(components[name])


def aggregate(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    by_strategy: dict[str, dict[str, float]] = defaultdict(dict)
    by_market: dict[str, dict[str, float]] = defaultdict(dict)
    totals = {name: 0.0 for name in COMPONENTS}
    realized = 0.0
    duplicates = 0
    for row in rows:
        attributed = attribute_terminal(row)
        if attributed is None:
            continue
        identity = attributed["record_id"] or json.dumps(
            row, sort_keys=True, separators=(",", ":")
        )
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        records.append(attributed)
        realized += attributed["realized_pnl"]
        _add(totals, attributed["components"])
        _add(by_strategy[attributed["strategy"]], attributed["components"])
        _add(by_market[attributed["market_id"]], attributed["components"])
    gross_terminal_pnl = sum(abs(row["realized_pnl"]) for row in records)
    other_abs = sum(abs(row["components"]["other"]) for row in records)
    identity_error = realized - sum(totals.values())
    if abs(identity_error) > 1e-9 * max(1.0, abs(realized)):
        raise AttributionError("aggregate_pnl_identity_failed")
    return {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "terminal_records": len(records),
        "duplicates_removed": duplicates,
        "realized_pnl": realized,
        "components": totals,
        "identity_error": identity_error,
        "unattributed_absolute_pnl": other_abs,
        "unattributed_fraction_of_gross_terminal_pnl": (
            other_abs / gross_terminal_pnl if gross_terminal_pnl > 0.0 else 0.0
        ),
        "by_strategy": {key: value for key, value in sorted(by_strategy.items())},
        "by_market": {key: value for key, value in sorted(by_market.items())},
        "records": records,
    }


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AttributionError(f"invalid_jsonl:{line_number}") from exc
            if isinstance(value, dict):
                yield value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = aggregate(iter_jsonl(args.ledger))
    atomic_json(args.output, report)
    print(json.dumps({
        "terminal_records": report["terminal_records"],
        "realized_pnl": report["realized_pnl"],
        "unattributed_fraction": report["unattributed_fraction_of_gross_terminal_pnl"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
