#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "external_intelligence.py"


def load_external():
    spec = importlib.util.spec_from_file_location("external_intelligence_audited", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load external_intelligence.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def asset_identity(module, pm, km) -> tuple[str | None, str | None]:
    pm_asset = module.detect_asset(pm.question + " " + pm.description)
    k_asset = module.detect_asset(km.title + " " + km.subtitle + " " + km.rules)
    return pm_asset, k_asset


def entity_compatible(module, pm, km) -> bool:
    pm_asset, k_asset = asset_identity(module, pm, km)
    if pm_asset is not None or k_asset is not None:
        return pm_asset is not None and pm_asset == k_asset
    pm_category = module.classify_market(pm.question + " " + pm.description)
    k_category = module.classify_market(km.title + " " + km.subtitle + " " + km.rules)
    if pm_category != "general" and k_category != "general" and pm_category != k_category:
        return False
    return True


def challenger_pair(module, pm, km, max_expiry_days: float = 14.0) -> dict[str, Any]:
    score, numeric, orient, expiry, incumbent_rejection = module.score_pair(pm, km, max_expiry_days)
    pm_asset, k_asset = asset_identity(module, pm, km)
    rejection = incumbent_rejection
    if not entity_compatible(module, pm, km):
        rejection = "entity_mismatch"
    return {
        "score": score,
        "numeric_match": numeric,
        "orientation_match": orient,
        "expiry_hours": expiry,
        "incumbent_pair_rejection": incumbent_rejection,
        "challenger_rejection": rejection,
        "pm_asset": pm_asset,
        "kalshi_asset": k_asset,
    }


def fixture_report() -> dict[str, Any]:
    m = load_external()
    now = 1787652000
    pm = m.PmMarket(
        market_id="pm-btc", condition_id="c", event_id="e",
        question="Will the price of Bitcoin be above $72,000 on August 25?",
        description="Bitcoin price threshold", category="crypto", end_ts=now + 5 * 3600,
        liquidity=1000.0, volume24h=1000.0, bid=0.48, ask=0.52, mid=0.50,
        yes_token="y", no_token="n", resolved_outcome=None,
    )
    silver = m.KMarket(
        ticker="KXSILVERD", event_ticker="KXSILVERD",
        title="Will the silver close price be above 72 USD/t.oz on August 25, 2026 at 5:00 PM EDT?",
        subtitle="", rules="", close_ts=pm.end_ts, updated_ts=now,
        bid=0.48, ask=0.52, mid=0.50, spread=0.04, volume=1000.0, liquidity=1000.0,
    )
    btc = m.KMarket(
        ticker="KXBTCD", event_ticker="KXBTCD",
        title="Will Bitcoin be above $72,000 on August 25, 2026 at 5:00 PM EDT?",
        subtitle="", rules="", close_ts=pm.end_ts, updated_ts=now,
        bid=0.48, ask=0.52, mid=0.50, spread=0.04, volume=1000.0, liquidity=1000.0,
    )
    cross_asset = challenger_pair(m, pm, silver)
    same_asset = challenger_pair(m, pm, btc)
    return {
        "decision": "MORE_EVIDENCE_REQUIRED",
        "source": str(SOURCE),
        "cross_asset_fixture": cross_asset,
        "same_asset_fixture": same_asset,
        "findings": {
            "incumbent_pair_scoring_has_no_entity_rejection": cross_asset["incumbent_pair_rejection"] != "entity_mismatch",
            "challenger_rejects_cross_asset": cross_asset["challenger_rejection"] == "entity_mismatch",
            "challenger_keeps_same_asset": same_asset["challenger_rejection"] != "entity_mismatch",
        },
        "aggressive_research_policy": {
            "polymarket_max_markets": 700,
            "polymarket_min_liquidity": 10.0,
            "kalshi_max_markets": 5000,
            "same_entity_min_match_score": 0.58,
            "same_entity_min_match_margin": 0.02,
            "same_entity_min_confidence": 0.20,
            "hard_requirements": [
                "entity_or_asset_compatibility",
                "critical_number_match",
                "orientation_compatibility",
                "expiry_within_existing_window",
            ],
            "note": "These are research-discovery thresholds only. A direct external probability still needs chronological OOS, terminal calibration, executable cost stress and bridge approval before paper admission.",
        },
    }


def main() -> int:
    print(json.dumps(fixture_report(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
