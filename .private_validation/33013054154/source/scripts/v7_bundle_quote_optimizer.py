#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from v7_market_common import (
    TapeFlow,
    fee_per_share,
    fill_probability_proxy,
    finite,
    parse_array,
    request_json,
    resolve_fee_details,
)

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]


@dataclass
class Leg:
    row: dict[str, str]
    token: str
    condition: str
    raw_market: dict[str, Any]
    bid: float
    ask: float
    bid_size: float
    min_order: float
    tick: float
    price: float
    queue: float
    fill_probability: float = 0.0


def load_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in FIELDS} for row in rows])
    os.replace(tmp, path)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def fetch_market(gamma: str, market_id: str) -> dict[str, Any] | None:
    try:
        value = request_json(f"{gamma.rstrip('/')}/markets/{market_id}")
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def side_token(raw: dict[str, Any], side: str) -> str:
    ids = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(value).strip().upper() for value in parse_array(raw.get("outcomes"))]
    for index, outcome in enumerate(outcomes[: len(ids)]):
        if outcome == side.upper():
            return ids[index]
    if len(ids) >= 2:
        return ids[0] if side.upper() == "YES" else ids[1]
    return ""


def fetch_books(clob: str, tokens: list[str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for start in range(0, len(tokens), 80):
        try:
            root = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in tokens[start:start + 80]])
        except Exception:
            continue
        for raw in root if isinstance(root, list) else []:
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("asset_id") or "")
            bids: list[tuple[float, float]] = []
            asks: list[tuple[float, float]] = []
            for key, values in (("bids", bids), ("asks", asks)):
                for row in raw.get(key, []):
                    if not isinstance(row, dict):
                        continue
                    price = finite(row.get("price")); size = max(0.0, finite(row.get("size"), 0.0))
                    if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
                        values.append((price, size))
            bids.sort(reverse=True); asks.sort()
            if token and bids and asks:
                output[token] = {
                    "bid": bids[0][0], "ask": asks[0][0], "bid_size": bids[0][1],
                    "min_order": max(1.0, finite(raw.get("min_order_size"), 1.0)),
                    "tick": max(1e-6, finite(raw.get("tick_size"), 0.01)),
                }
    return output


def queue_at_price(leg: Leg, price: float) -> float:
    return leg.bid_size if abs(price - leg.bid) <= max(1e-9, 0.25 * leg.tick) else 0.0


def update_fill_probability(leg: Leg, flow: TapeFlow, own_shares: float, horizon_seconds: int, lookback_seconds: int) -> None:
    rate = flow.compatible_sell_rate(leg.token, leg.price, lookback_seconds=lookback_seconds)
    leg.queue = queue_at_price(leg, leg.price)
    leg.fill_probability = fill_probability_proxy(
        queue_ahead=leg.queue,
        own_shares=max(leg.min_order, own_shares),
        compatible_flow_per_second=rate,
        horizon_seconds=horizon_seconds,
        prior_flow_per_second=1.0 / 300.0,
    )


def bundle_edge(legs: list[Leg], clob: str) -> tuple[float, bool]:
    total = 0.0
    verified = True
    for leg in legs:
        weight = max(0.0, finite(leg.row.get("weight"), 1.0))
        details = resolve_fee_details(leg.raw_market, clob, leg.condition, leg.token)
        verified = verified and details.verified
        total += weight * (leg.price + fee_per_share(leg.price, details, taker=False))
    return 1.0 - total, verified


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 per-leg queue/flow quote optimizer; final joint completion and economics belong to the V7 round-trip guard")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--min-edge", type=float, default=0.00005)
    parser.add_argument("--reserve-bps", type=float, default=0.5)
    parser.add_argument("--flow-lookback-seconds", type=int, default=300)
    parser.add_argument("--horizon-seconds", type=int, default=180)
    parser.add_argument("--min-leg-fill-probability", type=float, default=0.001)
    parser.add_argument("--target-leg-fill-probability", type=float, default=0.10)
    parser.add_argument("--max-improve-ticks-per-leg", type=int, default=1)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    now = int(time.time())
    flow = TapeFlow.from_csv(args.trade_tape, lookback_seconds=args.flow_lookback_seconds, now=now)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for input_path in args.input:
        for row in load_rows(input_path):
            bundle_id = str(row.get("bundle_id") or "")
            if bundle_id:
                grouped[bundle_id].append(row)

    accepted_rows: list[dict[str, Any]] = []
    reject = Counter()
    diagnostics: list[dict[str, Any]] = []
    improved_bundles = 0
    for bundle_id, rows in grouped.items():
        if not rows:
            continue
        strategy = str(rows[0].get("strategy") or "").upper()
        if strategy not in {"GRAPH_RV", "STRUCTURAL_TYPED"}:
            accepted_rows.extend(rows)
            continue
        raw_markets = [fetch_market(gamma, str(row.get("market_id") or "")) for row in rows]
        if any(raw is None for raw in raw_markets):
            reject["market_fetch"] += 1; continue
        raw_markets = [raw for raw in raw_markets if raw is not None]
        tokens = [side_token(raw, str(row.get("side") or "")) for raw, row in zip(raw_markets, rows)]
        if any(not token for token in tokens) or len(set(tokens)) != len(tokens):
            reject["token"] += 1; continue
        books = fetch_books(clob, tokens)
        if any(token not in books for token in tokens):
            reject["book_missing"] += 1; continue
        legs: list[Leg] = []
        for raw, row, token in zip(raw_markets, rows, tokens):
            book = books[token]
            original = finite(row.get("limit_price"), book["bid"])
            price = min(max(book["bid"], min(original, book["ask"] - book["tick"])), book["ask"] - book["tick"])
            legs.append(Leg(dict(row), token, str(raw.get("conditionId") or ""), raw, book["bid"], book["ask"], book["bid_size"], book["min_order"], book["tick"], price, 0.0))
        capital_per_unit = sum(max(0.0, finite(leg.row.get("weight"), 1.0)) * leg.price for leg in legs)
        max_notional = max(0.0, finite(rows[0].get("max_notional"), 0.0))
        if capital_per_unit <= 1e-12 or max_notional <= 0.0:
            reject["notional"] += 1; continue
        units = max_notional / capital_per_unit
        for leg in legs:
            own = units * max(0.0, finite(leg.row.get("weight"), 1.0))
            update_fill_probability(leg, flow, own, args.horizon_seconds, args.flow_lookback_seconds)
        initial_prices = [leg.price for leg in legs]
        edge, verified = bundle_edge(legs, clob)
        if not verified:
            reject["fee_unverified"] += 1; continue
        floor_edge = args.min_edge + max(0.0, args.reserve_bps) / 10000.0
        ticks_used = [0] * len(legs)
        while edge > floor_edge + 1e-12:
            candidates = [
                index for index, leg in enumerate(legs)
                if leg.fill_probability < args.target_leg_fill_probability
                and ticks_used[index] < args.max_improve_ticks_per_leg
                and leg.price + leg.tick < leg.ask - 1e-12
            ]
            if not candidates:
                break
            index = min(candidates, key=lambda value: legs[value].fill_probability)
            leg = legs[index]
            old_price, old_probability = leg.price, leg.fill_probability
            trial = min(leg.ask - leg.tick, leg.price + leg.tick)
            leg.price = trial
            new_edge, verified = bundle_edge(legs, clob)
            own = units * max(0.0, finite(leg.row.get("weight"), 1.0))
            update_fill_probability(leg, flow, own, args.horizon_seconds, args.flow_lookback_seconds)
            incremental_fill_value = new_edge * leg.fill_probability - edge * old_probability
            if not verified or new_edge < floor_edge - 1e-12 or incremental_fill_value <= 0.0:
                leg.price = old_price
                update_fill_probability(leg, flow, own, args.horizon_seconds, args.flow_lookback_seconds)
                ticks_used[index] = args.max_improve_ticks_per_leg
                continue
            edge = new_edge; ticks_used[index] += 1
        fill_probs = [leg.fill_probability for leg in legs]
        min_fill = min(fill_probs, default=0.0)
        bottleneck_score = edge * min_fill
        changed = any(abs(leg.price - initial) > 1e-12 for leg, initial in zip(legs, initial_prices))
        improved_bundles += int(changed)
        diagnostics.append({
            "bundle_id": bundle_id,
            "strategy": strategy,
            "edge": edge,
            "minimum_marginal_fill_probability": min_fill,
            "bottleneck_quote_priority_score": bottleneck_score,
            "improved": changed,
            "ticks_spent": sum(ticks_used),
            "final_joint_completion_estimator": "v7_graph_roundtrip_guard_empirical_fixed_horizon_joint_state",
            "marginal_product_used_for_admission": False,
        })
        if edge <= args.min_edge:
            reject["edge"] += 1; continue
        if min_fill < args.min_leg_fill_probability:
            reject["leg_fill_probability"] += 1; continue
        for leg in legs:
            leg.row["limit_price"] = f"{leg.price:.12g}"
            leg.row["expected_edge"] = f"{edge:.12g}"
            accepted_rows.append(leg.row)

    atomic_csv(args.output, accepted_rows)
    status = {
        "schema": "polymarket_v7_bundle_quote_optimizer_status_v1",
        "timestamp": now,
        "paper_only": True,
        "input_bundles": len(grouped),
        "accepted_for_prospective_joint_observation": len({row["bundle_id"] for row in accepted_rows}),
        "accepted_rows": len(accepted_rows),
        "improved_bundles": improved_bundles,
        "rejections": dict(sorted(reject.items())),
        "diagnostics": diagnostics[:50],
        "joint_completion_estimator": "none_in_quote_optimizer",
        "joint_completion_owner": "v7_graph_roundtrip_guard.py",
        "product_of_marginals_forbidden": True,
    }
    atomic_json(args.status, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
