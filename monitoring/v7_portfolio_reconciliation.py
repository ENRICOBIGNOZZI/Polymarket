#!/usr/bin/env python3
"""Fail-closed accounting reconciliation for the canonical V7 PAPER surfaces."""
from __future__ import annotations

import math
from typing import Any

TOLERANCE_USD = 1e-6


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= TOLERANCE_USD * max(1.0, abs(left), abs(right))


def reconcile(
    *, canonical: dict[str, Any], ledger: dict[str, Any],
    portfolio: dict[str, Any], allocations: dict[str, Any],
    state_realized_pnl: dict[str, float | None],
) -> dict[str, Any]:
    reasons: list[str] = []
    budgets = allocations.get("budgets") if isinstance(allocations.get("budgets"), dict) else {}
    sleeves = portfolio.get("sleeves") if isinstance(portfolio.get("sleeves"), dict) else {}
    account = _finite(allocations.get("account_starting_capital"))
    budget_sum = sum(value for value in (_finite(raw) for raw in budgets.values()) if value is not None)
    if account is None or not budgets or not _close(account, budget_sum):
        reasons.append("allocation_sum_divergence")

    portfolio_equity = _finite(portfolio.get("equity"))
    sleeve_equities = [_finite(row.get("equity")) for row in sleeves.values() if isinstance(row, dict)]
    sleeve_equity_sum = sum(value for value in sleeve_equities if value is not None)
    if portfolio_equity is None or not sleeves or any(value is None for value in sleeve_equities):
        reasons.append("portfolio_equity_unverifiable")
    elif not _close(portfolio_equity, sleeve_equity_sum):
        reasons.append("portfolio_sleeve_equity_divergence")

    canonical_total = _finite(canonical.get("net_pnl"))
    canonical_by_strategy_raw = canonical.get("strategy_net_pnl")
    canonical_by_strategy = canonical_by_strategy_raw if isinstance(canonical_by_strategy_raw, dict) else {}
    canonical_strategy_values = {
        str(strategy): value
        for strategy, raw in canonical_by_strategy.items()
        if (value := _finite(raw)) is not None
    }
    canonical_strategy_sum = sum(canonical_strategy_values.values())
    if canonical_total is None:
        # No mature terminal observation is a valid zero-realized evidence state.
        canonical_total = 0.0
    if not _close(canonical_total, canonical_strategy_sum):
        reasons.append("canonical_strategy_sum_divergence")

    ledger_total = _finite(
        (ledger.get("total") if isinstance(ledger.get("total"), dict) else {}).get("final_pnl")
    )
    if ledger_total is None:
        reasons.append("ledger_terminal_pnl_unverifiable")
    elif not _close(ledger_total, canonical_total):
        reasons.append("ledger_canonical_terminal_pnl_divergence")

    strategy_rows: dict[str, Any] = {}
    for strategy in sorted(set(canonical_strategy_values) | set(state_realized_pnl)):
        canonical_value = canonical_strategy_values.get(strategy, 0.0)
        state_value = _finite(state_realized_pnl.get(strategy))
        matched = state_value is not None and _close(state_value, canonical_value)
        if state_value is not None and not matched:
            reasons.append(f"strategy_realized_pnl_divergence:{strategy}")
        strategy_rows[strategy] = {
            "canonical_realized_pnl": canonical_value,
            "state_realized_pnl": state_value,
            "difference": state_value - canonical_value if state_value is not None else None,
            "matched": matched,
        }

    return {
        "schema": "polymarket_v7_portfolio_reconciliation_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "state": "RECONCILED" if not reasons else "DIVERGED",
        "reconciled": not reasons,
        "tolerance_usd": TOLERANCE_USD,
        "account_starting_capital": account,
        "allocation_budget_sum": budget_sum,
        "portfolio_equity": portfolio_equity,
        "sleeve_equity_sum": sleeve_equity_sum,
        "canonical_realized_pnl": canonical_total,
        "canonical_strategy_realized_pnl_sum": canonical_strategy_sum,
        "ledger_terminal_pnl": ledger_total,
        "strategies": strategy_rows,
        "reason_codes": sorted(set(reasons)),
    }
