#!/usr/bin/env python3
"""Read-only forward execution probe for the actual B2 multi-leg candidate class."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forward_maker_probe import (  # type: ignore
    Book,
    QuoteLeg,
    atomic_json,
    fetch_books,
    fetch_fee_rate,
    fetch_trades,
    finite,
    protocol_fee,
    queue_at,
    quote_price,
    request_json,
    simulate_leg,
)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


@dataclass(frozen=True)
class CandidateLeg:
    market_id: str
    outcome: str
    weight: float


@dataclass(frozen=True)
class MarketToken:
    market_id: str
    condition_id: str
    outcome: str
    token_id: str


def parse_leg_spec(spec: str) -> list[CandidateLeg]:
    out: list[CandidateLeg] = []
    for raw in str(spec or "").split("|"):
        parts = raw.split(":")
        if len(parts) != 3:
            continue
        market_id, outcome = parts[0].strip(), parts[1].strip().upper()
        weight = finite(parts[2], -1.0)
        if market_id and outcome in {"YES", "NO"} and weight > 0.0:
            out.append(CandidateLeg(market_id, outcome, weight))
    return out


def parse_gamma_market(value: dict[str, Any]) -> dict[str, MarketToken]:
    market_id = str(value.get("id") or value.get("market_id") or "")
    condition_id = str(value.get("conditionId") or value.get("condition_id") or "")
    outcomes = [str(x).strip().upper() for x in _json_list(value.get("outcomes"))]
    tokens = [str(x).strip() for x in _json_list(value.get("clobTokenIds") or value.get("clob_token_ids"))]
    out: dict[str, MarketToken] = {}
    for outcome, token in zip(outcomes, tokens):
        if outcome in {"YES", "NO"} and token and market_id and condition_id:
            out[outcome] = MarketToken(market_id, condition_id, outcome, token)
    return out


def fetch_market_tokens(gamma_url: str, market_ids: set[str], timeout: float) -> dict[tuple[str, str], MarketToken]:
    out: dict[tuple[str, str], MarketToken] = {}
    for market_id in sorted(market_ids):
        try:
            root = request_json(
                gamma_url.rstrip("/") + "/markets/" + urllib.parse.quote(market_id, safe=""),
                timeout=timeout,
            )
        except RuntimeError:
            continue
        if not isinstance(root, dict):
            continue
        for outcome, item in parse_gamma_market(root).items():
            out[(market_id, outcome)] = item
    return out


def sorted_levels(book: Book, side: str, n: int) -> list[Any]:
    if side == "bid":
        return sorted(book.bids, key=lambda x: x.price, reverse=True)[:n]
    return sorted(book.asks, key=lambda x: x.price)[:n]


def microstructure_features(book: Book) -> dict[str, float | None]:
    bids = sorted_levels(book, "bid", 5)
    asks = sorted_levels(book, "ask", 5)
    if not bids or not asks:
        return {"spread": None, "microprice": None}
    bb, ba = bids[0], asks[0]
    denom = bb.size + ba.size
    micro = (ba.price * bb.size + bb.price * ba.size) / denom if denom > 0.0 else math.nan
    out: dict[str, float | None] = {
        "best_bid": bb.price,
        "best_ask": ba.price,
        "spread": ba.price - bb.price,
        "microprice": micro if math.isfinite(micro) else None,
        "midpoint": 0.5 * (bb.price + ba.price),
    }
    for depth in (1, 3, 5):
        bid_depth = sum(x.size for x in bids[:depth])
        ask_depth = sum(x.size for x in asks[:depth])
        total = bid_depth + ask_depth
        out[f"bid_depth_l{depth}"] = bid_depth
        out[f"ask_depth_l{depth}"] = ask_depth
        out[f"imbalance_l{depth}"] = (bid_depth - ask_depth) / total if total > 0.0 else 0.0
    return out


def l1_ofi(previous: Book, current: Book) -> float:
    pb0, pa0 = previous.best_bid, previous.best_ask
    pb1, pa1 = current.best_bid, current.best_ask
    if not all(math.isfinite(x) for x in (pb0, pa0, pb1, pa1)):
        return 0.0
    qb0 = sorted_levels(previous, "bid", 1)[0].size
    qa0 = sorted_levels(previous, "ask", 1)[0].size
    qb1 = sorted_levels(current, "bid", 1)[0].size
    qa1 = sorted_levels(current, "ask", 1)[0].size
    bid_term = (qb1 if pb1 >= pb0 else 0.0) - (qb0 if pb1 <= pb0 else 0.0)
    ask_term = -(qa1 if pa1 <= pa0 else 0.0) + (qa0 if pa1 >= pa0 else 0.0)
    return bid_term + ask_term


def select_b2_candidates(payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    candidates = payload.get("candidates", {}).get("b2", []) if isinstance(payload.get("candidates"), dict) else []
    if not isinstance(candidates, list) or not candidates:
        coherence = payload.get("b2_coherence")
        candidates = coherence.get("top_raw", []) if isinstance(coherence, dict) else []
    valid = [x for x in candidates if isinstance(x, dict) and len(parse_leg_spec(str(x.get("legs") or ""))) >= 2]
    valid.sort(key=lambda x: finite(x.get("maker_entry_net_edge"), -1e9), reverse=True)
    return valid[: max(0, limit)]


def bundle_scale(
    legs: list[CandidateLeg],
    prices: dict[tuple[str, str], float],
    notional: float,
    max_leg_shares: float,
) -> float:
    denom = sum(leg.weight * max(0.0, prices.get((leg.market_id, leg.outcome), 0.0)) for leg in legs)
    if denom <= 0.0 or notional <= 0.0:
        return 0.0
    scale = notional / denom
    max_weight = max((leg.weight for leg in legs), default=0.0)
    if max_weight > 0.0 and max_leg_shares > 0.0:
        scale = min(scale, max_leg_shares / max_weight)
    return max(0.0, scale)


def break_even_completion(maker_edge: float, taker_edge: float) -> float | None:
    if maker_edge <= 0.0 or taker_edge >= 0.0:
        return None
    denom = maker_edge - taker_edge
    return (-taker_edge / denom) if denom > 0.0 else None


def compact_history(payload: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for row in payload.get("results", []) if isinstance(payload.get("results"), list) else []:
        if not isinstance(row, dict):
            continue
        rows.append({
            "candidate_market": row.get("candidate_market"),
            "policy": row.get("policy"),
            "arrival_latency_ms": row.get("arrival_latency_ms"),
            "queue_multiplier": row.get("queue_multiplier"),
            "full_completion": row.get("full_completion"),
            "bundle_completion_fraction": row.get("bundle_completion_fraction"),
            "filled_legs": row.get("filled_legs"),
            "legs": row.get("legs"),
            "force_completion_cost_usd": row.get("force_completion_cost_usd"),
            "filled_markout_60_usd": row.get("filled_markout_60_usd"),
            "filled_markout_300_usd": row.get("filled_markout_300_usd"),
            "initial_weighted_imbalance_l1": row.get("initial_weighted_imbalance_l1"),
            "initial_weighted_microprice_minus_mid": row.get("initial_weighted_microprice_minus_mid"),
            "snapshot_ofi_per_second": row.get("snapshot_ofi_per_second"),
        })
    return {
        "generated_ts": payload.get("generated_ts"),
        "source_git_sha": payload.get("source_git_sha"),
        "quote_start_ts": payload.get("quote_start_ts"),
        "quote_end_ts": payload.get("quote_end_ts"),
        "candidate_count": payload.get("candidate_count"),
        "results": rows,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "candidate_market", "policy", "arrival_latency_ms", "queue_multiplier", "legs",
        "maker_entry_net_edge", "taker_net_edge", "break_even_completion_probability",
        "filled_legs", "full_completion", "bundle_completion_fraction", "force_completion_cost_usd",
        "filled_markout_60_usd", "filled_markout_300_usd", "initial_weighted_imbalance_l1",
        "initial_weighted_microprice_minus_mid", "snapshot_ofi_per_second",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-smoke", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--history-line", type=Path)
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--data-url", default="https://data-api.polymarket.com")
    parser.add_argument("--candidates", type=int, default=3)
    parser.add_argument("--duration-seconds", type=int, default=300)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--policies", default="join,improve1")
    parser.add_argument("--arrival-latencies-ms", default="250,500,750")
    parser.add_argument("--queue-multipliers", default="1,1.25,1.5,2")
    parser.add_argument("--max-notional", type=float, default=250.0)
    parser.add_argument("--max-leg-shares", type=float, default=100.0)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    args = parser.parse_args()

    policies = [x.strip() for x in args.policies.split(",") if x.strip()]
    latencies = [max(0, int(x)) for x in args.arrival_latencies_ms.split(",") if x.strip()]
    queues = [float(x) for x in args.queue_multipliers.split(",") if x.strip() and float(x) >= 1.0]
    if not policies or any(x not in {"join", "improve1", "fade1"} for x in policies):
        raise SystemExit("invalid policies")
    if not latencies or not queues or args.duration_seconds < 1 or args.poll_seconds <= 0.0:
        raise SystemExit("invalid probe configuration")

    source = json.loads(args.live_smoke.read_text(encoding="utf-8"))
    candidates = select_b2_candidates(source, args.candidates)
    market_ids = {leg.market_id for row in candidates for leg in parse_leg_spec(str(row.get("legs") or ""))}
    token_map = fetch_market_tokens(args.gamma_url, market_ids, args.timeout_seconds)
    tokens = sorted({item.token_id for item in token_map.values()})
    initial_books = fetch_books(args.clob_url, tokens, args.timeout_seconds) if tokens else {}
    quote_start = int(time.time())
    snapshots: list[tuple[int, dict[str, Book]]] = [(quote_start, initial_books)]

    definitions: list[dict[str, Any]] = []
    for candidate in candidates:
        legs = parse_leg_spec(str(candidate.get("legs") or ""))
        if not legs or any((leg.market_id, leg.outcome) not in token_map for leg in legs):
            continue
        for policy in policies:
            prices: dict[tuple[str, str], float] = {}
            valid = True
            for leg in legs:
                item = token_map[(leg.market_id, leg.outcome)]
                book = initial_books.get(item.token_id)
                price = quote_price(book, policy) if book is not None else None
                if price is None:
                    valid = False
                    break
                prices[(leg.market_id, leg.outcome)] = price
            if not valid:
                continue
            executable = finite(candidate.get("executable_notional"), args.max_notional)
            notional = min(args.max_notional, executable) if executable > 0.0 else args.max_notional
            scale = bundle_scale(legs, prices, notional, args.max_leg_shares)
            if scale <= 0.0:
                continue
            for latency in latencies:
                qlegs: list[dict[str, Any]] = []
                valid = True
                for leg in legs:
                    item = token_map[(leg.market_id, leg.outcome)]
                    book = initial_books[item.token_id]
                    target = leg.weight * scale
                    if target + 1e-9 < book.min_order_size:
                        valid = False
                        break
                    qlegs.append({
                        "candidate": leg,
                        "token": item,
                        "price": prices[(leg.market_id, leg.outcome)],
                        "target": target,
                        "arrival": quote_start + latency / 1000.0,
                    })
                if valid:
                    definitions.append({
                        "candidate": candidate,
                        "policy": policy,
                        "latency": latency,
                        "scale": scale,
                        "legs": qlegs,
                    })

    deadline = time.monotonic() + args.duration_seconds
    while definitions and time.monotonic() < deadline:
        time.sleep(min(args.poll_seconds, max(0.0, deadline - time.monotonic())))
        try:
            snapshots.append((int(time.time()), fetch_books(args.clob_url, tokens, args.timeout_seconds)))
        except RuntimeError:
            continue
    quote_end = int(time.time())

    trades_by_condition: dict[str, list[Any]] = {}
    conditions = {item.condition_id for item in token_map.values()}
    for condition in conditions:
        try:
            trades_by_condition[condition] = fetch_trades(
                args.data_url,
                condition,
                quote_start - 1,
                quote_end + 1,
                args.timeout_seconds,
            )
        except RuntimeError:
            trades_by_condition[condition] = []
    fees = {token: fetch_fee_rate(args.clob_url, token, args.timeout_seconds) for token in tokens}

    results: list[dict[str, Any]] = []
    for definition in definitions:
        candidate = definition["candidate"]
        for qmult in queues:
            leg_rows: list[dict[str, Any]] = []
            matched_scales: list[float] = []
            mark60 = 0.0
            mark300 = 0.0
            force_cost = 0.0
            weighted_imbalance = 0.0
            weighted_micro = 0.0
            weighted_ofi = 0.0
            total_weight = 0.0
            for item in definition["legs"]:
                leg: CandidateLeg = item["candidate"]
                token: MarketToken = item["token"]
                initial = initial_books[token.token_id]
                qleg = QuoteLeg(
                    token.token_id,
                    leg.outcome,
                    item["price"],
                    item["target"],
                    queue_at(initial, item["price"]) * qmult,
                    item["arrival"],
                )
                replay = simulate_leg(
                    qleg,
                    trades_by_condition.get(token.condition_id, []),
                    snapshots,
                    fees.get(token.token_id, 0.0),
                )
                fill_fraction = replay.filled_shares / qleg.target_shares if qleg.target_shares > 0 else 0.0
                matched_scales.append(replay.filled_shares / leg.weight)
                if replay.markout_60_bid_per_share is not None:
                    mark60 += replay.markout_60_bid_per_share * replay.filled_shares
                if replay.markout_300_bid_per_share is not None:
                    mark300 += replay.markout_300_bid_per_share * replay.filled_shares
                final = snapshots[-1][1].get(token.token_id) if snapshots else None
                remaining = max(0.0, qleg.target_shares - replay.filled_shares)
                if remaining > 0.0 and final is not None and math.isfinite(final.best_ask):
                    force_cost += max(0.0, final.best_ask - qleg.limit_price) * remaining
                    force_cost += protocol_fee(remaining, final.best_ask, fees.get(token.token_id, 0.0))
                feat = microstructure_features(initial)
                weight = leg.weight
                total_weight += weight
                weighted_imbalance += weight * finite(feat.get("imbalance_l1"), 0.0)
                if feat.get("microprice") is not None and feat.get("midpoint") is not None:
                    weighted_micro += weight * (float(feat["microprice"]) - float(feat["midpoint"]))
                ofi = 0.0
                previous = initial
                for _, books in snapshots[1:]:
                    current = books.get(token.token_id)
                    if current is None:
                        continue
                    ofi += l1_ofi(previous, current)
                    previous = current
                weighted_ofi += weight * (ofi / max(1.0, quote_end - quote_start))
                leg_rows.append({
                    "market_id": leg.market_id,
                    "outcome": leg.outcome,
                    "weight": leg.weight,
                    "token_id": token.token_id,
                    "condition_id": token.condition_id,
                    "limit_price": qleg.limit_price,
                    "target_shares": qleg.target_shares,
                    "queue_ahead": qleg.queue_ahead,
                    "filled_shares": replay.filled_shares,
                    "fill_fraction": fill_fraction,
                    "markout_60_bid_per_share": replay.markout_60_bid_per_share,
                    "markout_300_bid_per_share": replay.markout_300_bid_per_share,
                    "initial_features": feat,
                })
            matched_scale = min(matched_scales) if matched_scales else 0.0
            completion = matched_scale / definition["scale"] if definition["scale"] > 0.0 else 0.0
            maker = finite(candidate.get("maker_entry_net_edge"), 0.0)
            taker = finite(candidate.get("taker_net_edge"), 0.0)
            results.append({
                "candidate_market": str(candidate.get("market") or ""),
                "candidate_slug": str(candidate.get("slug") or ""),
                "policy": definition["policy"],
                "arrival_latency_ms": definition["latency"],
                "queue_multiplier": qmult,
                "legs": len(leg_rows),
                "maker_entry_net_edge": maker,
                "taker_net_edge": taker,
                "break_even_completion_probability": break_even_completion(maker, taker),
                "filled_legs": sum(float(row["filled_shares"]) > 0.0 for row in leg_rows),
                "full_completion": completion >= 0.999,
                "bundle_completion_fraction": max(0.0, min(1.0, completion)),
                "force_completion_cost_usd": force_cost,
                "filled_markout_60_usd": mark60,
                "filled_markout_300_usd": mark300,
                "initial_weighted_imbalance_l1": weighted_imbalance / total_weight if total_weight else 0.0,
                "initial_weighted_microprice_minus_mid": weighted_micro / total_weight if total_weight else 0.0,
                "snapshot_ofi_per_second": weighted_ofi / total_weight if total_weight else 0.0,
                "leg_results": leg_rows,
            })

    payload = {
        "schema": "polymarket_b2_multileg_forward_probe_v1",
        "generated_ts": int(time.time()),
        "source_git_sha": source.get("git_sha"),
        "source_live_smoke_generated_ts": source.get("generated_ts"),
        "read_only": True,
        "submitted_orders": 0,
        "candidate_count": len(candidates),
        "definition_count": len(definitions),
        "book_snapshots": len(snapshots),
        "quote_start_ts": quote_start,
        "quote_end_ts": quote_end,
        "method": {
            "queue_model": "trade-print conservative FIFO; cancellations do not reduce queue",
            "policies": policies,
            "arrival_latencies_ms": latencies,
            "queue_multipliers": queues,
            "microstructure": "initial microprice/spread/depth/imbalance plus snapshot-to-snapshot L1 OFI proxy",
            "market_impact": "ignored; read-only counterfactual",
        },
        "results": results,
    }
    atomic_json(args.output, payload)
    if args.csv:
        write_csv(args.csv, results)
    if args.history_line:
        args.history_line.parent.mkdir(parents=True, exist_ok=True)
        args.history_line.write_text(
            json.dumps(compact_history(payload), separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    print(
        f"hf_multileg_forward_probe candidates={len(candidates)} definitions={len(definitions)} "
        f"rows={len(results)} full={sum(bool(row.get('full_completion')) for row in results)} "
        f"any_fill_rows={sum(int(row.get('filled_legs', 0)) > 0 for row in results)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
