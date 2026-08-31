#!/usr/bin/env python3
"""Derive balanced integer-base-unit journal facts from CLOB user trade evidence.

This is a pure conversion layer. It does not connect, sign, submit, or append;
the caller must pass a sealed authenticated user-WebSocket evidence record and
the matching immutable provenance FILL record hash to the canonical ledger
writer/spool.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PUSD_DECIMALS = 6
TOKEN_DECIMALS = 6
# MATCHED and MINED are not final settlement states. They remain evidence but
# cannot produce a canonical accounting fill until confirmation.
SETTLED_TRADE_STATUSES = frozenset({"TRADE_STATUS_CONFIRMED"})


class ClobJournalError(ValueError):
    pass


def _require_sealed(record: Any) -> None:
    validate = getattr(record, "validate", None)
    if not callable(validate):
        raise ClobJournalError("evidence:must_be_sealed_authenticated_clob_user_ws")
    try:
        validate(sealed=True)
    except Exception as exc:
        raise ClobJournalError("evidence:must_be_sealed_authenticated_clob_user_ws") from exc


def _ledger_module() -> Any:
    path = Path(__file__).with_name("v7_execution_ledger.py")
    module_name = "v7_execution_ledger_for_clob_journal"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ClobJournalError("ledger:unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _units(value: Any, *, decimals: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ClobJournalError(f"{field}:not_decimal_text")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
        raise ClobJournalError(f"{field}:unsupported_decimals")
    try:
        raw = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ClobJournalError(f"{field}:not_decimal_text") from exc
    if not raw.is_finite() or raw <= 0:
        raise ClobJournalError(f"{field}:not_positive")
    scaled = raw * (Decimal(10) ** decimals)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ClobJournalError(f"{field}:not_exact_base_units")
    return int(integral)


def clob_trade_journal(evidence_record: Any, *, provenance_record_hash: str,
                       token_decimals: int = TOKEN_DECIMALS) -> Any:
    """Create one unsealed ``TRADE_FILL`` journal entry from a sealed trade event."""
    if not isinstance(provenance_record_hash, str) or not SHA256_RE.fullmatch(provenance_record_hash):
        raise ClobJournalError("provenance_record_hash:invalid")
    if (getattr(evidence_record, "source", None) != "CLOB_USER_WS"
            or getattr(evidence_record, "authenticated_read", None) is not True
            or not isinstance(getattr(evidence_record, "record_hash", None), str)
            or not SHA256_RE.fullmatch(evidence_record.record_hash)):
        raise ClobJournalError("evidence:must_be_sealed_authenticated_clob_user_ws")
    _require_sealed(evidence_record)
    response = getattr(evidence_record, "response", None)
    if not isinstance(response, dict) or not isinstance(response.get("event"), dict):
        raise ClobJournalError("evidence:wire_event_missing")
    event = response["event"]
    if event.get("event_type") != "trade":
        raise ClobJournalError("evidence:event_not_trade")
    if event.get("status") not in SETTLED_TRADE_STATUSES:
        raise ClobJournalError("evidence:trade_not_settled")
    if event.get("trader_side") not in {"TAKER", "MAKER"}:
        raise ClobJournalError("evidence:trader_side_missing")
    try:
        fee_rate_bps = Decimal(str(event.get("fee_rate_bps")))
    except (InvalidOperation, ValueError) as exc:
        raise ClobJournalError("evidence:fee_rate_bps_missing") from exc
    if not fee_rate_bps.is_finite() or fee_rate_bps < 0:
        raise ClobJournalError("evidence:fee_rate_bps_invalid")
    if event["trader_side"] == "MAKER" and fee_rate_bps != 0:
        raise ClobJournalError("evidence:maker_fee_nonzero")
    if event["trader_side"] == "TAKER" and fee_rate_bps != 0:
        raise ClobJournalError("evidence:observed_taker_fee_required")
    side = event.get("side")
    if side not in {"BUY", "SELL"}:
        raise ClobJournalError("evidence:trade_side")
    for field in ("id", "taker_order_id", "market", "asset_id", "size", "price"):
        if not isinstance(event.get(field), str) or not event[field].strip():
            raise ClobJournalError(f"evidence:{field}:missing")
    token_units = _units(event["size"], decimals=token_decimals, field="size")
    try:
        cash_value = Decimal(event["size"]) * Decimal(event["price"])
    except (InvalidOperation, ValueError) as exc:
        raise ClobJournalError("price:not_decimal_text") from exc
    cash_units = _units(str(cash_value), decimals=PUSD_DECIMALS, field="notional")
    ledger = _ledger_module()
    if side == "BUY":
        cash_wallet, cash_clearing = -cash_units, cash_units
        token_wallet, token_clearing = token_units, -token_units
    else:
        cash_wallet, cash_clearing = cash_units, -cash_units
        token_wallet, token_clearing = -token_units, token_units
    token_asset = f"token:{event['asset_id']}"
    return ledger.EconomicJournalEntry(
        entry_type="TRADE_FILL",
        model_sha=evidence_record.model_sha,
        observed_ts_ms=evidence_record.received_ts_ms,
        source="CLOB_USER_WS",
        source_record_id=evidence_record.source_record_id,
        execution_mode="LIVE_OBSERVED",
        authenticated_execution=True,
        metadata={
            "evidence_record_hash": evidence_record.record_hash,
            "provenance_record_hash": provenance_record_hash,
            "clob_event_id": event["id"],
            "clob_taker_order_id": event["taker_order_id"],
            "condition_id": event["market"],
            "token_id": event["asset_id"],
            "side": side,
            "trader_side": event["trader_side"],
            "fee_rate_bps": event["fee_rate_bps"],
            "price": event["price"],
            "size": event["size"],
            "pUSD_decimals": PUSD_DECIMALS,
            "token_decimals": token_decimals,
        },
        postings=(
            ledger.JournalPosting("assets:cash:wallet", "pUSD", cash_wallet),
            ledger.JournalPosting("clearing:clob:cash", "pUSD", cash_clearing),
            ledger.JournalPosting("assets:outcome:position", token_asset, token_wallet),
            ledger.JournalPosting("clearing:clob:token", token_asset, token_clearing),
        ),
    )
