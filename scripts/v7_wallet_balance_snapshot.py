#!/usr/bin/env python3
"""Derive verifier-ready wallet balances from sealed, pinned RPC observations.

This is deliberately a decoder, not an RPC client.  Callers first capture
read-only ``eth_call`` responses in the immutable evidence tape.  This module
then checks their exact calldata and common Polygon block hash before exposing
the pUSD and ERC-1155 balances in the verifier's integer-base-unit format.
It cannot connect, sign, submit, or alter a ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
BLOCK_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEX_QUANTITY_RE = re.compile(r"^0x[0-9a-fA-F]+$")
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
USDCE = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
CONDITIONAL_TOKENS = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
ERC20_BALANCE_OF = "70a08231"
ERC1155_BALANCE_OF = "00fdd58e"


class WalletSnapshotError(ValueError):
    """A sealed RPC record cannot prove a point-in-time wallet balance."""


def _address(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ADDRESS_RE.fullmatch(value):
        raise WalletSnapshotError(f"{field}:invalid_address")
    return value.lower()


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not BLOCK_HASH_RE.fullmatch(value):
        raise WalletSnapshotError(f"{field}:invalid_hash")
    return value.lower()


def _word(value: int) -> str:
    if value < 0 or value >= 1 << 256:
        raise WalletSnapshotError("token_id:out_of_range")
    return value.to_bytes(32, "big").hex()


def erc20_balance_of_calldata(wallet: str) -> str:
    """Return exact ABI calldata for ``pUSD.balanceOf(wallet)``."""
    return "0x" + ERC20_BALANCE_OF + "0" * 24 + _address(wallet, "wallet")[2:]


def erc1155_balance_of_calldata(wallet: str, token_id: int) -> str:
    """Return exact ABI calldata for ``ConditionalTokens.balanceOf``."""
    if isinstance(token_id, bool) or not isinstance(token_id, int):
        raise WalletSnapshotError("token_id:invalid")
    return "0x" + ERC1155_BALANCE_OF + "0" * 24 + _address(wallet, "wallet")[2:] + _word(token_id)


def _result_units(record: Any, *, wallet: str, contract: str, calldata: str) -> tuple[str, int]:
    """Validate one sealed, block-pinned ``eth_call`` record and return units."""
    if (getattr(record, "source", None) != "WALLET_RPC"
            or not isinstance(getattr(record, "record_hash", None), str)
            or not SHA256_RE.fullmatch(record.record_hash)
            or not isinstance(getattr(record, "model_sha", None), str)
            or not SHA_RE.fullmatch(record.model_sha)):
        raise WalletSnapshotError("evidence:must_be_sealed_wallet_rpc")
    query = getattr(record, "query", None)
    if not isinstance(query, dict) or set(query) != {
            "chain_id", "jsonrpc_method", "block_hash", "call_to", "call_data"}:
        raise WalletSnapshotError("evidence:query_shape")
    if any(not isinstance(value, str) for value in query.values()):
        raise WalletSnapshotError("evidence:query_shape")
    if query["chain_id"] != "137" or query["jsonrpc_method"] != "eth_call":
        raise WalletSnapshotError("evidence:not_polygon_eth_call")
    block_hash = _hash(query["block_hash"], "evidence:block_hash")
    if _address(query["call_to"], "evidence:call_to") != contract:
        raise WalletSnapshotError("evidence:wrong_contract")
    if query["call_data"].lower() != calldata:
        raise WalletSnapshotError("evidence:wrong_calldata")
    response = getattr(record, "response", None)
    if not isinstance(response, dict) or "error" in response or set(response) - {"jsonrpc", "id", "result"}:
        raise WalletSnapshotError("evidence:response_shape")
    result = response.get("result")
    if not isinstance(result, str) or not HEX_QUANTITY_RE.fullmatch(result):
        raise WalletSnapshotError("evidence:result_not_hex_quantity")
    try:
        units = int(result[2:], 16)
    except ValueError as exc:
        raise WalletSnapshotError("evidence:result_not_hex_quantity") from exc
    if units >= 1 << 256:
        raise WalletSnapshotError("evidence:result_out_of_range")
    return block_hash, units


@dataclass(frozen=True)
class WalletBalanceSnapshot:
    """A point-in-time wallet balance snapshot traceable to sealed evidence."""

    model_sha: str
    wallet: str
    block_hash: str
    pUSD_units: int
    usdce_units: int | None
    outcome_token_units: tuple[tuple[int, int], ...]
    evidence_record_hashes: tuple[str, ...]

    def verifier_balances(self) -> dict[str, int]:
        """Produce the exact observed-balance mapping accepted by the verifier."""
        balances = {"assets:cash:wallet|pUSD": self.pUSD_units}
        if self.usdce_units is not None:
            balances["assets:cash:wallet|USDCe"] = self.usdce_units
        balances.update({f"assets:outcome:position|token:{token_id}": units
                         for token_id, units in self.outcome_token_units})
        return balances

    def to_dict(self) -> dict[str, Any]:
        """Retain the immutable evidence identities alongside the balances."""
        return {
            "schema": "polymarket_v7_wallet_balance_snapshot_v1",
            "model_sha": self.model_sha,
            "wallet": self.wallet,
            "block_hash": self.block_hash,
            "balances": self.verifier_balances(),
            "evidence_record_hashes": list(self.evidence_record_hashes),
        }


def wallet_balance_snapshot(pusd_record: Any, outcome_records: Iterable[tuple[int, Any]], *,
                            wallet: str, usdce_record: Any | None = None) -> WalletBalanceSnapshot:
    """Create a common-block pUSD/ERC-1155 snapshot from sealed RPC responses.

    ``outcome_records`` contains one ``(token_id, evidence_record)`` pair per
    outcome asset owned in the canonical ledger.  Empty snapshots are allowed
    for cash-only periods; duplicate token IDs and mixed blocks fail closed.
    """
    normalized_wallet = _address(wallet, "wallet")
    block_hash, cash = _result_units(
        pusd_record, wallet=normalized_wallet, contract=PUSD,
        calldata=erc20_balance_of_calldata(normalized_wallet),
    )
    model_sha = getattr(pusd_record, "model_sha", None)
    evidence_hashes = [str(getattr(pusd_record, "record_hash"))]
    usdce_units: int | None = None
    if usdce_record is not None:
        underlying_block, usdce_units = _result_units(
            usdce_record, wallet=normalized_wallet, contract=USDCE,
            calldata=erc20_balance_of_calldata(normalized_wallet),
        )
        if underlying_block != block_hash:
            raise WalletSnapshotError("evidence:mixed_block_hash")
        evidence_hashes.append(str(getattr(usdce_record, "record_hash")))
    outcome_units: list[tuple[int, int]] = []
    seen_tokens: set[int] = set()
    for pair in outcome_records:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise WalletSnapshotError("outcome_record:invalid")
        token_id, record = pair
        if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 0:
            raise WalletSnapshotError("token_id:invalid")
        if token_id in seen_tokens:
            raise WalletSnapshotError("token_id:duplicate")
        seen_tokens.add(token_id)
        if getattr(record, "model_sha", None) != model_sha:
            raise WalletSnapshotError("evidence:mixed_model_sha")
        token_block, units = _result_units(
            record, wallet=normalized_wallet, contract=CONDITIONAL_TOKENS,
            calldata=erc1155_balance_of_calldata(normalized_wallet, token_id),
        )
        if token_block != block_hash:
            raise WalletSnapshotError("evidence:mixed_block_hash")
        evidence_hashes.append(str(getattr(record, "record_hash")))
        outcome_units.append((token_id, units))
    return WalletBalanceSnapshot(
        model_sha=model_sha,
        wallet=normalized_wallet,
        block_hash=block_hash,
        pUSD_units=cash,
        usdce_units=usdce_units,
        outcome_token_units=tuple(sorted(outcome_units)),
        evidence_record_hashes=tuple(evidence_hashes),
    )


def _evidence_module() -> Any:
    name = "v7_real_pnl_evidence_for_wallet_snapshot"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("v7_real_pnl_evidence.py"))
    if spec is None or spec.loader is None:
        raise WalletSnapshotError("evidence:unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def snapshot_from_evidence(path: Path, *, model_sha: str, wallet: str,
                           token_ids: Iterable[int], include_usdce: bool = False) -> WalletBalanceSnapshot:
    """Select one exact pinned balance response per asset from an evidence tape."""
    if not isinstance(model_sha, str) or not SHA_RE.fullmatch(model_sha):
        raise WalletSnapshotError("model_sha:invalid")
    normalized_wallet = _address(wallet, "wallet")
    normalized_tokens: list[int] = []
    for token_id in token_ids:
        if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 0:
            raise WalletSnapshotError("token_id:invalid")
        if token_id in normalized_tokens:
            raise WalletSnapshotError("token_id:duplicate")
        normalized_tokens.append(token_id)
    records = [record for record in _evidence_module().iter_records(Path(path))
               if record.model_sha == model_sha and record.source == "WALLET_RPC"]

    def matching(contract: str, calldata: str) -> list[Any]:
        return [record for record in records
                if isinstance(record.query, dict)
                and record.query.get("call_to", "").lower() == contract
                and record.query.get("call_data", "").lower() == calldata]

    cash_records = matching(PUSD, erc20_balance_of_calldata(normalized_wallet))
    if len(cash_records) != 1:
        raise WalletSnapshotError("evidence:pUSD_balance_record_missing_or_ambiguous")
    usdce_record = None
    if include_usdce:
        underlying_records = matching(USDCE, erc20_balance_of_calldata(normalized_wallet))
        if len(underlying_records) != 1:
            raise WalletSnapshotError("evidence:usdce_balance_record_missing_or_ambiguous")
        usdce_record = underlying_records[0]
    outcome_records: list[tuple[int, Any]] = []
    for token_id in normalized_tokens:
        matching_records = matching(CONDITIONAL_TOKENS,
                                    erc1155_balance_of_calldata(normalized_wallet, token_id))
        if len(matching_records) != 1:
            raise WalletSnapshotError("evidence:outcome_balance_record_missing_or_ambiguous")
        outcome_records.append((token_id, matching_records[0]))
    return wallet_balance_snapshot(cash_records[0], outcome_records, wallet=normalized_wallet,
                                   usdce_record=usdce_record)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-tape", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--token-id", type=int, action="append", default=[])
    parser.add_argument("--include-usdce", action="store_true",
                        help="include the USDC.e wallet balance for wrap/unwrap reconciliation")
    parser.add_argument("--output", type=Path, required=True,
                        help="write the complete traceable snapshot JSON")
    parser.add_argument("--verifier-balances-output", type=Path,
                        help="optional flat JSON map accepted by v7_real_pnl_verifier.py")
    args = parser.parse_args()
    snapshot = snapshot_from_evidence(args.evidence_tape, model_sha=args.model_sha,
                                      wallet=args.wallet, token_ids=args.token_id,
                                      include_usdce=args.include_usdce)
    args.output.write_text(json.dumps(snapshot.to_dict(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if args.verifier_balances_output:
        args.verifier_balances_output.write_text(
            json.dumps(snapshot.verifier_balances(), sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
