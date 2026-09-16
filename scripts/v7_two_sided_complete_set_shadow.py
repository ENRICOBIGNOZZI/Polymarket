#!/usr/bin/env python3
"""Zero-authority PAPER shadow for paired YES/NO complete-set maker fills."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_causal_book import BookTimeline
from v7_pm_repricing_common import atomic_json

SCHEMA = "polymarket_v7_two_sided_complete_set_cycle_v1"
STATUS_SCHEMA = "polymarket_v7_two_sided_complete_set_shadow_status_v1"
TRADE_SCHEMA = "polymarket_v7_maker_fillability_ws_trade_v1"


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


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
            self.handle.close(); self.handle = None
        return out


def stable(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()


class Shadow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.book = BookTimeline(args.book_tape, args.model_sha, retention_ms=10_000)
        self.trades = TradeTail(args.trade_tape, args.model_sha)
        self.active: dict[str, Any] | None = None
        self.rows: list[dict[str, Any]] = []
        self.started_ms = time.time_ns() // 1_000_000
        args.output.parent.mkdir(parents=True, exist_ok=True)
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
        if self.active is not None or self.book.watermark_ms <= 0:
            return
        fair = load(self.args.fair_status)
        market = fair.get("market") if isinstance(fair.get("market"), dict) else {}
        market_id = str(market.get("market_id") or "")
        yes, no = str(market.get("yes_token") or ""), str(market.get("no_token") or "")
        if not market_id or not yes or not no or yes == no:
            return
        origin_ms = self.book.watermark_ms
        cuts = [self.book.asof(market_id, token, origin_ms) for token in (yes, no)]
        if any(cut is None for cut in cuts):
            return
        try:
            yes_bid, no_bid = float(cuts[0]["best_bid"]), float(cuts[1]["best_bid"])
            yes_q = 1.25 * max(0.0, float(cuts[0].get("bid_depth_l1") or 0.0))
            no_q = 1.25 * max(0.0, float(cuts[1].get("bid_depth_l1") or 0.0))
        except (TypeError, ValueError, OverflowError):
            return
        if not (0 < yes_bid < 1 and 0 < no_bid < 1 and yes_bid + no_bid < 1 - 1e-12):
            return
        cycle_id = stable(self.args.model_sha, market_id, origin_ms, yes_bid, no_bid)
        ttl = self.args.ttl_arms_ms[int(cycle_id[:16], 16) % len(self.args.ttl_arms_ms)]
        self.active = {
            "cycle_id": cycle_id, "market_id": market_id, "yes_token": yes, "no_token": no,
            "origin_ms": origin_ms, "expires_ms": origin_ms + ttl, "ttl_ms": ttl,
            "target_shares": self.args.quote_shares,
            "yes_price": yes_bid, "no_price": no_bid,
            "yes_queue": yes_q, "no_queue": no_q,
            "yes_filled": 0.0, "no_filled": 0.0,
            "yes_fill_ms": None, "no_fill_ms": None,
            "locked_edge_per_share": 1.0 - yes_bid - no_bid,
        }

    def apply_trade(self, row: dict[str, Any]) -> None:
        c = self.active
        if c is None or str(row.get("market_id") or "") != c["market_id"]:
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

    def maybe_finalize(self) -> None:
        c = self.active
        if c is None or self.book.watermark_ms < c["expires_ms"]:
            return
        cuts = [self.book.asof(c["market_id"], token, c["expires_ms"])
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
            "schema": SCHEMA, "model_sha": self.args.model_sha,
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "cycle_id": c["cycle_id"], "market_id": c["market_id"],
            "origin_ms": c["origin_ms"], "expires_ms": c["expires_ms"], "ttl_ms": c["ttl_ms"],
            "target_shares": c["target_shares"], "yes_price": c["yes_price"], "no_price": c["no_price"],
            "yes_filled_shares": c["yes_filled"], "no_filled_shares": c["no_filled"],
            "yes_fill_ms": c["yes_fill_ms"], "no_fill_ms": c["no_fill_ms"],
            "state": state, "paired_full": state == "BOTH_FULL",
            "locked_edge_per_share": c["locked_edge_per_share"],
            "matched_shares": matched, "locked_complete_set_pnl": locked,
            "yes_liquidation_bid": yes_liq, "no_liquidation_bid": no_liq,
            "legging_pnl": legging, "legging_loss": max(0.0, -legging) if legging is not None else None,
            "total_shadow_pnl": total,
            "joint_probability_semantics": "DIRECT_EMPIRICAL_CYCLE_STATES_NOT_PRODUCT_OF_MARGINALS",
        }
        with self.args.output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        self.rows.append(row)
        self.active = None

    def publish(self) -> None:
        usable = [r for r in self.rows if r.get("state") != "CENSORED"]
        states = Counter(str(r.get("state")) for r in usable)
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
            "mean_legging_loss": sum(legging) / len(legging) if legging else None,
            "uses_product_of_marginals": False,
            "joint_probability_semantics": "DIRECT_EMPIRICAL_CYCLE_STATES_NOT_PRODUCT_OF_MARGINALS",
            "ttl_arms_ms": self.args.ttl_arms_ms, "quote_shares": self.args.quote_shares,
            "active_cycle": self.active,
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
    ap.add_argument("--fair-status", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--quote-shares", type=float, default=5.0)
    ap.add_argument("--ttl-arms-ms", default="250,500,1000")
    ap.add_argument("--interval-ms", type=int, default=10)
    args = ap.parse_args()
    args.ttl_arms_ms = sorted({int(x) for x in args.ttl_arms_ms.split(",") if int(x) > 0})
    if len(args.model_sha) != 40 or not args.ttl_arms_ms or not math.isfinite(args.quote_shares) or args.quote_shares <= 0:
        raise SystemExit("invalid arguments")
    Shadow(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
