#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from v6_market_common import fee_per_share, finite, parse_array, request_json, resolve_fee_details
except ModuleNotFoundError:
    from scripts.v6_market_common import fee_per_share, finite, parse_array, request_json, resolve_fee_details

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]


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


def atomic_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def side_token(raw: dict[str, Any], side: str) -> str:
    ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).strip().upper() for x in parse_array(raw.get("outcomes"))]
    for i, name in enumerate(outcomes[: len(ids)]):
        if name == side:
            return ids[i]
    if len(ids) >= 2:
        return ids[0] if side == "YES" else ids[1]
    return ""


def fetch_market(gamma: str, market_id: str) -> dict[str, Any] | None:
    try:
        raw = request_json(f"{gamma.rstrip('/')}/markets/{market_id}")
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def fetch_book(clob: str, token: str) -> dict[str, Any] | None:
    try:
        raw = request_json(clob.rstrip("/") + "/book?token_id=" + token)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for key, out in (("bids", bids), ("asks", asks)):
        for row in raw.get(key, []):
            if not isinstance(row, dict):
                continue
            p, q = finite(row.get("price"), math.nan), finite(row.get("size"), 0.0)
            if math.isfinite(p) and 0 < p < 1 and q > 0:
                out.append((p, q))
    bids.sort(reverse=True)
    asks.sort()
    if not bids or not asks:
        return None
    return {
        "bid": bids[0][0], "bid_size": bids[0][1], "ask": asks[0][0],
        "tick": max(1e-6, finite(raw.get("tick_size"), 0.01)),
        "min_order": max(1.0, finite(raw.get("min_order_size"), 1.0)),
    }


def read_tape(path: Path, cutoff: int, now: int) -> list[tuple[int, str, str, float, float]]:
    out = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                ts = int(finite(row.get("timestamp"), 0.0))
                if ts < cutoff or ts > now + 30:
                    continue
                token = str(row.get("asset_id") or row.get("token_id") or "")
                side = str(row.get("side") or "").upper()
                price = finite(row.get("price"), math.nan)
                size = max(0.0, finite(row.get("size"), 0.0))
                if token and side in {"BUY", "SELL"} and math.isfinite(price) and size > 0:
                    out.append((ts, token, side, price, size))
    except OSError:
        pass
    out.sort()
    return out


def empirical_states(
    tape: list[tuple[int, str, str, float, float]],
    *,
    tokens: list[str],
    prices: list[float],
    required: list[float],
    start_ts: int,
    end_ts: int,
    window_seconds: int,
) -> list[tuple[bool, ...]]:
    states = []
    cursor = start_ts
    while cursor + window_seconds <= end_ts:
        volume = [0.0] * len(tokens)
        stop = cursor + window_seconds
        for ts, token, side, price, size in tape:
            if ts < cursor:
                continue
            if ts >= stop:
                break
            if side != "SELL":
                continue
            for i, expected_token in enumerate(tokens):
                if token == expected_token and price <= prices[i] + 1e-12:
                    volume[i] += size
        states.append(tuple(volume[i] + 1e-12 >= required[i] for i in range(len(tokens))))
        cursor = stop
    return states


def bootstrap_lower(values: list[float], seed: int, reps: int = 400, quantile: float = 0.10) -> float:
    if not values:
        return -math.inf
    if len(values) == 1:
        return values[0]
    rng = random.Random(seed)
    means = []
    n = len(values)
    for _ in range(max(50, reps)):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return means[min(len(means) - 1, max(0, int(quantile * (len(means) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser(description="Dependence-aware empirical multi-leg completion guard")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--trade-tape", type=Path, required=True)
    ap.add_argument("--min-edge", type=float, default=0.00005)
    ap.add_argument("--lookback-seconds", type=int, default=900)
    ap.add_argument("--window-seconds", type=int, default=180)
    ap.add_argument("--min-windows", type=int, default=4)
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--bootstrap-reps", type=int, default=400)
    ap.add_argument("--bootstrap-quantile", type=float, default=0.10)
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    now = int(time.time())
    tape = read_tape(args.trade_tape, now - max(args.lookback_seconds, args.window_seconds), now)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in load_rows(args.input):
        grouped[str(row.get("bundle_id") or "")].append(row)

    accepted: list[dict[str, Any]] = []
    reject = Counter()
    diagnostics = []
    for bundle_id, rows in grouped.items():
        if not bundle_id or not rows:
            continue
        strategy = str(rows[0].get("strategy") or "").upper()
        raw_markets = []
        tokens = []
        books = []
        fees = []
        valid = True
        for row in rows:
            raw = fetch_market(gamma, str(row.get("market_id") or ""))
            if raw is None:
                valid = False; break
            token = side_token(raw, str(row.get("side") or "").upper())
            book = fetch_book(clob, token) if token else None
            if not token or book is None:
                valid = False; break
            fee = resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), token)
            if not fee.verified:
                valid = False; break
            raw_markets.append(raw); tokens.append(token); books.append(book); fees.append(fee)
        if not valid:
            reject["market_book_fee"] += 1
            continue

        weights = [max(0.0, finite(row.get("weight"), 1.0)) for row in rows]
        prices = [max(0.0, finite(row.get("limit_price"), books[i]["bid"])) for i, row in enumerate(rows)]
        capital_per_unit = sum(w * p for w, p in zip(weights, prices))
        max_notional = max(0.0, finite(rows[0].get("max_notional"), 0.0))
        if capital_per_unit <= 1e-12 or max_notional <= 0:
            reject["notional"] += 1
            continue
        units = max_notional / capital_per_unit
        shares = [units * w for w in weights]
        if any(q + 1e-12 < books[i]["min_order"] for i, q in enumerate(shares)):
            reject["min_order"] += 1
            continue
        queues = [books[i]["bid_size"] if abs(prices[i] - books[i]["bid"]) <= 0.25 * books[i]["tick"] else 0.0 for i in range(len(rows))]
        required = [queues[i] + shares[i] for i in range(len(rows))]

        start = now - args.lookback_seconds
        states = empirical_states(
            tape, tokens=tokens, prices=prices, required=required,
            start_ts=start, end_ts=now, window_seconds=args.window_seconds,
        )
        if len(states) < args.min_windows:
            reject["insufficient_joint_windows"] += 1
            diagnostics.append({"bundle_id": bundle_id, "strategy": strategy, "windows": len(states), "reason": "insufficient_joint_windows"})
            continue

        maker_fee_per_unit = sum(w * fee_per_share(px, fee, taker=False) for w, px, fee in zip(weights, prices, fees))
        reported_edge = finite(rows[0].get("expected_edge"), 0.0)
        edge = reported_edge - (maker_fee_per_unit / capital_per_unit if strategy == "LOCAL_FACTOR" else 0.0)
        if edge <= args.min_edge:
            reject["edge_after_maker_fee"] += 1
            continue

        complete_prob = sum(all(state) for state in states) / len(states)
        distribution = Counter("".join("1" if x else "0" for x in state) for state in states)
        stress = {}
        accepted_stress = True
        for mult in (1.0, 1.5, 2.0):
            slip = max(0.0, args.slippage_bps) * mult / 10000.0
            complete_edge = edge
            if strategy == "LOCAL_FACTOR":
                complete_edge -= max(0.0, mult - 1.0) * args.slippage_bps / 10000.0
            pnls = []
            for state in states:
                if all(state):
                    pnls.append(complete_edge * max_notional)
                    continue
                loss = 0.0
                for i, filled in enumerate(state):
                    if not filled:
                        continue
                    unwind_px = max(1e-6, books[i]["bid"] * (1.0 - slip))
                    entry_fee = fee_per_share(prices[i], fees[i], taker=False)
                    exit_fee = fee_per_share(unwind_px, fees[i], taker=True)
                    loss += shares[i] * max(0.0, prices[i] + entry_fee - unwind_px + exit_fee)
                pnls.append(-loss)
            mean_ev = sum(pnls) / len(pnls)
            lower = bootstrap_lower(
                pnls,
                seed=20260825 + sum(ord(c) for c in bundle_id) + int(mult * 100),
                reps=args.bootstrap_reps,
                quantile=max(0.0, min(0.49, args.bootstrap_quantile)),
            )
            stress[f"{mult:g}x"] = {"mean_ev": mean_ev, "bootstrap_lower": lower}
            accepted_stress = accepted_stress and lower > 0.0

        diag = {
            "bundle_id": bundle_id, "strategy": strategy, "windows": len(states),
            "empirical_complete_probability": complete_prob,
            "state_distribution": {k: v / len(states) for k, v in sorted(distribution.items())},
            "edge": edge, "stress": stress,
            "max_required_compatible_volume": max(required, default=0.0),
            "reason": "accepted" if accepted_stress else "nonpositive_empirical_state_ev",
        }
        diagnostics.append(diag)
        if not accepted_stress:
            reject["nonpositive_empirical_state_ev"] += 1
            continue
        for row in rows:
            row["expected_edge"] = f"{edge:.12g}"
            accepted.append(row)

    atomic_csv(args.output, accepted)
    status = {
        "timestamp": now, "paper_only": True, "completion_model": "empirical_same_window_joint_state",
        "input_bundles": len(grouped),
        "accepted_bundles": len({str(row.get("bundle_id") or "") for row in accepted}),
        "accepted_rows": len(accepted), "rejections": dict(sorted(reject.items())),
        "best_empirical_complete_probability": max((finite(d.get("empirical_complete_probability"), 0.0) for d in diagnostics), default=0.0),
        "best_2x_bootstrap_lower_ev": max((finite((d.get("stress") or {}).get("2x", {}).get("bootstrap_lower"), -math.inf) for d in diagnostics), default=0.0),
        "diagnostics": sorted(diagnostics, key=lambda d: finite((d.get("stress") or {}).get("1x", {}).get("mean_ev"), -math.inf), reverse=True)[:30],
        "contracts": ["same_window_leg_state", "queue_plus_actual_target", "dependent_completion", "subset_unwind", "bootstrap_lower_ev", "cost_stress_1x_1.5x_2x"],
    }
    atomic_json(args.status, status)
    print(json.dumps({k: status[k] for k in ("input_bundles", "accepted_bundles", "best_empirical_complete_probability", "best_2x_bootstrap_lower_ev", "rejections")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
