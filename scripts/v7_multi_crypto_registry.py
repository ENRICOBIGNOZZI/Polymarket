#!/usr/bin/env python3
"""Fail-closed multi-crypto capability registry.

This module owns no execution authority and performs no network calls. It joins
the proposed multi-asset configuration to the existing settlement registry so
new lanes cannot become eligible merely because a symbol string exists.
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSETS = ROOT / "config/v7_multi_crypto_assets.json"
DEFAULT_SETTLEMENT = ROOT / "config/v7_crypto_settlement_markets.json"

EXPECTED_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
EXPECTED_HORIZONS = ("M5", "M15")
VERIFIED_VENUE_STATUSES = {"VERIFIED_EXISTING_REGISTRY", "VERIFIED_RUNTIME_SMOKE"}
FROZEN_BTC_PROTOCOL_HASH = "8b463ef8f66cd07fa694cbb49e355b812eac8d4724c35f65bd3e6a3505719d96"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def asset_handle(asset: str) -> int:
    """Stable handle compatible with the frozen BTC runtime's BTCUSD literal."""
    asset = asset.upper()
    if asset not in EXPECTED_ASSETS:
        raise ValueError(f"unsupported asset: {asset}")
    raw = (asset + "USD").encode("ascii")
    if len(raw) > 8:
        raise ValueError(f"asset handle too wide: {asset}")
    return int.from_bytes(raw, "big")


@dataclass(frozen=True)
class AssetCapability:
    asset: str
    horizon: str
    configured_mode: str
    horizon_state: str
    settlement_context_present: bool
    settlement_mapping_verified: bool
    market_mapping_verified: bool
    required_venues_verified: bool
    entry_authority: bool
    status: str
    blockers: tuple[str, ...]

    def as_row(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "horizon": self.horizon,
            "configured_mode": self.configured_mode,
            "horizon_state": self.horizon_state,
            "settlement_context_present": self.settlement_context_present,
            "settlement_mapping_verified": self.settlement_mapping_verified,
            "market_mapping_verified": self.market_mapping_verified,
            "required_venues_verified": self.required_venues_verified,
            "entry_authority": self.entry_authority,
            "status": self.status,
            "blockers": ";".join(self.blockers),
        }


def validate_asset_registry(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema") != "polymarket_v7_multi_crypto_asset_registry_v1" or value.get("version") != 1:
        raise ValueError("multi-crypto registry identity mismatch")
    required_false = (
        "authenticated_execution", "real_order_submission",
        "real_capital_at_risk", "automatic_promotion",
    )
    if value.get("paper_only") is not True or any(value.get(k) is not False for k in required_false):
        raise ValueError("multi-crypto registry safety flags invalid")
    if value.get("single_global_execution_owner") is not True or value.get("single_canonical_ledger_writer") is not True:
        raise ValueError("multi-crypto registry ownership invariant invalid")
    if value.get("frozen_btc_protocol_hash") != FROZEN_BTC_PROTOCOL_HASH:
        raise ValueError("frozen BTC protocol hash mismatch")
    if tuple(value.get("supported_horizons") or ()) != EXPECTED_HORIZONS:
        raise ValueError("supported horizon partition mismatch")

    assets = value.get("assets")
    if not isinstance(assets, list) or tuple(row.get("asset") for row in assets if isinstance(row, dict)) != EXPECTED_ASSETS:
        raise ValueError("asset partition/order mismatch")
    for row in assets:
        if row.get("allow_new_entry_authority") is not False:
            raise ValueError(f"{row.get('asset')}: new entry authority must remain false")
        horizons = row.get("horizons")
        if not isinstance(horizons, dict) or tuple(horizons) != EXPECTED_HORIZONS:
            raise ValueError(f"{row.get('asset')}: horizon partition mismatch")
        for horizon, cfg in horizons.items():
            if not isinstance(cfg, dict) or cfg.get("allow_entry_authority") is not False:
                raise ValueError(f"{row.get('asset')}/{horizon}: entry authority must remain false")
        venues = row.get("venues")
        if not isinstance(venues, dict):
            raise ValueError(f"{row.get('asset')}: venue map missing")
    return value


def _settlement_contexts(value: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    contexts = value.get("contexts")
    if not isinstance(contexts, list):
        raise ValueError("settlement registry contexts missing")
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for row in contexts:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("asset") or ""), str(row.get("horizon") or ""))
        if key[0] and key[1]:
            output[key] = row
    return output


def capability_rows(
    assets_cfg: dict[str, Any], settlement_cfg: dict[str, Any]
) -> list[AssetCapability]:
    validate_asset_registry(assets_cfg)
    settlements = _settlement_contexts(settlement_cfg)
    rows: list[AssetCapability] = []
    for asset_row in assets_cfg["assets"]:
        asset = asset_row["asset"]
        def venue_verified(name: str) -> bool:
            row = asset_row["venues"].get(name)
            return isinstance(row, dict) and bool(row.get("symbol"))                 and row.get("status") in VERIFIED_VENUE_STATUSES
        # Binance is the primary lead source for the initial family. Coinbase is
        # preferred confirmation, but Bybit is a valid explicit fallback for
        # assets such as BNB where Coinbase is unavailable.
        required_venues_verified = venue_verified("binance_spot")             and (venue_verified("coinbase_spot") or venue_verified("bybit_spot"))
        for horizon in EXPECTED_HORIZONS:
            hcfg = asset_row["horizons"][horizon]
            settlement = settlements.get((asset, horizon))
            settlement_present = isinstance(settlement, dict)
            settlement_verified = bool(settlement and settlement.get("settlement_mapping_verified") is True)
            market_verified = bool(settlement and settlement.get("market_mapping_verified") is True)
            blockers: list[str] = []
            if asset == "BTC" and horizon == "M5":
                blockers.append("BTC_FROZEN_NO_NEW_AUTHORITY")
            if not (settlement_present and settlement_verified and market_verified):
                blockers.append("SETTLEMENT_CONTEXT_UNVERIFIED_OR_MISSING")
            if not required_venues_verified:
                blockers.append("REQUIRED_VENUE_BINDING_UNVERIFIED")
            if hcfg["state"].startswith("BLOCKED"):
                blockers.append(hcfg["state"])
            entry_authority = False
            rows.append(AssetCapability(
                asset=asset,
                horizon=horizon,
                configured_mode=asset_row["mode"],
                horizon_state=hcfg["state"],
                settlement_context_present=settlement_present,
                settlement_mapping_verified=settlement_verified,
                market_mapping_verified=market_verified,
                required_venues_verified=required_venues_verified,
                entry_authority=entry_authority,
                status="READY_SHADOW" if not blockers else "BLOCKED",
                blockers=tuple(blockers),
            ))
    return rows


def write_matrix(path: Path, rows: list[AssetCapability]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].as_row()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_row())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    parser.add_argument("--settlement", type=Path, default=DEFAULT_SETTLEMENT)
    parser.add_argument("--emit-capability-matrix", type=Path)
    args = parser.parse_args()
    assets_cfg = validate_asset_registry(load_json(args.assets))
    settlement_cfg = load_json(args.settlement)
    rows = capability_rows(assets_cfg, settlement_cfg)
    if args.emit_capability_matrix:
        write_matrix(args.emit_capability_matrix, rows)
    print(json.dumps({
        "schema": "polymarket_v7_multi_crypto_capability_summary_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "asset_count": len(EXPECTED_ASSETS),
        "row_count": len(rows),
        "ready_shadow": sum(row.status == "READY_SHADOW" for row in rows),
        "blocked": sum(row.status == "BLOCKED" for row in rows),
        "rows": [row.as_row() for row in rows],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
