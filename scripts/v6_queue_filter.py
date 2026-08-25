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

try:
    from v6_market_common import (
    TapeFlow,
    fee_per_share,
    fill_probability_proxy,
    finite,
    parse_array,
    request_json,
    resolve_fee_details,
)
except ModuleNotFoundError:
    from scripts.v6_market_common import (
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in FIELDS} for row in rows])
    os.replace(tmp, path)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def fetch_market(gamma: str, market_id: str) -> dict[str, Any] | None:
    try:
        root = request_json(f"{gamma.rstrip('/')}/markets/{market_id}")
        return root if isinstance(root, dict) else None
    except Exception:
        return None


def side_token(raw: dict[str, Any], side: str) -> str:
    ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).strip().upper() for x in parse_array(raw.get("outcomes"))]
    for i, name in enumerate(outcomes[: len(ids)]):
        if name == side:
            return ids[i]
    if len(ids) >= 2:
        return ids[0] if side == "YES" else ids[1]
    return ""


def fetch_books(clob: str, tokens: list[str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for i in range(0, len(tokens), 80):
        root = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in tokens[i : i + 80]])
        for raw in root if isinstance(root, list) else []:
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("asset_id") or "")
            bids, asks = [], []
            for key, values in (("bids", bids), ("asks", asks)):
                for row in raw.get(key, []):
                    if not isinstance(row, dict):
                        continue
                    price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
                    if math.isfinite(price) and 0 < price < 1 and size > 0:
                        values.append((price, size))
            bids.sort(reverse=True)
            asks.sort()
            if token and bids and asks:
                output[token] = {
                    "bid": bids[0][0], "ask": asks[0][0], "bid_size": bids[0][1],
                    "min_order": max(1.0, finite(raw.get("min_order_size"), 1.0)),
                    "tick": max(1e-6, finite(raw.get("tick_size"), 0.01)),
                }
    return output


def queue_at_price(leg: Leg, price: float) -> float:
    return leg.bid_size if abs(price - leg.bid) <= max(1e-9, 0.25 * leg.tick) else 0.0


def update_fill_probability(leg: Leg, flow: TapeFlow, *, own_shares: float, horizon_seconds: int, lookback_seconds: int) -> None:
    rate = flow.compatible_sell_rate(leg.token, leg.price, lookback_seconds=lookback_seconds)
    leg.queue = queue_at_price(leg, leg.price)
    leg.fill_probability = fill_probability_proxy(
        queue_ahead=leg.queue, own_shares=max(leg.min_order, own_shares),
        compatible_flow_per_second=rate, horizon_seconds=horizon_seconds,
        prior_flow_per_second=1.0 / 300.0,
    )


def bundle_edge(legs: list[Leg], clob: str) -> tuple[float, bool]:
    total = 0.0
    verified = True
    for leg in legs:
        weight = max(0.0, finite(leg.row.get("weight"), 1.0))
        fee = resolve_fee_details(leg.raw_market, clob, leg.condition, leg.token)
        verified = verified and fee.verified
        total += weight * (leg.price + fee_per_share(leg.price, fee, taker=False))
    return 1.0 - total, verified


def main() -> int:
    parser = argparse.ArgumentParser(description="V6 maker queue/flow admission and quote optimizer")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--min-edge", type=float, default=0.0002)
    parser.add_argument("--reserve-bps", type=float, default=10.0)
    parser.add_argument("--flow-lookback-seconds", type=int, default=900)
    parser.add_argument("--horizon-seconds", type=int, default=180)
    parser.add_argument("--min-leg-fill-probability", type=float, default=0.02)
    parser.add_argument("--min-joint-fill-probability", type=float, default=0.0005)
    parser.add_argument("--target-leg-fill-probability", type=float, default=0.20)
    parser.add_argument("--max-improve-ticks-per-leg", type=int, default=3)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = cfg["gamma_url"], cfg["clob_url"]
    now = int(time.time())
    flow = TapeFlow.from_csv(args.trade_tape, lookback_seconds=args.flow_lookback_seconds, now=now)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for input_path in args.input:
        for row in load_rows(input_path):
            grouped[str(row.get("bundle_id") or "")].append(row)

    accepted_rows: list[dict[str, Any]] = []
    reject = Counter()
    diagnostics: list[dict[str, Any]] = []
    improved_bundles = 0

    for bundle_id, rows in grouped.items():
        if not bundle_id or not rows:
            continue
        strategy = str(rows[0].get("strategy") or "").upper()
        if strategy not in {"GRAPH_RV", "STRUCTURAL_TYPED"}:
            accepted_rows.extend(rows)
            continue

        raw_markets: list[dict[str, Any]] = []
        valid = True
        for row in rows:
            raw = fetch_market(gamma, str(row.get("market_id") or ""))
            if raw is None:
                valid = False
                break
            raw_markets.append(raw)
        if not valid:
            reject["market_fetch"] += 1
            continue

        tokens = [side_token(raw, str(row.get("side") or "").upper()) for raw, row in zip(raw_markets, rows)]
        if any(not token for token in tokens):
            reject["token"] += 1
            continue
        try:
            books = fetch_books(clob, tokens)
        except Exception:
            reject["book_fetch"] += 1
            continue
        if any(token not in books for token in tokens):
            reject["book_missing"] += 1
            continue

        legs: list[Leg] = []
        for raw, row, token in zip(raw_markets, rows, tokens):
            book = books[token]
            original = finite(row.get("limit_price"), book["bid"])
            price = min(max(book["bid"], min(original, book["ask"] - book["tick"])), book["ask"] - book["tick"])
            condition = str(raw.get("conditionId") or "")
            legs.append(Leg(row=dict(row), token=token, condition=condition, raw_market=raw,
                            bid=book["bid"], ask=book["ask"], bid_size=book["bid_size"],
                            min_order=book["min_order"], tick=book["tick"], price=price, queue=0.0))

        capital_per_unit = sum(max(0.0, finite(leg.row.get("weight"), 1.0)) * leg.price for leg in legs)
        max_notional = max(0.0, finite(rows[0].get("max_notional"), 0.0))
        units = max_notional / max(capital_per_unit, 1e-9)
        for leg in legs:
            own = units * max(0.0, finite(leg.row.get("weight"), 1.0))
            update_fill_probability(leg, flow, own_shares=own, horizon_seconds=args.horizon_seconds,
                                    lookback_seconds=args.flow_lookback_seconds)

        initial_prices = [leg.price for leg in legs]
        edge, fee_verified = bundle_edge(legs, clob)
        if not fee_verified:
            reject["fee_unverified"] += 1
            continue
        reserve = max(0.0, args.reserve_bps) / 10000.0
        floor_edge = args.min_edge + reserve
        ticks_used = [0] * len(legs)
        while edge > floor_edge + 1e-12:
            candidates = [i for i, leg in enumerate(legs)
                          if leg.fill_probability < args.target_leg_fill_probability
                          and ticks_used[i] < args.max_improve_ticks_per_leg
                          and leg.price + leg.tick < leg.ask - 1e-12]
            if not candidates:
                break
            i = min(candidates, key=lambda idx: legs[idx].fill_probability)
            leg = legs[i]
            old_price, old_probability = leg.price, leg.fill_probability
            leg.price = min(leg.ask - leg.tick, leg.price + leg.tick)
            new_edge, verified = bundle_edge(legs, clob)
            if not verified or new_edge < floor_edge - 1e-12:
                leg.price = old_price
                break
            own = units * max(0.0, finite(leg.row.get("weight"), 1.0))
            update_fill_probability(leg, flow, own_shares=own, horizon_seconds=args.horizon_seconds,
                                    lookback_seconds=args.flow_lookback_seconds)
            if leg.fill_probability <= old_probability + 1e-9:
                leg.price = old_price
                update_fill_probability(leg, flow, own_shares=own, horizon_seconds=args.horizon_seconds,
                                        lookback_seconds=args.flow_lookback_seconds)
                ticks_used[i] = args.max_improve_ticks_per_leg
                continue
            edge = new_edge
            ticks_used[i] += 1

        fill_probs = [leg.fill_probability for leg in legs]
        min_fill = min(fill_probs, default=0.0)
        joint_fill = math.prod(fill_probs) if fill_probs else 0.0
        expected_utility = edge * joint_fill
        changed = any(abs(leg.price - initial) > 1e-12 for leg, initial in zip(legs, initial_prices))
        if changed:
            improved_bundles += 1
        diagnostics.append({"bundle_id": bundle_id, "strategy": strategy, "edge": edge,
                            "min_leg_fill_probability": min_fill, "joint_fill_probability": joint_fill,
                            "expected_fill_edge": expected_utility, "improved": changed,
                            "max_queue_ahead": max((leg.queue for leg in legs), default=0.0),
                            "ticks_spent": sum(ticks_used)})
        if edge <= args.min_edge:
            reject["edge"] += 1
            continue
        if min_fill < args.min_leg_fill_probability:
            reject["leg_fill_probability"] += 1
            continue
        if joint_fill < args.min_joint_fill_probability:
            reject["joint_fill_probability"] += 1
            continue
        for leg in legs:
            leg.row["limit_price"] = f"{leg.price:.12g}"
            leg.row["expected_edge"] = f"{edge:.12g}"
            accepted_rows.append(leg.row)

    atomic_csv(args.output, accepted_rows)
    status = {"timestamp": now, "paper_only": True, "input_bundles": len(grouped),
              "accepted_bundles": len({row["bundle_id"] for row in accepted_rows}),
              "accepted_rows": len(accepted_rows), "improved_bundles": improved_bundles,
              "rejections": dict(sorted(reject.items())),
              "best_edge": max((finite(row.get("edge"), 0.0) for row in diagnostics), default=0.0),
              "best_joint_fill_probability": max((finite(row.get("joint_fill_probability"), 0.0) for row in diagnostics), default=0.0),
              "best_expected_fill_edge": max((finite(row.get("expected_fill_edge"), 0.0) for row in diagnostics), default=0.0),
              "max_queue_ahead": max((finite(row.get("max_queue_ahead"), 0.0) for row in diagnostics), default=0.0),
              "flow_lookback_seconds": args.flow_lookback_seconds, "reserve_bps": args.reserve_bps,
              "diagnostics": sorted(diagnostics, key=lambda row: row["expected_fill_edge"], reverse=True)[:20]}
    atomic_json(args.status, status)
    print(json.dumps({key: status[key] for key in ("input_bundles", "accepted_bundles", "improved_bundles",
                                                    "best_edge", "best_joint_fill_probability",
                                                    "best_expected_fill_edge", "rejections")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
