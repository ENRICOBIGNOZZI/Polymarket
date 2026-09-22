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
        params = fee_params(market)
        if params is None:
            self.funnel["fee_unverified"] += 1
            return None
        rate, exponent = params
        fees = fee_per_share(yes_bid, rate, exponent) + fee_per_share(no_bid, rate, exponent)
        if not math.isfinite(fees):
            self.funnel["fee_invalid"] += 1
            return None
        raw_edge = 1.0 - yes_bid - no_bid
        if raw_edge <= 1e-12:
            self.funnel["raw_edge_nonpositive"] += 1
            return None
        after_fee = raw_edge - fees
        if after_fee <= 1e-12:
            self.funnel["fee_killed"] += 1
            return None
        edge = after_fee - self.args.reserve_per_share
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
            "yes_filled": 0.0,
            "no_filled": 0.0,
            "yes_fill_ms": None,
            "no_fill_ms": None,
            "raw_edge_per_share": raw_edge,
            "fees_per_share": fees,
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
            if current["yes_filled"] > 0 or current["no_filled"] > 0:
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
        remaining = max(0.0, c["target_shares"] - c[f"{side}_filled"])
        if remaining <= 0:
            return
        queue_key = f"{side}_queue"
        queue = max(0.0, c[queue_key])
        consumed = min(queue, size)
        c[queue_key] = queue - consumed
        residual = max(0.0, size - consumed)
        fill = min(remaining, residual)
        if fill > 0:
            c[f"{side}_filled"] += fill
            if c[f"{side}_fill_ms"] is None:
                c[f"{side}_fill_ms"] = wall

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
            "fees_per_share": c["fees_per_share"],
            "reserve_per_share": c["reserve_per_share"],
            "locked_edge_per_share": c["locked_edge_per_share"],
            "matched_shares": matched,
            "locked_complete_set_pnl": locked,
            "yes_liquidation_bid": yes_liq,
            "no_liquidation_bid": no_liq,
            "legging_pnl": legging,
            "legging_loss": max(0.0, -legging) if legging is not None else None,
            "total_shadow_pnl": total,
            "queue_ahead_multiplier": self.args.queue_ahead_multiplier,
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
        by_context: dict[str, dict[str, Any]] = {}
        grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in usable:
            grouped[f'{row.get("asset")}:{row.get("horizon")}'].append(row)
        for key, rows in grouped.items():
            counts = Counter(str(r.get("state")) for r in rows)
            values = [float(r["total_shadow_pnl"]) for r in rows if r.get("total_shadow_pnl") is not None]
            by_context[key] = {
                "cycles": len(rows),
                "paired_full": counts.get("BOTH_FULL", 0),
                "one_leg": counts.get("YES_ONLY", 0) + counts.get("NO_ONLY", 0),
                "paired_fill_probability_direct": counts.get("BOTH_FULL", 0) / len(rows) if rows else None,
                "mean_total_shadow_pnl": sum(values) / len(values) if values else None,
            }
        atomic_json(self.args.status, {
            "schema": STATUS_SCHEMA,
            "model_sha": self.args.model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "timestamp_ms": time.time_ns() // 1_000_000,
            "started_ms": self.started_ms,
            "cycles": n,
            "censored_cycles": len(self.rows) - n,
            "active_cycles": len(self.active),
            "states": dict(states),
            "paired_fill_probability_direct": states.get("BOTH_FULL", 0) / n if n else None,
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
            "uses_product_of_marginals": False,
            "joint_probability_semantics": "DIRECT_EMPIRICAL_CYCLE_STATES_NOT_PRODUCT_OF_MARGINALS",
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
    ap.add_argument("--reserve-per-share", type=float, default=0.0005)
    ap.add_argument("--minimum-locked-edge-per-share", type=float, default=0.0005)
    ap.add_argument("--maximum-leg-skew-ms", type=int, default=100)
    ap.add_argument("--ttl-arms-ms", default="250,500,1000")
    ap.add_argument("--quote-refresh-ms", type=int, default=25)
    ap.add_argument("--interval-ms", type=int, default=5)
    args = ap.parse_args()
    args.ttl_arms_ms = sorted({int(x) for x in args.ttl_arms_ms.split(",") if int(x) > 0})
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("invalid model sha")
    if not args.ttl_arms_ms or not (0 < args.minimum_quote_shares <= args.maximum_quote_shares):
        raise SystemExit("invalid size")
    if not (0 < args.depth_fraction <= 1 and args.queue_ahead_multiplier >= 0):
        raise SystemExit("invalid fill model")
    if not (0 <= args.reserve_per_share < 1
            and 0 <= args.minimum_locked_edge_per_share < 1
            and 0 <= args.maximum_leg_skew_ms <= 5000):
        raise SystemExit("invalid economics")
    if not (1 <= args.quote_refresh_ms <= 1000 and 1 <= args.interval_ms <= 1000):
        raise SystemExit("invalid timing")
    Shadow(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
