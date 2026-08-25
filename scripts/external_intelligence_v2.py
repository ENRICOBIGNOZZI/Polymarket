#!/usr/bin/env python3
from __future__ import annotations

import math
from typing import Sequence

try:
    import external_intelligence as base
except ModuleNotFoundError:
    from scripts import external_intelligence as base

BASE_SCORE_PAIR = base.score_pair
BASE_READ_JSON = base.read_json


def _asset_identity(pm, km) -> tuple[str | None, str | None]:
    pm_asset = base.detect_asset(pm.question + " " + pm.description)
    km_asset = base.detect_asset(km.title + " " + km.subtitle + " " + km.rules)
    return pm_asset, km_asset


def entity_compatible(pm, km) -> bool:
    pm_asset, km_asset = _asset_identity(pm, km)
    if pm_asset is not None or km_asset is not None:
        return pm_asset is not None and pm_asset == km_asset
    pm_category = base.classify_market(pm.question + " " + pm.description)
    km_category = base.classify_market(km.title + " " + km.subtitle + " " + km.rules)
    if pm_category != "general" and km_category != "general" and pm_category != km_category:
        return False
    return True


def score_pair(pm, km, max_expiry_days: float):
    score, numeric, orient, expiry, rejection = BASE_SCORE_PAIR(pm, km, max_expiry_days)
    if not entity_compatible(pm, km):
        return -1.0, numeric, orient, expiry, "entity_mismatch"
    return score, numeric, orient, expiry, rejection


def match_kalshi(pm, candidates: Sequence, config: dict, now: int):
    source = (config.get("sources") or {}).get("kalshi") or {}
    max_expiry = base.finite(source.get("max_expiry_difference_days"), 14.0)
    scored = []
    for km in candidates:
        score, numeric, orient, expiry, rejection = score_pair(pm, km, max_expiry)
        scored.append((score, numeric, orient, expiry, rejection, km))
    if not scored:
        return None
    scored.sort(key=lambda row: row[0], reverse=True)
    score, numeric, orient, expiry, rejection, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else -1.0
    margin = score - second

    min_score = min(base.finite(source.get("min_match_score"), 0.68), 0.58)
    min_margin = min(base.finite(source.get("min_match_margin"), 0.04), 0.02)
    min_confidence = min(base.finite(source.get("min_confidence"), 0.35), 0.20)
    freshness_half_life = max(3600.0, base.finite(source.get("freshness_half_life_seconds"), 21600.0))
    quote_quality = math.exp(-best.spread / max(0.01, base.finite(source.get("spread_scale"), 0.05)))
    freshness = math.exp(-max(0, now - best.updated_ts) / freshness_half_life) if best.updated_ts else 0.75
    confidence = score * quote_quality * freshness * base.clip(margin / max(min_margin, 1e-6), 0.0, 1.0)
    if score < 0:
        rejection = rejection or "entity_mismatch"
    elif not rejection and score < min_score:
        rejection = "weak_match"
    elif not rejection and margin < min_margin:
        rejection = "ambiguous_match"
    elif not rejection and confidence < min_confidence:
        rejection = "low_confidence"
    return base.Match(score, margin, confidence, rejection, numeric, orient, expiry, best)


def read_json(path, default):
    value = BASE_READ_JSON(path, default)
    if not isinstance(value, dict) or value.get("schema") != "polymarket_external_intelligence_config_v1":
        return value
    value = dict(value)
    universe = dict(value.get("universe") or {})
    universe["max_markets"] = max(700, int(base.integer(universe.get("max_markets"), 400)))
    universe["min_liquidity"] = min(10.0, base.finite(universe.get("min_liquidity"), 100.0))
    value["universe"] = universe
    sources = dict(value.get("sources") or {})
    kalshi = dict(sources.get("kalshi") or {})
    if kalshi:
        kalshi["max_markets"] = max(5000, int(base.integer(kalshi.get("max_markets"), 2500)))
        sources["kalshi"] = kalshi
    value["sources"] = sources
    return value


def main() -> int:
    base.score_pair = score_pair
    base.match_kalshi = match_kalshi
    base.read_json = read_json
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
