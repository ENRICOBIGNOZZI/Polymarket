#!/usr/bin/env python3
"""Zero-authority PAPER shadow for paired YES/NO complete-set maker fills.

Runs concurrently across the full verified selection. Quotes are simulated at
current best bids, queue-ahead is observed from the causal book, and paired
fill probability is measured directly from joint cycle states. Missing fees or
stale books fail closed. No real order submission.
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


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def stable(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    if not (math.isfinite(price) and 0 < price < 1 and 0 <= rate <= 1 and exponent >= 0):
        return math.nan
    return rate * (price * (1.0 - price)) ** exponent if rate else 0.0


def selection_markets(value: dict[str, Any], model_sha: str) -> dict[str, dict[str, Any]]:
    if (
        value.get("schema") != "polymarket_v7_multi_crypto_book_selection_v1"
        or value.get("model_sha") != model_sha
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("execution_authority") is not False
    ):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in value.get("markets") or []:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("market_id") or "")
        yes = str(row.get("yes_token") or "")
        no = str(row.get("no_token") or "")
        if mid and yes and no and yes != no:
            out[mid] = row
    return out


def fee_params(market: dict[str, Any]) -> tuple[float, float] | None:
    fee = market.get("fee_schedule")
    if isinstance(fee, dict):
        try:
            rate, exponent = float(fee["rate"]), float(fee.get("exponent", 1.0))
        except (KeyError, TypeError, ValueError):
            return None
        if math.isfinite(rate) and 0 <= rate <= 1 and math.isfinite(exponent) and exponent >= 0:
            return rate, exponent
    if market.get("fees_enabled_explicit") is True and market.get("fees_enabled") is False:
        return 0.0, 1.0
    return None


class TradeTail:
    def __init__(self, path: Path, model_sha: str) -> None:
        self.path, self.model_sha, self.handle = path, model_sha, None

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
            self.handle.close(); self.handle = None
        return out


class Shadow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.book = BookTimeline(args.book_tape, args.model_sha, retention_ms=15_000)
        self.trades = TradeTail(args.trade_tape, args.model_sha)
        self.active: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        self.started_ms = time.time_ns() // 1_000_000
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

    def maybe_start(self) -> None:
        if self.book.watermark_ms <= 0:
            return
        now = self.book.watermark_ms
        for market_id, market in selection_markets(load(self.args.selection), self.args.model_sha).items():
            if market_id in self.active:
                continue
            try:
                start_ms = int(market.get("start_timestamp_ms") or 0)
                end_ms = int(market.get("end_timestamp_ms") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if not (start_ms <= now < end_ms):
                continue
            yes, no = str(market.get("yes_token") or ""), str(market.get("no_token") or "")
            cuts = [self.book.asof(market_id, token, now) for token in (yes, no)]
            if any(cut is None for cut in cuts):
                continue
            params = fee_params(market)
            if params is None:
                continue
            try:
                yes_bid, no_bid = float(cuts[0]["best_bid"]), float(cuts[1]["best_bid"])
                yes_depth = max(0.0, float(cuts[0].get("bid_depth_l1") or 0.0))
                no_depth = max(0.0, float(cuts[1].get("bid_depth_l1") or 0.0))
                skew = abs(int(cuts[0]["receive_wall_ms"]) - int(cuts[1]["receive_wall_ms"]))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if skew > self.args.maximum_leg_skew_ms or not (0 < yes_bid < 1 and 0 < no_bid < 1):
                continue
            rate, exponent = params
            yes_fee = fee_per_share(yes_bid, rate, exponent)
            no_fee = fee_per_share(no_bid, rate, exponent)
            if not math.isfinite(yes_fee) or not math.isfinite(no_fee):
                continue
            edge = 1.0 - yes_bid - no_bid - yes_fee - no_fee - self.args.reserve_per_share
            if edge <= self.args.minimum_locked_edge_per_share:
                continue
            reference_depth = min(yes_depth, no_depth)
            target = min(
                self.args.max_quote_shares,
                max(self.args.min_quote_shares, reference_depth * self.args.quote_depth_fraction),
            )
            if not math.isfinite(target) or target <= 0:
                continue
            cycle_id = stable(self.args.model_sha, market_id, now, yes_bid, no_bid)
            ttl = self.args.ttl_arms_ms[int(cycle_id[:16], 16) % len(self.args.ttl_arms_ms)]
            self.active[market_id] = {
                "cycle_id": cycle_id, "market_id": market_id,
                "asset": str(market.get("asset") or ""), "horizon": str(market.get("horizon") or ""),
                "yes_token": yes, "no_token": no, "origin_ms": now,
                "expires_ms": now + ttl, "ttl_ms": ttl, "target_shares": target,
                "yes_price": yes_bid, "no_price": no_bid,
                "yes_fee": yes_fee, "no_fee": no_fee,
                "yes_entry_cost": yes_bid + yes_fee + self.args.reserve_per_share / 2.0,
                "no_entry_cost": no_bid + no_fee + self.args.reserve_per_share / 2.0,
                "fee_rate": rate, "fee_exponent": exponent,
                "yes_queue": self.args.queue_ahead_multiplier * yes_depth,
                "no_queue": self.args.queue_ahead_multiplier * no_depth,
                "yes_filled": 0.0, "no_filled": 0.0,
                "yes_fill_ms": None, "no_fill_ms": None,
                "locked_edge_per_share": edge,
            }

    def apply_trade(self, row: dict[str, Any]) -> None:
        market_id = str(row.get("market_id") or "")
        c = self.active.get(market_id)
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
        if not side or price > c[f"{side}_price"] + 1e-12:
            return
        remaining = max(0.0, c["target_shares"] - c[f"{side}_filled"])
        if remaining <= 0:
            return
        queue_key = f"{side}_queue"
        queue = max(0.0, c[queue_key])
        consumed = min(queue, size)
        c[queue_key] = queue - consumed
        fill = min(remaining, max(0.0, size - consumed))
        if fill > 0:
            c[f"{side}_filled"] += fill
            if c[f"{side}_fill_ms"] is None:
                c[f"{side}_fill_ms"] = wall

    def maybe_finalize(self) -> None:
        for market_id in list(self.active):
            c = self.active[market_id]
            if self.book.watermark_ms < c["expires_ms"]:
                continue
            cuts = [self.book.asof(market_id, token, c["expires_ms"])
                    for token in (c["yes_token"], c["no_token"])]
            yes_liq = no_liq = None
            yes_liq_fee = no_liq_fee = 0.0
            if all(cut is not None for cut in cuts):
                try:
                    yes_liq, no_liq = float(cuts[0]["best_bid"]), float(cuts[1]["best_bid"])
                    yes_liq_fee = fee_per_share(yes_liq, c["fee_rate"], c["fee_exponent"])
                    no_liq_fee = fee_per_share(no_liq, c["fee_rate"], c["fee_exponent"])
                except (TypeError, ValueError, KeyError, OverflowError):
                    yes_liq = no_liq = None
            yf, nf, target = c["yes_filled"], c["no_filled"], c["target_shares"]
            if yes_liq is None or no_liq is None or not all(map(math.isfinite, (yes_liq_fee, no_liq_fee))):
                state = "CENSORED"
            elif yf >= target - 1e-9 and nf >= target - 1e-9:
                state = "BOTH_FULL"
            elif yf > 0 and nf > 0:
                state = "BOTH_PARTIAL"
            elif yf > 0:
                state = "YES_ONLY"
            elif nf > 0:
                state = "NO_ONLY"
            else:
                state = "NO_FILL"
            matched = min(yf, nf)
            locked = matched * c["locked_edge_per_share"]
            legging = total = None
            if yes_liq is not None and no_liq is not None:
                legging = (
                    (yf - matched) * ((yes_liq - yes_liq_fee) - c["yes_entry_cost"])
                    + (nf - matched) * ((no_liq - no_liq_fee) - c["no_entry_cost"])
                )
                total = locked + legging
            row = {
                "schema": SCHEMA, "model_sha": self.args.model_sha,
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "real_capital_at_risk": False,
                "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
                "asset": c["asset"], "horizon": c["horizon"],
                "cycle_id": c["cycle_id"], "market_id": market_id,
                "origin_ms": c["origin_ms"], "expires_ms": c["expires_ms"], "ttl_ms": c["ttl_ms"],
                "target_shares": target, "yes_price": c["yes_price"], "no_price": c["no_price"],
                "yes_entry_fee_per_share": c["yes_fee"], "no_entry_fee_per_share": c["no_fee"],
                "reserve_per_share": self.args.reserve_per_share,
                "yes_filled_shares": yf, "no_filled_shares": nf,
                "yes_fill_ms": c["yes_fill_ms"], "no_fill_ms": c["no_fill_ms"],
                "state": state, "paired_full": state == "BOTH_FULL",
                "locked_edge_per_share": c["locked_edge_per_share"],
                "matched_shares": matched, "locked_complete_set_pnl": locked,
                "yes_liquidation_bid": yes_liq, "no_liquidation_bid": no_liq,
                "legging_pnl": legging,
                "legging_loss": max(0.0, -legging) if legging is not None else None,
                "total_shadow_pnl": total,
                "joint_probability_semantics": "DIRECT_EMPIRICAL_CYCLE_STATES_NOT_PRODUCT_OF_MARGINALS",
            }
            with self.args.output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            self.rows.append(row)
            self.active.pop(market_id, None)

    def publish(self) -> None:
        usable = [r for r in self.rows if r.get("state") != "CENSORED"]
        states = Counter(str(r.get("state")) for r in usable)
        by_context: dict[str, Counter[str]] = defaultdict(Counter)
        for row in usable:
            by_context[f"{row.get('asset')}:{row.get('horizon')}"][str(row.get("state"))] += 1
        n = len(usable)
        pnls = [float(r["total_shadow_pnl"]) for r in usable if r.get("total_shadow_pnl") is not None]
        legging = [float(r["legging_loss"]) for r in usable if r.get("legging_loss") is not None]
        atomic_json(self.args.status, {
            "schema": STATUS_SCHEMA, "model_sha": self.args.model_sha,
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "timestamp_ms": time.time_ns() // 1_000_000, "started_ms": self.started_ms,
            "cycles": n, "censored_cycles": len(self.rows) - n, "states": dict(states),
            "paired_fill_probability_direct": states.get("BOTH_FULL", 0) / n if n else None,
            "both_any_probability_direct": (states.get("BOTH_FULL", 0) + states.get("BOTH_PARTIAL", 0)) / n if n else None,
            "one_leg_probability_direct": (states.get("YES_ONLY", 0) + states.get("NO_ONLY", 0)) / n if n else None,
            "mean_total_shadow_pnl": sum(pnls) / len(pnls) if pnls else None,
            "total_shadow_pnl": sum(pnls),
            "mean_legging_loss": sum(legging) / len(legging) if legging else None,
            "total_legging_loss": sum(legging),
            "uses_product_of_marginals": False,
            "joint_probability_semantics": "DIRECT_EMPIRICAL_CYCLE_STATES_NOT_PRODUCT_OF_MARGINALS",
            "ttl_arms_ms": self.args.ttl_arms_ms,
            "quote_depth_fraction": self.args.quote_depth_fraction,
            "min_quote_shares": self.args.min_quote_shares,
            "max_quote_shares": self.args.max_quote_shares,
            "active_cycles": len(self.active),
            "active_markets": sorted(self.active),
            "by_context": {k: dict(v) for k, v in sorted(by_context.items())},
            "state": "COLLECTING",
        })

    def run(self) -> None:
        next_status = 0.0
        while True:
            self.book.poll()
            for row in self.trades.poll():
                self.apply_trade(row)
            self.maybe_finalize()
            self.maybe_start()
            now = time.monotonic()
            if now >= next_status:
                self.publish(); next_status = now + 1.0
            time.sleep(max(0.001, self.args.interval_ms / 1000.0))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--book-tape", type=Path, required=True)
    ap.add_argument("--trade-tape", type=Path, required=True)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--ttl-arms-ms", default="250,500,1000")
    ap.add_argument("--quote-depth-fraction", type=float, default=0.25)
    ap.add_argument("--min-quote-shares", type=float, default=1.0)
    ap.add_argument("--max-quote-shares", type=float, default=20.0)
    ap.add_argument("--queue-ahead-multiplier", type=float, default=1.25)
    ap.add_argument("--reserve-per-share", type=float, default=0.0005)
    ap.add_argument("--minimum-locked-edge-per-share", type=float, default=0.0005)
    ap.add_argument("--maximum-leg-skew-ms", type=int, default=100)
    ap.add_argument("--interval-ms", type=int, default=10)
    args = ap.parse_args()
    args.ttl_arms_ms = sorted({int(x) for x in args.ttl_arms_ms.split(",") if int(x) > 0})
    if len(args.model_sha) != 40 or not args.ttl_arms_ms:
        raise SystemExit("invalid arguments")
    if not (0 < args.quote_depth_fraction <= 1 and 0 < args.min_quote_shares <= args.max_quote_shares):
        raise SystemExit("invalid quote sizing")
    if not (1 <= args.queue_ahead_multiplier <= 5 and 0 <= args.reserve_per_share < 1
            and 0 <= args.minimum_locked_edge_per_share < 1
            and 0 <= args.maximum_leg_skew_ms <= 5000):
        raise SystemExit("invalid economics")
    Shadow(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
