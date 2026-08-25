#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


old = '''        maximum = max(1, integer(source.get("max_markets_per_asset"), 20))
        for market in sorted(markets, key=lambda item: (item.volume24h, item.liquidity), reverse=True)[:maximum]:
            for name, value in sorted(features.items()):
                observations.append(observation_row(
                    market,
                    observed_ts=now,
                    source="binance",
                    source_id=symbol,
                    source_event_ts=source_ts or now,
                    feature_name=name,
                    feature_value=value,
                    confidence=finite(source.get("base_confidence"), 0.60),
                    mapping_score=1.0,
                    metadata={"asset": asset, "symbol": symbol},
                ))
            estimate = crypto_threshold_probability(market, asset, features, now, config)
            if estimate is not None:
                q_external, confidence, metadata = estimate
                observations.append(observation_row(
                    market,
                    observed_ts=now,
                    source="binance",
                    source_id=symbol,
                    source_event_ts=source_ts or now,
                    feature_name="external_probability",
                    feature_value=q_external - market.mid,
                    q_external=q_external,
                    confidence=confidence,
                    mapping_score=1.0,
                    metadata={**metadata, "symbol": symbol},
                ))
'''
new = '''        maximum = max(1, integer(source.get("max_markets_per_asset"), 20))
        ranked: list[tuple[bool, float, float, PmMarket, tuple[float, float, dict[str, Any]] | None]] = []
        for market in markets:
            estimate = crypto_threshold_probability(market, asset, features, now, config)
            ranked.append((
                estimate is not None,
                market.volume24h,
                market.liquidity,
                market,
                estimate,
            ))
        ranked.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        selected = ranked[:maximum]
        health[asset]["eligible_markets"] = len(markets)
        health[asset]["selected_markets"] = len(selected)
        health[asset]["direct_probability_markets"] = sum(row[0] for row in ranked)
        for _, _, _, market, estimate in selected:
            for name, value in sorted(features.items()):
                observations.append(observation_row(
                    market,
                    observed_ts=now,
                    source="binance",
                    source_id=symbol,
                    source_event_ts=source_ts or now,
                    feature_name=name,
                    feature_value=value,
                    confidence=finite(source.get("base_confidence"), 0.60),
                    mapping_score=1.0,
                    metadata={"asset": asset, "symbol": symbol},
                ))
            if estimate is not None:
                q_external, confidence, metadata = estimate
                observations.append(observation_row(
                    market,
                    observed_ts=now,
                    source="binance",
                    source_id=symbol,
                    source_event_ts=source_ts or now,
                    feature_name="external_probability",
                    feature_value=q_external - market.mid,
                    q_external=q_external,
                    confidence=confidence,
                    mapping_score=1.0,
                    metadata={**metadata, "symbol": symbol},
                ))
'''
replace_once("scripts/external_intelligence.py", old, new)

config_path = ROOT / "config" / "external_intelligence.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
config["sources"]["binance"]["max_markets_per_asset"] = 120
config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

Path(__file__).unlink()
print("direct crypto probability markets prioritized before Binance truncation")
