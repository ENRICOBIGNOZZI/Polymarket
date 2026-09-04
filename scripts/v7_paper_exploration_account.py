#!/usr/bin/env python3
"""Canonical exact-SHA accounting for bounded V7 PAPER exploration.

This process owns no venue connection, signature, OMS, capital allocation or
canonical ledger file descriptor. It consumes already-authorized simulated
PAPER lifecycle facts, writes deterministic recovery facts through the existing
single-writer spool, and reconstructs cash, inventory, settlement payout and
PnL from the canonical ledger plus its undrained spool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from v7_execution_ledger import (
    EconomicJournalEntry,
    LedgerContractError,
    LedgerEvent,
    canonical_ledger_path,
    iter_records,
)
from v7_ledger_spool import spool_event


STRATEGY = "CRYPTO_INFORMED_TAKER"
MODEL_VERSION = "external-fair-structural-v7-paper"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
TERMINAL_ORDER_STATES = frozenset({"CANCELLED", "EXPIRED", "REJECTED", "NONFILL"})
CAUSAL_PRIORITY = {
    "ORDER_SUBMITTED": 10,
    "ORDER_STATE": 20,
    "FILL": 30,
    "POSITION_MARK": 40,
    "MARKOUT": 50,
    "FINAL": 60,
}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def stable_id(*parts: Any) -> str:
    return hashlib.sha256(
        "|".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()[:32]


def _finite(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _canonical_bytes(event: LedgerEvent) -> str:
    return json.dumps(
        event.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def canonical_paper_exploration_event(event: LedgerEvent) -> bool:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    return bool(
        event.strategy.upper() == STRATEGY
        and metadata.get("paper_exploration") is True
        and metadata.get("economic_authority") == "PAPER_EXPLORATION"
        and metadata.get("counterfactual") is False
        and metadata.get("excluded_from_portfolio_equity") is False
        and metadata.get("research_evidence_only") is False
    )


def canonical_and_spooled_events(
    run_root: Path, model_sha: str
) -> tuple[list[LedgerEvent], dict[str, Any]]:
    """Load exact-SHA canonical and pending facts without double counting."""
    if SHA40.fullmatch(model_sha) is None:
        raise ValueError("exact model SHA required")
    root = Path(run_root)
    by_record_id: dict[str, LedgerEvent] = {}
    canonical_payloads: dict[str, str] = {}
    conflicts: list[str] = []
    invalid_spool: list[str] = []
    canonical_rows = 0
    spooled_rows = 0

    ledger = canonical_ledger_path(root)
    if ledger.is_file():
        for record in iter_records(ledger):
            if isinstance(record, EconomicJournalEntry):
                continue
            if record.model_sha != model_sha:
                continue
            rendered = _canonical_bytes(record)
            prior = canonical_payloads.get(record.record_id)
            if prior is not None and prior != rendered:
                conflicts.append(f"canonical_record_id_conflict:{record.record_id}")
                continue
            canonical_payloads[record.record_id] = rendered
            by_record_id.setdefault(record.record_id, record)
            canonical_rows += 1

    spool = root / "ledger" / "spool"
    for item in sorted(spool.glob("*.json")) if spool.is_dir() else []:
        try:
            raw = json.loads(item.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("record_kind") == "ECONOMIC_JOURNAL":
                continue
            record = LedgerEvent.from_dict(raw)
        except (OSError, json.JSONDecodeError, LedgerContractError, TypeError):
            invalid_spool.append(item.name)
            continue
        if record.model_sha != model_sha:
            continue
        rendered = _canonical_bytes(record)
        prior = canonical_payloads.get(record.record_id)
        if prior is not None and prior != rendered:
            conflicts.append(f"record_id_conflict:{record.record_id}")
            continue
        canonical_payloads[record.record_id] = rendered
        by_record_id.setdefault(record.record_id, record)
        spooled_rows += 1

    events = sorted(
        by_record_id.values(),
        key=lambda event: (
            int(event.recorded_ts_ms),
            CAUSAL_PRIORITY.get(event.event_type, 35),
            event.record_id,
        ),
    )
    return events, {
        "canonical_rows": canonical_rows,
        "spooled_rows": spooled_rows,
        "invalid_spool_records": sorted(set(invalid_spool)),
        "record_id_conflicts": sorted(set(conflicts)),
    }


def _complete_jsonl_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        raw = path.read_bytes()
    except OSError:
        return [], []
    boundary = raw.rfind(b"\n")
    complete = raw[: boundary + 1] if boundary >= 0 else b""
    rows: list[dict[str, Any]] = []
    invalid: list[str] = []
    for index, line in enumerate(complete.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            invalid.append(f"{path}:{index}")
            continue
        if isinstance(value, dict):
            rows.append(value)
        else:
            invalid.append(f"{path}:{index}")
    return rows, invalid


def evidence_paths(run_root: Path) -> list[Path]:
    root = Path(run_root)
    family = root.parent if root.name == "paper_v7_live" else root
    paths = [
        root / "external_fair" / "counterfactuals.jsonl",
        family / "paper_v7_durable" / "external_fair" / "counterfactuals.jsonl",
    ]
    archive_root = family / "paper_v7_archives"
    if archive_root.is_dir():
        paths.extend(sorted(
            archive_root.glob("cutover-*/external_fair/counterfactuals.jsonl")
        ))
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def counterfactual_records(
    run_root: Path, model_sha: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    payloads: dict[str, str] = {}
    invalid: list[str] = []
    conflicts: list[str] = []
    for path in evidence_paths(run_root):
        rows, bad = _complete_jsonl_rows(path)
        invalid.extend(bad)
        for row in rows:
            if row.get("model_sha") != model_sha:
                continue
            record_id = str(row.get("record_id") or "")
            if not record_id:
                invalid.append(f"{path}:missing_record_id")
                continue
            try:
                rendered = json.dumps(
                    row, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
            except (TypeError, ValueError):
                invalid.append(f"{path}:{record_id}:not_canonical_json")
                continue
            prior = payloads.get(record_id)
            if prior is not None and prior != rendered:
                conflicts.append(f"counterfactual_record_id_conflict:{record_id}")
                continue
            payloads[record_id] = rendered
            records.setdefault(record_id, row)
    return records, {
        "invalid_counterfactual_records": sorted(set(invalid)),
        "counterfactual_record_id_conflicts": sorted(set(conflicts)),
    }


def _bind(
    bucket: dict[str, LedgerEvent],
    key: str | None,
    event: LedgerEvent,
    label: str,
    issues: list[str],
) -> None:
    identity = str(key or "")
    if not identity:
        issues.append(f"{label}_identity_missing:{event.record_id}")
        return
    prior = bucket.get(identity)
    if prior is not None and prior.record_id != event.record_id:
        issues.append(f"duplicate_{label}:{identity}")
        return
    bucket[identity] = event


def canonical_nonfill_record_id(model_sha: str, order_id: str) -> str:
    return stable_id("PAPER_EXPLORATION_CANONICAL_NONFILL_V1", model_sha, order_id)


def canonical_final_record_id(model_sha: str, fill_id: str) -> str:
    return stable_id("PAPER_EXPLORATION_CANONICAL_FINAL_V1", model_sha, fill_id)


def reconcile_orphan_orders(
    run_root: Path,
    model_sha: str,
    *,
    current_ms: int | None = None,
    orphan_grace_ms: int = 5_000,
) -> dict[str, Any]:
    """Close a crash-stranded simulated FAK order without inventing a fill."""
    current = now_ms() if current_ms is None else int(current_ms)
    grace = max(1, int(orphan_grace_ms))
    events, transport = canonical_and_spooled_events(run_root, model_sha)
    orders: dict[str, LedgerEvent] = {}
    filled_orders: set[str] = set()
    terminal_orders: dict[str, LedgerEvent] = {}
    issues: list[str] = []
    existing_ids = {event.record_id for event in events}

    for event in events:
        if not canonical_paper_exploration_event(event):
            continue
        if event.event_type == "ORDER_SUBMITTED":
            _bind(orders, event.order_id, event, "order", issues)
        elif event.event_type == "FILL" and event.order_id:
            filled_orders.add(event.order_id)
        elif (
            event.event_type == "ORDER_STATE"
            and event.complete is True
            and str(event.order_state or "").upper() in TERMINAL_ORDER_STATES
        ):
            _bind(
                terminal_orders, event.order_id, event, "order_terminal", issues
            )

    pending: list[str] = []
    unresolved: list[str] = []
    spooled = 0
    for order_id, order in sorted(orders.items()):
        if order_id in filled_orders or order_id in terminal_orders:
            continue
        if current - int(order.recorded_ts_ms) < grace:
            pending.append(order_id)
            continue
        record_id = canonical_nonfill_record_id(model_sha, order_id)
        if record_id in existing_ids:
            unresolved.append(order_id)
            continue
        metadata = dict(order.metadata)
        metadata.update({
            "paper_exploration": True,
            "economic_authority": "PAPER_EXPLORATION",
            "counterfactual": False,
            "excluded_from_portfolio_equity": False,
            "research_evidence_only": False,
            "recovered_after_process_interruption": True,
            "no_fill_fabricated": True,
            "terminal_id": f"paper-exploration:{order_id}:nonfill",
        })
        recovered = LedgerEvent(
            event_type="ORDER_STATE",
            strategy=order.strategy,
            model_sha=model_sha,
            model_version=order.model_version,
            record_id=record_id,
            recorded_ts_ms=int(order.recorded_ts_ms) + grace,
            candidate_id=order.candidate_id,
            order_id=order_id,
            position_id=order.position_id,
            market_id=order.market_id,
            event_id=order.event_id,
            token_id=order.token_id,
            side=order.side,
            intended_action=order.intended_action or "TAKE",
            intended_size=order.intended_size,
            order_state="NONFILL",
            complete=True,
            cancel_reason="PAPER_EXPLORATION_PROCESS_INTERRUPTED_BEFORE_FILL",
            metadata=metadata,
        )
        spool_event(run_root, recovered)
        existing_ids.add(record_id)
        terminal_orders[order_id] = recovered
        spooled += 1

    for order_id in terminal_orders:
        if order_id not in orders:
            issues.append(f"terminal_without_order:{order_id}")
        if order_id in filled_orders:
            issues.append(f"filled_order_has_nonfill_terminal:{order_id}")
    unresolved.extend(
        order_id for order_id in orders
        if order_id not in filled_orders
        and order_id not in terminal_orders
        and order_id not in pending
    )
    blockers = (
        issues
        + transport["invalid_spool_records"]
        + transport["record_id_conflicts"]
        + [f"unresolved_order:{value}" for value in sorted(set(unresolved))]
    )
    return {
        "schema": "polymarket_v7_paper_exploration_order_reconciliation_v1",
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "orders": len(orders),
        "filled_orders": len(filled_orders & set(orders)),
        "terminal_nonfills": len(set(terminal_orders) & set(orders)),
        "pending_within_grace": sorted(pending),
        "unresolved_orders": sorted(set(unresolved)),
        "spooled_this_pass": spooled,
        "invalid_spool_records": transport["invalid_spool_records"],
        "record_id_conflicts": transport["record_id_conflicts"],
        "issues": sorted(set(issues)),
        "complete": not blockers,
    }


def _virtual_finals(
    records: Iterable[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    finals: dict[str, dict[str, Any]] = {}
    payloads: dict[str, str] = {}
    conflicts: list[str] = []
    for row in records:
        if row.get("event_type") != "VIRTUAL_FINAL":
            continue
        fill_id = str(row.get("fill_id") or "")
        if not fill_id:
            conflicts.append("virtual_final_missing_fill_id")
            continue
        rendered = json.dumps(
            row, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        prior = payloads.get(fill_id)
        if prior is not None and prior != rendered:
            conflicts.append(f"duplicate_virtual_final:{fill_id}")
            continue
        payloads[fill_id] = rendered
        finals.setdefault(fill_id, row)
    return finals, conflicts


def reconcile_finals(
    run_root: Path,
    model_sha: str,
) -> dict[str, Any]:
    """Materialize deterministic canonical FINAL facts from durable labels."""
    events, transport = canonical_and_spooled_events(run_root, model_sha)
    evidence, evidence_health = counterfactual_records(run_root, model_sha)
    fills: dict[str, LedgerEvent] = {}
    finals_by_fill: dict[str, LedgerEvent] = {}
    finals_by_position: dict[str, LedgerEvent] = {}
    issues: list[str] = []
    existing_ids = {event.record_id for event in events}

    for event in events:
        if not canonical_paper_exploration_event(event):
            continue
        if event.event_type == "FILL":
            _bind(fills, event.fill_id, event, "fill", issues)
        elif event.event_type == "FINAL":
            _bind(finals_by_fill, event.fill_id, event, "fill_final", issues)
            _bind(
                finals_by_position,
                event.position_id,
                event,
                "position_final",
                issues,
            )

    virtual_finals, virtual_conflicts = _virtual_finals(evidence.values())
    spooled = 0
    missing_canonical: list[str] = []
    invalid_virtual: list[str] = []
    orphan_virtual: list[str] = []
    for fill_id, virtual in sorted(virtual_finals.items()):
        fill = fills.get(fill_id)
        if fill is None:
            orphan_virtual.append(fill_id)
            continue
        existing = finals_by_fill.get(fill_id)
        if existing is not None:
            continue
        metadata = (
            virtual.get("metadata")
            if isinstance(virtual.get("metadata"), dict)
            else {}
        )
        payout = _finite(virtual.get("virtual_cashflow"))
        pnl = _finite(virtual.get("counterfactual_pnl"))
        shares = _finite(fill.filled_size)
        price = _finite(fill.fill_price)
        fee = _finite(fill.fee)
        identities_match = all((
            str(virtual.get("position_id") or "") == str(fill.position_id or ""),
            str(virtual.get("market_id") or "") == str(fill.market_id or ""),
            str(virtual.get("event_id") or "") == str(fill.event_id or ""),
            str(virtual.get("token_id") or "") == str(fill.token_id or ""),
        ))
        valid_numbers = bool(
            math.isfinite(payout) and payout >= 0.0
            and math.isfinite(pnl)
            and math.isfinite(shares) and shares > 0.0
            and math.isfinite(price) and 0.0 <= price <= 1.0
            and math.isfinite(fee) and fee >= 0.0
        )
        entry_debit = price * shares + fee if valid_numbers else math.nan
        won = metadata.get("won")
        expected_payout = shares if won is True else 0.0 if won is False else None
        cash_identity = bool(
            valid_numbers
            and payout <= shares + 1e-7
            and abs(pnl - (payout - entry_debit)) <= 1e-7
            and (
                expected_payout is None
                or abs(payout - expected_payout) <= 1e-7
            )
        )
        if not identities_match or not cash_identity:
            invalid_virtual.append(fill_id)
            continue
        record_id = canonical_final_record_id(model_sha, fill_id)
        if record_id in existing_ids:
            missing_canonical.append(fill_id)
            continue
        canonical_metadata = dict(fill.metadata)
        canonical_metadata.update({
            "paper_exploration": True,
            "economic_authority": "PAPER_EXPLORATION",
            "counterfactual": False,
            "excluded_from_portfolio_equity": False,
            "research_evidence_only": False,
            "settlement_outcome": metadata.get("settlement_outcome"),
            "winning_token_id": metadata.get("winning_token_id"),
            "won": won,
            "hold_to_settlement": metadata.get("hold_to_settlement") is True,
            "realized": True,
            "unwind_accounted": True,
            "cost_vector_complete": True,
            "entry_debit": entry_debit,
            "settlement_payout": payout,
            "cash_identity_verified": True,
            "canonical_reconciler": "V7_PAPER_EXPLORATION_ACCOUNT",
            "virtual_final_record_id": virtual.get("record_id"),
            "terminal_id": f"paper-exploration:{fill.position_id}:final",
            "pnl_decomposition": {
                "trading_pnl": pnl,
                "spread_capture": 0.0,
                "adverse_markout": 0.0,
                "inventory_pnl": 0.0,
                "maker_rebates": 0.0,
                "liquidity_rewards": 0.0,
                "own_reward_share_verified": False,
            },
        })
        duration = int(_finite(virtual.get("capital_duration_ms"), 0.0))
        final = LedgerEvent(
            event_type="FINAL",
            strategy=fill.strategy,
            model_sha=model_sha,
            model_version=fill.model_version or MODEL_VERSION,
            record_id=record_id,
            recorded_ts_ms=max(
                int(fill.recorded_ts_ms) + 1,
                int(_finite(virtual.get("timestamp_ms"), fill.recorded_ts_ms + 1)),
            ),
            candidate_id=fill.candidate_id,
            order_id=fill.order_id,
            fill_id=fill_id,
            position_id=fill.position_id,
            market_id=fill.market_id,
            event_id=fill.event_id,
            token_id=fill.token_id,
            side=fill.side,
            intended_action="TAKE",
            complete=True,
            final_pnl=pnl,
            realized_cashflow=payout,
            fee=0.0,
            slippage=0.0,
            unwind_loss=0.0,
            capital_cost=0.0,
            latency_cost=0.0,
            capital_duration_ms=max(0, duration),
            metadata=canonical_metadata,
        )
        spool_event(run_root, final)
        existing_ids.add(record_id)
        finals_by_fill[fill_id] = final
        if fill.position_id:
            finals_by_position[fill.position_id] = final
        spooled += 1

    for fill_id, final in finals_by_fill.items():
        if fill_id not in fills:
            issues.append(f"final_without_fill:{fill_id}")
        elif final.position_id != fills[fill_id].position_id:
            issues.append(f"final_position_mismatch:{fill_id}")

    blockers = (
        issues
        + transport["invalid_spool_records"]
        + transport["record_id_conflicts"]
        + evidence_health["invalid_counterfactual_records"]
        + evidence_health["counterfactual_record_id_conflicts"]
        + virtual_conflicts
        + [f"invalid_virtual_final:{value}" for value in invalid_virtual]
        + [f"missing_canonical_final:{value}" for value in missing_canonical]
    )
    return {
        "schema": "polymarket_v7_paper_exploration_final_reconciliation_v1",
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "canonical_fills": len(fills),
        "durable_virtual_finals": len(virtual_finals),
        "canonical_or_spooled_finals": len(finals_by_fill),
        "spooled_this_pass": spooled,
        "missing_canonical_fills": sorted(set(missing_canonical)),
        "invalid_virtual_finals": sorted(set(invalid_virtual)),
        "orphan_virtual_finals": sorted(set(orphan_virtual)),
        "invalid_spool_records": transport["invalid_spool_records"],
        "record_id_conflicts": transport["record_id_conflicts"],
        "invalid_counterfactual_records": evidence_health[
            "invalid_counterfactual_records"
        ],
        "counterfactual_record_id_conflicts": evidence_health[
            "counterfactual_record_id_conflicts"
        ],
        "issues": sorted(set(issues + virtual_conflicts)),
        "complete": not blockers,
    }


def resolve_starting_capital(
    run_root: Path, model_sha: str, explicit: float | None = None
) -> tuple[float, list[str]]:
    values: list[tuple[str, float]] = []
    if explicit is not None:
        values.append(("explicit", _finite(explicit)))
    allocation = _json(
        Path(run_root) / "control" / "allocations" / "manifest.json"
    )
    budgets = (
        allocation.get("engine_budgets")
        if isinstance(allocation.get("engine_budgets"), dict)
        else {}
    )
    if "CRYPTO_SETTLEMENT_ENGINE" in budgets:
        values.append((
            "allocation_manifest",
            _finite(budgets.get("CRYPTO_SETTLEMENT_ENGINE")),
        ))
    router_state = _json(
        Path(run_root) / "external_fair" / "paper_router_state.json"
    )
    if router_state.get("model_sha") == model_sha:
        values.append(("paper_router_state", _finite(router_state.get("starting_capital"))))
    valid = [(source, value) for source, value in values
             if math.isfinite(value) and value > 0.0]
    issues: list[str] = []
    if not valid:
        return math.nan, ["starting_capital_unavailable"]
    capital = valid[0][1]
    for source, value in valid[1:]:
        if abs(value - capital) > 1e-7:
            issues.append(
                f"starting_capital_conflict:{valid[0][0]}:{source}"
            )
    return capital, issues


def reconstruct_account(
    run_root: Path,
    model_sha: str,
    starting_capital: float,
    *,
    current_ms: int | None = None,
    orphan_grace_ms: int = 5_000,
    prior_peak_equity: float | None = None,
) -> dict[str, Any]:
    current = now_ms() if current_ms is None else int(current_ms)
    if not math.isfinite(starting_capital) or starting_capital <= 0.0:
        raise ValueError("starting capital must be finite and positive")
    events, transport = canonical_and_spooled_events(run_root, model_sha)
    evidence, evidence_health = counterfactual_records(run_root, model_sha)
    issues: list[str] = []
    orders: dict[str, LedgerEvent] = {}
    fills: dict[str, LedgerEvent] = {}
    order_terminals: dict[str, LedgerEvent] = {}
    finals_by_fill: dict[str, LedgerEvent] = {}
    finals_by_position: dict[str, LedgerEvent] = {}

    for event in events:
        if not canonical_paper_exploration_event(event):
            continue
        if event.event_type == "ORDER_SUBMITTED":
            _bind(orders, event.order_id, event, "order", issues)
        elif event.event_type == "FILL":
            _bind(fills, event.fill_id, event, "fill", issues)
        elif (
            event.event_type == "ORDER_STATE"
            and event.complete is True
            and str(event.order_state or "").upper() in TERMINAL_ORDER_STATES
        ):
            _bind(
                order_terminals,
                event.order_id,
                event,
                "order_terminal",
                issues,
            )
        elif event.event_type == "FINAL":
            _bind(finals_by_fill, event.fill_id, event, "fill_final", issues)
            _bind(
                finals_by_position,
                event.position_id,
                event,
                "position_final",
                issues,
            )

    fills_by_order: dict[str, LedgerEvent] = {}
    fills_by_position: dict[str, LedgerEvent] = {}
    total_entry_debit = 0.0
    for fill_id, fill in fills.items():
        order_id = str(fill.order_id or "")
        position_id = str(fill.position_id or "")
        if not order_id or order_id not in orders:
            issues.append(f"fill_without_order:{fill_id}")
        else:
            order = orders[order_id]
            if any((
                order.position_id != fill.position_id,
                order.market_id != fill.market_id,
                order.token_id != fill.token_id,
                order.side != fill.side,
            )):
                issues.append(f"order_fill_identity_mismatch:{fill_id}")
        prior_order_fill = fills_by_order.get(order_id)
        if prior_order_fill is not None and prior_order_fill.fill_id != fill_id:
            issues.append(f"multiple_fills_per_order:{order_id}")
        elif order_id:
            fills_by_order[order_id] = fill
        prior_position_fill = fills_by_position.get(position_id)
        if (
            prior_position_fill is not None
            and prior_position_fill.fill_id != fill_id
        ):
            issues.append(f"multiple_fills_per_position:{position_id}")
        elif position_id:
            fills_by_position[position_id] = fill
        price = _finite(fill.fill_price)
        shares = _finite(fill.filled_size)
        fee = _finite(fill.fee)
        if not (
            math.isfinite(price) and 0.0 <= price <= 1.0
            and math.isfinite(shares) and shares > 0.0
            and math.isfinite(fee) and fee >= 0.0
        ):
            issues.append(f"fill_economics_invalid:{fill_id}")
            continue
        total_entry_debit += price * shares + fee

    pending_orders: list[str] = []
    for order_id, order in orders.items():
        has_fill = order_id in fills_by_order
        has_terminal = order_id in order_terminals
        if has_fill and has_terminal:
            issues.append(f"filled_order_has_nonfill_terminal:{order_id}")
        elif not has_fill and not has_terminal:
            if current - int(order.recorded_ts_ms) < max(1, orphan_grace_ms):
                pending_orders.append(order_id)
            else:
                issues.append(f"order_without_fill_or_terminal:{order_id}")
    for order_id in order_terminals:
        if order_id not in orders:
            issues.append(f"terminal_without_order:{order_id}")

    total_settlement_payout = 0.0
    realized_pnl = 0.0
    terminal_positions = 0
    for fill_id, final in finals_by_fill.items():
        fill = fills.get(fill_id)
        if fill is None:
            issues.append(f"final_without_fill:{fill_id}")
            continue
        if final.position_id != fill.position_id:
            issues.append(f"final_position_mismatch:{fill_id}")
            continue
        payout = _finite(final.realized_cashflow)
        pnl = _finite(final.final_pnl)
        shares = _finite(fill.filled_size)
        entry_debit = (
            _finite(fill.fill_price) * shares + _finite(fill.fee)
        )
        if not all(math.isfinite(value) for value in (
            payout, pnl, shares, entry_debit
        )) or payout < 0.0 or shares <= 0.0:
            issues.append(f"final_economics_invalid:{fill_id}")
            continue
        if payout > shares + 1e-7:
            issues.append(f"final_payout_above_binary_par:{fill_id}")
        if abs(pnl - (payout - entry_debit)) > 1e-7:
            issues.append(f"final_pnl_cash_identity_mismatch:{fill_id}")
        won = final.metadata.get("won") if isinstance(final.metadata, dict) else None
        if isinstance(won, bool):
            expected = shares if won else 0.0
            if abs(payout - expected) > 1e-7:
                issues.append(f"final_binary_payout_mismatch:{fill_id}")
        total_settlement_payout += payout
        realized_pnl += pnl
        terminal_positions += 1

    latest_marks: dict[str, dict[str, Any]] = {}
    for row in evidence.values():
        if row.get("event_type") != "VIRTUAL_MARKOUT":
            continue
        fill_id = str(row.get("fill_id") or "")
        timestamp = int(_finite(row.get("timestamp_ms"), 0.0))
        if not fill_id:
            continue
        prior = latest_marks.get(fill_id)
        if prior is None or timestamp > int(_finite(prior.get("timestamp_ms"), 0.0)):
            latest_marks[fill_id] = row

    positions: list[dict[str, Any]] = []
    open_entry_debit = 0.0
    marked_open_value = 0.0
    for position_id, fill in sorted(fills_by_position.items()):
        if fill.fill_id in finals_by_fill:
            continue
        shares = _finite(fill.filled_size)
        entry_debit = _finite(fill.fill_price) * shares + _finite(fill.fee)
        mark = latest_marks.get(str(fill.fill_id or ""), {})
        mark_value = _finite(mark.get("executable_liquidation_value"), 0.0)
        if not math.isfinite(mark_value) or mark_value < 0.0:
            mark_value = 0.0
        if mark_value > shares + 1e-7:
            issues.append(f"open_mark_above_binary_par:{fill.fill_id}")
            mark_value = min(max(0.0, mark_value), shares)
        open_entry_debit += entry_debit
        marked_open_value += mark_value
        positions.append({
            "position_id": position_id,
            "fill_id": fill.fill_id,
            "order_id": fill.order_id,
            "market_id": fill.market_id,
            "event_id": fill.event_id,
            "token_id": fill.token_id,
            "side": fill.side,
            "shares": shares,
            "entry_price": _finite(fill.fill_price),
            "entry_fee": _finite(fill.fee),
            "entry_debit": entry_debit,
            "executable_liquidation_value": mark_value,
            "unrealized_pnl": mark_value - entry_debit,
            "mark_timestamp_ms": int(_finite(mark.get("timestamp_ms"), 0.0)),
        })

    cash = starting_capital - total_entry_debit + total_settlement_payout
    equity = cash + marked_open_value
    if not math.isfinite(cash) or not math.isfinite(equity):
        issues.append("paper_account_nonfinite")
    if cash < -1e-7:
        issues.append("paper_account_cash_negative")
    previous_peak = _finite(prior_peak_equity, starting_capital)
    if not math.isfinite(previous_peak) or previous_peak < starting_capital:
        previous_peak = starting_capital
    peak = max(starting_capital, previous_peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 1.0
    blockers = (
        issues
        + transport["invalid_spool_records"]
        + transport["record_id_conflicts"]
        + evidence_health["invalid_counterfactual_records"]
        + evidence_health["counterfactual_record_id_conflicts"]
    )
    return {
        "schema": "polymarket_v7_paper_exploration_account_v1",
        "timestamp_ms": current,
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "accounting_owner": "V7_CANONICAL_LEDGER_AND_SINGLE_WRITER_SPOOL",
        "execution_authority": "SIMULATED_PAPER_EXPLORATION_ONLY",
        "starting_capital": starting_capital,
        "orders_submitted": len(orders),
        "fills": len(fills),
        "terminal_nonfills": len(order_terminals),
        "terminal_positions": terminal_positions,
        "open_positions": len(positions),
        "pending_orders_within_grace": sorted(pending_orders),
        "entry_debit": total_entry_debit,
        "settlement_payout": total_settlement_payout,
        "open_entry_debit": open_entry_debit,
        "marked_open_value": marked_open_value,
        "cash": cash,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": marked_open_value - open_entry_debit,
        "total_pnl": equity - starting_capital,
        "equity": equity,
        "peak_equity": peak,
        "drawdown": drawdown,
        "positions": positions,
        "canonical_rows": transport["canonical_rows"],
        "spooled_rows": transport["spooled_rows"],
        "invalid_spool_records": transport["invalid_spool_records"],
        "record_id_conflicts": transport["record_id_conflicts"],
        "invalid_counterfactual_records": evidence_health[
            "invalid_counterfactual_records"
        ],
        "counterfactual_record_id_conflicts": evidence_health[
            "counterfactual_record_id_conflicts"
        ],
        "issues": sorted(set(issues)),
        "complete": not blockers,
    }


def reconcile_once(
    run_root: Path,
    model_sha: str,
    *,
    current_ms: int | None = None,
    orphan_grace_ms: int = 5_000,
    starting_capital: float | None = None,
) -> dict[str, Any]:
    current = now_ms() if current_ms is None else int(current_ms)
    status_path = Path(run_root) / "external_fair" / "paper_account_status.json"
    prior = _json(status_path)
    capital, capital_issues = resolve_starting_capital(
        run_root, model_sha, starting_capital
    )
    order_report = reconcile_orphan_orders(
        run_root,
        model_sha,
        current_ms=current,
        orphan_grace_ms=orphan_grace_ms,
    )
    final_report = reconcile_finals(run_root, model_sha)
    account = reconstruct_account(
        run_root,
        model_sha,
        capital,
        current_ms=current,
        orphan_grace_ms=orphan_grace_ms,
        prior_peak_equity=(
            prior.get("account", {}).get("peak_equity")
            if isinstance(prior.get("account"), dict)
            and prior.get("model_sha") == model_sha
            else None
        ),
    )
    blockers = sorted(set(
        capital_issues
        + ([] if order_report["complete"] else [
            "PAPER_EXPLORATION_ORDER_RECONCILIATION_INCOMPLETE"
        ])
        + ([] if final_report["complete"] else [
            "PAPER_EXPLORATION_FINAL_RECONCILIATION_INCOMPLETE"
        ])
        + ([] if account["complete"] else [
            "PAPER_EXPLORATION_ACCOUNT_RECONCILIATION_INCOMPLETE"
        ])
    ))
    status = {
        "schema": "polymarket_v7_paper_exploration_account_status_v1",
        "timestamp": current // 1000,
        "timestamp_ms": current,
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "single_execution_owner": True,
        "ledger_writer_authority": False,
        "spool_producer_only": True,
        "state": "OPERATIONAL" if not blockers else "BLOCKED",
        "complete": not blockers,
        "blockers": blockers,
        "order_reconciliation": order_report,
        "final_reconciliation": final_report,
        "account": account,
    }
    _atomic_json(status_path, status)
    return status


def _failure_status(
    run_root: Path, model_sha: str, exc: BaseException
) -> dict[str, Any]:
    current = now_ms()
    status = {
        "schema": "polymarket_v7_paper_exploration_account_status_v1",
        "timestamp": current // 1000,
        "timestamp_ms": current,
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "single_execution_owner": True,
        "ledger_writer_authority": False,
        "spool_producer_only": True,
        "state": "BLOCKED",
        "complete": False,
        "blockers": [f"ACCOUNT_RECONCILER_ERROR:{type(exc).__name__}"],
        "error": str(exc)[:500],
        "order_reconciliation": {},
        "final_reconciliation": {},
        "account": {},
    }
    _atomic_json(
        Path(run_root) / "external_fair" / "paper_account_status.json",
        status,
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--orphan-grace-ms", type=int, default=5_000)
    parser.add_argument("--starting-capital", type=float)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    if SHA40.fullmatch(args.model_sha) is None:
        raise SystemExit("exact model SHA required")
    root = args.run_root.resolve()
    if not args.loop:
        try:
            result = reconcile_once(
                root,
                args.model_sha,
                orphan_grace_ms=args.orphan_grace_ms,
                starting_capital=args.starting_capital,
            )
        except Exception as exc:
            result = _failure_status(root, args.model_sha, exc)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0 if result["complete"] else 2
    while True:
        try:
            result = reconcile_once(
                root,
                args.model_sha,
                orphan_grace_ms=args.orphan_grace_ms,
                starting_capital=args.starting_capital,
            )
        except Exception as exc:
            result = _failure_status(root, args.model_sha, exc)
        print(json.dumps({
            "timestamp": result["timestamp"],
            "state": result["state"],
            "complete": result["complete"],
            "blockers": result["blockers"],
        }, sort_keys=True), flush=True)
        time.sleep(max(0.25, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
