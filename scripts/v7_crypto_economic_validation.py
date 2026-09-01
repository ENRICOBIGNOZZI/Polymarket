#!/usr/bin/env python3
"""Independent fail-closed economics gate for each crypto settlement context."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from v7_crypto_settlement import load_registry


SCHEMA = "polymarket_v7_crypto_economic_validation_v1"
MINIMUM_TERMINAL_UNITS = 300
MINIMUM_DAY_BLOCKS = 30
REQUIRED_FIELDS = {
    "settlement_labeled_contracts", "days", "terminal_economic_units",
    "day_block_lcb_pnl", "net_pnl_2x_costs", "costs_complete",
    "observed_drawdown", "executable_capacity_usd", "capital_hours",
    "calibration_stable", "regime_stratified", "source_health_stratified",
    "brier_score", "log_loss", "maker_reach", "fill_given_reach",
    "fill_conditioned_markout", "taker_fill_fraction", "latency_decay",
    "net_pnl",
}


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def assess(registry_path: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    contexts = load_registry(registry_path)
    evidence_rows = evidence.get("contexts") if isinstance(evidence.get("contexts"), dict) else {}
    output: dict[str, Any] = {}
    for context in sorted(contexts.values(), key=lambda row: row.context_id):
        row = evidence_rows.get(context.context_id)
        blockers: list[str] = []
        if not isinstance(row, dict):
            row = {}
            blockers.append("EVIDENCE_MISSING")
        missing = sorted(REQUIRED_FIELDS - set(row))
        if missing:
            blockers.append("FIELDS_MISSING:" + ",".join(missing))
        numeric = {name: _finite(row.get(name)) for name in REQUIRED_FIELDS if name not in {
            "costs_complete", "calibration_stable", "regime_stratified",
            "source_health_stratified",
        }}
        if any(value is None for value in numeric.values()):
            blockers.append("NONFINITE_ECONOMICS")
        if (numeric.get("terminal_economic_units") or 0) < MINIMUM_TERMINAL_UNITS:
            blockers.append("TERMINAL_UNITS_LT_300")
        if (numeric.get("days") or 0) < MINIMUM_DAY_BLOCKS:
            blockers.append("INDEPENDENT_DAY_BLOCKS_LT_30")
        if (numeric.get("day_block_lcb_pnl") or 0) <= 0:
            blockers.append("DAY_BLOCK_95PCT_LCB_NOT_POSITIVE")
        if (numeric.get("net_pnl_2x_costs") or 0) <= 0:
            blockers.append("TWO_X_COST_PNL_NOT_POSITIVE")
        if row.get("costs_complete") is not True:
            blockers.append("COSTS_INCOMPLETE")
        if (numeric.get("observed_drawdown") or 0) < 0:
            blockers.append("DRAWDOWN_INVALID")
        if (numeric.get("executable_capacity_usd") or 0) <= 0:
            blockers.append("CAPACITY_UNOBSERVED")
        if (numeric.get("capital_hours") or 0) <= 0:
            blockers.append("CAPITAL_HOURS_UNOBSERVED")
        for field, reason in (
            ("calibration_stable", "CALIBRATION_UNSTABLE"),
            ("regime_stratified", "REGIME_STRATIFICATION_MISSING"),
            ("source_health_stratified", "SOURCE_HEALTH_STRATIFICATION_MISSING"),
        ):
            if row.get(field) is not True:
                blockers.append(reason)
        blockers = sorted(set(blockers))
        output[context.context_id] = {
            "asset": context.asset.value, "horizon": context.horizon.value,
            "contract_family": context.contract_family,
            "settlement_semantic_hash": context.settlement_semantic_hash,
            "authority": context.authority, "research_only": context.research_only,
            "economically_ready": not blockers,
            "new_risk_authorized": False,
            "automatic_promotion": False,
            "recommendation": (
                "OPERATOR_PROMOTION_REVIEW_REQUIRED" if not blockers
                else "MORE_EVIDENCE_REQUIRED"
            ),
            "blocking_reasons": blockers,
            "evidence": row,
        }
    return {
        "schema": SCHEMA, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "automatic_promotion": False, "context_count": len(output),
        "contexts": output,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path("config/v7_crypto_settlement_markets.json"))
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"evidence_unreadable:{exc}")
    print(json.dumps(assess(args.registry, evidence), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
