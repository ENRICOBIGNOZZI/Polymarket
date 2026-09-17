#!/usr/bin/env python3
"""Validate the single-owner execution/accounting contract for multi-crypto V7."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_multi_crypto_execution_accounting_contract_v1"
ASSETS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}
DECISION_CHAIN = [
    "LIVE_ALGORITHM", "V7_GLOBAL_PORTFOLIO_COORDINATOR", "V7_CANONICAL_ALLOCATOR",
    "V7_CANONICAL_RISK", "V7_CANONICAL_OMS", "V7_CANONICAL_INVENTORY", "V7_CANONICAL_LEDGER",
]


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: object required")
    return value


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)

def validate(root: Path) -> dict[str, Any]:
    paper = load(root / "config/paper_v7.json")
    authority = load(root / "config/v7_authority_registry.json")
    assets = load(root / "config/v7_multi_crypto_assets.json")
    settlement = load(root / "config/v7_crypto_settlement_markets.json")
    shadow = load(root / "config/v7_multi_crypto_shadow_runtime.json")

    require(paper.get("paper_only") is True, "paper_v7_not_paper")
    multi = paper.get("multi_strategy") or {}; v7 = paper.get("v7") or {}
    require(multi.get("paper_only") is True and multi.get("single_account_allocator") is True,
            "paper_v7_allocator_contract")
    require(multi.get("single_canonical_ledger_writer") is True,
            "paper_v7_ledger_writer_contract")
    require(v7.get("authenticated_execution") is False and v7.get("real_order_submission") is False,
            "paper_v7_private_execution_enabled")
    require(v7.get("shared_execution_ledger_required") is True
            and v7.get("single_canonical_ledger_writer") is True,
            "paper_v7_shared_ledger_contract")
    live = v7.get("live_capability") or {}
    require(live.get("live_enabled") is False and all(float(live.get(k) or 0) == 0
            for k in ("max_daily_loss", "max_exposure", "max_order")),
            "paper_v7_live_capability_not_zero")

    require(authority.get("paper_only") is True and authority.get("authenticated_execution") is False
            and authority.get("real_order_submission") is False
            and authority.get("real_capital_at_risk") is False,
            "authority_registry_private_execution")
    require(authority.get("decision_chain") == DECISION_CHAIN, "authority_decision_chain")
    owners = authority.get("owners") or {}
    expected_owners = {
        "global_portfolio_coordinator": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "capital_allocator": "V7_CANONICAL_ALLOCATOR", "risk_engine": "V7_CANONICAL_RISK",
        "oms": "V7_CANONICAL_OMS", "inventory": "V7_CANONICAL_INVENTORY",
        "ledger": "V7_CANONICAL_LEDGER",
    }
    require(all(owners.get(k) == v for k, v in expected_owners.items()), "authority_owner_mismatch")

    require(assets.get("paper_only") is True and assets.get("single_global_execution_owner") is True
            and assets.get("single_canonical_ledger_writer") is True,
            "multi_crypto_owner_contract")
    asset_rows = assets.get("assets") if isinstance(assets.get("assets"), list) else []
    require({str(row.get("asset") or "") for row in asset_rows if isinstance(row, dict)} == ASSETS,
            "multi_crypto_asset_partition")
    for row in asset_rows:
        require(row.get("allow_new_entry_authority") is False,
                f"{row.get('asset')}:new_entry_authority")
        horizons = row.get("horizons") if isinstance(row.get("horizons"), dict) else {}
        require(horizons and all(isinstance(cfg, dict) and cfg.get("allow_entry_authority") is False
                                 for cfg in horizons.values()),
                f"{row.get('asset')}:horizon_entry_authority")

    require(set(settlement.get("supported_assets") or []) == ASSETS,
            "settlement_asset_partition")
    contexts = settlement.get("contexts") if isinstance(settlement.get("contexts"), list) else []
    for row in contexts:
        if not isinstance(row, dict):
            raise ValueError("settlement_context_row")
        if row.get("asset") != "BTC":
            require(row.get("authority") == "SHADOW_ZERO_AUTHORITY"
                    and row.get("research_only") is True,
                    f"{row.get('asset')}/{row.get('horizon')}:non_btc_authority")

    require(shadow.get("paper_only") is True and shadow.get("execution_authority") is False
            and shadow.get("real_order_submission") is False
            and shadow.get("automatic_promotion") is False,
            "shadow_runtime_authority")

    shadow_source = (root / "scripts/v7_multi_crypto_shadow_runtime.py").read_text(encoding="utf-8")
    forbidden = ("v7_global_portfolio_coordinator.py", "v7_execution_ledger.py",
                 "authorized_maker_paper_executor", "v7_lead_lag_taker_runtime.py")
    require(all(term not in shadow_source for term in forbidden), "shadow_runtime_execution_owner_present")
    opportunity_source = (root / "scripts/v7_opportunity.py").read_text(encoding="utf-8")
    require("crypto_context_zero_authority" in opportunity_source,
            "opportunity_zero_authority_gate_missing")

    return {
        "schema": SCHEMA, "version": 1, "state": "VERIFIED_SHADOW_CONTRACT",
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "real_capital_at_risk": False, "new_risk_authorized": False,
        "single_global_execution_owner": True, "single_canonical_ledger_writer": True,
        "global_portfolio_coordinator": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "ledger_owner": "V7_CANONICAL_LEDGER", "decision_chain": DECISION_CHAIN,
        "supported_assets": sorted(ASSETS),
        "claim_boundary": "Contract verification only; SHADOW lanes remain unable to add risk.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    args = parser.parse_args(); print(json.dumps(validate(args.repository_root.resolve()), indent=2, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
