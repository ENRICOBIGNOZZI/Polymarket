#!/usr/bin/env python3
"""Bounded zero-authority A0-A5 alpha-driven maker horse race.

Reads the live causal Polymarket book tape, the verified M5/M15 market
selection, and already-running external crypto market-data status files.
Every alpha sees the same quote anchors and the same native maker queue replay.
Alpha only decides whether a quote belongs to its ex-ante cohort.

No canonical ledger writes. No OMS. No authenticated execution. No orders.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
from typing import Any

from v7_causal_book import BookTimeline

SCHEMA = "polymarket_v7_alpha_driven_maker_shadow_v2"
STATUS_SCHEMA = "polymarket_v7_alpha_driven_maker_shadow_status_v2"
SELECTION_SCHEMA = "polymarket_v7_multi_crypto_book_selection_v1"
EXTERNAL_SCHEMA = "polymarket_v7_external_venue_runtime_v1"
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
HORIZONS = {"M5", "M15"}
POLICIES = (
    "A0_BASELINE",
    "A1_EXTERNAL_MOMENTUM",
    "A2_PM_MOMENTUM",
    "A3_COMBINED_MOMENTUM",
    "A4_MEAN_REVERSION",
    "A5_TOXICITY_VETO",
)


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()


def finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def sign(value: float | None, eps: float = 1e-15) -> int:
    if value is None or not math.isfinite(value) or abs(value) <= eps:
        return 0
    return 1 if value > 0 else -1


def make_protocol(ttls_ms: list[int], markouts_ms: list[int]) -> dict[str, Any]:
    arms = []
    for placement in ("JOIN", "IMPROVE1"):
        for ttl in sorted(set(ttls_ms)):
            arms.append({"id": f"{placement}_{ttl}MS", "placement": placement, "lifetime_ms": ttl})
    return {
        "maker": {
            "markout_horizons_ms": sorted(set(markouts_ms)),
            "arms": arms,
        }
    }


def external_path(run_root: Path, asset: str) -> Path:
    if asset == "BTC":
        return run_root / "external_fair" / "external_venues.json"
    return run_root / "external_fair" / "assets" / asset.lower() / "external_venues.json"


def external_features(run_root: Path, asset: str, model_sha: str, now_ns: int) -> dict[str, Any]:
    value = load(external_path(run_root, asset))
    try:
        timestamp_ns = int(value.get("timestamp_ns") or 0)
    except (TypeError, ValueError, OverflowError):
        timestamp_ns = 0
    age_ns = now_ns - timestamp_ns if timestamp_ns > 0 else 10**30
    identity_ok = (
        value.get("schema") == EXTERNAL_SCHEMA
        and value.get("asset") == asset
        and value.get("code_sha") == model_sha
        and value.get("paper_only") is True
        and value.get("authenticated_execution") is False
        and value.get("real_order_submission") is False
        and value.get("state") in {"OPERATIONAL", "WARMING_OR_DEGRADED"}
        and value.get("valid") is True
        and -5_000_000_000 <= age_ns <= 2_000_000_000
    )
    names = (
        "return_50ms", "return_100ms", "return_250ms", "return_1s", "return_5s",
        "aggregate_ofi", "aggregate_trade_imbalance", "dispersion_bps",
        "realized_vol_fast", "composite_price", "composite_microprice",
    )
    features = {name: finite(value.get(name)) for name in names}
    vote_names = (
        "return_100ms", "return_250ms", "return_1s",
        "aggregate_ofi", "aggregate_trade_imbalance",
    )
    votes = [sign(features[name]) for name in vote_names if features[name] is not None]
    vote = sum(votes)
    confidence = abs(vote) / len(votes) if votes else 0.0
    direction = sign(float(vote)) if votes else 0
    return {
        "ready": bool(identity_ok and votes),
        "age_ms": age_ns / 1_000_000.0 if timestamp_ns else None,
        "direction": direction,
        "confidence": confidence,
        "vote": vote,
        "vote_count": len(votes),
        "features": features,
        "state": value.get("state"),
        "valid": value.get("valid"),
    }


def pm_features(row: dict[str, Any]) -> dict[str, Any]:
    placement = row.get("placement_features")
    placement = placement if isinstance(placement, dict) else {}
    imbalance = finite(placement.get("imbalance"))
    ofi = finite(placement.get("ofi"))
    short_return = finite(placement.get("short_return_ticks"))
    aggressive_buy = finite(placement.get("aggressive_buy_prints_per_second"))
    aggressive_sell = finite(placement.get("aggressive_sell_prints_per_second"))
    flow_delta = (
        aggressive_buy - aggressive_sell
        if aggressive_buy is not None and aggressive_sell is not None
        else None
    )
    raw = {
        "imbalance": imbalance,
        "ofi": ofi,
        "short_return_ticks": short_return,
        "aggressive_buy_prints_per_second": aggressive_buy,
        "aggressive_sell_prints_per_second": aggressive_sell,
        "aggressive_flow_delta": flow_delta,
        "spread_ticks": finite(placement.get("spread_ticks")),
        "ew_vol_ticks": finite(placement.get("ew_vol_ticks")),
    }
    vote_fields = (imbalance, ofi, short_return, flow_delta)
    votes = [sign(x) for x in vote_fields if x is not None]
    vote = sum(votes)
    confidence = abs(vote) / len(votes) if votes else 0.0
    return {
        "ready": bool(votes),
        "direction": sign(float(vote)) if votes else 0,
        "confidence": confidence,
        "vote": vote,
        "vote_count": len(votes),
        "features": raw,
    }


def policy_flags(*, outcome: str, external: dict[str, Any], pm: dict[str, Any]) -> dict[str, bool]:
    outcome_sign = 1 if outcome == "YES" else -1
    ext_support = (
        outcome_sign * int(external["direction"]) * float(external["confidence"])
        if external.get("ready") else 0.0
    )
    pm_support = (
        int(pm["direction"]) * float(pm["confidence"])
        if pm.get("ready") else 0.0
    )
    p = pm.get("features") or {}
    short_return = finite(p.get("short_return_ticks"))
    imbalance = finite(p.get("imbalance"))
    ofi = finite(p.get("ofi"))

    ext_momentum = bool(external.get("ready") and ext_support >= 0.60)
    pm_momentum = bool(pm.get("ready") and pm_support >= 0.50)
    combined = ext_momentum and pm_momentum
    mean_reversion = bool(
        short_return is not None and short_return < 0
        and (not external.get("ready") or ext_support >= 0.0)
        and ((imbalance is not None and imbalance > 0) or (ofi is not None and ofi > 0))
    )
    toxic = bool(
        (external.get("ready") and ext_support <= -0.60)
        or (pm.get("ready") and pm_support <= -0.50)
    )
    return {
        "A0_BASELINE": True,
        "A1_EXTERNAL_MOMENTUM": ext_momentum,
        "A2_PM_MOMENTUM": pm_momentum,
        "A3_COMBINED_MOMENTUM": combined,
        "A4_MEAN_REVERSION": mean_reversion,
        "A5_TOXICITY_VETO": not toxic,
    }


def active_markets(selection: dict[str, Any], model_sha: str, now_ms: int) -> list[dict[str, Any]]:
    if (
        selection.get("schema") != SELECTION_SCHEMA
        or selection.get("model_sha") != model_sha
        or selection.get("paper_only") is not True
        or selection.get("authenticated_execution") is not False
        or selection.get("real_order_submission") is not False
        or selection.get("execution_authority") is not False
        or selection.get("selection_only") is not True
    ):
        return []
    rows = []
    for row in selection.get("markets") or []:
        if not isinstance(row, dict):
            continue
        asset = str(row.get("asset") or "").upper()
        horizon = str(row.get("horizon") or "").upper()
        try:
            start = int(row.get("start_timestamp_ms") or 0)
            end = int(row.get("end_timestamp_ms") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            asset in ASSETS and horizon in HORIZONS and start <= now_ms < end
            and str(row.get("market_id") or "")
            and str(row.get("yes_token") or "")
            and str(row.get("no_token") or "")
        ):
            rows.append(row)
    rows.sort(key=lambda r: (str(r["asset"]), str(r["horizon"]), str(r["market_id"])))
    return rows


def make_anchor(row: dict[str, Any], *, market: dict[str, Any], token_id: str,
                outcome: str, model_sha: str, quantity: float,
                external: dict[str, Any], pm: dict[str, Any],
                policies: dict[str, bool]) -> dict[str, Any]:
    bid = float(row["best_bid"])
    receive_ms = int(row["receive_wall_ms"])
    sequence = int(row["observer_sequence"])
    record_id = stable_id("alpha-maker-v2", model_sha, market["market_id"], token_id, sequence)
    return {
        "kind": "MAKER_SHADOW_ANCHOR",
        "market_id": str(market["market_id"]),
        "token_id": token_id,
        "outcome": outcome,
        "asset": str(market["asset"]),
        "horizon": str(market["horizon"]),
        "origin_ms": receive_ms,
        "observer_sequence": sequence,
        "book_gap_counter": None,
        "observer_session_id": None,
        "connection_epoch": None,
        "external": external,
        "pm": pm,
        "policies": policies,
        "order": {
            "event_type": "ORDER_SUBMITTED",
            "record_id": record_id,
            "market_id": str(market["market_id"]),
            "token_id": token_id,
            "model_sha": model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "receive_ts_ms": receive_ms,
            "intended_size": quantity,
            "limit_price": bid,
            "metadata": {
                "component": "professional_maker",
                "counterfactual": True,
                "excluded_from_portfolio_equity": True,
                "arrival_receive_monotonic_ns": int(row.get("receive_monotonic_ns") or 0),
                "arrival_exchange_event_ns": int(row.get("exchange_event_ns") or 0),
            },
        },
    }


def latest_cut(history: list[dict[str, Any]], target_ns: int) -> dict[str, Any] | None:
    for row in reversed(history):
        if int(row.get("receive_monotonic_ns") or 0) <= target_ns:
            if row.get("valid") is not True or row.get("lineage_continuous") is not True:
                return None
            return row
    return None


def replay_anchor_native(anchor: dict[str, Any], book: BookTimeline, binary: Path,
                         protocol: dict[str, Any], evaluation_ms: int) -> dict[str, Any]:
    order = anchor["order"]
    meta = order["metadata"]
    market_id = str(anchor["market_id"])
    token_id = str(anchor["token_id"])
    arrival_ns = int(meta.get("arrival_receive_monotonic_ns") or 0)
    exchange_ns = int(meta.get("arrival_exchange_event_ns") or 0)
    origin_ms = int(anchor["origin_ms"])
    history = list(book.history.get((market_id, token_id), ()))
    continuity_ok = (
        anchor.get("book_gap_counter") == book.gaps
        and anchor.get("observer_session_id") == book.session
        and int(anchor.get("connection_epoch") or 0) == book.epoch
        and arrival_ns > 0 and exchange_ns > 0
        and book.watermark_ms >= origin_ms + evaluation_ms
        and book.watermark_monotonic_ns >= arrival_ns + evaluation_ms * 1_000_000
    )
    origin = latest_cut(history, arrival_ns)
    if not continuity_ok or origin is None:
        return {
            "arms": [
                {"arm": a["id"], "state": "LOCAL_TAPE_CONTINUITY_CENSORED",
                 "fills": [], "operational_filled_shares": None}
                for a in protocol["maker"]["arms"]
            ],
            "replay_engine": "NATIVE_MAKER_PAPER_VIA_RESEARCH_ADAPTER",
        }

    output = []
    for arm in protocol["maker"]["arms"]:
        arm_id = str(arm["id"])
        try:
            tick = float(origin["tick_size"])
            bid = float(origin["best_bid"])
            ask = float(origin["best_ask"])
            quantity = float(order["intended_size"])
            price = bid + (tick if arm["placement"] == "IMPROVE1" else 0.0)
            price = round(price / tick) * tick
            queue_ahead = float(origin.get("bid_depth_l1") or 0.0) if arm["placement"] == "JOIN" else 0.0
        except (KeyError, TypeError, ValueError, OverflowError):
            output.append({"arm": arm_id, "state": "INPUT_INVALID", "fills": [],
                           "operational_filled_shares": None})
            continue
        if not (
            0 < tick < 1 and 0 < bid < ask < 1 and 0 < price < ask
            and quantity > 0 and queue_ahead >= 0
        ):
            output.append({"arm": arm_id, "state": "POST_ONLY_OR_INPUT_INELIGIBLE", "fills": [],
                           "operational_filled_shares": 0.0})
            continue

        start_ns = arrival_ns - 1_000_000
        trade_end_ns = start_ns + (int(arm["lifetime_ms"]) + 101) * 1_000_000
        trades = []
        trade_lineage_bad = False
        for row in history:
            receive_ns = int(row.get("receive_monotonic_ns") or 0)
            if not start_ns <= receive_ns <= trade_end_ns:
                continue
            trade = row.get("public_trade")
            if not isinstance(trade, dict):
                continue
            if row.get("valid") is not True or row.get("lineage_continuous") is not True:
                trade_lineage_bad = True
                break
            try:
                trades.append({
                    "observer_sequence": int(row["observer_sequence"]),
                    "aggressor_side": str(trade.get("aggressor_side") or ""),
                    "price": float(trade["price"]),
                    "size": float(trade["size"]),
                    "exchange_event_ns": int(trade.get("exchange_event_ns") or row["exchange_event_ns"]),
                    "receive_monotonic_ns": receive_ns,
                })
            except (KeyError, TypeError, ValueError, OverflowError):
                trade_lineage_bad = True
                break
        if trade_lineage_bad:
            output.append({"arm": arm_id, "state": "TRADE_LINEAGE_CENSORED", "fills": [],
                           "operational_filled_shares": None})
            continue

        request = {
            "start_ns": start_ns,
            "exchange_ns": exchange_ns,
            "tick": tick,
            "price": price,
            "quantity": quantity,
            "best_ask": ask,
            "queue_ahead": queue_ahead,
            "lifetime_ms": int(arm["lifetime_ms"]),
            "trades": trades,
        }
        try:
            completed = subprocess.run(
                [str(binary)], input=json.dumps(request), text=True,
                capture_output=True, timeout=5, check=True,
            )
            native = json.loads(completed.stdout)
            if (
                native.get("schema") != "polymarket_v7_maker_research_replay_v1"
                or native.get("paper_only") is not True
                or native.get("authenticated_execution") is not False
                or native.get("real_order_submission") is not False
            ):
                raise ValueError("native replay safety contract")
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
            output.append({
                "arm": arm_id, "state": "NATIVE_REPLAY_CENSORED", "fills": [],
                "operational_filled_shares": None, "error": f"{type(exc).__name__}:{exc}",
            })
            continue

        fills = native.get("fills") if isinstance(native.get("fills"), list) else []
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            fill["markouts"] = {}
            fill_ns = int(fill.get("receive_monotonic_ns") or 0)
            fill_qty = float(fill.get("quantity") or 0.0)
            for horizon in protocol["maker"]["markout_horizons_ms"]:
                cut = latest_cut(history, fill_ns + int(horizon) * 1_000_000)
                value = None
                if cut is not None:
                    try:
                        future_bid = float(cut["best_bid"])
                        future_ask = float(cut["best_ask"])
                        future_depth = float(cut.get("bid_depth_l1") or 0.0)
                        if 0 < future_bid < future_ask < 1:
                            value = {
                                "mid_minus_fill": 0.5 * (future_bid + future_ask) - price,
                                "best_bid_minus_fill": future_bid - price,
                                "bid_depth_l1": future_depth,
                                "liquidation_depth_sufficient": future_depth >= fill_qty,
                            }
                    except (KeyError, TypeError, ValueError, OverflowError):
                        value = None
                fill["markouts"][str(horizon)] = value

        output.append({
            **native,
            "arm": arm_id,
            "state": "OBSERVED",
            "research_request": request,
            "fills": fills,
        })
    return {
        "arms": output,
        "replay_engine": "NATIVE_MAKER_PAPER_VIA_RESEARCH_ADAPTER",
        "book_scope": "L1_PLUS_PUBLIC_PRINTS_CAUSAL_REPLAY",
        "comparison_semantics": "SAME_ANCHOR_SAME_REPLAY_ALPHA_FILTER_ONLY",
    }


def new_stat() -> dict[str, Any]:
    return {
        "eligible_anchors": 0,
        "evaluated": 0,
        "observed": 0,
        "censored_or_ineligible": 0,
        "filled_anchors": 0,
        "fill_events": 0,
        "filled_shares": 0.0,
        "mid_markout": defaultdict(lambda: {"dollars": 0.0, "shares": 0.0, "observations": 0}),
        "executable_markout": defaultdict(
            lambda: {"dollars": 0.0, "shares": 0.0, "observations": 0,
                     "adverse_dollars": 0.0, "favorable_dollars": 0.0}
        ),
    }


def add_result(stat: dict[str, Any], arm: dict[str, Any]) -> None:
    stat["evaluated"] += 1
    if arm.get("state") != "OBSERVED":
        stat["censored_or_ineligible"] += 1
        return
    stat["observed"] += 1
    fills = arm.get("fills") if isinstance(arm.get("fills"), list) else []
    if fills:
        stat["filled_anchors"] += 1
        stat["fill_events"] += len(fills)
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        quantity = float(fill.get("quantity") or 0.0)
        if not math.isfinite(quantity) or quantity <= 0:
            continue
        stat["filled_shares"] += quantity
        marks = fill.get("markouts") if isinstance(fill.get("markouts"), dict) else {}
        for horizon, value in marks.items():
            if not isinstance(value, dict):
                continue
            mid = finite(value.get("mid_minus_fill"))
            executable = finite(value.get("best_bid_minus_fill"))
            if mid is not None:
                row = stat["mid_markout"][str(horizon)]
                row["dollars"] += mid * quantity
                row["shares"] += quantity
                row["observations"] += 1
            if executable is not None:
                dollars = executable * quantity
                row = stat["executable_markout"][str(horizon)]
                row["dollars"] += dollars
                row["shares"] += quantity
                row["observations"] += 1
                if dollars < 0:
                    row["adverse_dollars"] += -dollars
                elif dollars > 0:
                    row["favorable_dollars"] += dollars


def serialize_stat(stat: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in stat.items() if k not in {"mid_markout", "executable_markout"}}
    out["fill_anchor_rate"] = (
        stat["filled_anchors"] / stat["observed"] if stat["observed"] else None
    )
    out["mean_filled_shares_per_observed"] = (
        stat["filled_shares"] / stat["observed"] if stat["observed"] else None
    )
    out["mid_markout"] = {}
    for horizon, row in stat["mid_markout"].items():
        out["mid_markout"][horizon] = {
            **row,
            "per_share": row["dollars"] / row["shares"] if row["shares"] else None,
        }
    out["executable_markout"] = {}
    for horizon, row in stat["executable_markout"].items():
        out["executable_markout"][horizon] = {
            **row,
            "per_share": row["dollars"] / row["shares"] if row["shares"] else None,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-root", type=Path, required=True)
    ap.add_argument("--book-tape", type=Path, required=True)
    ap.add_argument("--book-status", type=Path, required=False)
    ap.add_argument("--selection", type=Path)
    ap.add_argument("--binary", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--duration-seconds", type=float, default=30.0)
    ap.add_argument("--sample-ms", type=int, default=2000)
    ap.add_argument("--quantity-shares", type=float, default=5.0)
    ap.add_argument("--ttl-arms-ms", default="250,500,1000")
    ap.add_argument("--markout-horizons-ms", default="250,500,1000,2000,5000")
    ap.add_argument("--maximum-active-markets", type=int, default=24)
    args = ap.parse_args()

    if len(args.model_sha) != 40 or any(c not in "0123456789abcdef" for c in args.model_sha):
        raise SystemExit("invalid model sha")
    if args.duration_seconds <= 0 or not 100 <= args.sample_ms <= 10_000:
        raise SystemExit("invalid bounded run")
    if not 0 < args.quantity_shares <= 20 or not args.binary.is_file():
        raise SystemExit("invalid quantity or native replay binary")
    selection_path = args.selection or (args.run_root / "universe" / "book_selection.json")

    ttls = [int(x) for x in args.ttl_arms_ms.split(",") if x.strip()]
    markouts = [int(x) for x in args.markout_horizons_ms.split(",") if x.strip()]
    if not ttls or not markouts or min(ttls + markouts) <= 0:
        raise SystemExit("invalid horizon grid")
    protocol = make_protocol(ttls, markouts)
    evaluation_ms = max(max(ttls) + 101, max(markouts) + 50)
    book = BookTimeline(args.book_tape, args.model_sha, retention_ms=evaluation_ms + 15_000)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.status.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    pending: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    policy_stats: dict[str, dict[str, dict[str, Any]]] = {
        policy: {arm["id"]: new_stat() for arm in protocol["maker"]["arms"]}
        for policy in POLICIES
    }
    slice_stats: dict[str, Counter[str]] = defaultdict(Counter)
    external_states: dict[str, Counter[str]] = {asset: Counter() for asset in ASSETS}
    sampling_end = time.monotonic() + args.duration_seconds
    drain_end = sampling_end + evaluation_ms / 1000.0 + 5.0
    next_sample = 0.0

    def publish(state: str) -> None:
        payload = {
            "schema": STATUS_SCHEMA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "automatic_promotion": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "excluded_from_portfolio_equity": True,
            "model_sha": args.model_sha,
            "state": state,
            "policies": list(POLICIES),
            "counts": dict(counts),
            "external_states": {k: dict(v) for k, v in external_states.items()},
            "policy_arm": {
                policy: {arm: serialize_stat(stat) for arm, stat in arms.items()}
                for policy, arms in policy_stats.items()
            },
            "asset_horizon": {k: dict(v) for k, v in slice_stats.items()},
            "pending": len(pending),
            "timestamp_ns": time.time_ns(),
            "semantics": {
                "alpha_role": "EX_ANTE_QUOTE_FILTER_ONLY",
                "execution": "SAME_NATIVE_REPLAY_FOR_ALL_POLICIES",
                "queue": "CANONICAL_MAKER_PAPER_ENGINE",
                "continuity": "LOCALLY_CONSUMED_CAUSAL_TAPE_RESEARCH_ONLY",
                "selection": "ACTIVE_M5_M15_FROM_VERIFIED_LIVE_SELECTION",
                "threshold_tuning": "NONE_PNL_BLIND_FIXED_SIGN_MAJORITIES",
            },
        }
        args.status.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def write_record(out, item: dict[str, Any], result: dict[str, Any], state: str) -> None:
        record = {
            "schema": SCHEMA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "automatic_promotion": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "excluded_from_portfolio_equity": True,
            "model_sha": args.model_sha,
            "state": state,
            "anchor": item["anchor"],
            "result": result,
            "timestamp_ns": time.time_ns(),
        }
        out.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        out.flush()

    with args.output.open("a", encoding="utf-8") as out:
        while time.monotonic() < drain_end:
            book.poll()
            mono = time.monotonic()
            now_ns = time.time_ns()
            now_ms = now_ns // 1_000_000

            if mono < sampling_end and mono >= next_sample:
                next_sample = mono + args.sample_ms / 1000.0
                selection = load(selection_path)
                markets = active_markets(selection, args.model_sha, now_ms)
                if len(markets) > args.maximum_active_markets:
                    markets = markets[:args.maximum_active_markets]
                counts["sampling_ticks"] += 1
                counts["active_markets_seen"] += len(markets)
                external_by_asset = {
                    asset: external_features(args.run_root, asset, args.model_sha, now_ns)
                    for asset in ASSETS
                }
                for asset, ext in external_by_asset.items():
                    external_states[asset]["ready" if ext["ready"] else "unavailable"] += 1

                for market in markets:
                    asset = str(market["asset"])
                    horizon = str(market["horizon"])
                    ext = external_by_asset[asset]
                    for outcome, token in (
                        ("YES", str(market["yes_token"])),
                        ("NO", str(market["no_token"])),
                    ):
                        row = book.asof(str(market["market_id"]), token, now_ms)
                        if row is None:
                            counts["missing_book"] += 1
                            continue
                        age_ms = now_ms - int(row["receive_wall_ms"])
                        if not 0 <= age_ms <= 2_000:
                            counts["stale_book"] += 1
                            continue
                        if not isinstance(row.get("placement_features"), dict):
                            counts["missing_pm_features"] += 1
                            continue
                        key = stable_id(market["market_id"], token, row["observer_sequence"])
                        if key in seen:
                            continue
                        seen.add(key)
                        pm = pm_features(row)
                        flags = policy_flags(outcome=outcome, external=ext, pm=pm)
                        anchor = make_anchor(
                            row, market=market, token_id=token, outcome=outcome,
                            model_sha=args.model_sha, quantity=args.quantity_shares,
                            external=ext, pm=pm, policies=flags,
                        )
                        anchor["book_gap_counter"] = book.gaps
                        anchor["observer_session_id"] = book.session
                        anchor["connection_epoch"] = book.epoch
                        pending[key] = {"anchor": anchor}
                        counts["anchors_total"] += 1
                        slice_stats[f"{asset}:{horizon}"]["anchors"] += 1
                        for policy, active in flags.items():
                            if active:
                                counts[f"eligible:{policy}"] += 1
                                for stat in policy_stats[policy].values():
                                    stat["eligible_anchors"] += 1

            for key, item in list(pending.items()):
                anchor = item["anchor"]
                continuity_changed = (
                    anchor["book_gap_counter"] != book.gaps
                    or anchor["observer_session_id"] != book.session
                    or anchor["connection_epoch"] != book.epoch
                )
                matured = (
                    book.watermark_monotonic_ns
                    >= int(anchor["order"]["metadata"]["arrival_receive_monotonic_ns"])
                    + evaluation_ms * 1_000_000
                )
                if not continuity_changed and not matured:
                    continue
                result = replay_anchor_native(anchor, book, args.binary, protocol, evaluation_ms)
                counts["anchors_evaluated"] += 1
                asset_horizon = f"{anchor['asset']}:{anchor['horizon']}"
                slice_stats[asset_horizon]["evaluated"] += 1
                flags = anchor["policies"]
                for policy, active in flags.items():
                    if not active:
                        continue
                    for arm in result.get("arms") or []:
                        arm_id = str(arm.get("arm") or "")
                        if arm_id in policy_stats[policy]:
                            add_result(policy_stats[policy][arm_id], arm)
                write_record(out, item, result, "EVALUATED")
                del pending[key]

            publish("SAMPLING" if mono < sampling_end else "DRAINING")
            time.sleep(0.05)

        for key, item in list(pending.items()):
            counts["anchors_end_censored"] += 1
            censored = {
                "arms": [
                    {"arm": arm["id"], "state": "RUN_END_BEFORE_MATURITY",
                     "fills": [], "operational_filled_shares": None}
                    for arm in protocol["maker"]["arms"]
                ],
                "replay_engine": "NATIVE_MAKER_PAPER_VIA_RESEARCH_ADAPTER",
            }
            flags = item["anchor"]["policies"]
            for policy, active in flags.items():
                if not active:
                    continue
                for arm in censored["arms"]:
                    add_result(policy_stats[policy][arm["arm"]], arm)
            write_record(out, item, censored, "CENSORED")
            del pending[key]

    publish("COMPLETE")
    print("FINAL_SUMMARY=" + json.dumps(load(args.status), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
