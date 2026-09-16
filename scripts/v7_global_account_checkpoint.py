#!/usr/bin/env python3
"""Build a fail-closed flat-account checkpoint for shared PAPER reservations.

This is a read-only reconciler. It issues no execution authority and never
writes the canonical ledger. The first shared-reservation checkpoint is emitted
only when every pre-existing lane is economically flat and canonical terminal
PnL reconciles to component cash. Unknown/open legacy exposure blocks creation.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from v7_execution_ledger import EconomicJournalEntry, LedgerEvent, iter_records
from v7_lead_lag_replay import ZERO, ReplayError, decimal, digest

SHA40 = re.compile(r"^[0-9a-f]{40}$")
ENGINES = ("CRYPTO_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE")


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_state(value: Mapping[str, Any]) -> bool:
    return (value.get("paper_only") is True
            and value.get("authenticated_execution") is False
            and value.get("real_order_submission") is False
            and value.get("real_capital_at_risk") in (None, False))


def finite(value: Any, reason: str, *, nonnegative: bool = False) -> Decimal:
    result = decimal(value)
    if nonnegative and result < ZERO:
        raise ReplayError(reason)
    return result


def allocation(path: Path) -> dict[str, Any]:
    value = load(path)
    budgets = value.get("engine_budgets") if isinstance(value.get("engine_budgets"), dict) else {}
    if (value.get("schema") != "polymarket_v7_capital_allocation_v3"
            or not safe_state(value)
            or value.get("capital_authority_owner") != "V7_CANONICAL_ALLOCATOR"
            or value.get("capital_authority_owner_count") != 1
            or set(budgets) != set(ENGINES)):
        raise ReplayError("GLOBAL_CHECKPOINT_ALLOCATION_INVALID")
    account = finite(value.get("account_starting_capital"), "GLOBAL_CHECKPOINT_ACCOUNT_INVALID", nonnegative=True)
    reserve = finite(value.get("reserve_budget"), "GLOBAL_CHECKPOINT_RESERVE_INVALID", nonnegative=True)
    engine = {name: finite(budgets[name], "GLOBAL_CHECKPOINT_ENGINE_BUDGET_INVALID", nonnegative=True)
              for name in ENGINES}
    if abs(sum(engine.values(), reserve) - account) > Decimal("0.000001"):
        raise ReplayError("GLOBAL_CHECKPOINT_ALLOCATION_SUM_MISMATCH")
    return {"account": account, "reserve": reserve, "engine": engine, "raw": value}


def external_fair_flat(status: Mapping[str, Any], *, expected_sha: str,
                       expected_start: Decimal) -> dict[str, Decimal]:
    account = status.get("paper_exploration_account") if isinstance(status.get("paper_exploration_account"), dict) else {}
    if (status.get("schema") != "polymarket_v7_crypto_settlement_engine_status_v1"
            or not safe_state(status) or not safe_state(account)
            or account.get("schema") != "polymarket_v7_paper_exploration_account_v1"
            or account.get("complete") is not True
            or account.get("model_sha") != expected_sha
            or int(account.get("open_positions") or 0) != 0
            or int(account.get("pending_maker_orders") or 0) != 0
            or (account.get("issues") or []) != []
            or (account.get("invalid_spool_records") or []) != []):
        raise ReplayError("GLOBAL_CHECKPOINT_EXTERNAL_FAIR_NOT_FLAT_OR_UNRECONCILED")
    starting = finite(account.get("starting_capital"), "GLOBAL_CHECKPOINT_EXTERNAL_START_INVALID", nonnegative=True)
    cash = finite(account.get("cash"), "GLOBAL_CHECKPOINT_EXTERNAL_CASH_INVALID", nonnegative=True)
    realized = finite(account.get("realized_pnl"), "GLOBAL_CHECKPOINT_EXTERNAL_PNL_INVALID")
    if abs(starting - expected_start) > Decimal("0.000001"):
        raise ReplayError("GLOBAL_CHECKPOINT_EXTERNAL_START_BUDGET_MISMATCH")
    if abs(cash - starting - realized) > Decimal("0.000001"):
        raise ReplayError("GLOBAL_CHECKPOINT_EXTERNAL_CASH_IDENTITY_MISMATCH")
    return {"starting": starting, "cash": cash, "realized": realized}


def lead_lag_flat(status: Mapping[str, Any], state: Mapping[str, Any], *, expected_sha: str) -> dict[str, Decimal]:
    if not status and not state:
        return {"cash_adjustment": ZERO, "realized": ZERO}
    if (status.get("schema") != "polymarket_v7_lead_lag_taker_v1_status"
            or not safe_state(status)
            or status.get("model_sha") != expected_sha
            or state.get("model_sha") != expected_sha
            or status.get("protocol_hash") != state.get("protocol_hash")):
        raise ReplayError("GLOBAL_CHECKPOINT_LEAD_LAG_IDENTITY_INVALID")
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else None
    if positions is None:
        raise ReplayError("GLOBAL_CHECKPOINT_LEAD_LAG_POSITIONS_MISSING")
    entries = int(status.get("entries") or 0); settled = int(status.get("settled") or 0)
    open_positions = int(status.get("open_positions") or 0)
    if entries != settled or open_positions != 0 or entries != len(positions):
        raise ReplayError("GLOBAL_CHECKPOINT_LEAD_LAG_NOT_FLAT")
    realized = finite(status.get("realized_pnl"), "GLOBAL_CHECKPOINT_LEAD_LAG_PNL_INVALID")
    state_realized = finite(state.get("realized_pnl"), "GLOBAL_CHECKPOINT_LEAD_LAG_STATE_PNL_INVALID")
    terminal = ZERO
    for position_id, row in positions.items():
        if (not isinstance(position_id, str) or not position_id or not isinstance(row, dict)
                or row.get("settled") is not True
                or row.get("protocol_hash") != status.get("protocol_hash")):
            raise ReplayError("GLOBAL_CHECKPOINT_LEAD_LAG_POSITION_INVALID")
        terminal += finite(row.get("final_pnl"), "GLOBAL_CHECKPOINT_LEAD_LAG_FINAL_PNL_INVALID")
    tolerance = Decimal("0.000001")
    if abs(realized - state_realized) > tolerance or abs(realized - terminal) > tolerance:
        raise ReplayError("GLOBAL_CHECKPOINT_LEAD_LAG_PNL_MISMATCH")
    return {"cash_adjustment": realized, "realized": realized}


def structural_flat(status: Mapping[str, Any], *, budget: Decimal) -> dict[str, Decimal]:
    if not status:
        return {"cash": budget, "realized": ZERO}
    if not safe_state(status):
        raise ReplayError("GLOBAL_CHECKPOINT_STRUCTURAL_UNSAFE")
    open_positions = status.get("open_positions")
    if open_positions not in (None, 0):
        raise ReplayError("GLOBAL_CHECKPOINT_STRUCTURAL_NOT_FLAT")
    realized = finite(status.get("realized_pnl_total", 0.0), "GLOBAL_CHECKPOINT_STRUCTURAL_PNL_INVALID")
    equity = finite(status.get("equity_cost_basis"), "GLOBAL_CHECKPOINT_STRUCTURAL_EQUITY_INVALID", nonnegative=True)
    if abs(equity - budget - realized) > Decimal("0.000001"):
        raise ReplayError("GLOBAL_CHECKPOINT_STRUCTURAL_CASH_IDENTITY_MISMATCH")
    return {"cash": equity, "realized": realized}


def canonical_terminal_pnl(ledger: Path, *, expected_sha: str) -> tuple[Decimal, int, int]:
    total = ZERO; finals = 0; journals = 0
    for record in iter_records(ledger):
        if isinstance(record, EconomicJournalEntry):
            journals += 1
            continue
        if record.model_sha != expected_sha:
            raise ReplayError("GLOBAL_CHECKPOINT_LEDGER_MIXED_SHA")
        if record.event_type == "FINAL":
            total += finite(record.final_pnl, "GLOBAL_CHECKPOINT_FINAL_PNL_INVALID")
            finals += 1
    return total, finals, journals


def build(*, run_root: Path, allocation_manifest: Path, ledger_snapshot: Path,
          expected_sha: str) -> dict[str, Any]:
    if not SHA40.fullmatch(expected_sha):
        raise ReplayError("GLOBAL_CHECKPOINT_EXACT_SHA_REQUIRED")
    run_root = Path(run_root); allocation_manifest = Path(allocation_manifest); ledger_snapshot = Path(ledger_snapshot)
    alloc = allocation(allocation_manifest)
    external_path = run_root / "external_fair/paper_router_status.json"
    lead_status_path = run_root / "research/lead_lag_taker_v1/status.json"
    lead_state_path = run_root / "research/lead_lag_taker_v1/state.json"
    structural_path = run_root / "hard_arb/status.json"
    canonical_path = run_root / "canonical_economics.json"
    external = external_fair_flat(load(external_path), expected_sha=expected_sha,
                                  expected_start=alloc["engine"]["CRYPTO_SETTLEMENT_ENGINE"])
    lead = lead_lag_flat(load(lead_status_path), load(lead_state_path), expected_sha=expected_sha)
    structural = structural_flat(load(structural_path), budget=alloc["engine"]["STRUCTURAL_ARB_ENGINE"])
    canonical = load(canonical_path)
    if not canonical or canonical.get("paper_only") is not True or canonical.get("authenticated_execution") is not False:
        raise ReplayError("GLOBAL_CHECKPOINT_CANONICAL_ECONOMICS_INVALID")
    canonical_pnl = finite(canonical.get("net_pnl"), "GLOBAL_CHECKPOINT_CANONICAL_PNL_INVALID")
    ledger_pnl, final_count, journal_count = canonical_terminal_pnl(ledger_snapshot, expected_sha=expected_sha)
    component_pnl = external["realized"] + lead["realized"] + structural["realized"]
    tolerance = Decimal("0.000001")
    if abs(component_pnl - canonical_pnl) > tolerance or abs(ledger_pnl - canonical_pnl) > tolerance:
        raise ReplayError("GLOBAL_CHECKPOINT_TERMINAL_PNL_RECONCILIATION_FAILED")
    cash = alloc["reserve"] + external["cash"] + lead["cash_adjustment"] + structural["cash"]
    expected_cash = alloc["account"] + canonical_pnl
    if abs(cash - expected_cash) > tolerance:
        raise ReplayError("GLOBAL_CHECKPOINT_GLOBAL_CASH_IDENTITY_MISMATCH")
    sources = {}
    for name, path in (("allocation", allocation_manifest), ("external_fair", external_path),
                       ("lead_lag_status", lead_status_path), ("lead_lag_state", lead_state_path),
                       ("canonical_economics", canonical_path), ("ledger", ledger_snapshot)):
        if path.is_file():
            sources[name] = {"path": str(path), "sha256": file_hash(path), "bytes": path.stat().st_size}
    if structural_path.is_file():
        sources["structural"] = {"path": str(structural_path), "sha256": file_hash(structural_path),
                                  "bytes": structural_path.stat().st_size}
    payload = {
        "schema": "polymarket_v7_global_account_checkpoint_v1", "code_sha": expected_sha,
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "real_capital_at_risk": False, "automatic_promotion": False, "entry_authority": False,
        "whole_portfolio_reconciled": True, "all_preexisting_lanes_flat": True,
        "currency": "USDC", "account_starting_capital": str(alloc["account"]),
        "available_cash": str(cash), "external_exposures": [],
        "canonical_terminal_pnl": str(canonical_pnl), "ledger_terminal_pnl": str(ledger_pnl),
        "terminal_final_count": final_count, "economic_journal_count": journal_count,
        "components": {
            "reserve_cash": str(alloc["reserve"]),
            "external_fair_cash": str(external["cash"]),
            "lead_lag_cash_adjustment": str(lead["cash_adjustment"]),
            "structural_cash": str(structural["cash"]),
        },
        "sources": sources,
        "limitations": [
            "Checkpoint creation is intentionally blocked while any pre-existing lane is open or unreconciled.",
            "A subsequent foreign monetary event invalidates this checkpoint for new reservations until rebuilt.",
            "This checkpoint is PAPER accounting evidence, not real wallet collateral evidence.",
        ],
    }
    payload["checkpoint_id"] = digest(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--allocation-manifest", type=Path, required=True)
    parser.add_argument("--ledger-snapshot", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or "ledger" in args.output.parts:
        parser.exit(2, "refusing overwrite or canonical ledger destination\n")
    try:
        report = build(run_root=args.run_root, allocation_manifest=args.allocation_manifest,
                       ledger_snapshot=args.ledger_snapshot, expected_sha=args.expected_sha)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True, indent=2, allow_nan=False); handle.write("\n")
        print(json.dumps({"checkpoint_id": report["checkpoint_id"], "available_cash": report["available_cash"],
                          "whole_portfolio_reconciled": True, "entry_authority": False}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"global account checkpoint failed closed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
