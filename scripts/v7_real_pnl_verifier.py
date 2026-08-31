#!/usr/bin/env python3
"""Independent, read-only verification of V7 monetary-journal PnL.

This module intentionally does not import the production ledger implementation.
It parses the JSONL bytes itself, rechecks the hash chain and double-entry
invariants, reconstructs terminal cash PnL in integer pUSD base units, and can
write a tamper-evident HMAC attestation.  It has no network, signer, OMS, or
ledger-write capability.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCHEMA = "polymarket_v7_real_pnl_independent_verifier_v1"
ATTESTATION_SCHEMA = "polymarket_v7_real_pnl_attestation_v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACCOUNT_RE = re.compile(r"^(?:assets|liabilities|equity|income|expenses|clearing):[A-Za-z0-9._:-]+$")
ASSET_RE = re.compile(r"^(?:pUSD|USDCe|token:[A-Za-z0-9._:-]+)$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
BLOCK_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
ENTRY_TYPES = {
    "DEPOSIT", "WITHDRAW", "TRADE_FILL", "TAKER_FEE", "MAKER_REBATE", "TAKER_REBATE",
    "LIQUIDITY_REWARD", "WALLET_GAS", "RELAYER_FEE", "BRIDGE_FEE",
    "PUSD_WRAP", "PUSD_UNWRAP", "TOKEN_SPLIT", "TOKEN_MERGE", "TOKEN_REDEEM", "SETTLEMENT",
}
SOURCES = {"CLOB_USER_WS", "CLOB_API", "DATA_API", "WALLET_RPC", "POLYGON_RPC"}
# Data API snapshots are evidence inputs, but a final chain settlement must be
# backed by a Polygon receipt rather than a generic activity row.
REQUIRED_JOURNAL_SOURCES = {"CLOB_USER_WS", "WALLET_RPC", "POLYGON_RPC"}
REQUIRED_EVIDENCE_SOURCES = {"CLOB_USER_WS", "DATA_API_ACTIVITY", "DATA_API_POSITIONS", "WALLET_RPC", "POLYGON_RPC"}
GENESIS_HASH = "0" * 64
EVIDENCE_KIND = "REAL_PNL_EVIDENCE"
EVIDENCE_SOURCES = {"CLOB_USER_WS", "CLOB_USER_TRADES", "CLOB_USER_ORDERS", "DATA_API_ACTIVITY", "DATA_API_POSITIONS", "WALLET_RPC", "POLYGON_RPC"}
EVIDENCE_RULES = {
    "CLOB_USER_WS": ("WS", "wss"), "CLOB_USER_TRADES": ("GET", "https"),
    "CLOB_USER_ORDERS": ("GET", "https"), "DATA_API_ACTIVITY": ("GET", "https"),
    "DATA_API_POSITIONS": ("GET", "https"), "WALLET_RPC": ("POST", "https"),
    "POLYGON_RPC": ("POST", "https"),
}
JOURNAL_EVIDENCE_COMPATIBILITY = {
    "CLOB_USER_WS": {"CLOB_USER_WS", "CLOB_USER_TRADES", "CLOB_USER_ORDERS"},
    "CLOB_API": {"CLOB_USER_TRADES", "CLOB_USER_ORDERS"},
    "DATA_API": {"DATA_API_ACTIVITY", "DATA_API_POSITIONS"},
    "WALLET_RPC": {"WALLET_RPC"}, "POLYGON_RPC": {"POLYGON_RPC"},
}
PROVENANCE_KIND = "EXECUTION_PROVENANCE"
PROVENANCE_STAGES = ("DECISION", "SIGNED_ORDER", "CLOB_ACCEPTED", "FILL", "SETTLEMENT")
PROVENANCE_STAGE_INDEX = {stage: index for index, stage in enumerate(PROVENANCE_STAGES)}
PROVENANCE_PAYLOAD_KEYS = {
    "DECISION": {"decision_hash"},
    "SIGNED_ORDER": {"order_payload_hash", "signature_digest"},
    "CLOB_ACCEPTED": {"acceptance_payload_hash"},
    "FILL": {"fill_payload_hash"},
    "SETTLEMENT": {"settlement_payload_hash"},
}
WALLET_SNAPSHOT_SCHEMA = "polymarket_v7_wallet_balance_snapshot_v1"
POSITION_SNAPSHOT_SCHEMA = "polymarket_v7_data_api_position_snapshot_v1"
ACTIVITY_COVERAGE_SCHEMA = "polymarket_v7_data_api_activity_coverage_v1"
CLOB_USER_WS_WIRE_SCHEMA = "polymarket_v7_clob_user_ws_wire_v2"
# Pre-finality match/mined messages cannot support a settled real-PnL fact.
CLOB_SETTLED_TRADE_STATUSES = {"TRADE_STATUS_CONFIRMED"}
CLOB_ORDER_EVENT_TYPES = {"PLACEMENT", "UPDATE", "CANCELLATION"}
CLOB_ORDER_STATUSES = {"LIVE", "MATCHED", "DELAYED", "UNMATCHED", "CANCELED"}
CLOB_ACCEPTED_ORDER_STATUSES = {"LIVE", "MATCHED", "DELAYED", "UNMATCHED"}
PUSD_CONTRACT = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
USDCE_CONTRACT = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
CONDITIONAL_TOKENS_CONTRACT = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
ERC20_BALANCE_OF = "70a08231"
ERC1155_BALANCE_OF = "00fdd58e"
ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ERC1155_TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
ERC1155_TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
ZERO_ADDRESS = "0x" + "0" * 40


class VerificationError(ValueError):
    """The evidence cannot support a real-PnL claim."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    out = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            out.update(chunk)
    return out.hexdigest()


def _integer(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationError(code)
    return value


def _provenance_next_stage_is_valid(previous_stage: str | None, next_stage: str) -> bool:
    if previous_stage is None:
        return next_stage == "DECISION"
    if next_stage == "FILL":
        return previous_stage in {"CLOB_ACCEPTED", "FILL"}
    return PROVENANCE_STAGE_INDEX[next_stage] == PROVENANCE_STAGE_INDEX[previous_stage] + 1


def _json_object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError("clob_ws_duplicate_json_key")
        result[key] = value
    return result


def _decimal_base_units(value: Any, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise VerificationError(code)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise VerificationError(code) from exc
    scaled = number * Decimal(10) ** 6
    if not number.is_finite() or number <= 0 or scaled != scaled.to_integral_value():
        raise VerificationError(code)
    return int(scaled)


def _receipt_word(value: bytes, offset: int, code: str) -> int:
    if offset < 0 or offset % 32 or offset + 32 > len(value):
        raise VerificationError(code)
    return int.from_bytes(value[offset:offset + 32], "big")


def _receipt_address(value: Any, code: str) -> str:
    if not isinstance(value, str) or not HEX_RE.fullmatch(value) or len(value) != 66:
        raise VerificationError(code)
    return "0x" + value[-40:].lower()


def _receipt_bytes(value: Any, code: str) -> bytes:
    if not isinstance(value, str) or not HEX_RE.fullmatch(value) or len(value) % 2:
        raise VerificationError(code)
    return bytes.fromhex(value[2:])


def _receipt_uint_array(value: bytes, offset: int, code: str) -> list[int]:
    length = _receipt_word(value, offset, code)
    if offset + 32 + 32 * length > len(value):
        raise VerificationError(code)
    return [_receipt_word(value, offset + 32 + index * 32, code) for index in range(length)]


def _polygon_redeem_matches_journal(entry: dict[str, Any], raw_evidence: dict[str, Any]) -> bool:
    """Independently reconstruct one final redeem from its raw Polygon receipt."""
    try:
        if raw_evidence.get("source") != "POLYGON_RPC":
            return False
        response = raw_evidence.get("response")
        receipt = response.get("result") if isinstance(response, dict) else None
        if not isinstance(receipt, dict) or receipt.get("status") != "0x1" or not isinstance(receipt.get("logs"), list):
            return False
        tx_hash = receipt.get("transactionHash")
        if not isinstance(tx_hash, str) or not BLOCK_HASH_RE.fullmatch(tx_hash):
            return False
        tx_hash = tx_hash.lower()
        if raw_evidence.get("query") != {
                "chain_id": "137", "jsonrpc_method": "eth_getTransactionReceipt",
                "transaction_hash": tx_hash}:
            return False
        metadata = entry.get("metadata")
        expected_metadata = {"evidence_record_hash", "provenance_record_hash", "transaction_hash", "condition_id",
                             "operation", "wallet", "pUSD_contract", "conditional_tokens_contract"}
        if (not isinstance(metadata, dict) or set(metadata) != expected_metadata
                or metadata.get("transaction_hash") != tx_hash
                or metadata.get("operation") != "REDEEM"
                or not isinstance(metadata.get("condition_id"), str)
                or not BLOCK_HASH_RE.fullmatch(metadata["condition_id"])
                or not isinstance(metadata.get("wallet"), str)
                or not ADDRESS_RE.fullmatch(metadata["wallet"])
                or metadata.get("pUSD_contract") != PUSD_CONTRACT
                or metadata.get("conditional_tokens_contract") != CONDITIONAL_TOKENS_CONTRACT
                or entry.get("source") != "POLYGON_RPC"
                or entry.get("source_record_id") != tx_hash
                or raw_evidence.get("source_record_id") != tx_hash
                or metadata.get("evidence_record_hash") != raw_evidence.get("record_hash")):
            return False
        wallet = metadata["wallet"].lower()
        cash_delta = 0
        token_deltas: dict[int, int] = {}
        for log in receipt["logs"]:
            if not isinstance(log, dict) or not isinstance(log.get("topics"), list):
                return False
            address = log.get("address")
            topics = log["topics"]
            if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address) or not topics or not isinstance(topics[0], str):
                return False
            signature = topics[0].lower()
            if address.lower() == PUSD_CONTRACT and signature == ERC20_TRANSFER:
                if len(topics) != 3:
                    return False
                amount = _receipt_word(_receipt_bytes(log.get("data"), "polygon_redeem_erc20_data"), 0,
                                       "polygon_redeem_erc20_data")
                if len(_receipt_bytes(log.get("data"), "polygon_redeem_erc20_data")) != 32:
                    return False
                from_address = _receipt_address(topics[1], "polygon_redeem_erc20_from")
                to_address = _receipt_address(topics[2], "polygon_redeem_erc20_to")
                cash_delta += amount if to_address == wallet else 0
                cash_delta -= amount if from_address == wallet else 0
            elif address.lower() == CONDITIONAL_TOKENS_CONTRACT and signature in {ERC1155_TRANSFER_SINGLE, ERC1155_TRANSFER_BATCH}:
                if len(topics) != 4:
                    return False
                from_address = _receipt_address(topics[2], "polygon_redeem_erc1155_from")
                to_address = _receipt_address(topics[3], "polygon_redeem_erc1155_to")
                sign = (1 if to_address == wallet else 0) - (1 if from_address == wallet else 0)
                data = _receipt_bytes(log.get("data"), "polygon_redeem_erc1155_data")
                if signature == ERC1155_TRANSFER_SINGLE:
                    if len(data) != 64:
                        return False
                    pairs = [(_receipt_word(data, 0, "polygon_redeem_erc1155_single"),
                              _receipt_word(data, 32, "polygon_redeem_erc1155_single"))]
                else:
                    if len(data) < 64:
                        return False
                    ids = _receipt_uint_array(data, _receipt_word(data, 0, "polygon_redeem_erc1155_ids"),
                                             "polygon_redeem_erc1155_ids")
                    values = _receipt_uint_array(data, _receipt_word(data, 32, "polygon_redeem_erc1155_values"),
                                                "polygon_redeem_erc1155_values")
                    if not ids or len(ids) != len(values):
                        return False
                    pairs = list(zip(ids, values))
                for token_id, value in pairs:
                    if value and sign:
                        token_deltas[token_id] = token_deltas.get(token_id, 0) + sign * value
        token_deltas = {token_id: delta for token_id, delta in token_deltas.items() if delta}
        if cash_delta <= 0 or not token_deltas or any(delta >= 0 for delta in token_deltas.values()):
            return False
        expected = {
            ("assets:cash:wallet", "pUSD"): cash_delta,
            ("clearing:polygon:cash", "pUSD"): -cash_delta,
        }
        for token_id, delta in token_deltas.items():
            expected[("assets:outcome:position", f"token:{token_id}")] = delta
            expected[("clearing:polygon:token", f"token:{token_id}")] = -delta
        postings = {(row["account"], row["asset"]): row["units"] for row in entry["postings"]}
        return postings == expected
    except (TypeError, ValueError, VerificationError):
        return False


def _clob_user_ws_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Independently decode the exact V2 raw user-stream frame in evidence."""
    response = raw.get("response")
    if not isinstance(response, dict) or set(response) != {"schema", "wire_json", "wire_sha256", "event"}:
        raise VerificationError("clob_ws_response_shape")
    wire = response.get("wire_json")
    if not isinstance(wire, str) or response.get("schema") != CLOB_USER_WS_WIRE_SCHEMA:
        raise VerificationError("clob_ws_response_schema")
    if response.get("wire_sha256") != hashlib.sha256(wire.encode("utf-8")).hexdigest():
        raise VerificationError("clob_ws_wire_hash")
    try:
        frame = json.loads(wire, object_pairs_hook=_json_object_no_duplicates)
    except (json.JSONDecodeError, VerificationError) as exc:
        raise VerificationError("clob_ws_wire_json") from exc
    if not isinstance(frame, dict) or set(frame) != {"topic", "type", "payload"} or frame.get("topic") != "user":
        raise VerificationError("clob_ws_frame_shape")
    event_type, payload = frame.get("type"), frame.get("payload")
    if event_type not in {"order", "trade"} or not isinstance(payload, dict):
        raise VerificationError("clob_ws_frame_shape")
    if event_type == "order":
        required = {"id", "owner", "market", "tokenId", "side", "originalSize", "sizeMatched", "price",
                    "orderEventType", "status", "timestamp"}
        if (not required.issubset(payload) or payload.get("side") not in {"BUY", "SELL"}
                or payload.get("orderEventType") not in CLOB_ORDER_EVENT_TYPES
                or payload.get("status") not in CLOB_ORDER_STATUSES):
            raise VerificationError("clob_ws_order_shape")
        normalized = {"event_type": "order", "id": payload["id"], "owner": payload["owner"],
                      "market": payload["market"], "asset_id": payload["tokenId"], "side": payload["side"],
                      "original_size": payload["originalSize"], "size_matched": str(payload["sizeMatched"]),
                      "price": payload["price"], "order_event_type": payload["orderEventType"],
                      "status": payload["status"], "timestamp": str(payload["timestamp"])}
    else:
        required = {"id", "takerOrderId", "owner", "market", "tokenId", "side", "size", "price", "status", "timestamp"}
        if not required.issubset(payload) or payload.get("side") not in {"BUY", "SELL"}:
            raise VerificationError("clob_ws_trade_shape")
        normalized = {"event_type": "trade", "id": payload["id"], "taker_order_id": payload["takerOrderId"],
                      "owner": payload["owner"], "market": payload["market"], "asset_id": payload["tokenId"],
                      "side": payload["side"], "size": payload["size"], "price": payload["price"],
                      "status": payload["status"], "timestamp": str(payload["timestamp"])}
        if "traderSide" in payload:
            if payload["traderSide"] not in {"TAKER", "MAKER"}:
                raise VerificationError("clob_ws_trade_shape")
            normalized["trader_side"] = payload["traderSide"]
        if "feeRateBps" in payload:
            try:
                rate = Decimal(str(payload["feeRateBps"]))
            except (InvalidOperation, ValueError) as exc:
                raise VerificationError("clob_ws_trade_shape") from exc
            if not rate.is_finite() or rate < 0:
                raise VerificationError("clob_ws_trade_shape")
            normalized["fee_rate_bps"] = str(payload["feeRateBps"])
    if any(not isinstance(value, str) or not value for value in normalized.values()):
        raise VerificationError("clob_ws_field")
    if response.get("event") != normalized:
        raise VerificationError("clob_ws_normalization_mismatch")
    expected_source_id = f"{event_type}:{normalized['id']}:{response['wire_sha256']}"
    if raw.get("source_record_id") != expected_source_id:
        raise VerificationError("clob_ws_source_record_id")
    return normalized


def _clob_provenance_stage_matches_event(stage: str, *, lineage_id: str,
                                         evidence: dict[str, Any]) -> None:
    """Require the raw user-stream event to prove this CLOB lifecycle stage.

    A hash link alone does not establish that an order was accepted or that a
    fill belongs to it.  The CLOB V2 user frame is the authoritative input for
    both facts, so this check deliberately rejects REST summaries here.
    """
    if evidence.get("source") != "CLOB_USER_WS":
        raise VerificationError("clob_evidence_source")
    event = _clob_user_ws_event(evidence)
    if stage == "CLOB_ACCEPTED":
        if (event["event_type"] != "order" or event["order_event_type"] != "PLACEMENT"
                or event["status"] not in CLOB_ACCEPTED_ORDER_STATUSES):
            raise VerificationError("clob_acceptance_event")
        if event["id"] != lineage_id:
            raise VerificationError("clob_acceptance_order_link")
    elif stage == "FILL":
        if event["event_type"] != "trade" or event["status"] not in CLOB_SETTLED_TRADE_STATUSES:
            raise VerificationError("clob_fill_event")
        if event["taker_order_id"] != lineage_id:
            raise VerificationError("clob_fill_order_link")
    else:
        raise VerificationError("clob_provenance_stage")


def _clob_fill_matches_journal(entry: dict[str, Any], raw_evidence: dict[str, Any]) -> bool:
    try:
        event = _clob_user_ws_event(raw_evidence)
        if event["event_type"] != "trade" or event["status"] not in CLOB_SETTLED_TRADE_STATUSES:
            return False
        metadata = entry.get("metadata")
        required_metadata = {"evidence_record_hash", "provenance_record_hash", "clob_event_id", "clob_taker_order_id",
                             "condition_id", "token_id", "side", "trader_side", "fee_rate_bps", "price", "size",
                             "pUSD_decimals", "token_decimals"}
        if not isinstance(metadata, dict) or set(metadata) != required_metadata:
            return False
        mapping = {"clob_event_id": "id", "clob_taker_order_id": "taker_order_id", "condition_id": "market",
                   "token_id": "asset_id", "side": "side", "trader_side": "trader_side",
                   "fee_rate_bps": "fee_rate_bps", "price": "price", "size": "size"}
        if any(metadata[key] != event[value] for key, value in mapping.items()):
            return False
        if event["trader_side"] == "TAKER" and Decimal(event["fee_rate_bps"]) != 0:
            return False
        if event["trader_side"] == "MAKER" and Decimal(event["fee_rate_bps"]) != 0:
            return False
        if metadata["pUSD_decimals"] != 6 or metadata["token_decimals"] != 6:
            return False
        token_units = _decimal_base_units(event["size"], code="clob_ws_size")
        notional_units = _decimal_base_units(str(Decimal(event["size"]) * Decimal(event["price"])),
                                             code="clob_ws_notional")
        cash, tokens = (-notional_units, token_units) if event["side"] == "BUY" else (notional_units, -token_units)
        postings = {(row["account"], row["asset"]): row["units"] for row in entry["postings"]}
        expected = {("assets:cash:wallet", "pUSD"): cash, ("clearing:clob:cash", "pUSD"): -cash,
                    ("assets:outcome:position", f"token:{event['asset_id']}"): tokens,
                    ("clearing:clob:token", f"token:{event['asset_id']}"): -tokens}
        return postings == expected and entry.get("source_record_id") == raw_evidence.get("source_record_id")
    except (InvalidOperation, ValueError, VerificationError):
        return False


def _validate_entry(raw: Any, *, expected_sha: str, expected_previous: str,
                    source_ids: set[tuple[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("record_kind") != "ECONOMIC_JOURNAL":
        raise VerificationError("journal_record_expected")
    required = {
        "record_kind", "entry_type", "model_sha", "observed_ts_ms", "source",
        "source_record_id", "postings", "execution_mode", "authenticated_execution",
        "entry_id", "schema_version", "metadata", "previous_entry_hash", "entry_hash",
    }
    if set(raw) != required:
        raise VerificationError("journal_record_shape")
    if raw["schema_version"] != 1 or raw["entry_type"] not in ENTRY_TYPES:
        raise VerificationError("journal_record_schema_or_type")
    if raw["model_sha"] != expected_sha or not SHA_RE.fullmatch(str(raw["model_sha"])):
        raise VerificationError("journal_model_sha")
    if _integer(raw["observed_ts_ms"], "journal_observed_ts") <= 0:
        raise VerificationError("journal_observed_ts")
    if raw["source"] not in SOURCES or not isinstance(raw["source_record_id"], str) or not raw["source_record_id"].strip():
        raise VerificationError("journal_source")
    source_key = (raw["source"], raw["source_record_id"])
    if source_key in source_ids:
        raise VerificationError("journal_duplicate_source_record")
    source_ids.add(source_key)
    if raw["execution_mode"] not in {"PAPER", "LIVE_OBSERVED"}:
        raise VerificationError("journal_execution_mode")
    if not isinstance(raw["authenticated_execution"], bool):
        raise VerificationError("journal_authenticated_execution")
    if raw["execution_mode"] == "PAPER" and raw["authenticated_execution"]:
        raise VerificationError("journal_paper_authenticated")
    if raw["execution_mode"] == "LIVE_OBSERVED" and not raw["authenticated_execution"]:
        raise VerificationError("journal_live_not_authenticated")
    if not isinstance(raw["entry_id"], str) or not raw["entry_id"]:
        raise VerificationError("journal_entry_id")
    if not isinstance(raw["metadata"], dict):
        raise VerificationError("journal_metadata")
    if raw["previous_entry_hash"] != expected_previous or not SHA256_RE.fullmatch(str(raw["entry_hash"])):
        raise VerificationError("journal_chain_link")
    hash_payload = dict(raw)
    hash_payload.pop("entry_hash")
    if digest(hash_payload) != raw["entry_hash"]:
        raise VerificationError("journal_hash_mismatch")
    postings = raw["postings"]
    if not isinstance(postings, list) or len(postings) < 2:
        raise VerificationError("journal_postings")
    totals: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for posting in postings:
        if not isinstance(posting, dict) or set(posting) != {"account", "asset", "units"}:
            raise VerificationError("journal_posting_shape")
        account, asset = posting["account"], posting["asset"]
        units = _integer(posting["units"], "journal_posting_units")
        if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
            raise VerificationError("journal_posting_account")
        if not isinstance(asset, str) or not ASSET_RE.fullmatch(asset) or units == 0:
            raise VerificationError("journal_posting_asset_or_units")
        key = (account, asset)
        if key in seen:
            raise VerificationError("journal_duplicate_account_asset")
        seen.add(key)
        totals[asset] = totals.get(asset, 0) + units
    if any(value != 0 for value in totals.values()):
        raise VerificationError("journal_unbalanced")
    return raw


def _flat_observed_balances(value: dict[str, Any] | None) -> dict[tuple[str, str], int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise VerificationError("observed_balances_not_object")
    out: dict[tuple[str, str], int] = {}
    for key, units in value.items():
        if not isinstance(key, str) or "|" not in key:
            raise VerificationError("observed_balance_key")
        account, asset = key.split("|", 1)
        if not ACCOUNT_RE.fullmatch(account) or not ASSET_RE.fullmatch(asset):
            raise VerificationError("observed_balance_key")
        out[(account, asset)] = _integer(units, "observed_balance_units")
    return out


def _snapshot_call_data(wallet: str, token_id: int | None) -> str:
    if token_id is None:
        return "0x" + ERC20_BALANCE_OF + "0" * 24 + wallet[2:]
    return ("0x" + ERC1155_BALANCE_OF + "0" * 24 + wallet[2:]
            + token_id.to_bytes(32, "big").hex())


def _snapshot_balances(value: dict[str, Any] | None, *, model_sha: str,
                       evidence_records: dict[str, dict[str, Any]]) -> tuple[dict[tuple[str, str], int], bool]:
    """Independently check a pinned wallet snapshot against raw RPC evidence.

    A legacy flat map remains readable for diagnostic reports, but cannot make
    a report reconciled: its values have no immutable RPC provenance.
    """
    if value is None or not isinstance(value, dict) or value.get("schema") != WALLET_SNAPSHOT_SCHEMA:
        return _flat_observed_balances(value), False
    required = {"schema", "model_sha", "wallet", "block_hash", "balances", "evidence_record_hashes"}
    if set(value) != required or value.get("model_sha") != model_sha:
        raise VerificationError("wallet_snapshot_shape_or_model_sha")
    wallet = value.get("wallet")
    block_hash = value.get("block_hash")
    if (not isinstance(wallet, str) or not ADDRESS_RE.fullmatch(wallet)
            or not isinstance(block_hash, str) or not BLOCK_HASH_RE.fullmatch(block_hash)):
        raise VerificationError("wallet_snapshot_identity")
    wallet, block_hash = wallet.lower(), block_hash.lower()
    balances = _flat_observed_balances(value.get("balances"))
    raw_hashes = value.get("evidence_record_hashes")
    if (not isinstance(raw_hashes, list) or not raw_hashes
            or any(not isinstance(item, str) or not SHA256_RE.fullmatch(item) for item in raw_hashes)
            or len(set(raw_hashes)) != len(raw_hashes)):
        raise VerificationError("wallet_snapshot_evidence_hashes")
    expected_calls: dict[tuple[str, str], tuple[str, str]] = {}
    for (account, asset), units in balances.items():
        if account == "assets:cash:wallet" and asset == "pUSD":
            expected_calls[(account, asset)] = (PUSD_CONTRACT, _snapshot_call_data(wallet, None))
        elif account == "assets:cash:wallet" and asset == "USDCe":
            expected_calls[(account, asset)] = (USDCE_CONTRACT, _snapshot_call_data(wallet, None))
        elif account == "assets:outcome:position" and asset.startswith("token:") and asset[6:].isdigit():
            token_id = int(asset[6:])
            expected_calls[(account, asset)] = (
                CONDITIONAL_TOKENS_CONTRACT, _snapshot_call_data(wallet, token_id)
            )
        else:
            raise VerificationError("wallet_snapshot_unsupported_balance_account")
        if units < 0:
            raise VerificationError("wallet_snapshot_negative_balance")
    if ("assets:cash:wallet", "pUSD") not in expected_calls or len(expected_calls) != len(raw_hashes):
        raise VerificationError("wallet_snapshot_coverage")
    decoded: dict[tuple[str, str], int] = {}
    for record_hash in raw_hashes:
        raw = evidence_records.get(record_hash)
        if not isinstance(raw, dict) or raw.get("source") != "WALLET_RPC" or raw.get("model_sha") != model_sha:
            raise VerificationError("wallet_snapshot_evidence_source")
        query = raw.get("query")
        if not isinstance(query, dict) or set(query) != {
                "chain_id", "jsonrpc_method", "block_hash", "call_to", "call_data"}:
            raise VerificationError("wallet_snapshot_query")
        if any(not isinstance(item, str) for item in query.values()):
            raise VerificationError("wallet_snapshot_query")
        if (query["chain_id"] != "137" or query["jsonrpc_method"] != "eth_call"
                or query["block_hash"].lower() != block_hash):
            raise VerificationError("wallet_snapshot_query")
        matching = [key for key, (contract, calldata) in expected_calls.items()
                    if query["call_to"].lower() == contract and query["call_data"].lower() == calldata]
        if len(matching) != 1 or matching[0] in decoded:
            raise VerificationError("wallet_snapshot_call_coverage")
        response = raw.get("response")
        result = response.get("result") if isinstance(response, dict) and "error" not in response else None
        if not isinstance(result, str) or not HEX_RE.fullmatch(result):
            raise VerificationError("wallet_snapshot_result")
        units = int(result[2:], 16)
        if units >= 1 << 256:
            raise VerificationError("wallet_snapshot_result")
        decoded[matching[0]] = units
    if decoded != balances:
        raise VerificationError("wallet_snapshot_value_mismatch")
    return balances, True


def _position_snapshot(value: dict[str, Any] | None, *, model_sha: str,
                       evidence_records: dict[str, dict[str, Any]]) -> tuple[dict[str, int], bool]:
    """Independently reconstruct a Data API positions snapshot from raw bytes."""
    if value is None:
        return {}, False
    if not isinstance(value, dict) or set(value) != {
            "schema", "model_sha", "wallet", "evidence_record_hash", "positions"}:
        raise VerificationError("position_snapshot_shape")
    if value.get("schema") != POSITION_SNAPSHOT_SCHEMA or value.get("model_sha") != model_sha:
        raise VerificationError("position_snapshot_identity")
    wallet, evidence_hash, supplied = value.get("wallet"), value.get("evidence_record_hash"), value.get("positions")
    if (not isinstance(wallet, str) or not ADDRESS_RE.fullmatch(wallet)
            or not isinstance(evidence_hash, str) or not SHA256_RE.fullmatch(evidence_hash)
            or not isinstance(supplied, dict)):
        raise VerificationError("position_snapshot_identity")
    wallet = wallet.lower()
    expected: dict[str, int] = {}
    for key, units in supplied.items():
        if (not isinstance(key, str) or not key.startswith("token:") or not key[6:].isdigit()
                or _integer(units, "position_snapshot_units") <= 0):
            raise VerificationError("position_snapshot_positions")
        if key in expected:
            raise VerificationError("position_snapshot_positions")
        expected[key] = units
    raw = evidence_records.get(evidence_hash)
    if (not isinstance(raw, dict) or raw.get("source") != "DATA_API_POSITIONS"
            or raw.get("model_sha") != model_sha):
        raise VerificationError("position_snapshot_evidence_source")
    endpoint = urlparse(str(raw.get("endpoint") or ""))
    query, response = raw.get("query"), raw.get("response")
    if (endpoint.scheme != "https" or endpoint.netloc != "data-api.polymarket.com" or endpoint.path != "/positions"
            or not isinstance(query, dict) or str(query.get("user") or "").lower() != wallet
            or not isinstance(response, list)):
        raise VerificationError("position_snapshot_request")
    decoded: dict[str, int] = {}
    for row in response:
        if not isinstance(row, dict):
            raise VerificationError("position_snapshot_row")
        row_wallet = row.get("proxyWallet", row.get("wallet"))
        if not isinstance(row_wallet, str) or row_wallet.lower() != wallet:
            raise VerificationError("position_snapshot_wallet")
        token = row.get("asset", row.get("tokenId"))
        raw_size = row.get("size")
        if (not isinstance(token, str) or not token.isdigit()
                or isinstance(raw_size, bool) or not isinstance(raw_size, (str, int, float))):
            raise VerificationError("position_snapshot_row")
        try:
            from decimal import Decimal, InvalidOperation
            amount = Decimal(str(raw_size))
        except (InvalidOperation, ValueError) as exc:
            raise VerificationError("position_snapshot_row") from exc
        scaled = amount * Decimal(10) ** 6
        if not amount.is_finite() or amount < 0 or scaled != scaled.to_integral_value():
            raise VerificationError("position_snapshot_row")
        units = int(scaled)
        key = f"token:{int(token)}"
        if key in decoded:
            raise VerificationError("position_snapshot_duplicate_token")
        if units:
            decoded[key] = units
    if decoded != expected:
        raise VerificationError("position_snapshot_value_mismatch")
    return decoded, True


def _activity_coverage(value: dict[str, Any] | None, *, model_sha: str,
                       evidence_records: dict[str, dict[str, Any]]) -> bool:
    """Independently verify a contiguous Data API activity pagination window."""
    if value is None:
        return False
    if not isinstance(value, dict) or set(value) != {"schema", "model_sha", "wallet", "pages", "activity_count"}:
        raise VerificationError("activity_coverage_shape")
    wallet, pages, declared_count = value.get("wallet"), value.get("pages"), value.get("activity_count")
    if (value.get("schema") != ACTIVITY_COVERAGE_SCHEMA or value.get("model_sha") != model_sha
            or not isinstance(wallet, str) or not ADDRESS_RE.fullmatch(wallet) or not isinstance(pages, list)
            or _integer(declared_count, "activity_coverage_count") < 0 or not pages):
        raise VerificationError("activity_coverage_identity")
    wallet = wallet.lower()
    decoded: list[tuple[int, int, int]] = []
    hashes: set[str] = set()
    for page in pages:
        if not isinstance(page, dict) or set(page) != {"offset", "evidence_record_hash"}:
            raise VerificationError("activity_coverage_page")
        offset, record_hash = page.get("offset"), page.get("evidence_record_hash")
        if _integer(offset, "activity_coverage_offset") < 0 or not isinstance(record_hash, str) or not SHA256_RE.fullmatch(record_hash):
            raise VerificationError("activity_coverage_page")
        if record_hash in hashes:
            raise VerificationError("activity_coverage_duplicate_page")
        hashes.add(record_hash)
        raw = evidence_records.get(record_hash)
        if not isinstance(raw, dict) or raw.get("source") != "DATA_API_ACTIVITY" or raw.get("model_sha") != model_sha:
            raise VerificationError("activity_coverage_evidence_source")
        endpoint, query, response = urlparse(str(raw.get("endpoint") or "")), raw.get("query"), raw.get("response")
        if (endpoint.scheme != "https" or endpoint.netloc != "data-api.polymarket.com" or endpoint.path != "/activity"
                or not isinstance(query, dict) or set(query) != {"user", "offset", "limit", "excludeDepositsWithdrawals"}
                or not isinstance(response, list) or any(not isinstance(row, dict) for row in response)):
            raise VerificationError("activity_coverage_request")
        if (str(query.get("user") or "").lower() != wallet or str(query.get("excludeDepositsWithdrawals") or "").lower() != "false"
                or not str(query.get("offset") or "").isdigit() or not str(query.get("limit") or "").isdigit()):
            raise VerificationError("activity_coverage_request")
        if int(query["offset"]) != offset or int(query["limit"]) <= 0 or len(response) > int(query["limit"]):
            raise VerificationError("activity_coverage_page")
        decoded.append((offset, int(query["limit"]), len(response)))
    decoded.sort()
    expected, observed_count = 0, 0
    for index, (offset, limit, count) in enumerate(decoded):
        if offset != expected:
            raise VerificationError("activity_coverage_offset_gap")
        expected += limit
        observed_count += count
        if count < limit and index != len(decoded) - 1:
            raise VerificationError("activity_coverage_page_after_terminal")
    if decoded[-1][2] >= decoded[-1][1]:
        raise VerificationError("activity_coverage_terminal_missing")
    if observed_count != declared_count:
        raise VerificationError("activity_coverage_count_mismatch")
    return True


def _evidence_hashes(path: Path, *, expected_sha: str) -> tuple[dict[str, str], set[str], str, dict[str, dict[str, Any]]]:
    """Independently validate the immutable raw-evidence tape.

    This deliberately duplicates the compact hash-chain checks instead of
    importing the production evidence collector.
    """
    tips: dict[str, str] = {}
    identities: dict[str, set[tuple[str, str]]] = {}
    selected_hashes: dict[str, str] = {}
    selected_sources: set[str] = set()
    selected_records: dict[str, dict[str, Any]] = {}
    digest_file = file_sha256(path)
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise VerificationError(f"evidence_line_{line_number}:invalid_json") from exc
            required = {
                "record_kind", "model_sha", "source", "source_record_id", "received_ts_ms",
                "request_method", "endpoint", "response", "query", "authenticated_read",
                "record_id", "schema_version", "previous_record_hash", "record_hash",
            }
            if not isinstance(raw, dict) or set(raw) != required or raw.get("record_kind") != EVIDENCE_KIND:
                raise VerificationError(f"evidence_line_{line_number}:shape")
            row_sha = raw.get("model_sha")
            if not isinstance(row_sha, str) or not SHA_RE.fullmatch(row_sha):
                raise VerificationError(f"evidence_line_{line_number}:model_sha")
            if raw.get("schema_version") != 1 or raw.get("source") not in EVIDENCE_SOURCES:
                raise VerificationError(f"evidence_line_{line_number}:schema_or_source")
            if not isinstance(raw.get("source_record_id"), str) or not raw["source_record_id"]:
                raise VerificationError(f"evidence_line_{line_number}:source_record_id")
            if _integer(raw.get("received_ts_ms"), "evidence_received_ts") <= 0:
                raise VerificationError(f"evidence_line_{line_number}:received_ts")
            expected_method, expected_scheme = EVIDENCE_RULES[str(raw["source"])]
            endpoint = raw.get("endpoint")
            parsed = urlparse(endpoint if isinstance(endpoint, str) else "")
            if raw.get("request_method") != expected_method or parsed.scheme != expected_scheme or not parsed.netloc:
                raise VerificationError(f"evidence_line_{line_number}:request_not_read_only")
            if str(raw["source"]).startswith("DATA_API_") and parsed.netloc != "data-api.polymarket.com":
                raise VerificationError(f"evidence_line_{line_number}:data_api_endpoint")
            if (raw["source"] == "CLOB_USER_WS"
                    and (parsed.netloc != "ws-subscriptions-clob.polymarket.com" or parsed.path != "/ws/user")):
                raise VerificationError(f"evidence_line_{line_number}:clob_endpoint")
            if raw["source"] in {"CLOB_USER_TRADES", "CLOB_USER_ORDERS"} and parsed.netloc != "clob.polymarket.com":
                raise VerificationError(f"evidence_line_{line_number}:clob_endpoint")
            if str(raw["source"]).startswith("CLOB_USER_") and raw.get("authenticated_read") is not True:
                raise VerificationError(f"evidence_line_{line_number}:authenticated_read")
            if not isinstance(raw.get("query"), dict) or not isinstance(raw.get("authenticated_read"), bool):
                raise VerificationError(f"evidence_line_{line_number}:request_shape")
            if not isinstance(raw.get("previous_record_hash"), str) or not SHA256_RE.fullmatch(str(raw.get("record_hash"))):
                raise VerificationError(f"evidence_line_{line_number}:hash_shape")
            expected_previous = tips.get(row_sha, GENESIS_HASH)
            if raw["previous_record_hash"] != expected_previous:
                raise VerificationError(f"evidence_line_{line_number}:chain_break")
            payload = dict(raw)
            payload.pop("record_hash")
            if digest(payload) != raw["record_hash"]:
                raise VerificationError(f"evidence_line_{line_number}:hash_mismatch")
            if raw["source"] == "CLOB_USER_WS":
                try:
                    _clob_user_ws_event(raw)
                except VerificationError as exc:
                    raise VerificationError(f"evidence_line_{line_number}:{exc}") from exc
            key = (str(raw["source"]), str(raw["source_record_id"]))
            row_identities = identities.setdefault(row_sha, set())
            if key in row_identities:
                raise VerificationError(f"evidence_line_{line_number}:duplicate_source_record")
            row_identities.add(key)
            tips[row_sha] = str(raw["record_hash"])
            if row_sha == expected_sha:
                selected_hashes[str(raw["record_hash"])] = str(raw["source"])
                selected_records[str(raw["record_hash"])] = raw
                selected_sources.add(str(raw["source"]))
    return selected_hashes, selected_sources, digest_file, selected_records


def _provenance_records(path: Path, *, expected_sha: str, evidence_hashes: dict[str, str],
                        evidence_records: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, str], str]:
    """Independently validate the decision -> signed order -> settlement chain.

    The verifier deliberately repeats the tape's small schema and hash checks;
    a production provenance module is never imported into the audit process.
    """
    global_tips: dict[str, str] = {}
    lineage_tips: dict[tuple[str, str], tuple[str, str]] = {}
    seen_ids: set[str] = set()
    selected_terminal: dict[str, dict[str, Any]] = {}
    selected_fill_evidence: dict[str, str] = {}
    selected_fill_lineage: dict[str, str] = {}
    digest_file = file_sha256(path)
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise VerificationError(f"provenance_line_{line_number}:invalid_json") from exc
            required = {
                "record_kind", "model_sha", "lineage_id", "stage", "event_ts_ms", "payload",
                "evidence_record_hash", "record_id", "schema_version", "previous_stage_hash",
                "previous_record_hash", "record_hash",
            }
            if not isinstance(raw, dict) or set(raw) != required or raw.get("record_kind") != PROVENANCE_KIND:
                raise VerificationError(f"provenance_line_{line_number}:shape")
            row_sha, lineage_id, stage = raw.get("model_sha"), raw.get("lineage_id"), raw.get("stage")
            if (not isinstance(row_sha, str) or not SHA_RE.fullmatch(row_sha)
                    or not isinstance(lineage_id, str) or not lineage_id
                    or stage not in PROVENANCE_STAGE_INDEX
                    or raw.get("schema_version") != 1
                    or _integer(raw.get("event_ts_ms"), "provenance_event_ts") <= 0):
                raise VerificationError(f"provenance_line_{line_number}:identity")
            record_id = raw.get("record_id")
            if not isinstance(record_id, str) or not record_id or record_id in seen_ids:
                raise VerificationError(f"provenance_line_{line_number}:record_id")
            seen_ids.add(record_id)
            payload = raw.get("payload")
            if (not isinstance(payload, dict) or set(payload) != PROVENANCE_PAYLOAD_KEYS[stage]
                    or any(not isinstance(value, str) or not SHA256_RE.fullmatch(value)
                           for value in payload.values())):
                raise VerificationError(f"provenance_line_{line_number}:payload")
            evidence_hash = raw.get("evidence_record_hash")
            evidence_required = stage in {"CLOB_ACCEPTED", "FILL", "SETTLEMENT"}
            if evidence_required != (evidence_hash is not None):
                raise VerificationError(f"provenance_line_{line_number}:evidence_requirement")
            if evidence_required and (not isinstance(evidence_hash, str) or evidence_hash not in evidence_hashes):
                raise VerificationError(f"provenance_line_{line_number}:evidence_link")
            if stage in {"CLOB_ACCEPTED", "FILL"}:
                raw_evidence = evidence_records.get(str(evidence_hash))
                if not isinstance(raw_evidence, dict):
                    raise VerificationError(f"provenance_line_{line_number}:clob_evidence_link")
                try:
                    _clob_provenance_stage_matches_event(stage, lineage_id=lineage_id, evidence=raw_evidence)
                except VerificationError as exc:
                    raise VerificationError(f"provenance_line_{line_number}:{exc}") from exc
            if stage == "SETTLEMENT" and evidence_hashes.get(str(evidence_hash)) != "POLYGON_RPC":
                raise VerificationError(f"provenance_line_{line_number}:settlement_evidence_source")
            if (not isinstance(raw.get("previous_record_hash"), str)
                    or not isinstance(raw.get("previous_stage_hash"), str)
                    or not SHA256_RE.fullmatch(str(raw.get("record_hash")))):
                raise VerificationError(f"provenance_line_{line_number}:hash_shape")
            if raw["previous_record_hash"] != global_tips.get(row_sha, GENESIS_HASH):
                raise VerificationError(f"provenance_line_{line_number}:global_chain_break")
            key = (row_sha, lineage_id)
            previous = lineage_tips.get(key)
            if previous is None:
                if not _provenance_next_stage_is_valid(None, stage) or raw["previous_stage_hash"] != GENESIS_HASH:
                    raise VerificationError(f"provenance_line_{line_number}:lineage_start")
            elif (not _provenance_next_stage_is_valid(previous[0], stage)
                  or raw["previous_stage_hash"] != previous[1]):
                raise VerificationError(f"provenance_line_{line_number}:lineage_stage_break")
            payload_for_hash = dict(raw)
            payload_for_hash.pop("record_hash")
            if digest(payload_for_hash) != raw["record_hash"]:
                raise VerificationError(f"provenance_line_{line_number}:hash_mismatch")
            global_tips[row_sha] = str(raw["record_hash"])
            lineage_tips[key] = (stage, str(raw["record_hash"]))
            if row_sha == expected_sha:
                selected_terminal[lineage_id] = raw
                if stage == "FILL":
                    selected_fill_evidence[str(raw["record_hash"])] = str(evidence_hash)
                    selected_fill_lineage[str(raw["record_hash"])] = lineage_id
    complete = {lineage_id: record for lineage_id, record in selected_terminal.items()
                if record["stage"] == "SETTLEMENT"}
    complete_ids = set(complete)
    selected_fill_evidence = {record_hash: evidence_hash for record_hash, evidence_hash
                              in selected_fill_evidence.items()
                              if selected_fill_lineage.get(record_hash) in complete_ids}
    selected_fill_lineage = {record_hash: lineage_id for record_hash, lineage_id
                             in selected_fill_lineage.items() if lineage_id in complete_ids}
    return complete, selected_fill_evidence, selected_fill_lineage, digest_file


def verify(ledger_path: Path, *, model_sha: str,
           observed_balances: dict[str, Any] | None = None,
           observed_positions: dict[str, Any] | None = None,
           observed_activity_coverage: dict[str, Any] | None = None,
           evidence_path: Path | None = None,
           provenance_path: Path | None = None,
           require_live_sources: bool = True) -> dict[str, Any]:
    """Rebuild terminal PnL from exact-SHA journal rows without production code."""
    if not SHA_RE.fullmatch(model_sha):
        raise VerificationError("expected_model_sha_not_exact_git_sha")
    evidence_hashes: dict[str, str] = {}
    evidence_sources: set[str] = set()
    evidence_sha256: str | None = None
    evidence_records: dict[str, dict[str, Any]] = {}
    if evidence_path is not None:
        evidence_hashes, evidence_sources, evidence_sha256, evidence_records = _evidence_hashes(
            Path(evidence_path), expected_sha=model_sha
        )
    complete_lineages: dict[str, dict[str, Any]] = {}
    fill_provenance_evidence: dict[str, str] = {}
    fill_provenance_lineage: dict[str, str] = {}
    provenance_sha256: str | None = None
    if provenance_path is not None:
        complete_lineages, fill_provenance_evidence, fill_provenance_lineage, provenance_sha256 = _provenance_records(
            Path(provenance_path), expected_sha=model_sha, evidence_hashes=evidence_hashes,
            evidence_records=evidence_records,
        )
    balances: dict[tuple[str, str], int] = {}
    sources: set[str] = set()
    source_ids_by_sha: dict[str, set[tuple[str, str]]] = {}
    tips: dict[str, str] = {}
    count = 0
    all_live = True
    evidence_reference_breaks: list[str] = []
    clob_fill_evidence_breaks: list[str] = []
    polygon_lifecycle_evidence_breaks: list[str] = []
    provenance_reference_breaks: list[str] = []
    settlement_provenance_links: set[tuple[str, str]] = set()
    journal_started = False
    with Path(ledger_path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise VerificationError(f"line_{line_number}:invalid_json") from exc
            if not isinstance(raw, dict) or raw.get("record_kind") != "ECONOMIC_JOURNAL":
                continue
            row_sha = raw.get("model_sha")
            if not isinstance(row_sha, str) or not SHA_RE.fullmatch(row_sha):
                raise VerificationError(f"line_{line_number}:journal_model_sha")
            entry = _validate_entry(raw, expected_sha=row_sha,
                                    expected_previous=tips.get(row_sha, GENESIS_HASH),
                                    source_ids=source_ids_by_sha.setdefault(row_sha, set()))
            tips[row_sha] = str(entry["entry_hash"])
            if row_sha != model_sha:
                continue
            journal_started = True
            count += 1
            sources.add(str(entry["source"]))
            all_live = all_live and entry["execution_mode"] == "LIVE_OBSERVED" and bool(entry["authenticated_execution"])
            if entry["execution_mode"] == "LIVE_OBSERVED":
                evidence_hash = (entry.get("metadata") or {}).get("evidence_record_hash")
                compatible_sources = JOURNAL_EVIDENCE_COMPATIBILITY.get(str(entry.get("source")), set())
                if (not isinstance(evidence_hash, str) or evidence_hash not in evidence_hashes
                        or evidence_hashes.get(evidence_hash) not in compatible_sources):
                    evidence_reference_breaks.append(str(entry.get("entry_id") or "unknown"))
                elif entry["entry_type"] == "TRADE_FILL" and entry["source"] == "CLOB_USER_WS":
                    raw_evidence = evidence_records.get(evidence_hash)
                    if not isinstance(raw_evidence, dict) or not _clob_fill_matches_journal(entry, raw_evidence):
                        clob_fill_evidence_breaks.append(str(entry.get("entry_id") or "unknown"))
                elif entry["entry_type"] == "TOKEN_REDEEM":
                    raw_evidence = evidence_records.get(evidence_hash)
                    if not isinstance(raw_evidence, dict) or not _polygon_redeem_matches_journal(entry, raw_evidence):
                        polygon_lifecycle_evidence_breaks.append(str(entry.get("entry_id") or "unknown"))
                if str(entry.get("source")) in {"CLOB_USER_WS", "CLOB_API"}:
                    provenance_hash = (entry.get("metadata") or {}).get("provenance_record_hash")
                    matched = (bool(complete_lineages)
                               and fill_provenance_evidence.get(str(provenance_hash)) == evidence_hash
                               and fill_provenance_lineage.get(str(provenance_hash))
                               == (entry.get("metadata") or {}).get("clob_taker_order_id"))
                    if not isinstance(provenance_hash, str) or not SHA256_RE.fullmatch(provenance_hash) or not matched:
                        provenance_reference_breaks.append(str(entry.get("entry_id") or "unknown"))
                if str(entry.get("entry_type")) in {"TOKEN_REDEEM", "SETTLEMENT"}:
                    settlement_hash = (entry.get("metadata") or {}).get("provenance_record_hash")
                    if isinstance(settlement_hash, str) and SHA256_RE.fullmatch(settlement_hash) and isinstance(evidence_hash, str):
                        settlement_provenance_links.add((settlement_hash, evidence_hash))
            for posting in entry["postings"]:
                key = (posting["account"], posting["asset"])
                balances[key] = balances.get(key, 0) + int(posting["units"])
    if not journal_started:
        raise VerificationError("journal_no_exact_sha_entries")

    expected, wallet_snapshot_verified = _snapshot_balances(
        observed_balances, model_sha=model_sha, evidence_records=evidence_records
    )
    observed_position_units, position_snapshot_verified = _position_snapshot(
        observed_positions, model_sha=model_sha, evidence_records=evidence_records
    )
    activity_coverage_verified = _activity_coverage(
        observed_activity_coverage, model_sha=model_sha, evidence_records=evidence_records
    )
    settlement_provenance_breaks = sorted(
        lineage_id for lineage_id, terminal in complete_lineages.items()
        if (str(terminal.get("record_hash")), str(terminal.get("evidence_record_hash")))
        not in settlement_provenance_links
    )
    balance_breaks = []
    for key, observed in sorted(expected.items()):
        if balances.get(key, 0) != observed:
            balance_breaks.append({"account": key[0], "asset": key[1], "ledger_units": balances.get(key, 0), "observed_units": observed})
    owned_keys = {
        key for key in balances
        if key[0].startswith("assets:") or key[0].startswith("liabilities:")
    }
    missing_observed_balances = sorted(
        f"{account}|{asset}" for account, asset in owned_keys - set(expected)
    )
    unexpected_observed_balances = sorted(
        f"{account}|{asset}" for account, asset in set(expected) - owned_keys
    )

    pUSD_cash_units = sum(units for (account, asset), units in balances.items()
                          if account.startswith("assets:cash:") and asset == "pUSD")
    usdce_cash_units = sum(units for (account, asset), units in balances.items()
                           if account.startswith("assets:cash:") and asset == "USDCe")
    cash_units = pUSD_cash_units + usdce_cash_units
    external_funding_units = sum(units for (account, asset), units in balances.items()
                                 if account == "equity:external_funding" and asset == "pUSD")
    open_outcomes = {
        f"{account}|{asset}": units for (account, asset), units in balances.items()
        if account.startswith("assets:outcome:") and units != 0
    }
    reasons: list[str] = []
    if not all_live:
        reasons.append("not_all_entries_live_observed")
    if (require_live_sources and (not REQUIRED_JOURNAL_SOURCES.issubset(sources)
                                  or not REQUIRED_EVIDENCE_SOURCES.issubset(evidence_sources))):
        reasons.append("required_independent_sources_missing")
    if not expected:
        reasons.append("observed_balances_missing")
    elif not wallet_snapshot_verified:
        reasons.append("wallet_snapshot_unverifiable")
    if not position_snapshot_verified:
        reasons.append("data_api_position_snapshot_missing")
    if not activity_coverage_verified:
        reasons.append("data_api_activity_coverage_missing")
    if evidence_path is None:
        reasons.append("evidence_tape_missing")
    elif not evidence_hashes:
        reasons.append("no_exact_sha_evidence_records")
    if evidence_reference_breaks:
        reasons.append("journal_evidence_reference_break")
    if clob_fill_evidence_breaks:
        reasons.append("journal_clob_fill_evidence_break")
    if polygon_lifecycle_evidence_breaks:
        reasons.append("journal_polygon_lifecycle_evidence_break")
    if provenance_path is None:
        reasons.append("provenance_tape_missing")
    elif not complete_lineages:
        reasons.append("complete_execution_provenance_missing")
    if provenance_reference_breaks:
        reasons.append("journal_provenance_reference_break")
    if settlement_provenance_breaks:
        reasons.append("settlement_provenance_reference_break")
    if missing_observed_balances or unexpected_observed_balances:
        reasons.append("observed_balance_coverage_break")
    if balance_breaks:
        reasons.append("observed_balance_reconciliation_break")
    if open_outcomes:
        reasons.append("open_outcome_inventory")
    ledger_outcome_units = {asset: units for (account, asset), units in balances.items()
                            if account == "assets:outcome:position" and units != 0}
    if ledger_outcome_units != observed_position_units:
        reasons.append("data_api_position_reconciliation_break")

    # Deposits credit equity:external_funding (negative balance); withdrawals
    # debit it.  Therefore terminal cash plus that equity balance is PnL after
    # every fee, rebate, reward, gas, split/merge/redeem and settlement flow.
    report = {
        "schema": SCHEMA,
        "model_sha": model_sha,
        "ledger_path": str(ledger_path),
        "ledger_sha256": file_sha256(Path(ledger_path)),
        "evidence_tape_path": str(evidence_path) if evidence_path is not None else None,
        "evidence_tape_sha256": evidence_sha256,
        "provenance_tape_path": str(provenance_path) if provenance_path is not None else None,
        "provenance_tape_sha256": provenance_sha256,
        "complete_execution_lineages": sorted(complete_lineages),
        "evidence_sources_seen": sorted(evidence_sources),
        "journal_evidence_reference_breaks": evidence_reference_breaks,
        "journal_clob_fill_evidence_breaks": clob_fill_evidence_breaks,
        "journal_polygon_lifecycle_evidence_breaks": polygon_lifecycle_evidence_breaks,
        "journal_provenance_reference_breaks": provenance_reference_breaks,
        "settlement_provenance_reference_breaks": settlement_provenance_breaks,
        "journal_entries": count,
        "journal_head_hash": tips.get(model_sha, GENESIS_HASH),
        "sources_seen": sorted(sources),
        "all_entries_live_observed": all_live,
        "integer_base_asset": "pUSD",
        "terminal_pusd_cash_units": pUSD_cash_units,
        "terminal_usdce_cash_units": usdce_cash_units,
        "terminal_cash_units": cash_units,
        "external_funding_units": external_funding_units,
        "reconstructed_realized_pnl_units": cash_units + external_funding_units,
        "open_outcome_positions": open_outcomes,
        "data_api_position_snapshot_verified": position_snapshot_verified,
        "data_api_activity_coverage_verified": activity_coverage_verified,
        "data_api_observed_positions": observed_position_units,
        "missing_observed_balances": missing_observed_balances,
        "unexpected_observed_balances": unexpected_observed_balances,
        "observed_balance_breaks": balance_breaks,
        "wallet_snapshot_verified": wallet_snapshot_verified,
        "reason_codes": sorted(reasons),
        "state": "REAL_PNL_RECONCILED_UNSIGNED" if not reasons else "MORE_EVIDENCE_REQUIRED",
        "real_pnl_verified": False,
    }
    report["report_sha256"] = digest(report)
    return report


def attest(report: dict[str, Any], *, operator_id: str, signing_key: str) -> dict[str, Any]:
    """Add an HMAC-SHA256 attestation using a private runtime secret.

    The secret is supplied at runtime and must never be committed.  An unsigned
    or non-reconciled report is deliberately not eligible for REAL_PNL_VERIFIED.
    """
    if report.get("state") != "REAL_PNL_RECONCILED_UNSIGNED" or report.get("reason_codes"):
        raise VerificationError("attestation:report_not_reconciled")
    if not operator_id.strip() or not signing_key:
        raise VerificationError("attestation:operator_or_key_missing")
    payload = {
        "schema": ATTESTATION_SCHEMA,
        "operator_id": operator_id,
        "report_sha256": report.get("report_sha256"),
        "model_sha": report.get("model_sha"),
        "journal_head_hash": report.get("journal_head_hash"),
    }
    if not SHA256_RE.fullmatch(str(payload["report_sha256"])) or not SHA_RE.fullmatch(str(payload["model_sha"])):
        raise VerificationError("attestation:report_identity_invalid")
    signature = hmac.new(signing_key.encode("utf-8"), canonical_bytes(payload), hashlib.sha256).hexdigest()
    return {**payload, "algorithm": "HMAC-SHA256", "signature": signature}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--observed-balances", type=Path, required=True,
                        help="traceable v7_wallet_balance_snapshot JSON; flat maps stay diagnostic-only")
    parser.add_argument("--evidence-tape", type=Path, required=True)
    parser.add_argument("--provenance-tape", type=Path, required=True)
    parser.add_argument("--observed-positions", type=Path, required=True,
                        help="traceable v7_data_api_position_snapshot JSON")
    parser.add_argument("--observed-activity-coverage", type=Path, required=True,
                        help="traceable v7_data_api_activity_coverage JSON")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--attestation-output", type=Path)
    parser.add_argument("--operator-id")
    parser.add_argument("--signing-key-env", default="V7_ATTESTATION_HMAC_KEY")
    args = parser.parse_args()
    observed = json.loads(args.observed_balances.read_text(encoding="utf-8"))
    positions = json.loads(args.observed_positions.read_text(encoding="utf-8"))
    coverage = json.loads(args.observed_activity_coverage.read_text(encoding="utf-8"))
    report = verify(args.ledger, model_sha=args.model_sha, observed_balances=observed,
                    observed_positions=positions, observed_activity_coverage=coverage,
                    evidence_path=args.evidence_tape,
                    provenance_path=args.provenance_tape)
    if args.attestation_output:
        key = os.environ.get(args.signing_key_env, "")
        attestation = attest(report, operator_id=args.operator_id or "", signing_key=key)
        report["attestation"] = attestation
        report["real_pnl_verified"] = True
        report["state"] = "REAL_PNL_VERIFIED"
    rendered = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    if args.attestation_output:
        args.attestation_output.write_text(json.dumps(report["attestation"], sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
