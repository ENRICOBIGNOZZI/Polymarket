#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def levels(raw: Any, *, reverse: bool) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                continue
            price = finite(row.get("price"), -1.0)
            size = finite(row.get("size"), 0.0)
            if 0.0 < price < 1.0 and size > 0.0:
                out.append((price, size))
    out.sort(key=lambda item: item[0], reverse=reverse)
    return out


def depth(rows: list[tuple[float, float]], n: int) -> float:
    return sum(size for _, size in rows[: max(0, n)])


def imbalance(bids: list[tuple[float, float]], asks: list[tuple[float, float]], n: int) -> float | None:
    bid_depth = depth(bids, n)
    ask_depth = depth(asks, n)
    total = bid_depth + ask_depth
    if total <= 0.0:
        return None
    return (bid_depth - ask_depth) / total


def book_features(book: dict[str, Any]) -> dict[str, Any]:
    bids = levels(book.get("bids"), reverse=True)
    asks = levels(book.get("asks"), reverse=False)
    if not bids or not asks:
        return {"valid": False}
    bid, bid_size = bids[0]
    ask, ask_size = asks[0]
    if ask <= bid:
        return {"valid": False}
    tick = max(1e-9, finite(book.get("tick_size"), 0.01))
    midpoint = 0.5 * (bid + ask)
    microprice = (ask * bid_size + bid * ask_size) / (bid_size + ask_size)
    spread = ask - bid
    micro_dev = microprice - midpoint
    return {
        "valid": True,
        "best_bid": bid,
        "best_ask": ask,
        "midpoint": midpoint,
        "spread": spread,
        "spread_ticks": spread / tick,
        "spread_bps_mid": 10000.0 * spread / midpoint if midpoint > 0.0 else None,
        "microprice": microprice,
        "microprice_minus_mid": micro_dev,
        "microprice_minus_mid_ticks": micro_dev / tick,
        "microprice_minus_mid_bps": 10000.0 * micro_dev / midpoint if midpoint > 0.0 else None,
        "l1_bid_depth": depth(bids, 1),
        "l1_ask_depth": depth(asks, 1),
        "l3_bid_depth": depth(bids, 3),
        "l3_ask_depth": depth(asks, 3),
        "l5_bid_depth": depth(bids, 5),
        "l5_ask_depth": depth(asks, 5),
        "imbalance_l1": imbalance(bids, asks, 1),
        "imbalance_l3": imbalance(bids, asks, 3),
        "imbalance_l5": imbalance(bids, asks, 5),
    }


def ofi_proxy(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    prev = book_features(previous)
    cur = book_features(current)
    if not prev.get("valid") or not cur.get("valid"):
        return {"valid": False}
    pb0, qb0 = finite(prev["best_bid"]), finite(prev["l1_bid_depth"])
    pa0, qa0 = finite(prev["best_ask"]), finite(prev["l1_ask_depth"])
    pb1, qb1 = finite(cur["best_bid"]), finite(cur["l1_bid_depth"])
    pa1, qa1 = finite(cur["best_ask"]), finite(cur["l1_ask_depth"])
    bid_flow = (qb1 if pb1 >= pb0 else 0.0) - (qb0 if pb1 <= pb0 else 0.0)
    ask_flow = -(qa1 if pa1 <= pa0 else 0.0) + (qa0 if pa1 >= pa0 else 0.0)
    ofi = bid_flow + ask_flow
    scale = max(1e-12, 0.5 * (qb0 + qa0 + qb1 + qa1))
    return {
        "valid": True,
        "ofi_l1_proxy": ofi,
        "ofi_l1_proxy_normalized": ofi / scale,
        "note": "snapshot-to-snapshot L1 OFI proxy; not event-time order-flow reconstruction",
    }


def break_even_pair_probability(maker_edge: float, taker_fallback_edge: float) -> float | None:
    maker = finite(maker_edge, math.nan)
    taker = finite(taker_fallback_edge, math.nan)
    if not math.isfinite(maker) or not math.isfinite(taker) or maker <= 0.0 or taker >= 0.0:
        return None
    denominator = maker - taker
    return -taker / denominator if denominator > 0.0 else None


def best_positive_b2(live: dict[str, Any]) -> dict[str, Any] | None:
    block = live.get("b2_coherence") if isinstance(live.get("b2_coherence"), dict) else {}
    rows = block.get("top_raw") if isinstance(block, dict) else []
    if not isinstance(rows, list):
        return None
    candidates = [
        row for row in rows
        if isinstance(row, dict) and finite(row.get("maker_entry_net_edge"), -1.0) > 0.0
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: finite(row.get("maker_entry_net_edge"), -1.0))


def forward_probe_class(probe: dict[str, Any]) -> str:
    rows = probe.get("results")
    if not isinstance(rows, list) or not rows:
        return "unknown"
    two_leg = all(isinstance(row, dict) and isinstance(row.get("yes"), dict) and isinstance(row.get("no"), dict) for row in rows)
    complete_set = all("source_locked_complete_set_edge" in row for row in rows if isinstance(row, dict))
    return "two_leg_complete_set_reward" if two_leg and complete_set else "other"


def calibration_policy_summary(calibration: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    by_policy = calibration.get("by_policy")
    if not isinstance(by_policy, dict):
        return out
    for name, row in by_policy.items():
        if not isinstance(row, dict):
            continue
        out[str(name)] = {
            "sessions": int(finite(row.get("sessions"))),
            "probes": int(finite(row.get("probes"))),
            "any_fills": int(finite(row.get("any_fills"))),
            "pair_fills": int(finite(row.get("pair_fills"))),
            "one_sided_only": int(finite(row.get("one_sided_only"))),
            "pair_fill_rate_wilson_upper": row.get("pair_fill_rate_wilson_upper"),
            "pnl_ex_rewards_usd": row.get("total_pnl_ex_rewards_usd"),
            "markout_60_per_share": row.get("filled_share_weighted_markout_60_bid_per_share"),
            "eligible_for_paper_shadow": bool(row.get("eligible_for_paper_shadow")),
        }
    return out


def coverage_report(live: dict[str, Any], probe: dict[str, Any], calibration: dict[str, Any]) -> dict[str, Any]:
    b2 = best_positive_b2(live)
    intents = live.get("intents") if isinstance(live.get("intents"), dict) else {}
    live_bundles = int(finite(intents.get("bundles")))
    probe_class = forward_probe_class(probe)
    active_b2 = b2 is not None and live_bundles > 0
    mismatch = active_b2 and probe_class != "b2_multileg"
    maker_edge = finite(b2.get("maker_entry_net_edge"), math.nan) if b2 else math.nan
    taker_edge = finite(b2.get("taker_net_edge"), math.nan) if b2 else math.nan
    q_be = break_even_pair_probability(maker_edge, taker_edge)
    return {
        "schema": "polymarket_hf_execution_microstructure_diagnostic_v1",
        "live_git_sha": live.get("git_sha"),
        "live_generated_ts": live.get("generated_ts"),
        "active_live_maker_class": "B2_multi_leg" if active_b2 else "none_detected",
        "live_intent_bundles": live_bundles,
        "best_positive_b2": None if b2 is None else {
            "market": b2.get("market"),
            "slug": b2.get("slug"),
            "legs": b2.get("legs"),
            "maker_entry_net_edge": maker_edge,
            "taker_net_edge": taker_edge,
            "break_even_pair_completion_probability": q_be,
        },
        "forward_probe_class": probe_class,
        "forward_calibration": calibration_policy_summary(calibration),
        "execution_evidence_class_mismatch": mismatch,
        "cross_class_pair_fill_transfer_valid": False if mismatch else None,
        "decision": "MORE_EVIDENCE_REQUIRED" if mismatch or active_b2 else "NO_ACTIVE_MAKER_CHALLENGER",
        "required_next_measurements": [
            "B2-leg-specific queue ahead and compatible taker flow",
            "paired and partial completion probability on the actual B2 leg set",
            "60s and 300s fill-conditioned markout per leg and bundle",
            "microprice and microprice-minus-mid at admission and before fills",
            "L1/L3/L5 order-book imbalance and near-touch depth",
            "snapshot OFI proxy plus event-time OFI when WebSocket lineage is available",
            "submit/cancel latency stress and stale-book age",
            "forced-completion/taker-fallback cost and inventory-risk exposure",
        ],
    }


def load_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object: {path}")
    return obj


def analyze_books(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous_by_token: dict[str, dict[str, Any]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        if not isinstance(row, dict):
            continue
        token = str(row.get("token_id") or row.get("asset_id") or "")
        features = book_features(row)
        features.update({"ts": row.get("ts"), "token_id": token})
        previous = previous_by_token.get(token)
        if previous is not None:
            features.update(ofi_proxy(previous, row))
        else:
            features.update({"ofi_l1_proxy": None, "ofi_l1_proxy_normalized": None})
        if token:
            previous_by_token[token] = row
        rows.append(features)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose HF execution-evidence coverage and microstructure features")
    parser.add_argument("--live-smoke", type=Path, required=True)
    parser.add_argument("--forward-probe", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--books-jsonl", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = coverage_report(load_json(args.live_smoke), load_json(args.forward_probe), load_json(args.calibration))
    if args.books_jsonl is not None and args.books_jsonl.exists():
        report["microstructure_rows"] = analyze_books(args.books_jsonl)
    else:
        report["microstructure_rows"] = []
        report["microstructure_collection_status"] = "missing; current forward-maker telemetry does not persist book-depth snapshots"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "decision": report["decision"],
        "execution_evidence_class_mismatch": report["execution_evidence_class_mismatch"],
        "microstructure_rows": len(report["microstructure_rows"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
