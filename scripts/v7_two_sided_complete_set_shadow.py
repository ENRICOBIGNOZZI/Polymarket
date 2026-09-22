#!/usr/bin/env python3
"""Zero-authority PAPER shadow for paired YES/NO complete-set maker fills.

Quotes both outcomes at the observed best bid only when the complete-set edge
remains positive after verified fees and a conservative reserve. Each market has
its own independent cycle. Before any fill, a best-bid change cancels/requotes
and resets queue position; after a leg fills, the cycle is held to TTL so legging
risk is measured rather than hidden.

No real orders. No capital authority. No product-of-marginals fill shortcut.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_causal_book import BookTimeline
from v7_pm_repricing_common import atomic_json

SCHEMA = "polymarket_v7_two_sided_complete_set_cycle_v2"
STATUS_SCHEMA = "polymarket_v7_two_sided_complete_set_shadow_status_v2"
TRADE_SCHEMA = "polymarket_v7_maker_fillability_ws_trade_v1"
SELECTION_SCHEMA = "polymarket_v7_multi_crypto_book_selection_v1"


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    if not (math.isfinite(price) and 0 < price < 1
            and math.isfinite(rate) and 0 <= rate <= 1
            and math.isfinite(exponent) and exponent >= 0):
        return math.nan
    return rate * (price * (1.0 - price)) ** exponent if rate > 0 else 0.0


def fee_params(row: dict[str, Any]) -> tuple[float, float] | None:
    fee = row.get("fee_schedule")
    if isinstance(fee, dict):
        try:
            rate = float(fee["rate"])
            exponent = float(fee.get("exponent", 1.0))
        except (KeyError, TypeError, ValueError):
            return None
        if math.isfinite(rate) and 0 <= rate <= 1 and math.isfinite(exponent) and exponent >= 0:
            return rate, exponent
    if row.get("fees_enabled_explicit") is True and row.get("fees_enabled") is False:
        return 0.0, 1.0
    return None


def selection_markets(value: dict[str, Any], model_sha: str, now_ms: int) -> dict[str, dict[str, Any]]:
    if (
        value.get("schema") != SELECTION_SCHEMA
        or value.get("model_sha") != model_sha
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("execution_authority") is not False
        or value.get("selection_only") is not True
    ):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in value.get("markets") or []:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("market_id") or "")
        yes = str(row.get("yes_token") or "")
        no = str(row.get("no_token") or "")
        try:
            start = int(row.get("start_timestamp_ms") or 0)
            end = int(row.get("end_timestamp_ms") or 0)
        except (TypeError, ValueError):
            continue
        if (mid and yes and no and yes != no and start > 0 and end > start
                and start <= now_ms < end):
            out[mid] = row
    return out


class TradeTail:
    def __init__(self, path: Path, model_sha: str) -> None:
        self.path, self.model_sha = path, model_sha
        self.handle = None

    def poll(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _ in range(2):
            if self.handle is None:
                try:
                    self.handle = self.path.open("rb")
                except OSError:
                    return out
            while True:
                offset = self.handle.tell()
                raw = self.handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    self.handle.seek(offset)
                    break
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if (isinstance(row, dict) and row.get("schema") == TRADE_SCHEMA
                        and row.get("model_sha") == self.model_sha
                        and row.get("paper_only") is True
                        and row.get("authenticated_execution") is False
                        and row.get("real_order_submission") is False
                        and row.get("lineage_continuous") is True):
                    out.append(row)
            try:
                old, current = os.fstat(self.handle.fileno()), self.path.stat()
                if (old.st_dev, old.st_ino) == (current.st_dev, current.st_ino):
                    if current.st_size < self.handle.tell():
                        self.handle.seek(0)
                    return out
            except OSError:
                return out
            self.handle.close()
            self.handle = None
        return out


def stable(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()


def semantic_fingerprint(row: dict[str, Any]) -> str:
    payload={
        "market_id":str(row.get("market_id") or ""),
        "event_id":str(row.get("event_id") or ""),
        "yes_token":str(row.get("yes_token") or ""),
        "no_token":str(row.get("no_token") or ""),
        "start_timestamp_ms":int(row.get("start_timestamp_ms") or 0),
        "end_timestamp_ms":int(row.get("end_timestamp_ms") or 0),
        "normalized_rules_hash":str(row.get("normalized_rules_hash") or ""),
        "rule_snapshot_sha256":str(row.get("rule_snapshot_sha256") or ""),
    }
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def wilson_lower(successes: int, n: int, z: float = 1.6448536269514722) -> float | None:
    """One-sided 90% Wilson lower bound for a binomial probability."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1.0 + z*z/n
    center = p + z*z/(2*n)
    radius = z * math.sqrt((p*(1-p) + z*z/(4*n))/n)
    return max(0.0, (center-radius)/denom)


def conservative_mean(values: list[float], z: float = 1.6448536269514722) -> float | None:
    xs=[float(x) for x in values if math.isfinite(float(x))]
    if not xs:
        return None
    mean=sum(xs)/len(xs)
    if len(xs)<2:
        return mean
    variance=sum((x-mean)**2 for x in xs)/(len(xs)-1)
    return mean-z*math.sqrt(variance/len(xs))


class Shadow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.book = BookTimeline(args.book_tape, args.model_sha, retention_ms=30_000)
        self.trades = TradeTail(args.trade_tape, args.model_sha)
        self.active: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        self.started_ms = time.time_ns() // 1_000_000
        self.funnel: Counter[str] = Counter()
        self.cancels: Counter[str] = Counter()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.status.parent.mkdir(parents=True, exist_ok=True)
        self._restore()

    def _restore(self) -> None:
        try:
            for line in self.args.output.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row.get("schema") == SCHEMA and row.get("model_sha") == self.args.model_sha:
                    self.rows.append(row)
        except (OSError, json.JSONDecodeError):
            pass

    def _candidate(self, market: dict[str, Any], origin_ms: int) -> dict[str, Any] | None:
        self.funnel["market_checks"] += 1
        mid = str(market.get("market_id") or "")
        yes, no = str(market.get("yes_token") or ""), str(market.get("no_token") or "")
        if not mid or not yes or not no or yes == no:
            return None
        cuts = [self.book.asof(mid, token, origin_ms) for token in (yes, no)]
        if any(cut is None for cut in cuts):
            self.funnel["book_missing"] += 1
            return None
        try:
            yes_bid, no_bid = float(cuts[0]["best_bid"]), float(cuts[1]["best_bid"])
            yes_q = max(0.0, float(cuts[0].get("bid_depth_l1") or 0.0))
            no_q = max(0.0, float(cuts[1].get("bid_depth_l1") or 0.0))
            yes_ts, no_ts = int(cuts[0]["receive_wall_ms"]), int(cuts[1]["receive_wall_ms"])
        except (TypeError, ValueError, KeyError, OverflowError):
            self.funnel["book_invalid"] += 1
            return None
        if not (0 < yes_bid < 1 and 0 < no_bid < 1 and yes_q > 0 and no_q > 0):
            self.funnel["book_invalid"] += 1
            return None
        if abs(yes_ts - no_ts) > self.args.maximum_leg_skew_ms:
            self.funnel["leg_skew"] += 1
            return None
        # CLOB V2 makers are fee-free. Market fee parameters are useful only
        # for ancillary rebate attribution; they are never subtracted from the
        # complete-set maker entry edge.
        params = fee_params(market)
        rate = exponent = None
        taker_fee_equivalent = 0.0
        rebate_reference = 0.0
        if params is None:
            self.funnel["rebate_fee_schedule_unverified"] += 1
        else:
            rate, exponent = params
            taker_fee_equivalent = (
                fee_per_share(yes_bid, rate, exponent)
                + fee_per_share(no_bid, rate, exponent)
            )
            if not math.isfinite(taker_fee_equivalent):
                taker_fee_equivalent = 0.0
                self.funnel["rebate_fee_schedule_invalid"] += 1
            rebate_reference = self.args.crypto_maker_rebate_fraction * taker_fee_equivalent

        raw_edge = 1.0 - yes_bid - no_bid
        if raw_edge <= 1e-12:
            self.funnel["raw_edge_nonpositive"] += 1
            return None
        edge = raw_edge - self.args.reserve_per_share
        if edge <= self.args.minimum_locked_edge_per_share:
            self.funnel["reserve_killed"] += 1
            return None

        target = min(
            self.args.maximum_quote_shares,
            max(self.args.minimum_quote_shares,
                self.args.depth_fraction * min(yes_q, no_q)),
        )
        if target + 1e-12 < self.args.minimum_quote_shares:
            self.funnel["size_too_small"] += 1
            return None
        cycle_id = stable(self.args.model_sha, mid, origin_ms, yes_bid, no_bid)
        ttl = self.args.ttl_arms_ms[int(cycle_id[:16], 16) % len(self.args.ttl_arms_ms)]
        self.funnel["candidate"] += 1
        return {
            "cycle_id": cycle_id,
            "market_id": mid,
            "semantic_fingerprint": semantic_fingerprint(market),
            "market_end_ms": int(market.get("end_timestamp_ms") or 0),
            "asset": str(market.get("asset") or ""),
            "horizon": str(market.get("horizon") or ""),
            "yes_token": yes,
            "no_token": no,
            "origin_ms": origin_ms,
            "expires_ms": origin_ms + ttl,
            "ttl_ms": ttl,
            "target_shares": target,
            "yes_price": yes_bid,
            "no_price": no_bid,
            "yes_queue": self.args.queue_ahead_multiplier * yes_q,
            "no_queue": self.args.queue_ahead_multiplier * no_q,
            "yes_visible_bid_depth": yes_q,
            "no_visible_bid_depth": no_q,
            "yes_traded_since_refresh": 0.0,
            "no_traded_since_refresh": 0.0,
            "yes_filled": 0.0,
            "no_filled": 0.0,
            "yes_fill_ms": None,
            "no_fill_ms": None,
            "queue_arms": {
                f"q={arm:.3f}|c={relief:.3f}": {
                    "multiplier": float(arm.get("multiplier") or 0.0),
                    "cancel_relief_fraction": float(arm.get("cancel_relief_fraction") or 0.0),
                    "cancel_relief_fraction": relief,
                    "yes_queue": arm * yes_q,
                    "no_queue": arm * no_q,
                    "yes_filled": 0.0,
                    "no_filled": 0.0,
                    "yes_fill_ms": None,
                    "no_fill_ms": None,
                }
                for arm in self.args.queue_ahead_arms
                for relief in self.args.cancel_relief_arms
            },
            "raw_edge_per_share": raw_edge,
            "maker_fee_per_share": 0.0,
            "fees_per_share": 0.0,
            "taker_fee_equivalent_per_share": taker_fee_equivalent,
            "maker_rebate_reference_per_share": rebate_reference,
            "rebate_reference_used_in_entry_gate": False,
            "reserve_per_share": self.args.reserve_per_share,
            "locked_edge_per_share": edge,
            "fee_rate": rate,
            "fee_exponent": exponent,
        }

    def refresh_quotes(self) -> None:
        if self.book.watermark_ms <= 0:
            return
        now_ms = self.book.watermark_ms
        markets = selection_markets(load(self.args.selection), self.args.model_sha, now_ms)
        self.funnel["active_markets_last"] = len(markets)

        for mid in list(self.active):
            if mid not in markets:
                self.finalize(mid, "MARKET_ROLLOVER")

        for mid, market in markets.items():
            candidate = self._candidate(market, now_ms)
            current = self.active.get(mid)
            if current is None:
                if candidate is not None:
                    self.active[mid] = candidate
                continue
            if current.get("semantic_fingerprint") != semantic_fingerprint(market):
                self.cancels["SEMANTIC_RESET"] += 1
                self.finalize(mid, "SEMANTIC_RESET")
                if candidate is not None:
                    self.active[mid] = candidate
                continue
            if candidate is not None:
                for side in ("yes", "no"):
                    previous_depth = max(0.0, float(current.get(f"{side}_visible_bid_depth") or 0.0))
                    new_depth = max(0.0, float(candidate.get(f"{side}_visible_bid_depth") or 0.0))
                    traded = max(0.0, float(current.get(f"{side}_traded_since_refresh") or 0.0))
                    unexplained_contraction = max(0.0, previous_depth - new_depth - traded)
                    if unexplained_contraction > 0.0:
                        self.funnel[f"{side}_unexplained_depth_contraction_events"] += 1
                    for arm in (current.get("queue_arms") or {}).values():
                        relief = max(0.0, min(1.0, float(arm.get("cancel_relief_fraction") or 0.0)))
                        queue_key = f"{side}_queue"
                        arm[queue_key] = max(
                            0.0,
                            float(arm.get(queue_key) or 0.0) - relief * unexplained_contraction,
                        )
                    current[f"{side}_visible_bid_depth"] = new_depth
                    current[f"{side}_traded_since_refresh"] = 0.0
            if any(
                float(arm.get("yes_filled") or 0.0) > 0.0
                or float(arm.get("no_filled") or 0.0) > 0.0
                for arm in (current.get("queue_arms") or {}).values()
            ):
                continue
            if candidate is None:
                self.cancels["EDGE_GONE"] += 1
                self.finalize(mid, "CANCEL_EDGE_GONE")
                continue
            price_changed = (
                abs(candidate["yes_price"] - current["yes_price"]) > 1e-12
                or abs(candidate["no_price"] - current["no_price"]) > 1e-12
            )
            if price_changed:
                self.cancels["REPRICE"] += 1
                self.finalize(mid, "CANCEL_REPRICE")
                self.active[mid] = candidate

    def apply_trade(self, row: dict[str, Any]) -> None:
        mid = str(row.get("market_id") or "")
        c = self.active.get(mid)
        if c is None:
            return
        try:
            wall = int(row.get("receive_wall_ms") or 0)
            price, size = float(row.get("price")), float(row.get("size"))
        except (TypeError, ValueError, OverflowError):
            return
        if not c["origin_ms"] <= wall <= c["expires_ms"] or row.get("aggressor_side") != "SELL":
            return
        token = str(row.get("token_id") or "")
        side = "yes" if token == c["yes_token"] else "no" if token == c["no_token"] else ""
        if not side:
            return
        quote = c[f"{side}_price"]
        if price > quote + 1e-12:
            return
        c[f"{side}_traded_since_refresh"] = (
            float(c.get(f"{side}_traded_since_refresh") or 0.0) + max(0.0, size)
        )

        for arm in (c.get("queue_arms") or {}).values():
            remaining = max(0.0, c["target_shares"] - float(arm.get(f"{side}_filled") or 0.0))
            if remaining <= 0:
                continue
            queue_key = f"{side}_queue"
            queue = max(0.0, float(arm.get(queue_key) or 0.0))
            consumed = min(queue, size)
            arm[queue_key] = queue - consumed
            residual = max(0.0, size - consumed)
            fill = min(remaining, residual)
            if fill > 0:
                arm[f"{side}_filled"] = float(arm.get(f"{side}_filled") or 0.0) + fill
                if arm.get(f"{side}_fill_ms") is None:
                    arm[f"{side}_fill_ms"] = wall

        primary = (c.get("queue_arms") or {}).get(
            f"q={self.args.queue_ahead_multiplier:.3f}|c=0.000")
        if isinstance(primary, dict):
            for name in ("yes_queue","no_queue","yes_filled","no_filled","yes_fill_ms","no_fill_ms"):
                c[name] = primary.get(name)


    def finalize(self, mid: str, reason: str = "TTL") -> None:
        c = self.active.pop(mid, None)
        if c is None:
            return
        mark_ms = min(max(self.book.watermark_ms, c["origin_ms"]), c["expires_ms"])
        cuts = [self.book.asof(c["market_id"], token, mark_ms)
                for token in (c["yes_token"], c["no_token"])]
        if any(cut is None for cut in cuts):
            state, yes_liq, no_liq = "CENSORED", None, None
        else:
            yes_liq, no_liq = float(cuts[0]["best_bid"]), float(cuts[1]["best_bid"])
            yf, nf, target = c["yes_filled"], c["no_filled"], c["target_shares"]
            if yf >= target - 1e-9 and nf >= target - 1e-9:
                state = "BOTH_FULL"
            elif yf > 0 and nf > 0:
                state = "BOTH_PARTIAL"
            elif yf > 0:
                state = "YES_ONLY"
            elif nf > 0:
                state = "NO_ONLY"
            else:
                state = "NO_FILL"
        matched = min(c["yes_filled"], c["no_filled"])
        locked = matched * c["locked_edge_per_share"]
        rebate_reference = matched * c.get("maker_rebate_reference_per_share", 0.0)

        queue_scenarios = []
        for arm in sorted((c.get("queue_arms") or {}).values(), key=lambda x: float(x.get("multiplier") or 0.0)):
            yf = float(arm.get("yes_filled") or 0.0)
            nf = float(arm.get("no_filled") or 0.0)
            target = float(c["target_shares"])
            if yf >= target - 1e-9 and nf >= target - 1e-9:
                arm_state = "BOTH_FULL"
            elif yf > 0 and nf > 0:
                arm_state = "BOTH_PARTIAL"
            elif yf > 0:
                arm_state = "YES_ONLY"
            elif nf > 0:
                arm_state = "NO_ONLY"
            else:
                arm_state = "NO_FILL"
            arm_matched = min(yf, nf)
            arm_locked = arm_matched * c["locked_edge_per_share"]
            arm_legging = None
            arm_total = None
            if yes_liq is not None and no_liq is not None:
                arm_legging = ((yf - arm_matched) * (yes_liq - c["yes_price"])
                               + (nf - arm_matched) * (no_liq - c["no_price"]))
                arm_total = arm_locked + arm_legging
            queue_scenarios.append({
                "multiplier": float(arm.get("multiplier") or 0.0),
                "state": arm_state,
                "yes_filled_shares": yf,
                "no_filled_shares": nf,
                "matched_shares": arm_matched,
                "locked_complete_set_pnl": arm_locked,
                "legging_pnl": arm_legging,
                "total_shadow_pnl": arm_total,
            })
        legging = None
        total = None
        if yes_liq is not None and no_liq is not None:
            legging = ((c["yes_filled"] - matched) * (yes_liq - c["yes_price"])
                       + (c["no_filled"] - matched) * (no_liq - c["no_price"]))
            total = locked + legging
        row = {
            "schema": SCHEMA,
            "model_sha": self.args.model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "strategy": "TWO_SIDED_COMPLETE_SET_MAKER",
            "asset": c["asset"],
            "horizon": c["horizon"],
            "cycle_id": c["cycle_id"],
            "market_id": c["market_id"],
            "semantic_fingerprint": c.get("semantic_fingerprint"),
            "market_end_ms": c.get("market_end_ms"),
            "origin_ms": c["origin_ms"],
            "expires_ms": c["expires_ms"],
            "finalized_ms": mark_ms,
            "finalize_reason": reason,
            "ttl_ms": c["ttl_ms"],
            "target_shares": c["target_shares"],
            "yes_price": c["yes_price"],
            "no_price": c["no_price"],
            "yes_filled_shares": c["yes_filled"],
            "no_filled_shares": c["no_filled"],
            "yes_fill_ms": c["yes_fill_ms"],
            "no_fill_ms": c["no_fill_ms"],
            "state": state,
            "paired_full": state == "BOTH_FULL",
            "raw_edge_per_share": c["raw_edge_per_share"],
            "maker_fee_per_share": 0.0,
            "fees_per_share": 0.0,
            "taker_fee_equivalent_per_share": c.get("taker_fee_equivalent_per_share", 0.0),
            "maker_rebate_reference_pnl": rebate_reference,
            "rebate_reference_used_in_entry_gate": False,
            "reserve_per_share": c["reserve_per_share"],
            "locked_edge_per_share": c["locked_edge_per_share"],
            "matched_shares": matched,
            "locked_complete_set_pnl": locked,
            "yes_liquidation_bid": yes_liq,
            "no_liquidation_bid": no_liq,
            "legging_pnl": legging,
            "legging_loss": max(0.0, -legging) if legging is not None else None,
            "total_shadow_pnl": total,
            "total_shadow_pnl_with_reference_rebate":
                (total + rebate_reference) if total is not None else None,
            "queue_ahead_multiplier": self.args.queue_ahead_multiplier,
            "cancel_relief_arms": self.args.cancel_relief_arms,
            "queue_scenarios": queue_scenarios,
            "joint_probability_semantics": "DIRECT_EMPIRICAL_CYCLE_STATES_NOT_PRODUCT_OF_MARGINALS",
        }
        with self.args.output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        self.rows.append(row)

    def finalize_expired(self) -> None:
        now_ms = self.book.watermark_ms
        for mid, c in list(self.active.items()):
            if now_ms >= c["expires_ms"]:
                self.finalize(mid, "TTL")

    def publish(self) -> None:
        usable = [r for r in self.rows if r.get("state") != "CENSORED"]
        states = Counter(str(r.get("state")) for r in usable)
        n = len(usable)
        pnls = [float(r["total_shadow_pnl"]) for r in usable
                if r.get("total_shadow_pnl") is not None]
        legging = [float(r["legging_loss"]) for r in usable
                   if r.get("legging_loss") is not None]

        grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        by_ttl_rows: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in usable:
            grouped[f'{row.get("asset")}:{row.get("horizon")}'].append(row)
            try:
                by_ttl_rows[int(row.get("ttl_ms") or 0)].append(row)
            except (TypeError, ValueError):
                pass

        by_context: dict[str, dict[str, Any]] = {}
        mature_contexts = 0
        for key, rows in grouped.items():
            counts = Counter(str(r.get("state")) for r in rows)
            values = [float(r["total_shadow_pnl"]) for r in rows
                      if r.get("total_shadow_pnl") is not None]
            paired = counts.get("BOTH_FULL", 0)
            lower = wilson_lower(paired, len(rows))
            if len(rows) >= self.args.maturity_min_cycles_per_context:
                mature_contexts += 1
            by_context[key] = {
                "cycles": len(rows),
                "paired_full": paired,
                "one_leg": counts.get("YES_ONLY", 0) + counts.get("NO_ONLY", 0),
                "paired_fill_probability_direct": paired / len(rows) if rows else None,
                "paired_fill_probability_lower_90": lower,
                "mean_total_shadow_pnl": sum(values) / len(values) if values else None,
            }

        by_ttl: dict[str, dict[str, Any]] = {}
        ttl_mature = True
        for ttl in self.args.ttl_arms_ms:
            rows = by_ttl_rows.get(ttl, [])
            counts = Counter(str(r.get("state")) for r in rows)
            paired = counts.get("BOTH_FULL", 0)
            lower = wilson_lower(paired, len(rows))
            values = [float(r["total_shadow_pnl"]) for r in rows
                      if r.get("total_shadow_pnl") is not None]
            one_leg = counts.get("YES_ONLY", 0) + counts.get("NO_ONLY", 0)
            if len(rows) < self.args.maturity_min_cycles_per_ttl:
                ttl_mature = False
            by_ttl[str(ttl)] = {
                "cycles": len(rows),
                "paired_full": paired,
                "one_leg": one_leg,
                "paired_fill_probability_direct": paired / len(rows) if rows else None,
                "paired_fill_probability_lower_90": lower,
                "one_leg_probability_direct": one_leg / len(rows) if rows else None,
                "mean_total_shadow_pnl": sum(values) / len(values) if values else None,
                "sum_total_shadow_pnl": sum(values) if values else 0.0,
            }

        by_queue_arm: dict[str, dict[str, Any]] = {}
        scenario_groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in usable:
            for scenario in row.get("queue_scenarios") or []:
                if isinstance(scenario, dict):
                    scenario_groups[
                        f'q={float(scenario.get("multiplier") or 0.0):.3f}'
                        f'|c={float(scenario.get("cancel_relief_fraction") or 0.0):.3f}'
                    ].append(scenario)
        for arm, rows in sorted(scenario_groups.items()):
            counts = Counter(str(x.get("state")) for x in rows)
            paired = counts.get("BOTH_FULL", 0)
            values = [float(x["total_shadow_pnl"]) for x in rows
                      if x.get("total_shadow_pnl") is not None]
            by_queue_arm[arm] = {
                "cycles": len(rows),
                "paired_full": paired,
                "paired_fill_probability_direct": paired / len(rows) if rows else None,
                "paired_fill_probability_lower_90": wilson_lower(paired, len(rows)),
                "one_leg": counts.get("YES_ONLY", 0) + counts.get("NO_ONLY", 0),
                "mean_total_shadow_pnl": sum(values) / len(values) if values else None,
            }

        policy_matrix: dict[str, dict[str, Any]] = {}
        for ttl in self.args.ttl_arms_ms:
            for arm in self.args.queue_ahead_arms:
                for relief in self.args.cancel_relief_arms:
                    values=[]
                    cycles=0
                    paired=0
                    one_leg=0
                    for row in usable:
                        if int(row.get("ttl_ms") or 0)!=ttl:
                            continue
                        scenario=next(
                            (x for x in row.get("queue_scenarios") or []
                             if isinstance(x,dict)
                             and abs(float(x.get("multiplier") or 0.0)-arm)<1e-12
                             and abs(float(x.get("cancel_relief_fraction") or 0.0)-relief)<1e-12),
                            None)
                        if scenario is None:
                            continue
                        cycles+=1
                        state=str(scenario.get("state") or "")
                        paired+=state=="BOTH_FULL"
                        one_leg+=state in {"YES_ONLY","NO_ONLY"}
                        if isinstance(scenario.get("total_shadow_pnl"),(int,float)):
                            values.append(float(scenario["total_shadow_pnl"]))
                    lower_pnl=conservative_mean(values)
                    key=f"ttl={ttl}|queue={arm:.3f}|cancel_relief={relief:.3f}"
                    mature=cycles>=self.args.maturity_min_cycles_per_ttl
                    policy_matrix[key]={
                        "ttl_ms":ttl,"queue_ahead_multiplier":arm,
                        "cancel_relief_fraction":relief,"cycles":cycles,
                        "paired_full":paired,"one_leg":one_leg,
                        "paired_fill_probability_direct":paired/cycles if cycles else None,
                        "paired_fill_probability_lower_90":wilson_lower(paired,cycles),
                        "mean_total_shadow_pnl":sum(values)/len(values) if values else None,
                        "conservative_mean_total_shadow_pnl_lower_90":lower_pnl,
                        "mature":mature,
                        "deployment_candidate":bool(mature and lower_pnl is not None and lower_pnl>0),
                    }

        paired_total = states.get("BOTH_FULL", 0)
        paired_lower = wilson_lower(paired_total, n)
        research_mature = (
            n >= self.args.maturity_min_cycles
            and ttl_mature
            and mature_contexts >= self.args.maturity_min_contexts
        )
        atomic_json(self.args.status, {
            "schema": STATUS_SCHEMA,
            "model_sha": self.args.model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "automatic_promotion": False,
            "timestamp_ms": time.time_ns() // 1_000_000,
            "started_ms": self.started_ms,
            "cycles": n,
            "censored_cycles": len(self.rows) - n,
            "active_cycles": len(self.active),
            "states": dict(states),
            "paired_fill_probability_direct": paired_total / n if n else None,
            "paired_fill_probability_lower_90": paired_lower,
            "both_any_probability_direct": (
                states.get("BOTH_FULL", 0) + states.get("BOTH_PARTIAL", 0)
            ) / n if n else None,
            "one_leg_probability_direct": (
                states.get("YES_ONLY", 0) + states.get("NO_ONLY", 0)
            ) / n if n else None,
            "mean_total_shadow_pnl": sum(pnls) / len(pnls) if pnls else None,
            "sum_total_shadow_pnl": sum(pnls) if pnls else 0.0,
            "total_legging_loss": sum(legging) if legging else 0.0,
            "mean_legging_loss": sum(legging) / len(legging) if legging else None,
            "funnel": dict(self.funnel),
            "cancels": dict(self.cancels),
            "by_context": by_context,
            "by_ttl": by_ttl,
            "by_queue_arm": by_queue_arm,
            "policy_matrix": policy_matrix,
            "deployment_candidate_arms": [
                key for key,row in policy_matrix.items()
                if row.get("deployment_candidate") is True
            ],
            "uses_product_of_marginals": False,
            "joint_probability_semantics": "DIRECT_EMPIRICAL_CYCLE_STATES_NOT_PRODUCT_OF_MARGINALS",
            "confidence_semantics": "WILSON_ONE_SIDED_90_LOWER_ON_DIRECT_PAIRED_FULL",
            "research_mature": research_mature,
            "maturity_requirements": {
                "minimum_cycles_total": self.args.maturity_min_cycles,
                "minimum_cycles_per_ttl": self.args.maturity_min_cycles_per_ttl,
                "minimum_cycles_per_context": self.args.maturity_min_cycles_per_context,
                "minimum_mature_contexts": self.args.maturity_min_contexts,
            },
            "ttl_arms_ms": self.args.ttl_arms_ms,
            "minimum_quote_shares": self.args.minimum_quote_shares,
            "maximum_quote_shares": self.args.maximum_quote_shares,
            "depth_fraction": self.args.depth_fraction,
            "reserve_per_share": self.args.reserve_per_share,
            "queue_ahead_multiplier": self.args.queue_ahead_multiplier,
            "active_cycle_ids": sorted(c["cycle_id"] for c in self.active.values()),
            "state": "COLLECTING",
        })

    def run(self) -> None:
        next_status = 0.0
        next_refresh = 0.0
        while True:
            self.book.poll()
            for row in self.trades.poll():
                self.apply_trade(row)
            self.finalize_expired()
            now = time.monotonic()
            if now >= next_refresh:
                self.refresh_quotes()
                next_refresh = now + self.args.quote_refresh_ms / 1000.0
            if now >= next_status:
                self.publish()
                next_status = now + 1.0
            time.sleep(max(0.001, self.args.interval_ms / 1000.0))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--book-tape", type=Path, required=True)
    ap.add_argument("--trade-tape", type=Path, required=True)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--minimum-quote-shares", type=float, default=1.0)
    ap.add_argument("--maximum-quote-shares", type=float, default=5.0)
    ap.add_argument("--depth-fraction", type=float, default=0.25)
    ap.add_argument("--queue-ahead-multiplier", type=float, default=1.25)
    ap.add_argument("--queue-ahead-arms", default="1.0,1.25,1.5,2.0")
    ap.add_argument("--cancel-relief-arms", default="0,0.25,0.5,1.0")
    ap.add_argument("--reserve-per-share", type=float, default=0.0005)
    ap.add_argument("--minimum-locked-edge-per-share", type=float, default=0.0005)
    ap.add_argument("--maximum-leg-skew-ms", type=int, default=100)
    ap.add_argument("--ttl-arms-ms", default="250,500,1000")
    ap.add_argument("--quote-refresh-ms", type=int, default=25)
    ap.add_argument("--interval-ms", type=int, default=5)
    ap.add_argument("--crypto-maker-rebate-fraction", type=float, default=0.20)
    ap.add_argument("--maturity-min-cycles", type=int, default=300)
    ap.add_argument("--maturity-min-cycles-per-ttl", type=int, default=75)
    ap.add_argument("--maturity-min-cycles-per-context", type=int, default=20)
    ap.add_argument("--maturity-min-contexts", type=int, default=2)
    args = ap.parse_args()
    args.ttl_arms_ms = sorted({int(x) for x in args.ttl_arms_ms.split(",") if int(x) > 0})
    args.queue_ahead_arms = sorted({float(x) for x in args.queue_ahead_arms.split(",") if float(x) >= 0})
    args.cancel_relief_arms = sorted({
        float(x) for x in args.cancel_relief_arms.split(",") if 0.0 <= float(x) <= 1.0
    })
    if args.queue_ahead_multiplier not in args.queue_ahead_arms:
        args.queue_ahead_arms.append(args.queue_ahead_multiplier)
        args.queue_ahead_arms.sort()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("invalid model sha")
    if not args.ttl_arms_ms or not (0 < args.minimum_quote_shares <= args.maximum_quote_shares):
        raise SystemExit("invalid size")
    if not (0 < args.depth_fraction <= 1 and args.queue_ahead_multiplier >= 0
            and args.queue_ahead_arms and all(0 <= x <= 10 for x in args.queue_ahead_arms)
            and args.cancel_relief_arms):
        raise SystemExit("invalid fill model")
    if not (0 <= args.reserve_per_share < 1
            and 0 <= args.minimum_locked_edge_per_share < 1
            and 0 <= args.maximum_leg_skew_ms <= 5000):
        raise SystemExit("invalid economics")
    if not (1 <= args.quote_refresh_ms <= 1000 and 1 <= args.interval_ms <= 1000):
        raise SystemExit("invalid timing")
    if not (0.0 <= args.crypto_maker_rebate_fraction <= 1.0):
        raise SystemExit("invalid maker rebate reference fraction")
    if not (args.maturity_min_cycles > 0
            and args.maturity_min_cycles_per_ttl > 0
            and args.maturity_min_cycles_per_context > 0
            and args.maturity_min_contexts > 0):
        raise SystemExit("invalid maturity requirements")
    Shadow(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
