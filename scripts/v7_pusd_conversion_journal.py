#!/usr/bin/env python3
"""Decode observed USDC.e/pUSD wrap and unwrap receipts into ledger facts.

The adapter accepts only a sealed Polygon receipt.  It does not issue RPC
requests, read credentials, sign, or send a conversion transaction.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any


ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
USDCE = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
ONRAMP = "0x93070a847efef7f70739046a929d47a521f5b8ee"
OFFRAMP = "0x2957922eb93258b93368531d39facca3b4dc5854"
ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class PusdConversionError(ValueError):
    pass


def _require_sealed(record: Any) -> None:
    validate = getattr(record, "validate", None)
    if not callable(validate):
        raise PusdConversionError("evidence:must_be_sealed_polygon_rpc")
    try:
        validate(sealed=True)
    except Exception as exc:
        raise PusdConversionError("evidence:must_be_sealed_polygon_rpc") from exc


def _ledger_module() -> Any:
    name = "v7_execution_ledger_for_pusd_conversion"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("v7_execution_ledger.py"))
    if spec is None or spec.loader is None:
        raise PusdConversionError("ledger:unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _address(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ADDRESS_RE.fullmatch(value):
        raise PusdConversionError(f"{field}:invalid_address")
    return value.lower()


def _topic_address(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise PusdConversionError(f"{field}:invalid_topic_address")
    return "0x" + value[-40:].lower()


def _delta(log: dict[str, Any], wallet: str) -> int | None:
    if _address(log.get("address"), "log:address") not in {PUSD, USDCE}:
        return None
    topics = log.get("topics")
    if (not isinstance(topics, list) or len(topics) != 3 or topics[0].lower() != ERC20_TRANSFER
            or not all(isinstance(item, str) for item in topics)):
        raise PusdConversionError("erc20:transfer_shape")
    data = log.get("data")
    if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", data):
        raise PusdConversionError("erc20:transfer_amount")
    amount = int(data[2:], 16)
    if amount == 0:
        return 0
    from_address = _topic_address(topics[1], "erc20:from")
    to_address = _topic_address(topics[2], "erc20:to")
    return (amount if to_address == wallet else 0) - (amount if from_address == wallet else 0)


def conversion_journal(evidence_record: Any, *, operation: str, wallet: str) -> Any:
    """Return one unsealed, exact-base-unit `PUSD_WRAP` or `PUSD_UNWRAP` fact."""
    if operation not in {"WRAP", "UNWRAP"}:
        raise PusdConversionError("operation:unsupported")
    wallet = _address(wallet, "wallet")
    if (getattr(evidence_record, "source", None) != "POLYGON_RPC"
            or not isinstance(getattr(evidence_record, "record_hash", None), str)
            or not SHA256_RE.fullmatch(evidence_record.record_hash)):
        raise PusdConversionError("evidence:must_be_sealed_polygon_rpc")
    _require_sealed(evidence_record)
    response = getattr(evidence_record, "response", None)
    receipt = response.get("result") if isinstance(response, dict) else None
    if (not isinstance(receipt, dict) or receipt.get("status") != "0x1"
            or not isinstance(receipt.get("logs"), list)
            or not isinstance(receipt.get("transactionHash"), str)
            or not HASH_RE.fullmatch(receipt["transactionHash"])):
        raise PusdConversionError("receipt:invalid")
    if getattr(evidence_record, "query", None) != {
            "chain_id": "137", "jsonrpc_method": "eth_getTransactionReceipt",
            "transaction_hash": receipt["transactionHash"].lower()}:
        raise PusdConversionError("receipt:query")
    expected_ramp = ONRAMP if operation == "WRAP" else OFFRAMP
    if _address(receipt.get("to"), "receipt:to") != expected_ramp:
        raise PusdConversionError("receipt:wrong_ramp")
    deltas = {PUSD: 0, USDCE: 0}
    for log in receipt["logs"]:
        if not isinstance(log, dict):
            raise PusdConversionError("receipt:log_not_object")
        address = _address(log.get("address"), "log:address")
        if address in deltas:
            delta = _delta(log, wallet)
            assert delta is not None
            deltas[address] += delta
    pUSD_delta, usdce_delta = deltas[PUSD], deltas[USDCE]
    if pUSD_delta == 0 or usdce_delta == 0 or pUSD_delta != -usdce_delta:
        raise PusdConversionError("receipt:conversion_invariant")
    if (operation == "WRAP" and not (pUSD_delta > 0 and usdce_delta < 0)
            or operation == "UNWRAP" and not (pUSD_delta < 0 and usdce_delta > 0)):
        raise PusdConversionError("receipt:conversion_direction")
    ledger = _ledger_module()
    return ledger.EconomicJournalEntry(
        entry_type="PUSD_WRAP" if operation == "WRAP" else "PUSD_UNWRAP",
        model_sha=evidence_record.model_sha,
        observed_ts_ms=evidence_record.received_ts_ms,
        source="POLYGON_RPC",
        source_record_id=receipt["transactionHash"].lower(),
        execution_mode="LIVE_OBSERVED",
        authenticated_execution=True,
        metadata={"evidence_record_hash": evidence_record.record_hash,
                  "transaction_hash": receipt["transactionHash"].lower(), "operation": operation,
                  "pUSD_contract": PUSD, "usdce_contract": USDCE, "ramp_contract": expected_ramp},
        postings=(
            ledger.JournalPosting("assets:cash:wallet", "pUSD", pUSD_delta),
            ledger.JournalPosting("clearing:conversion:pUSD", "pUSD", -pUSD_delta),
            ledger.JournalPosting("assets:cash:wallet", "USDCe", usdce_delta),
            ledger.JournalPosting("clearing:conversion:USDCe", "USDCe", -usdce_delta),
        ),
    )
