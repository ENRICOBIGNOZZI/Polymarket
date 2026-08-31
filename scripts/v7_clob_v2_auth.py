#!/usr/bin/env python3
"""Exact CLOB V2 authentication wire contracts, with no network transport.

L1 EIP-712 signing remains in an external signer.  This module only validates
the resulting L1 headers, creates L2 HMAC request headers over the exact wire
body, and builds the authenticated user-WebSocket subscription frame.  It
never reads environment variables, writes credentials, or sends a request.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Final


ADDRESS_RE: Final = re.compile(r"^0x[0-9a-fA-F]{40}$")
INTEGER_RE: Final = re.compile(r"^[0-9]+$")
BASE64_RE: Final = re.compile(r"^[A-Za-z0-9_+/=-]+$")
L1_MESSAGE: Final = "This message attests that I control the given wallet"
L1_CHAIN_ID: Final = 137
L1_DOMAIN: Final = {"name": "ClobAuthDomain", "version": "1", "chainId": L1_CHAIN_ID}
USER_WEBSOCKET_URL: Final = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class ClobAuthError(ValueError):
    pass


def _text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ClobAuthError(f"{name}:missing")
    if value != value.strip():
        raise ClobAuthError(f"{name}:whitespace")
    return value


def _address(value: str) -> str:
    value = _text("address", value)
    if not ADDRESS_RE.fullmatch(value):
        raise ClobAuthError("address:invalid")
    return value


def _timestamp(value: str | int) -> str:
    rendered = str(value)
    if not INTEGER_RE.fullmatch(rendered) or int(rendered) <= 0:
        raise ClobAuthError("timestamp:invalid")
    return rendered


def _request_path(path: str) -> str:
    path = _text("path", path)
    if not path.startswith("/") or "?" in path or "#" in path:
        raise ClobAuthError("path:must_be_query_free_absolute_path")
    return path


def _base64_secret(secret: str) -> bytes:
    secret = _text("secret", secret)
    if not BASE64_RE.fullmatch(secret):
        raise ClobAuthError("secret:not_base64")
    padded = secret + "=" * (-len(secret) % 4)
    try:
        decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ClobAuthError("secret:not_base64") from exc
    if not decoded:
        raise ClobAuthError("secret:empty")
    return decoded


@dataclass(frozen=True, repr=False)
class L2Credentials:
    """In-memory CLOB L2 credentials; deliberately hidden from repr()."""

    api_key: str
    secret: str
    passphrase: str

    def validate(self) -> None:
        _text("api_key", self.api_key)
        _base64_secret(self.secret)
        _text("passphrase", self.passphrase)


@dataclass(frozen=True)
class SignedRequest:
    method: str
    path: str
    body: bytes
    headers: dict[str, str]


def l1_typed_data(address: str, timestamp: str | int, nonce: int) -> dict[str, object]:
    """Return the exact typed-data shape an external EIP-712 signer must sign."""
    if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce < 0:
        raise ClobAuthError("nonce:invalid")
    return {
        "domain": dict(L1_DOMAIN),
        "types": {"ClobAuth": [
            {"name": "address", "type": "address"},
            {"name": "timestamp", "type": "string"},
            {"name": "nonce", "type": "uint256"},
            {"name": "message", "type": "string"},
        ]},
        "primaryType": "ClobAuth",
        "message": {"address": _address(address), "timestamp": _timestamp(timestamp),
                    "nonce": nonce, "message": L1_MESSAGE},
    }


def l1_headers(address: str, signature: str, timestamp: str | int, nonce: int) -> dict[str, str]:
    """Build L1 headers for ``POST /auth/api-key`` from an external signature."""
    l1_typed_data(address, timestamp, nonce)
    signature = _text("l1_signature", signature)
    if not signature.startswith("0x"):
        raise ClobAuthError("l1_signature:invalid")
    return {"POLY_ADDRESS": _address(address), "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": _timestamp(timestamp), "POLY_NONCE": str(nonce)}


def l2_signing_message(timestamp: str | int, method: str, path: str, body: bytes = b"") -> bytes:
    method = _text("method", method).upper()
    if method not in {"GET", "POST", "DELETE"}:
        raise ClobAuthError("method:unsupported")
    if not isinstance(body, bytes):
        raise ClobAuthError("body:must_be_bytes")
    return (_timestamp(timestamp) + method + _request_path(path)).encode("ascii") + body


def l2_signed_request(credentials: L2Credentials, *, address: str, timestamp: str | int,
                      method: str, path: str, body: bytes = b"") -> SignedRequest:
    """Sign the exact bytes to transmit; do not serialize or mutate ``body``."""
    credentials.validate()
    timestamp_text = _timestamp(timestamp)
    method_text = _text("method", method).upper()
    path_text = _request_path(path)
    message = l2_signing_message(timestamp_text, method_text, path_text, body)
    signature = base64.urlsafe_b64encode(hmac.new(
        _base64_secret(credentials.secret), message, hashlib.sha256).digest()).decode("ascii")
    headers = {"POLY_ADDRESS": _address(address), "POLY_API_KEY": credentials.api_key,
               "POLY_PASSPHRASE": credentials.passphrase, "POLY_SIGNATURE": signature,
               "POLY_TIMESTAMP": timestamp_text}
    return SignedRequest(method=method_text, path=path_text, body=body, headers=headers)


def user_websocket_subscription(credentials: L2Credentials, markets: list[str] | None = None) -> dict[str, object]:
    """Build the first authenticated user-stream frame without opening a socket."""
    credentials.validate()
    frame: dict[str, object] = {"auth": {"apiKey": credentials.api_key,
                                           "secret": credentials.secret,
                                           "passphrase": credentials.passphrase},
                                "type": "user"}
    if markets is not None:
        if (not isinstance(markets, list) or not markets
                or any(not isinstance(market, str) or not market.strip() for market in markets)
                or markets != sorted(set(markets))):
            raise ClobAuthError("markets:must_be_nonempty_sorted_unique_strings")
        frame["markets"] = markets
    return frame
