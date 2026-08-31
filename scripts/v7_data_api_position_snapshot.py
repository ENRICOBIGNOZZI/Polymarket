#!/usr/bin/env python3
"""Convert one sealed Data API positions response into exact token balances.

This is a read-only decoder. It does not fetch the endpoint or modify the
ledger; its output is passed to the independent real-PnL verifier.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from urllib.parse import urlparse


ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DECIMALS = 6


class PositionSnapshotError(ValueError):
    pass


def _address(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ADDRESS_RE.fullmatch(value):
        raise PositionSnapshotError(f"{field}:invalid_address")
    return value.lower()


def _units(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise PositionSnapshotError("position:size_invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PositionSnapshotError("position:size_invalid") from exc
    if not amount.is_finite() or amount < 0:
        raise PositionSnapshotError("position:size_invalid")
    scaled = amount * (Decimal(10) ** DECIMALS)
    if scaled != scaled.to_integral_value():
        raise PositionSnapshotError("position:size_not_exact_base_units")
    return int(scaled)


@dataclass(frozen=True)
class DataApiPositionSnapshot:
    model_sha: str
    wallet: str
    evidence_record_hash: str
    positions: tuple[tuple[int, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "polymarket_v7_data_api_position_snapshot_v1", "model_sha": self.model_sha,
                "wallet": self.wallet, "evidence_record_hash": self.evidence_record_hash,
                "positions": {f"token:{token_id}": units for token_id, units in self.positions}}


def position_snapshot(evidence_record: Any, *, wallet: str) -> DataApiPositionSnapshot:
    """Parse an exact six-decimal Data API response for one wallet."""
    wallet = _address(wallet, "wallet")
    if (getattr(evidence_record, "source", None) != "DATA_API_POSITIONS"
            or not isinstance(getattr(evidence_record, "model_sha", None), str)
            or not SHA_RE.fullmatch(evidence_record.model_sha)
            or not isinstance(getattr(evidence_record, "record_hash", None), str)
            or not SHA256_RE.fullmatch(evidence_record.record_hash)):
        raise PositionSnapshotError("evidence:must_be_sealed_data_api_positions")
    endpoint = urlparse(str(getattr(evidence_record, "endpoint", "")))
    query = getattr(evidence_record, "query", None)
    if (endpoint.scheme != "https" or endpoint.netloc != "data-api.polymarket.com"
            or endpoint.path != "/positions" or not isinstance(query, dict)
            or query.get("user", "").lower() != wallet):
        raise PositionSnapshotError("evidence:wrong_positions_request")
    response = getattr(evidence_record, "response", None)
    if not isinstance(response, list):
        raise PositionSnapshotError("evidence:positions_not_array")
    positions: dict[int, int] = {}
    for row in response:
        if not isinstance(row, dict):
            raise PositionSnapshotError("position:not_object")
        row_wallet = row.get("proxyWallet", row.get("wallet"))
        if _address(row_wallet, "position:wallet") != wallet:
            raise PositionSnapshotError("position:wrong_wallet")
        token = row.get("asset", row.get("tokenId"))
        if not isinstance(token, str) or not token.isdigit() or int(token) < 0:
            raise PositionSnapshotError("position:token_id")
        token_id = int(token)
        if token_id in positions:
            raise PositionSnapshotError("position:duplicate_token_id")
        units = _units(row.get("size"))
        if units:
            positions[token_id] = units
    return DataApiPositionSnapshot(evidence_record.model_sha, wallet, evidence_record.record_hash,
                                   tuple(sorted(positions.items())))
