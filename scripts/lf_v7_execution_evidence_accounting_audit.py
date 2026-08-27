#!/usr/bin/env python3
"""Audit fill/PnL observation accounting in the canonical V7 evidence sidecar.

Research-only: this does not mutate execution, allocation, risk, or operator authority.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v7_execution_evidence import row_has_realized_pnl, row_is_fill


def run_audit() -> dict[str, Any]:
    entry = {"action": "BUY", "net_pnl": "0", "timestamp": "1"}
    exit_row = {"action": "SELL", "net_pnl": "1.0", "timestamp": "2"}
    partial_entry = {"action": "FILL", "net_pnl": "0", "timestamp": "3"}

    counted_fills = sum(row_is_fill(row) for row in (entry, exit_row))
    counted_realized = sum(row_has_realized_pnl(row) for row in (entry, exit_row, partial_entry))

    return {
        "schema": "lf_v7_execution_evidence_accounting_audit_v1",
        "decision": "STRUCTURAL_EVIDENCE_ACCOUNTING_BLOCKER",
        "submission_count": 1,
        "round_trip_execution_rows": 2,
        "current_counted_fills": counted_fills,
        "current_fill_rate": counted_fills / 1,
        "expected_fill_opportunities_completed": 1,
        "entry_zero_pnl_counted_as_realized": row_has_realized_pnl(entry),
        "partial_zero_pnl_counted_as_realized": row_has_realized_pnl(partial_entry),
        "current_counted_realized_pnl_rows": counted_realized,
        "expected_terminal_realized_pnl_rows": 1,
        "implication": (
            "The sidecar can double-count a single round trip as two fills and can treat entry/partial rows "
            "with net_pnl='0' as mature realized-PnL observations, weakening min-fill/min-PnL evidence gates."
        ),
        "required_successor": [
            "define fill labels at immutable order/leg fill events only; exits/settles are not additional entry fills",
            "deduplicate fills by canonical order_id/fill_id (and bundle_id/leg_id for multi-leg evidence)",
            "count realized PnL only on explicit terminal close/unwind/settle/bundle-terminal records",
            "derive fill rate from unique submitted opportunities/orders and unique mature fills with compatible denominators",
            "keep incomplete/partial orders censored until their terminal outcome is known",
        ],
        "paper_only": True,
        "authenticated_execution": False,
    }


def main() -> int:
    print(json.dumps(run_audit(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
