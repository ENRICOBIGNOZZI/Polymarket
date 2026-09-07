#!/usr/bin/env python3
"""Forward PAPER attribution for CRYPTO_SETTLEMENT_ENGINE.

Expected attribution and realized cash PnL are deliberately reported in
separate namespaces. The report never retrofits historical PnL into model
components that were not frozen before the order.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "polymarket_v7_crypto_pnl_attribution_report_v1"
DECISION_SCHEMA = "polymarket_v7_crypto_execution_alpha_decision_v1"
CORE_ATTRIBUTION = (
    "settlement_alpha", "spread_capture", "rebate", "fees", "slippage",
    "adverse_selection", "latency", "inventory", "unwind", "cancel", "capital",
)
TAKER_EXPECTED_ATTRIBUTION = (
    "settlement_alpha", "crossing_and_spread", "fees", "execution_latency_risk",
)
NATIVE_REALIZED_COMPONENTS = (
    "trading_pnl", "spread_capture", "adverse_markout", "inventory_pnl",
    "maker_rebates", "liquidity_rewards",
)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def sum_map(target: dict[str, float], source: dict[str, Any], names: Iterable[str]) -> None:
    for name in names:
        target[name] += finite(source.get(name), 0.0)


def decision_surface(path: Path) -> dict[str, Any]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    raw_rows = 0
    for row in iter_jsonl(path):
        if row.get("schema") != DECISION_SCHEMA:
            continue
        raw_rows += 1
        report = row.get("report") if isinstance(row.get("report"), dict) else {}
        market_id = str(report.get("market_id") or "")
        snapshot = str(report.get("source_snapshot_identity") or "")
        if market_id and snapshot:
            unique[(market_id, snapshot)] = row
    actions: Counter[str] = Counter()
    attribution = defaultdict(float)
    point_gap = 0.0
    information_recommendations = 0
    positive_selected = 0
    for row in unique.values():
        report = row["report"]
        selected = report.get("selected_action") if isinstance(report.get("selected_action"), dict) else {}
        action = str(selected.get("action") or "UNKNOWN")
        actions[action] += 1
        if finite(selected.get("conservative_expected_wealth_change"), 0.0) > 0.0:
            positive_selected += 1
        selected_attr = selected.get("attribution") if isinstance(selected.get("attribution"), dict) else {}
        sum_map(attribution, selected_attr, CORE_ATTRIBUTION)
        point_gap += max(
            0.0,
            finite((report.get("best_point_action") or {}).get("point_expected_wealth_change"), 0.0)
            - finite(selected.get("conservative_expected_wealth_change"), 0.0),
        )
        if report.get("maker_information_probe_recommended") is True:
            information_recommendations += 1
    total = sum(attribution[name] for name in CORE_ATTRIBUTION)
    return {
        "raw_decision_rows": raw_rows,
        "unique_causal_snapshots": len(unique),
        "selected_action_counts": dict(sorted(actions.items())),
        "positive_conservative_action_snapshots": positive_selected,
        "maker_information_probe_recommendations": information_recommendations,
        "sum_expected_attribution_over_unique_snapshots": dict(attribution),
        "sum_expected_wealth_change_over_unique_snapshots": total,
        "sum_point_minus_conservative_value_gap": point_gap,
        "warning": "snapshot sums are an opportunity-surface statistic, not realized PnL",
    }


def executed_paper(ledger_path: Path) -> dict[str, Any]:
    finals: list[dict[str, Any]] = []
    fill_fees = 0.0
    fill_count = 0
    for row in iter_jsonl(ledger_path):
        if row.get("paper_only") is not True or row.get("strategy") not in {
            "CRYPTO_INFORMED_TAKER", "PROFESSIONAL_MAKER",
        }:
            continue
        if row.get("event_type") == "FILL":
            fill_count += 1
            fill_fees += max(0.0, finite(row.get("fee"), 0.0))
        elif row.get("event_type") == "FINAL" and row.get("complete") is not False:
            finals.append(row)
    realized_pnl = 0.0
    native = defaultdict(float)
    expected = defaultdict(float)
    point = defaultdict(float)
    attributed_finals = 0
    expected_identity_failures: list[str] = []
    actions: Counter[str] = Counter()
    markets: set[str] = set()
    for row in finals:
        realized_pnl += finite(row.get("final_pnl"), 0.0)
        markets.add(str(row.get("market_id") or ""))
        action = str(row.get("intended_action") or "UNKNOWN")
        actions[action] += 1
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        decomposition = metadata.get("pnl_decomposition") if isinstance(metadata.get("pnl_decomposition"), dict) else {}
        sum_map(native, decomposition, NATIVE_REALIZED_COMPONENTS)
        frozen = metadata.get("expected_pnl_attribution") if isinstance(metadata.get("expected_pnl_attribution"), dict) else None
        point_frozen = metadata.get("point_pnl_attribution") if isinstance(metadata.get("point_pnl_attribution"), dict) else None
        if frozen is not None:
            attributed_finals += 1
            sum_map(expected, frozen, TAKER_EXPECTED_ATTRIBUTION)
            expected_total = sum(finite(frozen.get(name), 0.0) for name in TAKER_EXPECTED_ATTRIBUTION)
            declared = finite(frozen.get("total_expected_wealth_change"), math.nan)
            if not math.isfinite(declared) or abs(expected_total - declared) > 1e-9 or frozen.get("identity_verified") is not True:
                expected_identity_failures.append(str(row.get("record_id") or row.get("candidate_id") or "unknown"))
        if point_frozen is not None:
            sum_map(point, point_frozen, TAKER_EXPECTED_ATTRIBUTION)
    expected_total = sum(expected[name] for name in TAKER_EXPECTED_ATTRIBUTION)
    point_total = sum(point[name] for name in TAKER_EXPECTED_ATTRIBUTION)
    return {
        "terminal_positions": len(finals),
        "independent_markets": len({market for market in markets if market}),
        "terminal_action_counts": dict(sorted(actions.items())),
        "realized_cash_pnl": realized_pnl,
        "native_realized_pnl_decomposition": dict(native),
        "observed_fill_count": fill_count,
        "observed_fees": fill_fees,
        "fees_already_embedded_in_realized_cash_pnl": True,
        "terminal_positions_with_frozen_expected_attribution": attributed_finals,
        "terminal_positions_without_frozen_expected_attribution": len(finals) - attributed_finals,
        "frozen_conservative_expected_attribution": dict(expected),
        "frozen_conservative_expected_wealth_change": expected_total,
        "frozen_point_expected_attribution": dict(point),
        "frozen_point_expected_wealth_change": point_total,
        "realized_minus_frozen_conservative_expected": (
            realized_pnl - expected_total if attributed_finals == len(finals) and finals else None
        ),
        "expected_attribution_identity_failures": expected_identity_failures,
        "historical_unattributed_residual_policy": (
            "do not infer component attribution for terminal positions created before the fields existed"
        ),
    }


def build(decisions: Path, ledger: Path) -> dict[str, Any]:
    surface = decision_surface(decisions)
    executed = executed_paper(ledger)
    return {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "decision_surface": surface,
        "executed_paper": executed,
        "separation_contract": {
            "expected_opportunity_surface_is_realized_pnl": False,
            "frozen_expected_attribution_is_realized_pnl": False,
            "canonical_final_cash_pnl_is_realized_pnl": True,
            "retroactive_component_imputation": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build(args.decisions, args.ledger)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "realized_cash_pnl": report["executed_paper"]["realized_cash_pnl"],
        "terminal_positions": report["executed_paper"]["terminal_positions"],
        "attributed_finals": report["executed_paper"]["terminal_positions_with_frozen_expected_attribution"],
        "unique_causal_snapshots": report["decision_surface"]["unique_causal_snapshots"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
