#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import v6_relation_intents_v2 as relation
    from v6_market_common import fee_per_share, finite, request_json, resolve_fee_details
except ModuleNotFoundError:
    from scripts import v6_relation_intents_v2 as relation
    from scripts.v6_market_common import fee_per_share, finite, request_json, resolve_fee_details

FIELDS = [
    "family", "direction", "kind", "end_ts", "market_id", "question", "threshold",
    "market_mid", "projected_q", "projection_delta", "bid", "ask", "spread", "weight",
    "maker_edge_yes", "taker_edge_yes", "taker_edge_no", "best_taker_side", "best_taker_edge",
]


def isotonic_increasing(values: list[float], weights: list[float]) -> list[float]:
    if len(values) != len(weights):
        raise ValueError("values and weights must have equal length")
    blocks: list[dict[str, float | int]] = []
    for i, (value, weight) in enumerate(zip(values, weights)):
        w = max(1e-12, float(weight))
        blocks.append({"start": i, "end": i, "w": w, "wy": w * float(value)})
        while len(blocks) >= 2:
            a, b = blocks[-2], blocks[-1]
            mean_a = float(a["wy"]) / float(a["w"])
            mean_b = float(b["wy"]) / float(b["w"])
            if mean_a <= mean_b + 1e-15:
                break
            blocks[-2:] = [{
                "start": int(a["start"]), "end": int(b["end"]),
                "w": float(a["w"]) + float(b["w"]),
                "wy": float(a["wy"]) + float(b["wy"]),
            }]
    output = [0.0] * len(values)
    for block in blocks:
        mean = float(block["wy"]) / float(block["w"])
        for i in range(int(block["start"]), int(block["end"]) + 1):
            output[i] = mean
    return output


def monotone_projection(values: list[float], weights: list[float], *, decreasing: bool) -> list[float]:
    transformed = [-float(x) for x in values] if decreasing else [float(x) for x in values]
    projected = isotonic_increasing(transformed, weights)
    if decreasing:
        projected = [-x for x in projected]
    return [max(0.001, min(0.999, x)) for x in projected]


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


def main() -> int:
    parser = argparse.ArgumentParser(description="V6 typed monotone structural probability curve")
    parser.add_argument("--config", type=Path, default=Path("config/paper_v6.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=700)
    parser.add_argument("--min-liquidity", type=float, default=2.0)
    parser.add_argument("--min-family-size", type=int, default=3)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    now = int(time.time())
    failures: list[str] = []
    try:
        markets = relation.base.discover(gamma, args.markets, args.min_liquidity)
        tokens = [token for market in markets for token in (market.yes_token, market.no_token)]
        books = relation.base.fetch_books(clob, tokens)
    except Exception as exc:
        markets, books = [], {}
        failures.append(f"market_data:{type(exc).__name__}:{exc}")

    grouped: dict[tuple[str, str, str, int], list[tuple[float, Any]]] = defaultdict(list)
    for market in markets:
        sig = relation.typed_signature(market.question)
        if sig is None or int(market.end_ts or 0) <= 0:
            continue
        grouped[(sig.family, sig.direction, sig.kind, int(market.end_ts))].append((sig.threshold, market))

    rows: list[dict[str, Any]] = []
    curves = 0
    violating_curves = 0
    executable_positive = 0
    total_projection_l1 = 0.0
    max_abs_delta = 0.0
    slip = max(0.0, args.slippage_bps) / 10000.0

    for (family, direction, kind, end_ts), members in grouped.items():
        members.sort(key=lambda item: item[0])
        usable = []
        for threshold, market in members:
            yes = books.get(market.yes_token)
            no = books.get(market.no_token)
            if yes is None or no is None:
                continue
            mid = 0.5 * (yes.bid + yes.ask)
            spread = max(1e-6, yes.ask - yes.bid)
            depth = max(1e-6, yes.bid_size + yes.ask_size + no.bid_size + no.ask_size)
            weight = math.sqrt(depth) / spread
            usable.append((threshold, market, yes, no, mid, spread, weight))
        if len(usable) < max(2, args.min_family_size):
            continue
        curves += 1
        observed = [item[4] for item in usable]
        weights = [item[6] for item in usable]
        projected = monotone_projection(observed, weights, decreasing=(direction == "UP"))
        deltas = [q - p for p, q in zip(observed, projected)]
        if any(abs(delta) > 1e-9 for delta in deltas):
            violating_curves += 1
        total_projection_l1 += sum(abs(delta) for delta in deltas)
        max_abs_delta = max(max_abs_delta, max(abs(delta) for delta in deltas))

        for (threshold, market, yes, no, mid, spread, weight), q, delta in zip(usable, projected, deltas):
            try:
                raw = request_json(f"{gamma.rstrip('/')}/markets/{market.market_id}")
            except Exception:
                raw = None
            yes_fee = no_fee = None
            if isinstance(raw, dict):
                yes_fee = resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), market.yes_token)
                no_fee = resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), market.no_token)
            taker_yes = -math.inf
            taker_no = -math.inf
            if yes_fee is not None and yes_fee.verified:
                entry = min(0.999999, yes.ask * (1.0 + slip))
                taker_yes = q - entry - fee_per_share(entry, yes_fee, taker=True)
            if no_fee is not None and no_fee.verified:
                entry = min(0.999999, no.ask * (1.0 + slip))
                taker_no = (1.0 - q) - entry - fee_per_share(entry, no_fee, taker=True)
            best_side = "YES" if taker_yes >= taker_no else "NO"
            best_edge = max(taker_yes, taker_no)
            executable_positive += int(math.isfinite(best_edge) and best_edge > 0.0)
            rows.append({
                "family": family, "direction": direction, "kind": kind, "end_ts": end_ts,
                "market_id": market.market_id, "question": market.question, "threshold": threshold,
                "market_mid": mid, "projected_q": q, "projection_delta": delta,
                "bid": yes.bid, "ask": yes.ask, "spread": spread, "weight": weight,
                "maker_edge_yes": q - yes.bid,
                "taker_edge_yes": taker_yes if math.isfinite(taker_yes) else "",
                "taker_edge_no": taker_no if math.isfinite(taker_no) else "",
                "best_taker_side": best_side if math.isfinite(best_edge) else "",
                "best_taker_edge": best_edge if math.isfinite(best_edge) else "",
            })

    atomic_csv(args.output, rows)
    status = {
        "timestamp": now,
        "paper_only": True,
        "model": "typed_weighted_isotonic_threshold_curve",
        "markets": len(markets),
        "candidate_families": len(grouped),
        "curves": curves,
        "violating_curves": violating_curves,
        "rows": len(rows),
        "executable_positive_rows": executable_positive,
        "total_projection_l1": total_projection_l1,
        "max_abs_projection_delta": max_abs_delta,
        "failures": failures[:30],
        "trade_admission": "diagnostic_only_until_forward_markout_validation",
        "contracts": ["typed_threshold", "same_end_ts", "weighted_isotonic", "verified_fee_taker_edge"],
    }
    atomic_json(args.status, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
