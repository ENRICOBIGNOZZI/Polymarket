#!/usr/bin/env python3
"""Derive simple cash journal facts from sealed Data API activity evidence.

Only activity variants with an unambiguous pUSD amount are converted. Unknown
or structurally incomplete activity remains evidence-only instead of being
guessed into accounting. This module has no transport, signer, or writer.
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
ACTIVITY_TYPES = {
    "DEPOSIT": ("DEPOSIT", "equity:external_funding", -1),
    "WITHDRAWAL": ("WITHDRAW", "equity:external_funding", 1),
    "REWARD": ("LIQUIDITY_REWARD", "income:liquidity_reward", -1),
    "MAKER_REBATE": ("MAKER_REBATE", "income:maker_rebate", -1),
    "TAKER_REBATE": ("TAKER_REBATE", "income:taker_rebate", -1),
}


class ActivityJournalError(ValueError):
    pass


def _require_sealed(record: Any) -> None:
    validate = getattr(record, "validate", None)
    if not callable(validate):
        raise ActivityJournalError("evidence:must_be_sealed_data_api_activity")
    try:
        validate(sealed=True)
    except Exception as exc:
        raise ActivityJournalError("evidence:must_be_sealed_data_api_activity") from exc


def _ledger_module() -> Any:
    name = "v7_execution_ledger_for_data_activity_journal"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("v7_execution_ledger.py"))
    if spec is None or spec.loader is None:
        raise ActivityJournalError("ledger:unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _amount_units(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ActivityJournalError("activity:amount_missing")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ActivityJournalError("activity:amount_invalid") from exc
    if not amount.is_finite() or amount <= 0:
        raise ActivityJournalError("activity:amount_invalid")
    scaled = amount * (Decimal(10) ** PUSD_DECIMALS)
    if scaled != scaled.to_integral_value():
        raise ActivityJournalError("activity:amount_not_exact_pusd_units")
    return int(scaled)


def activity_journal(evidence_record: Any, activity_index: int) -> Any:
    """Create one unsealed economic entry from one exact response-array item."""
    if isinstance(activity_index, bool) or not isinstance(activity_index, int) or activity_index < 0:
        raise ActivityJournalError("activity_index:invalid")
    if (getattr(evidence_record, "source", None) != "DATA_API_ACTIVITY"
            or not isinstance(getattr(evidence_record, "record_hash", None), str)
            or not SHA256_RE.fullmatch(evidence_record.record_hash)):
        raise ActivityJournalError("evidence:must_be_sealed_data_api_activity")
    _require_sealed(evidence_record)
    response = getattr(evidence_record, "response", None)
    if not isinstance(response, list) or activity_index >= len(response) or not isinstance(response[activity_index], dict):
        raise ActivityJournalError("activity_index:missing")
    activity = response[activity_index]
    activity_type = activity.get("type")
    if not isinstance(activity_type, str) or activity_type not in ACTIVITY_TYPES:
        raise ActivityJournalError("activity:type_not_accountable")
    tx_hash = activity.get("transactionHash")
    timestamp = activity.get("timestamp")
    if not isinstance(tx_hash, str) or not tx_hash.strip():
        raise ActivityJournalError("activity:transaction_hash_missing")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, str)) or not str(timestamp).isdigit() or int(timestamp) <= 0:
        raise ActivityJournalError("activity:timestamp_invalid")
    units = _amount_units(activity.get("amount"))
    entry_type, counterpart, counterpart_sign = ACTIVITY_TYPES[activity_type]
    ledger = _ledger_module()
    return ledger.EconomicJournalEntry(
        entry_type=entry_type,
        model_sha=evidence_record.model_sha,
        observed_ts_ms=evidence_record.received_ts_ms,
        source="DATA_API",
        source_record_id=f"{evidence_record.source_record_id}:{activity_index}:{tx_hash}",
        execution_mode="LIVE_OBSERVED",
        authenticated_execution=True,
        metadata={
            "evidence_record_hash": evidence_record.record_hash,
            "data_api_activity_type": activity_type,
            "transaction_hash": tx_hash,
            "activity_timestamp": str(timestamp),
            "pUSD_decimals": PUSD_DECIMALS,
        },
        postings=(
            ledger.JournalPosting("assets:cash:wallet", "pUSD", units if activity_type != "WITHDRAWAL" else -units),
            ledger.JournalPosting(counterpart, "pUSD", counterpart_sign * units),
        ),
    )
