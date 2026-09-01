#!/usr/bin/env python3
"""Classify discovered Polymarket crypto markets without granting authority."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from v7_crypto_settlement import load_registry


SLUG = re.compile(r"^(btc|eth|sol|xrp)-updown-(5m|15m)-([0-9]{10})$")
ASSET = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP"}
HORIZON = {"5m": "M5", "15m": "M15"}


def discover(events: list[dict[str, Any]], registry_path: Path) -> dict[str, Any]:
    contexts = load_registry(registry_path)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for event in events:
        slug = str(event.get("slug") or "")
        match = SLUG.fullmatch(slug)
        if match is None:
            rejected.append({"slug": slug, "reason": "UNRECOGNIZED_CRYPTO_CONTEXT"})
            continue
        asset, horizon = ASSET[match.group(1)], HORIZON[match.group(2)]
        context = next((row for key, row in contexts.items()
                        if key[0].value == asset and key[1].value == horizon), None)
        description = str(event.get("description") or "")
        if context is None:
            rejected.append({"slug": slug, "reason": "UNREGISTERED_CONTEXT"})
            continue
        settlement = context.raw["settlement"]
        semantics_match = (
            settlement["stream_url"] in description
            and "greater than or equal to" in description.lower()
            and "time-weighted average price" in description.lower()
        )
        if not semantics_match:
            rejected.append({"slug": slug, "reason": "SETTLEMENT_SEMANTICS_MISMATCH"})
            continue
        accepted.append({
            "slug": slug, "asset": asset, "horizon": horizon,
            "context_id": context.context_id,
            "settlement_semantic_hash": context.settlement_semantic_hash,
            "authority": "SHADOW_ZERO_AUTHORITY",
            "research_only": True,
            "new_risk_authorized": False,
            "discovery_transition": "DISCOVER_TO_RESEARCH_ONLY",
        })
    return {
        "schema": "polymarket_v7_crypto_market_discovery_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "automatic_execution": False,
        "accepted": accepted,
        "rejected": rejected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("config/v7_crypto_settlement_markets.json"))
    args = parser.parse_args()
    events = json.loads(args.events.read_text(encoding="utf-8"))
    if not isinstance(events, list):
        raise SystemExit("events_must_be_array")
    print(json.dumps(discover(events, args.registry), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
