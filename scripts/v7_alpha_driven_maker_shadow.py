#!/usr/bin/env python3
"""Bounded zero-authority alpha-driven maker shadow over the live causal book.

This script never writes to the canonical ledger or OMS. It samples the current
market, labels each quote opportunity by whether the canonical maker bridge
would emit a MAKE proposal, and replays conservative resting-order fills using
the same public-print/queue-ahead semantics as the native research adapter.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

from v7_causal_book import BookTimeline
from v7_maker_opportunity_bridge import build_maker_opportunities

SCHEMA = "polymarket_v7_alpha_driven_maker_shadow_v1"
STATUS_SCHEMA = "polymarket_v7_alpha_driven_maker_shadow_status_v1"


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()


def make_protocol(ttls_ms: list[int], markouts_ms: list[int]) -> dict[str, Any]:
    arms = []
    for placement in ("JOIN", "IMPROVE1"):
        for ttl in sorted(set(ttls_ms)):
            arms.append({"id": f"{placement}_{ttl}MS", "placement": placement, "lifetime_ms": ttl})
    return {
        "maker": {
            "maximum_feature_age_ms": 2_000,
            "markout_horizons_ms": sorted(set(markouts_ms)),
            "arms": arms,
        }
    }


def alpha_compact(opportunity: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(opportunity, dict):
        return None
    packet = opportunity.get("execution_alpha")
    plan = opportunity.get("execution_plan") if isinstance(opportunity.get("execution_plan"), dict) else {}
    leg = (plan.get("legs") or [{}])[0] if isinstance(plan.get("legs"), list) else {}
    return {
        "replay_key": opportunity.get("deterministic_replay_key"),
        "conservative_ev": opportunity.get("conservative_expected_wealth_change"),
        "fair_value": opportunity.get("fair_value"),
        "execution_alpha": packet if isinstance(packet, dict) else None,
        "limit_price": leg.get("limit_price") if isinstance(leg, dict) else None,
        "target_quantity": leg.get("target_quantity") if isinstance(leg, dict) else None,
        "timeout_ms": plan.get("timeout_ms"),
    }


def make_anchor(row: dict[str, Any], *, market_id: str, token_id: str, model_sha: str,
                quantity: float, opportunity: dict[str, Any] | None) -> dict[str, Any]:
    bid = float(row["best_bid"])
    receive_ms = int(row["receive_wall_ms"])
    sequence = int(row["observer_sequence"])
    record_id = stable_id("alpha-maker-shadow", model_sha, market_id, token_id, sequence)
    envelope = opportunity if isinstance(opportunity, dict) else {
        "exploration": {"probe_loss_cap": quantity * bid},
    }
    order = {
        "event_type": "ORDER_SUBMITTED",
        "record_id": record_id,
        "market_id": market_id,
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
            "opportunity_envelope": envelope,
        },
    }
    return {
        "kind": "MAKER_ANCHOR",
        "market_id": market_id,
        "token_id": token_id,
        "origin_ms": receive_ms,
        "order": order,
        "book_gap_counter": None,
        "observer_session_id": None,
        "connection_epoch": None,
    }



def replay_anchor_python(
    anchor: dict[str, Any],
    book: BookTimeline,
    status: dict[str, Any],
    protocol: dict[str, Any],
    *,
    evaluation_ms: int,
) -> dict[str, Any]:
    """Deterministic pessimistic BUY-maker replay; no execution authority.

    Mirrors the native research adapter's economically material rules:
    1ms submission latency is already encoded in the anchor clock, 100ms cancel
    latency after TTL, strict receive/exchange causality, SELL aggressor only,
    crossing trade price <= resting BUY price, 1.50x visible queue ahead for the
    operational/pessimistic fill, and partial fills.
    """
    order = anchor["order"]
    metadata = order["metadata"]
    start_ms = int(anchor["origin_ms"])
    market = str(anchor["market_id"])
    token = str(anchor["token_id"])
    arrival_receive_ns = int(metadata.get("arrival_receive_monotonic_ns") or 0)
    arrival_exchange_ns = int(metadata.get("arrival_exchange_event_ns") or 0)
    history = list(book.history.get((market, token), ()))
    origin = next(
        (
            row for row in reversed(history)
            if int(row.get("receive_monotonic_ns") or 0) <= arrival_receive_ns
            and int(row.get("receive_wall_ms") or 0) <= start_ms
        ),
        None,
    )

    reason: str | None = None
    now_ms = time.time_ns() // 1_000_000
    try:
        status_ts = int(status.get("timestamp_ms") or 0)
        status_written = int(status.get("book_events_written") or 0)
        status_wall = int(status.get("book_watermark_receive_wall_ms") or 0)
        status_mono = int(status.get("book_watermark_receive_monotonic_ns") or 0)
    except (TypeError, ValueError, OverflowError):
        status_ts = status_written = status_wall = status_mono = 0

    if (
        origin is None
        or anchor.get("book_gap_counter") != book.gaps
        or anchor.get("observer_session_id") != book.session
        or int(anchor.get("connection_epoch") or 0) != book.epoch
        or not history
        or int(history[0].get("receive_wall_ms") or 0) > start_ms
        or book.watermark_ms < start_ms + evaluation_ms
        or book.watermark_monotonic_ns < arrival_receive_ns + evaluation_ms * 1_000_000
        or status.get("evidence_complete") is not True
        or status.get("model_sha") != book.model_sha
        or status.get("observer_session_id") != book.session
        or int(status.get("connection_epoch") or 0) != book.epoch
        or status.get("state") != "running"
        or status.get("paper_only") is not True
        or status.get("authenticated_execution") is not False
        or status.get("real_order_submission") is not False
        or status_written > book.sequence
        or status_wall < start_ms + evaluation_ms
        or status_mono < arrival_receive_ns + evaluation_ms * 1_000_000
        or not 0 <= now_ms - status_ts <= 2_000
    ):
        reason = "BOOK_CONTINUITY_CENSORED"

    if arrival_receive_ns <= 0 or arrival_exchange_ns <= 0:
        reason = "MISSING_NATIVE_ARRIVAL_CLOCK"
    if origin is not None and (
        origin.get("valid") is not True
        or origin.get("lineage_continuous") is not True
        or int(origin.get("receive_monotonic_ns") or 0) <= 0
    ):
        reason = "INVALID_ARRIVAL_BOOK"

    path = [
        row for row in history
        if arrival_receive_ns <= int(row.get("receive_monotonic_ns") or 0)
        <= arrival_receive_ns + evaluation_ms * 1_000_000
    ]
    if origin is not None and any(row.get("tick_size") != origin.get("tick_size") for row in path):
        reason = "TICK_REGIME_CHANGED"
    invalid_books = sum(
        row.get("valid") is not True or row.get("lineage_continuous") is not True
        for row in path
    )

    output: list[dict[str, Any]] = []
    for arm in protocol["maker"]["arms"]:
        arm_reason = reason
        execution_end_ns = (
            arrival_receive_ns
            + (int(arm["lifetime_ms"]) + 100) * 1_000_000
        )
        execution_path = [
            row for row in path
            if int(row.get("receive_monotonic_ns") or 0) <= execution_end_ns
        ]
        if any("public_trade" not in row for row in execution_path):
            arm_reason = "MISSING_TRADE_PAYLOAD_CENSORED"
        if any(
            row.get("public_trade")
            and (row.get("valid") is not True or row.get("lineage_continuous") is not True)
            for row in execution_path
        ):
            arm_reason = "TRADE_LINEAGE_CENSORED"

        row_out: dict[str, Any] = {
            "arm": arm["id"],
            "state": arm_reason or "OBSERVED",
            "operational_filled_shares": None,
            "fills": [],
            "counterfactual": True,
            "intermediate_invalid_book_rows": invalid_books,
            "replay_input_basis": "CONTINUOUS_TRANSPORT_VALID_PRINTS_AND_VALID_ARRIVAL_BOOK",
            "queue_model": "VISIBLE_QUEUE_X_1_50_PESSIMISTIC",
            "cancel_latency_ms": 100,
        }
        if arm_reason or origin is None:
            output.append(row_out)
            continue

        quantity = float(order["intended_size"])
        tick = float(origin["tick_size"])
        bid = float(origin["best_bid"])
        ask = float(origin["best_ask"])
        price = bid + (tick if arm["placement"] == "IMPROVE1" else 0.0)
        if not (
            math.isfinite(quantity) and quantity > 0
            and math.isfinite(tick) and 0 < tick < 1
            and math.isfinite(price) and 0 < price < ask < 1
        ):
            row_out["state"] = "POST_ONLY_OR_INPUT_INELIGIBLE"
            output.append(row_out)
            continue

        visible_ahead = float(origin.get("bid_depth_l1") or 0.0) if arm["placement"] == "JOIN" else 0.0
        if not math.isfinite(visible_ahead) or visible_ahead < 0:
            row_out["state"] = "QUEUE_AHEAD_INVALID"
            output.append(row_out)
            continue

        queue_ahead = math.ceil(visible_ahead * 1.50 * 1_000_000.0) / 1_000_000.0
        remaining = quantity
        cancel_request_ns = arrival_receive_ns + int(arm["lifetime_ms"]) * 1_000_000
        cancel_effective_ns = cancel_request_ns + 100_000_000
        fills: list[dict[str, Any]] = []
        funnel = Counter()

        for book_row in execution_path:
            trade = book_row.get("public_trade")
            if not isinstance(trade, dict):
                continue
            receive_ns = int(book_row.get("receive_monotonic_ns") or 0)
            exchange_ns = int(trade.get("exchange_event_ns") or book_row.get("exchange_event_ns") or 0)
            if receive_ns <= arrival_receive_ns or exchange_ns <= arrival_exchange_ns:
                funnel["causally_pre_arrival"] += 1
                continue
            if receive_ns >= cancel_effective_ns:
                funnel["cancel_effective_before_trade"] += 1
                continue
            if str(trade.get("aggressor_side") or "").upper() != "SELL":
                funnel["wrong_aggressor_side"] += 1
                continue
            try:
                trade_price = float(trade["price"])
                trade_size = float(trade["size"])
            except (KeyError, TypeError, ValueError, OverflowError):
                funnel["invalid_trade"] += 1
                continue
            if not math.isfinite(trade_price) or not math.isfinite(trade_size) or trade_size <= 0:
                funnel["invalid_trade"] += 1
                continue
            if trade_price > price + 1e-12:
                funnel["price_not_crossing"] += 1
                continue

            funnel["eligible_prints"] += 1
            available = trade_size
            if queue_ahead > 0:
                consumed = min(queue_ahead, available)
                queue_ahead -= consumed
                available -= consumed
            if available <= 0 or remaining <= 0:
                funnel["queue_not_depleted"] += 1
                continue

            fill_qty = min(remaining, available)
            if fill_qty <= 0:
                continue
            remaining -= fill_qty
            fill = {
                "receive_monotonic_ns": receive_ns,
                "quantity": fill_qty,
                "price": price,
                "trade_id": int(book_row.get("observer_sequence") or 0),
                "after_cancel_request": receive_ns >= cancel_request_ns,
                "markouts": {},
            }
            for horizon in protocol["maker"]["markout_horizons_ms"]:
                target_ns = receive_ns + int(horizon) * 1_000_000
                cut = next(
                    (
                        candidate for candidate in reversed(history)
                        if int(candidate.get("receive_monotonic_ns") or 0) <= target_ns
                    ),
                    None,
                )
                if (
                    cut is not None
                    and cut.get("valid") is True
                    and cut.get("lineage_continuous") is True
                ):
                    try:
                        cut_bid = float(cut["best_bid"])
                        cut_ask = float(cut["best_ask"])
                        cut_depth = float(cut.get("bid_depth_l1") or 0.0)
                    except (KeyError, TypeError, ValueError, OverflowError):
                        cut = None
                if cut is not None and 0 < cut_bid < cut_ask < 1:
                    fill["markouts"][str(horizon)] = {
                        "mid_minus_fill": 0.5 * (cut_bid + cut_ask) - price,
                        "best_bid_minus_fill": cut_bid - price,
                        "bid_depth_l1": cut_depth,
                        "liquidation_depth_sufficient": cut_depth >= fill_qty,
                    }
                else:
                    fill["markouts"][str(horizon)] = None
            fills.append(fill)
            funnel["fill_events"] += 1
            if remaining <= 1e-12:
                remaining = 0.0
                break

        row_out.update(
            state="OBSERVED",
            research_request={
                "arrival_receive_ns": arrival_receive_ns,
                "arrival_exchange_ns": arrival_exchange_ns,
                "tick": tick,
                "price": price,
                "quantity": quantity,
                "best_ask": ask,
                "visible_queue_ahead": visible_ahead,
                "pessimistic_queue_ahead": math.ceil(visible_ahead * 1.50 * 1_000_000.0) / 1_000_000.0,
                "lifetime_ms": int(arm["lifetime_ms"]),
            },
            simulation_fill_events=len(fills),
            operational_filled_shares=quantity - remaining,
            fills=fills,
            execution_outcome=(
                "FILLED" if remaining == 0
                else "PARTIAL_FILL" if remaining < quantity
                else "QUEUE_NOT_DEPLETED"
            ),
            execution_funnel=dict(funnel),
        )
        output.append(row_out)

    return {
        "arms": output,
        "book_scope": "L1_PLUS_PUBLIC_PRINTS_CAUSAL_REPLAY",
        "comparison_semantics": "PAIRED_RESEARCH_COUNTERFACTUAL_NOT_ADDITIONAL_CANONICAL_FILLS",
        "replay_engine": "PYTHON_PESSIMISTIC_QUEUE_PARITY_CANDIDATE_V1",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-root", type=Path, required=True)
    ap.add_argument("--book-tape", type=Path, required=True)
    ap.add_argument("--book-status", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--duration-seconds", type=float, default=30.0)
    ap.add_argument("--sample-ms", type=int, default=1000)
    ap.add_argument("--quantity-shares", type=float, default=5.0)
    ap.add_argument("--ttl-arms-ms", default="250,500,1000")
    ap.add_argument("--markout-horizons-ms", default="250,500,1000,2000,5000")
    args = ap.parse_args()

    if len(args.model_sha) != 40 or any(c not in "0123456789abcdef" for c in args.model_sha):
        raise SystemExit("invalid model sha")
    if args.duration_seconds <= 0 or args.sample_ms < 100 or args.quantity_shares <= 0:
        raise SystemExit("invalid bounded-run arguments")

    ttls = [int(x) for x in args.ttl_arms_ms.split(",") if x.strip()]
    markouts = [int(x) for x in args.markout_horizons_ms.split(",") if x.strip()]
    if not ttls or not markouts or min(ttls + markouts) <= 0:
        raise SystemExit("invalid horizon grid")

    protocol = make_protocol(ttls, markouts)
    evaluation_ms = max(ttls) + max(markouts) + 250
    book = BookTimeline(args.book_tape, args.model_sha, retention_ms=evaluation_ms + 5_000)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.status.parent.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    pending: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    by_arm: dict[str, Counter[str]] = defaultdict(Counter)
    bridge_states: Counter[str] = Counter()
    markout_dollars: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    sampling_end = time.monotonic() + args.duration_seconds
    drain_end = sampling_end + evaluation_ms / 1000.0 + 3.0
    next_sample = 0.0

    def publish(state: str) -> None:
        payload = {
            "schema": STATUS_SCHEMA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "excluded_from_portfolio_equity": True,
            "model_sha": args.model_sha,
            "state": state,
            "counts": dict(counts),
            "bridge_states": dict(bridge_states),
            "by_arm": {k: dict(v) for k, v in by_arm.items()},
            "markout_dollars": {k: dict(v) for k, v in markout_dollars.items()},
            "pending": len(pending),
            "timestamp_ns": time.time_ns(),
        }
        args.status.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    with args.output.open("a", encoding="utf-8") as out:
        while time.monotonic() < drain_end:
            book.poll()
            now_mono = time.monotonic()
            status = load(args.book_status)

            if now_mono < sampling_end and now_mono >= next_sample:
                next_sample = now_mono + args.sample_ms / 1000.0
                opportunities, bridge = build_maker_opportunities(
                    args.run_root, now_ns=time.time_ns(), repository_root=Path.cwd()
                )
                bridge_states[str(bridge.get("state") or "UNKNOWN")] += 1
                opp_by_token: dict[str, dict[str, Any]] = {}
                for opp in opportunities:
                    plan = opp.get("execution_plan") if isinstance(opp.get("execution_plan"), dict) else {}
                    legs = plan.get("legs") if isinstance(plan.get("legs"), list) else []
                    if legs and isinstance(legs[0], dict):
                        opp_by_token[str(legs[0].get("token_id") or "")] = opp

                fair = load(args.run_root / "external_fair" / "status.json")
                market = fair.get("market") if isinstance(fair.get("market"), dict) else {}
                market_id = str(market.get("market_id") or "")
                tokens = [str(market.get("yes_token") or ""), str(market.get("no_token") or "")]
                now_ms = time.time_ns() // 1_000_000
                if not market_id or any(not token for token in tokens):
                    counts["samples_without_active_market"] += 1
                else:
                    for token in tokens:
                        row = book.asof(market_id, token, now_ms)
                        if row is None:
                            counts["anchors_missing_book"] += 1
                            continue
                        if now_ms - int(row["receive_wall_ms"]) > 1_000:
                            counts["anchors_stale_book"] += 1
                            continue
                        if not isinstance(row.get("placement_features"), dict):
                            counts["anchors_missing_placement_features"] += 1
                            continue
                        key = stable_id(market_id, token, row["observer_sequence"])
                        if key in seen:
                            continue
                        seen.add(key)
                        opp = opp_by_token.get(token)
                        anchor = make_anchor(
                            row, market_id=market_id, token_id=token, model_sha=args.model_sha,
                            quantity=args.quantity_shares, opportunity=opp,
                        )
                        anchor["book_gap_counter"] = book.gaps
                        anchor["observer_session_id"] = book.session
                        anchor["connection_epoch"] = book.epoch
                        pending[key] = {
                            "anchor": anchor,
                            "alpha_active": opp is not None,
                            "alpha": alpha_compact(opp),
                            "bridge": bridge,
                        }
                        counts["anchors_total"] += 1
                        counts["anchors_alpha_active" if opp is not None else "anchors_alpha_inactive"] += 1

            for key, item in list(pending.items()):
                anchor = item["anchor"]
                if book.watermark_ms < int(anchor["origin_ms"]) + evaluation_ms:
                    continue
                try:
                    result = replay_anchor_python(
                        anchor, book, status, protocol,
                        evaluation_ms=evaluation_ms,
                    )
                except Exception as exc:
                    counts["replay_exception"] += 1
                    result = {"arms": [], "error": f"{type(exc).__name__}:{exc}"}
                cohort = "ALPHA_ACTIVE" if item["alpha_active"] else "ALPHA_INACTIVE"
                counts["anchors_evaluated"] += 1
                record = {
                    "schema": SCHEMA,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
                    "excluded_from_portfolio_equity": True,
                    "model_sha": args.model_sha,
                    "cohort": cohort,
                    "alpha": item["alpha"],
                    "bridge_state": item["bridge"].get("state"),
                    "anchor": anchor,
                    "result": result,
                    "timestamp_ns": time.time_ns(),
                }
                for arm in result.get("arms") or []:
                    arm_id = str(arm.get("arm") or "UNKNOWN")
                    by_arm[arm_id]["evaluated"] += 1
                    if arm.get("state") != "OBSERVED":
                        by_arm[arm_id]["censored_or_ineligible"] += 1
                        continue
                    by_arm[arm_id]["observed"] += 1
                    fills = arm.get("fills") if isinstance(arm.get("fills"), list) else []
                    filled_shares = sum(float(f.get("quantity") or 0.0) for f in fills if isinstance(f, dict))
                    if fills:
                        by_arm[arm_id]["filled_anchors"] += 1
                        by_arm[arm_id]["fill_events"] += len(fills)
                        by_arm[arm_id]["filled_millishares"] += int(round(1000 * filled_shares))
                    for fill in fills:
                        if not isinstance(fill, dict):
                            continue
                        quantity = float(fill.get("quantity") or 0.0)
                        marks = fill.get("markouts") if isinstance(fill.get("markouts"), dict) else {}
                        for horizon, value in marks.items():
                            if isinstance(value, dict):
                                mark = value.get("mid_minus_fill")
                                if isinstance(mark, (int, float)) and math.isfinite(float(mark)):
                                    markout_dollars[f"{cohort}:{arm_id}"][str(horizon)] += float(mark) * quantity
                out.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                out.flush()
                del pending[key]
            publish("SAMPLING" if now_mono < sampling_end else "DRAINING")
            time.sleep(0.05)

    publish("COMPLETE")
    final = load(args.status)
    print("FINAL_SUMMARY=" + json.dumps(final, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
