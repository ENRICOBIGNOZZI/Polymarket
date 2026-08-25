#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from v6_market_common import TapeFlow, fee_per_share, fill_probability_proxy, finite, parse_array, request_json, resolve_fee_details
except ModuleNotFoundError:
    from scripts.v6_market_common import TapeFlow, fee_per_share, fill_probability_proxy, finite, parse_array, request_json, resolve_fee_details

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]


def load_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as h:
            return [dict(row) for row in csv.DictReader(h)]
    except OSError:
        return []


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=FIELDS)
        w.writeheader()
        w.writerows([{k: row.get(k, "") for k in FIELDS} for row in rows])
    os.replace(tmp, path)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def market(gamma: str, market_id: str) -> dict[str, Any] | None:
    try:
        root = request_json(f"{gamma.rstrip('/')}/markets/{market_id}")
        return root if isinstance(root, dict) else None
    except Exception:
        return None


def end_ts(raw: dict[str, Any]) -> int | None:
    values: list[Any] = [raw.get("endDate"), raw.get("end_date"), raw.get("endDateIso")]
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        values += [events[0].get("endDate"), events[0].get("end_date")]
    for value in values:
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            out = int(value)
            if out > 10_000_000_000:
                out //= 1000
            if out > 0:
                return out
        if isinstance(value, str):
            try:
                return int(datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
            except ValueError:
                pass
    return None


def token_for(raw: dict[str, Any], side: str) -> str:
    ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).strip().upper() for x in parse_array(raw.get("outcomes"))]
    for i, name in enumerate(outcomes[: len(ids)]):
        if name == side:
            return ids[i]
    if len(ids) >= 2:
        return ids[0] if side == "YES" else ids[1]
    return ""


def book(clob: str, token: str) -> dict[str, float] | None:
    try:
        root = request_json(clob.rstrip("/") + "/book?token_id=" + token)
    except Exception:
        return None
    if not isinstance(root, dict):
        return None
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for key, out in (("bids", bids), ("asks", asks)):
        for row in root.get(key, []):
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
        "tick": max(1e-6, finite(root.get("tick_size"), 0.01)),
        "min_order": max(1.0, finite(root.get("min_order_size"), 1.0)),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="V6 maker-bundle completion, unwind and cost-stress guard")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--status", type=Path, required=True)
    p.add_argument("--trade-tape", type=Path, required=True)
    p.add_argument("--min-edge", type=float, default=0.00005)
    p.add_argument("--min-joint-fill", type=float, default=1e-7)
    p.add_argument("--flow-lookback-seconds", type=int, default=900)
    p.add_argument("--fill-horizon-seconds", type=int, default=180)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--ttr-buffer-seconds", type=int, default=900)
    args = p.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    now = int(time.time())
    flow = TapeFlow.from_csv(args.trade_tape, lookback_seconds=args.flow_lookback_seconds, now=now)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in load_rows(args.input):
        grouped[str(row.get("bundle_id") or "")].append(row)

    accepted: list[dict[str, Any]] = []
    rejects = Counter()
    diagnostics: list[dict[str, Any]] = []

    for bundle_id, rows in grouped.items():
        if not rows:
            continue
        strategy = str(rows[0].get("strategy") or "").upper()
        raws: list[dict[str, Any]] = []
        tokens: list[str] = []
        books: list[dict[str, float]] = []
        fees = []
        invalid = False
        for row in rows:
            raw = market(gamma, str(row.get("market_id") or ""))
            if raw is None:
                invalid = True; break
            token = token_for(raw, str(row.get("side") or "").upper())
            if not token:
                invalid = True; break
            b = book(clob, token)
            if b is None:
                invalid = True; break
            fee = resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), token)
            if not fee.verified:
                invalid = True; break
            raws.append(raw); tokens.append(token); books.append(b); fees.append(fee)
        if invalid or len(raws) != len(rows):
            rejects["market_book_fee"] += 1
            continue

        # Local-factor exits are markout trades: every leg must have point-in-time
        # expiry and the planned hold must end before resolution minus a buffer.
        if strategy == "LOCAL_FACTOR":
            expiries = [end_ts(raw) for raw in raws]
            if any(ts is None for ts in expiries):
                rejects["ttr_missing"] += 1
                continue
            hold_deadline = max(int(finite(row.get("hold_deadline_ts"), 0.0)) for row in rows)
            if hold_deadline <= now or any(hold_deadline > int(ts) - args.ttr_buffer_seconds for ts in expiries if ts is not None):
                rejects["ttr_invalid"] += 1
                continue

        weights = [max(0.0, finite(row.get("weight"), 1.0)) for row in rows]
        prices = [max(0.0, finite(row.get("limit_price"), books[i]["bid"])) for i, row in enumerate(rows)]
        capital_per_unit = sum(w * px for w, px in zip(weights, prices))
        max_notional = max(0.0, finite(rows[0].get("max_notional"), 0.0))
        if capital_per_unit <= 1e-12 or max_notional <= 0:
            rejects["notional"] += 1
            continue
        units = max_notional / capital_per_unit
        shares = [units * w for w in weights]
        if any(q + 1e-12 < books[i]["min_order"] for i, q in enumerate(shares)):
            rejects["min_order"] += 1
            continue

        fill_probs: list[float] = []
        for i, (token, px, q) in enumerate(zip(tokens, prices, shares)):
            queue = books[i]["bid_size"] if abs(px - books[i]["bid"]) <= 0.25 * books[i]["tick"] else 0.0
            rate = flow.compatible_sell_rate(token, px, lookback_seconds=args.flow_lookback_seconds)
            fill_probs.append(fill_probability_proxy(
                queue_ahead=queue, own_shares=q, compatible_flow_per_second=rate,
                horizon_seconds=args.fill_horizon_seconds, prior_flow_per_second=1.0 / 300.0,
            ))
        joint = math.prod(fill_probs) if fill_probs else 0.0
        if joint < args.min_joint_fill:
            rejects["joint_fill_probability"] += 1
            diagnostics.append({"bundle_id": bundle_id, "strategy": strategy, "joint_fill": joint, "reason": "joint_fill_probability"})
            continue

        reported_edge = finite(rows[0].get("expected_edge"), 0.0)
        maker_fee_per_unit = sum(
            w * fee_per_share(px, fee, taker=False)
            for w, px, fee in zip(weights, prices, fees)
        )
        # Graph queue-filter edge is already maker-fee aware; LF v3 edge was not.
        edge = reported_edge - (maker_fee_per_unit / capital_per_unit if strategy == "LOCAL_FACTOR" else 0.0)
        if edge <= args.min_edge:
            rejects["edge_after_maker_fee"] += 1
            continue

        stress_ev: dict[str, float] = {}
        for mult in (1.0, 1.5, 2.0):
            slip = max(0.0, args.slippage_bps) * mult / 10000.0
            # Complete LF carries exit slippage already at 1x. Stress only the
            # incremental 1.5x/2x component. Complete-set Graph has no exit leg.
            complete_edge = edge
            if strategy == "LOCAL_FACTOR":
                complete_edge -= max(0.0, mult - 1.0) * args.slippage_bps / 10000.0
            complete_profit = joint * complete_edge * max_notional
            abort_loss = 0.0
            for i, (pfill, q, px, fee) in enumerate(zip(fill_probs, shares, prices, fees)):
                other_joint = math.prod(fill_probs[:i] + fill_probs[i + 1:]) if len(fill_probs) > 1 else 1.0
                p_incomplete_fill = pfill * (1.0 - other_joint)
                unwind_px = max(0.0, books[i]["bid"] * (1.0 - slip))
                entry_fee = fee_per_share(px, fee, taker=False)
                exit_fee = fee_per_share(unwind_px, fee, taker=True)
                loss_ps = max(0.0, px + entry_fee - unwind_px + exit_fee)
                abort_loss += p_incomplete_fill * q * loss_ps
            stress_ev[f"{mult:g}x"] = complete_profit - abort_loss

        if any(value <= 0.0 for value in stress_ev.values()):
            rejects["nonpositive_completion_unwind_ev"] += 1
            diagnostics.append({"bundle_id": bundle_id, "strategy": strategy, "joint_fill": joint, "edge": edge, "stress_ev": stress_ev, "reason": "nonpositive_completion_unwind_ev"})
            continue

        for row in rows:
            row["expected_edge"] = f"{edge:.12g}"
            accepted.append(row)
        diagnostics.append({"bundle_id": bundle_id, "strategy": strategy, "joint_fill": joint, "edge": edge, "stress_ev": stress_ev, "reason": "accepted"})

    atomic_csv(args.output, accepted)
    status = {
        "timestamp": now, "paper_only": True, "input_bundles": len(grouped),
        "accepted_bundles": len({str(row.get('bundle_id') or '') for row in accepted}),
        "accepted_rows": len(accepted), "rejections": dict(sorted(rejects.items())),
        "best_joint_fill": max((finite(x.get("joint_fill"), 0.0) for x in diagnostics), default=0.0),
        "best_stressed_ev_2x": max((finite((x.get("stress_ev") or {}).get("2x"), -math.inf) for x in diagnostics), default=0.0),
        "diagnostics": sorted(diagnostics, key=lambda x: finite((x.get("stress_ev") or {}).get("1x"), -math.inf), reverse=True)[:30],
        "contracts": ["actual_target_shares", "joint_completion", "maker_entry_fee", "partial_fill_unwind", "cost_stress_1x_1.5x_2x", "local_factor_ttr_fail_closed"],
    }
    atomic_json(args.status, status)
    print(json.dumps({k: status[k] for k in ("input_bundles", "accepted_bundles", "best_joint_fill", "best_stressed_ev_2x", "rejections")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
