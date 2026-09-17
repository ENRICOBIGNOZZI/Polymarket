#!/usr/bin/env python3
"""Summarize zero-authority multi-crypto Polymarket WS book readiness."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def token_ready(value: dict[str, Any], token: str, expected_sha: str) -> tuple[bool, str]:
    if not value:
        return False, "PM_BOOK_MISSING"
    if value.get("model_sha") != expected_sha or str(value.get("token_id") or "") != token:
        return False, "PM_BOOK_IDENTITY_MISMATCH"
    if value.get("paper_only") is not True or value.get("authenticated_execution") is not False \
            or value.get("real_order_submission") is not False \
            or value.get("execution_authority") not in (False, "ZERO_AUTHORITY_RESEARCH_ONLY"):
        return False, "PM_BOOK_AUTHORITY_INVALID"
    if value.get("lineage_continuous") is not True:
        return False, "PM_BOOK_LINEAGE_WARMING"
    if value.get("valid") is not True:
        return False, "PM_BOOK_INVALID"
    try:
        bid = float(value.get("best_bid"))
        ask = float(value.get("best_ask"))
    except (TypeError, ValueError):
        return False, "PM_BOOK_INVALID"
    if not (0 <= bid <= ask <= 1) or ask <= 0:
        return False, "PM_BOOK_INVALID"
    return True, "BOOK_READY"


def summarize(selection: dict[str, Any], books_dir: Path, expected_sha: str) -> dict[str, Any]:
    if not SHA40.fullmatch(expected_sha):
        raise ValueError("exact expected SHA required")
    if selection.get("schema") != "polymarket_v7_multi_crypto_book_selection_v1" \
            or selection.get("paper_only") is not True \
            or selection.get("execution_authority") is not False \
            or selection.get("real_order_submission") is not False:
        raise ValueError("invalid zero-authority selection")
    markets = selection.get("markets")
    if not isinstance(markets, list):
        raise ValueError("selection markets missing")
    rows: list[dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        reasons: list[str] = []
        for field in ("yes_token", "no_token"):
            token = str(market.get(field) or "")
            ready, reason = token_ready(load(books_dir / f"{token}.json"), token, expected_sha)
            if not ready:
                reasons.append(reason)
        state = "BOOK_READY" if not reasons else "FEEDS_WARMING"
        rows.append({
            "asset": str(market.get("asset") or ""),
            "horizon": str(market.get("horizon") or ""),
            "market_id": str(market.get("market_id") or ""),
            "state": state,
            "reasons": sorted(set(reasons)),
        })
    states = Counter(row["state"] for row in rows)
    by_lane: dict[str, dict[str, int]] = {}
    for row in rows:
        lane = f"{row['asset']}/{row['horizon']}"
        bucket = by_lane.setdefault(lane, {"BOOK_READY": 0, "FEEDS_WARMING": 0})
        bucket[row["state"]] += 1
    return {
        "schema": "polymarket_v7_multi_crypto_book_status_v1",
        "expected_sha": expected_sha,
        "selection_generation_sha256": selection.get("generation_sha256"),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "market_count": len(rows),
        "book_ready": states["BOOK_READY"],
        "feeds_warming": states["FEEDS_WARMING"],
        "by_lane": dict(sorted(by_lane.items())),
        "markets": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--book-features-dir", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(load(args.selection), args.book_features_dir, args.expected_sha), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
