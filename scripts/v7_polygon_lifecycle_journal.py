#!/usr/bin/env python3
"""Build split/merge/redeem journal facts from a sealed Polygon RPC receipt.

The adapter is receipt-only: it decodes pUSD ERC-20 and Conditional Tokens
ERC-1155 transfer logs for one wallet, validates the documented inventory
invariant for the supplied lifecycle operation, and returns an unsealed ledger
entry. It has no RPC client, signer, or transaction sender.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any


HEX_RE = re.compile(r"^0x[0-9a-fA-F]*$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
CONDITIONAL_TOKENS = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ERC1155_TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
ERC1155_TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
ZERO_ADDRESS = "0x" + "0" * 40
OPERATIONS = {"SPLIT", "MERGE", "REDEEM"}


class PolygonLifecycleError(ValueError):
    pass


def _require_sealed(record: Any) -> None:
    validate = getattr(record, "validate", None)
    if not callable(validate):
        raise PolygonLifecycleError("evidence:must_be_sealed_polygon_rpc")
    try:
        validate(sealed=True)
    except Exception as exc:
        raise PolygonLifecycleError("evidence:must_be_sealed_polygon_rpc") from exc


def _ledger_module() -> Any:
    name = "v7_execution_ledger_for_polygon_lifecycle"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("v7_execution_ledger.py"))
    if spec is None or spec.loader is None:
        raise PolygonLifecycleError("ledger:unavailable")
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
        raise PolygonLifecycleError(f"{field}:invalid_address")
    return value.lower()


def _hex_bytes(value: Any, field: str) -> bytes:
    if not isinstance(value, str) or not HEX_RE.fullmatch(value) or len(value) % 2:
        raise PolygonLifecycleError(f"{field}:invalid_hex")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as exc:
        raise PolygonLifecycleError(f"{field}:invalid_hex") from exc


def _topic_address(value: Any, field: str) -> str:
    raw = _hex_bytes(value, field)
    if len(raw) != 32:
        raise PolygonLifecycleError(f"{field}:invalid_topic_address")
    return "0x" + raw[-20:].hex()


def _word(raw: bytes, offset: int, field: str) -> int:
    if offset < 0 or offset + 32 > len(raw) or offset % 32:
        raise PolygonLifecycleError(f"{field}:invalid_abi_offset")
    return int.from_bytes(raw[offset:offset + 32], "big")


def _uint_array(raw: bytes, offset: int, field: str) -> list[int]:
    length = _word(raw, offset, field)
    end = offset + 32 + 32 * length
    if end > len(raw):
        raise PolygonLifecycleError(f"{field}:truncated_array")
    return [_word(raw, offset + 32 + 32 * index, field) for index in range(length)]


def _erc1155_transfers(log: dict[str, Any], wallet: str) -> list[tuple[int, int]]:
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 4 or not all(isinstance(topic, str) for topic in topics):
        raise PolygonLifecycleError("erc1155:topics")
    from_address = _topic_address(topics[2], "erc1155:from")
    to_address = _topic_address(topics[3], "erc1155:to")
    raw = _hex_bytes(log.get("data"), "erc1155:data")
    signature = topics[0].lower()
    if signature == ERC1155_TRANSFER_SINGLE:
        if len(raw) != 64:
            raise PolygonLifecycleError("erc1155:single_data")
        pairs = [(_word(raw, 0, "erc1155:single"), _word(raw, 32, "erc1155:single"))]
    elif signature == ERC1155_TRANSFER_BATCH:
        if len(raw) < 64:
            raise PolygonLifecycleError("erc1155:batch_data")
        ids = _uint_array(raw, _word(raw, 0, "erc1155:batch_ids_offset"), "erc1155:batch_ids")
        values = _uint_array(raw, _word(raw, 32, "erc1155:batch_values_offset"), "erc1155:batch_values")
        if not ids or len(ids) != len(values):
            raise PolygonLifecycleError("erc1155:batch_shape")
        pairs = list(zip(ids, values))
    else:
        return []
    sign = (1 if to_address == wallet else 0) - (1 if from_address == wallet else 0)
    return [(token_id, sign * value) for token_id, value in pairs if sign and value]


def _receipt(evidence_record: Any) -> dict[str, Any]:
    if (getattr(evidence_record, "source", None) != "POLYGON_RPC"
            or not isinstance(getattr(evidence_record, "record_hash", None), str)
            or not SHA256_RE.fullmatch(evidence_record.record_hash)):
        raise PolygonLifecycleError("evidence:must_be_sealed_polygon_rpc")
    _require_sealed(evidence_record)
    response = getattr(evidence_record, "response", None)
    receipt = response.get("result") if isinstance(response, dict) else None
    if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
        raise PolygonLifecycleError("receipt:not_successful")
    tx_hash = receipt.get("transactionHash")
    if not isinstance(tx_hash, str) or not HASH_RE.fullmatch(tx_hash):
        raise PolygonLifecycleError("receipt:transaction_hash")
    query = getattr(evidence_record, "query", None)
    if (not isinstance(query, dict) or query != {
            "chain_id": "137", "jsonrpc_method": "eth_getTransactionReceipt",
            "transaction_hash": tx_hash.lower()}):
        raise PolygonLifecycleError("receipt:query")
    if not isinstance(receipt.get("logs"), list):
        raise PolygonLifecycleError("receipt:logs")
    return receipt


def lifecycle_journal(evidence_record: Any, *, operation: str, wallet: str,
                      condition_id: str, provenance_record_hash: str | None = None) -> Any:
    """Return a validated, unsealed split/merge/redeem economic journal fact."""
    if operation not in OPERATIONS:
        raise PolygonLifecycleError("operation:unsupported")
    if provenance_record_hash is not None and (not isinstance(provenance_record_hash, str)
                                               or not SHA256_RE.fullmatch(provenance_record_hash)):
        raise PolygonLifecycleError("provenance_record_hash:invalid")
    wallet = _address(wallet, "wallet")
    if not isinstance(condition_id, str) or not HASH_RE.fullmatch(condition_id):
        raise PolygonLifecycleError("condition_id:invalid")
    receipt = _receipt(evidence_record)
    cash_delta = 0
    token_deltas: dict[int, int] = {}
    for log in receipt["logs"]:
        if not isinstance(log, dict):
            raise PolygonLifecycleError("receipt:log_not_object")
        address = _address(log.get("address"), "receipt:log_address")
        topics = log.get("topics")
        if not isinstance(topics, list) or not topics or not isinstance(topics[0], str):
            raise PolygonLifecycleError("receipt:log_topics")
        signature = topics[0].lower()
        if address == PUSD and signature == ERC20_TRANSFER:
            if len(topics) != 3:
                raise PolygonLifecycleError("erc20:topics")
            raw = _hex_bytes(log.get("data"), "erc20:data")
            if len(raw) != 32:
                raise PolygonLifecycleError("erc20:data")
            amount = _word(raw, 0, "erc20:amount")
            from_address = _topic_address(topics[1], "erc20:from")
            to_address = _topic_address(topics[2], "erc20:to")
            cash_delta += (1 if to_address == wallet else 0) * amount
            cash_delta -= (1 if from_address == wallet else 0) * amount
        elif address == CONDITIONAL_TOKENS and signature in {ERC1155_TRANSFER_SINGLE, ERC1155_TRANSFER_BATCH}:
            for token_id, delta in _erc1155_transfers(log, wallet):
                token_deltas[token_id] = token_deltas.get(token_id, 0) + delta
    token_deltas = {token_id: delta for token_id, delta in token_deltas.items() if delta}
    if not token_deltas or cash_delta == 0:
        raise PolygonLifecycleError("receipt:lifecycle_balance_missing")
    values = list(token_deltas.values())
    if operation == "SPLIT":
        if cash_delta >= 0 or len(values) != 2 or any(delta != -cash_delta for delta in values):
            raise PolygonLifecycleError("split:invariant")
        entry_type = "TOKEN_SPLIT"
    elif operation == "MERGE":
        if cash_delta <= 0 or len(values) != 2 or any(delta != -cash_delta for delta in values):
            raise PolygonLifecycleError("merge:invariant")
        entry_type = "TOKEN_MERGE"
    else:
        if cash_delta <= 0 or any(delta >= 0 for delta in values):
            raise PolygonLifecycleError("redeem:invariant")
        entry_type = "TOKEN_REDEEM"
    ledger = _ledger_module()
    postings: list[Any] = [
        ledger.JournalPosting("assets:cash:wallet", "pUSD", cash_delta),
        ledger.JournalPosting("clearing:polygon:cash", "pUSD", -cash_delta),
    ]
    for token_id, delta in sorted(token_deltas.items()):
        asset = f"token:{token_id}"
        postings.extend((
            ledger.JournalPosting("assets:outcome:position", asset, delta),
            ledger.JournalPosting("clearing:polygon:token", asset, -delta),
        ))
    return ledger.EconomicJournalEntry(
        entry_type=entry_type,
        model_sha=evidence_record.model_sha,
        observed_ts_ms=evidence_record.received_ts_ms,
        source="POLYGON_RPC",
        source_record_id=receipt["transactionHash"].lower(),
        execution_mode="LIVE_OBSERVED",
        authenticated_execution=True,
        metadata={"evidence_record_hash": evidence_record.record_hash,
                  **({"provenance_record_hash": provenance_record_hash}
                     if provenance_record_hash is not None else {}),
                  "transaction_hash": receipt["transactionHash"].lower(),
                  "condition_id": condition_id.lower(), "operation": operation, "wallet": wallet,
                  "pUSD_contract": PUSD, "conditional_tokens_contract": CONDITIONAL_TOKENS},
        postings=tuple(postings),
    )
