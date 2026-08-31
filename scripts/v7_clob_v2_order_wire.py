#!/usr/bin/env python3
"""Fail-closed Polymarket CLOB V2 order wire builder.

This is deliberately not an execution client.  It builds the documented
EIP-712 order payload and the exact compact JSON bytes for ``POST /order``;
an external signer supplies the wallet signature and the caller may pass those
same bytes to the separate L2-authentication builder.  It never reads a secret,
opens a connection, or submits an order.
"""
from __future__ import annotations

import json
import re
from typing import Any


CHAIN_ID = 137
STANDARD_EXCHANGE = "0xe111180000d2663c0091e4f400237545b87b996b"
NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310f59"
ZERO_BYTES32 = "0x" + "0" * 64
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
BYTES32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
HEX_RE = re.compile(r"^0x(?:[0-9a-fA-F]{2})+$")
UINT_RE = re.compile(r"^[0-9]+$")
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991

ORDER_FIELDS = (
    ("salt", "uint256"), ("maker", "address"), ("signer", "address"),
    ("tokenId", "uint256"), ("makerAmount", "uint256"), ("takerAmount", "uint256"),
    ("side", "uint8"), ("signatureType", "uint8"), ("timestamp", "uint256"),
    ("metadata", "bytes32"), ("builder", "bytes32"),
)
TYPED_DATA_SIGN_FIELDS = (
    ("contents", "Order"), ("name", "string"), ("version", "string"),
    ("chainId", "uint256"), ("verifyingContract", "address"), ("salt", "bytes32"),
)


class ClobOrderWireError(ValueError):
    pass


def _address(field: str, value: Any) -> str:
    if not isinstance(value, str) or not ADDRESS_RE.fullmatch(value):
        raise ClobOrderWireError(f"{field}:invalid_address")
    return value.lower()


def _bytes32(field: str, value: Any) -> str:
    if not isinstance(value, str) or not BYTES32_RE.fullmatch(value):
        raise ClobOrderWireError(f"{field}:invalid_bytes32")
    return value.lower()


def _uint(field: str, value: Any, *, positive: bool = False, json_safe: bool = False) -> str:
    if isinstance(value, bool):
        raise ClobOrderWireError(f"{field}:invalid_uint")
    rendered = str(value)
    if not UINT_RE.fullmatch(rendered) or (positive and int(rendered) <= 0):
        raise ClobOrderWireError(f"{field}:invalid_uint")
    if json_safe and int(rendered) > MAX_SAFE_JSON_INTEGER:
        raise ClobOrderWireError(f"{field}:not_json_safe")
    return rendered


def _side(value: Any) -> tuple[str, int]:
    if value not in {"BUY", "SELL"}:
        raise ClobOrderWireError("side:invalid")
    return str(value), 0 if value == "BUY" else 1


def _signature(value: Any) -> str:
    if not isinstance(value, str) or not HEX_RE.fullmatch(value) or len(value) < 132:
        raise ClobOrderWireError("signature:invalid_hex")
    return value.lower()


def _order_message(*, maker: Any, signer: Any, token_id: Any, maker_amount: Any,
                   taker_amount: Any, side: Any, signature_type: Any, timestamp_ms: Any,
                   salt: Any, metadata: Any, builder: Any) -> dict[str, Any]:
    side_text, side_code = _side(side)
    signature_type_text = _uint("signature_type", signature_type)
    if int(signature_type_text) not in {0, 1, 2, 3}:
        raise ClobOrderWireError("signature_type:unsupported")
    return {
        "salt": _uint("salt", salt, positive=True, json_safe=True),
        "maker": _address("maker", maker), "signer": _address("signer", signer),
        "tokenId": _uint("token_id", token_id, positive=True),
        "makerAmount": _uint("maker_amount", maker_amount, positive=True),
        "takerAmount": _uint("taker_amount", taker_amount, positive=True),
        "side": side_code, "signatureType": int(signature_type_text),
        "timestamp": _uint("timestamp_ms", timestamp_ms, positive=True),
        "metadata": _bytes32("metadata", metadata), "builder": _bytes32("builder", builder),
        # Kept solely for the final JSON body, never included in typed data.
        "_wire_side": side_text,
    }


def order_typed_data(*, maker: str, signer: str, token_id: str | int, maker_amount: str | int,
                     taker_amount: str | int, side: str, signature_type: int,
                     timestamp_ms: str | int, salt: str | int, neg_risk: bool,
                     metadata: str = ZERO_BYTES32, builder: str = ZERO_BYTES32,
                     wallet_kind: str = "standard") -> dict[str, Any]:
    """Build V2 typed data for a standard or ERC-7739 Deposit Wallet signer."""
    if not isinstance(neg_risk, bool):
        raise ClobOrderWireError("neg_risk:invalid")
    order = _order_message(maker=maker, signer=signer, token_id=token_id,
                           maker_amount=maker_amount, taker_amount=taker_amount,
                           side=side, signature_type=signature_type,
                           timestamp_ms=timestamp_ms, salt=salt,
                           metadata=metadata, builder=builder)
    order.pop("_wire_side")
    exchange = NEG_RISK_EXCHANGE if neg_risk else STANDARD_EXCHANGE
    domain = {"name": "Polymarket CTF Exchange", "version": "2",
              "chainId": CHAIN_ID, "verifyingContract": exchange}
    order_types = [{"name": name, "type": type_name} for name, type_name in ORDER_FIELDS]
    if wallet_kind == "standard":
        if order["signatureType"] == 3:
            raise ClobOrderWireError("wallet_kind:deposit_required_for_signature_type_3")
        return {
            "domain": domain, "types": {"Order": order_types},
            "primaryType": "Order", "message": order,
        }
    if wallet_kind != "deposit":
        raise ClobOrderWireError("wallet_kind:unsupported")
    if order["signatureType"] != 3:
        raise ClobOrderWireError("wallet_kind:deposit_requires_signature_type_3")
    return {
        "domain": domain,
        "types": {"Order": order_types,
                  "TypedDataSign": [{"name": name, "type": type_name}
                                    for name, type_name in TYPED_DATA_SIGN_FIELDS]},
        "primaryType": "TypedDataSign",
        "message": {"contents": order, "name": "DepositWallet", "version": "1",
                    "chainId": CHAIN_ID, "verifyingContract": order["maker"],
                    "salt": ZERO_BYTES32},
    }


def _typed_order(typed_data: Any) -> dict[str, Any]:
    if not isinstance(typed_data, dict) or set(typed_data) != {"domain", "types", "primaryType", "message"}:
        raise ClobOrderWireError("typed_data:shape")
    domain = typed_data.get("domain")
    message = typed_data.get("message")
    types = typed_data.get("types")
    order_types = [{"name": name, "type": type_name} for name, type_name in ORDER_FIELDS]
    if (not isinstance(domain, dict) or domain != {"name": "Polymarket CTF Exchange", "version": "2",
                                                    "chainId": CHAIN_ID,
                                                    "verifyingContract": domain.get("verifyingContract")}
            or domain["verifyingContract"] not in {STANDARD_EXCHANGE, NEG_RISK_EXCHANGE}
            or not isinstance(types, dict) or not isinstance(message, dict)):
        raise ClobOrderWireError("typed_data:contract")
    if typed_data["primaryType"] == "Order":
        if types != {"Order": order_types}:
            raise ClobOrderWireError("typed_data:types")
        order = message
        deposit = False
    elif typed_data["primaryType"] == "TypedDataSign":
        expected_types = {"Order": order_types,
                          "TypedDataSign": [{"name": name, "type": type_name}
                                            for name, type_name in TYPED_DATA_SIGN_FIELDS]}
        if types != expected_types or set(message) != {name for name, _ in TYPED_DATA_SIGN_FIELDS}:
            raise ClobOrderWireError("typed_data:types")
        if (message.get("name") != "DepositWallet" or message.get("version") != "1"
                or message.get("chainId") != CHAIN_ID or message.get("salt") != ZERO_BYTES32):
            raise ClobOrderWireError("typed_data:deposit_wrapper")
        order = message.get("contents")
        deposit = True
    else:
        raise ClobOrderWireError("typed_data:primary_type")
    required = {name for name, _ in ORDER_FIELDS}
    if not isinstance(order, dict) or set(order) != required:
        raise ClobOrderWireError("typed_data:message_shape")
    wire = _order_message(maker=order["maker"], signer=order["signer"],
                          token_id=order["tokenId"], maker_amount=order["makerAmount"],
                          taker_amount=order["takerAmount"],
                          side="BUY" if order["side"] == 0 else "SELL" if order["side"] == 1 else None,
                          signature_type=order["signatureType"], timestamp_ms=order["timestamp"],
                          salt=order["salt"], metadata=order["metadata"], builder=order["builder"])
    if deposit:
        if wire["signatureType"] != 3 or message["verifyingContract"] != wire["maker"]:
            raise ClobOrderWireError("typed_data:deposit_wrapper")
    elif wire["signatureType"] == 3:
        raise ClobOrderWireError("typed_data:deposit_wrapper_required")
    return wire


def order_submission_bytes(typed_data: dict[str, Any], *, signature: str, owner: str,
                           order_type: str = "GTC", expiration: str | int = 0,
                           post_only: bool = False, defer_exec: bool = False) -> bytes:
    """Serialize one `/order` request once, before L2 signing, with no mutation."""
    wire = _typed_order(typed_data)
    if order_type not in {"GTC", "GTD", "FOK", "FAK"}:
        raise ClobOrderWireError("order_type:unsupported")
    expiration_text = _uint("expiration", expiration)
    if order_type == "GTC" and expiration_text != "0":
        raise ClobOrderWireError("expiration:gtc_must_be_zero")
    if order_type != "GTC" and int(expiration_text) <= 0:
        raise ClobOrderWireError("expiration:required")
    if not isinstance(post_only, bool) or not isinstance(defer_exec, bool):
        raise ClobOrderWireError("submission_flags:invalid")
    body = {
        "deferExec": defer_exec,
        "order": {"builder": wire["builder"], "expiration": expiration_text,
                  "maker": wire["maker"], "makerAmount": wire["makerAmount"],
                  "metadata": wire["metadata"], "salt": int(wire["salt"]),
                  "side": wire["_wire_side"], "signature": _signature(signature),
                  "signatureType": wire["signatureType"], "signer": wire["signer"],
                  "takerAmount": wire["takerAmount"], "timestamp": wire["timestamp"],
                  "tokenId": wire["tokenId"]},
        "orderType": order_type, "owner": _text("owner", owner), "postOnly": post_only,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def l2_signed_order_request(auth_module: Any, credentials: Any, *, address: str,
                            timestamp: str | int, typed_data: dict[str, Any], signature: str,
                            owner: str, order_type: str = "GTC", expiration: str | int = 0,
                            post_only: bool = False, defer_exec: bool = False) -> Any:
    """Build exactly once, then delegate L2 authentication over those exact bytes.

    ``auth_module`` is injected to keep this wire builder free of credential
    imports and environment access. It must expose ``l2_signed_request``. The
    result is only an in-memory signed request; this module never transports it.
    """
    signer = getattr(auth_module, "l2_signed_request", None)
    if not callable(signer):
        raise ClobOrderWireError("auth_module:l2_signer_missing")
    body = order_submission_bytes(typed_data, signature=signature, owner=owner,
                                  order_type=order_type, expiration=expiration,
                                  post_only=post_only, defer_exec=defer_exec)
    return signer(credentials, address=_address("address", address), timestamp=timestamp,
                  method="POST", path="/order", body=body)


def _text(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ClobOrderWireError(f"{field}:invalid")
    return value
