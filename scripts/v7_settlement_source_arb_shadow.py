#!/usr/bin/env python3
"""Zero-authority PAPER shadow for exact settlement-source arbitrage.

No prediction. Once the verified settlement source fixes the outcome, evaluate
both deterministic paths:
  BUY_WINNER: buy the winning token below redemption value.
  SELL_LOSER: from a prefunded complete set, sell the losing token and redeem
              the winner.

Fees come from each market's verified selection metadata. Missing fee metadata
fails closed. No real order submission or automatic promotion.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import hashlib
import math
import time
from pathlib import Path
from typing import Any

from v7_causal_book import BookTimeline

SCHEMA = "polymarket_v7_settlement_source_arb_cycle_v2"
STATUS_SCHEMA = "polymarket_v7_settlement_source_arb_status_v2"


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


def market_rows(selection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if (
        selection.get("schema") != "polymarket_v7_multi_crypto_book_selection_v1"
        or selection.get("paper_only") is not True
        or selection.get("authenticated_execution") is not False
        or selection.get("real_order_submission") is not False
        or selection.get("execution_authority") is not False
    ):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in selection.get("markets") or []:
        if not isinstance(row, dict) or row.get("horizon") not in {"M5", "M15"}:
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
            rate = float(fee["rate"])
            exponent = float(fee.get("exponent", 1.0))
        except (KeyError, TypeError, ValueError):
            return None
        if math.isfinite(rate) and 0 <= rate <= 1 and math.isfinite(exponent) and exponent >= 0:
            return rate, exponent
    if market.get("fees_enabled_explicit") is True and market.get("fees_enabled") is False:
        return 0.0, 1.0
    return None


class Shadow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.book = BookTimeline(args.book_tape, args.model_sha, retention_ms=60_000)
        self.seen: set[str] = set()
        self.pending: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        self.market_cache: dict[str, dict[str, Any]] = {}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.status.parent.mkdir(parents=True, exist_ok=True)
        self._restore()

    def _restore(self) -> None:
        try:
            for line in self.args.output.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row.get("schema") == SCHEMA and row.get("model_sha") == self.args.model_sha:
                    self.rows.append(row)
                    self.seen.add(str(row.get("cycle_key") or ""))
        except (OSError, json.JSONDecodeError):
            pass

    def refresh_market_cache(self) -> None:
        for market_id, row in market_rows(load(self.args.selection)).items():
            self.market_cache[market_id] = row

    def ingest_outcomes(self) -> None:
        self.refresh_market_cache()
        oracle = load(self.args.oracle_status)
        if (
            oracle.get("schema") != "polymarket_v7_multi_crypto_oracle_hub_v2"
            or oracle.get("model_sha") != self.args.model_sha
            or oracle.get("paper_only") is not True
            or oracle.get("authenticated_execution") is not False
            or oracle.get("real_order_submission") is not False
            or oracle.get("execution_authority") is not False
        ):
            return
        outcomes = oracle.get("settlement_outcomes")
        if not isinstance(outcomes, dict):
            return
        for market_id, outcome in outcomes.items():
            market_id = str(market_id)
            market = self.market_cache.get(market_id)
            if market is None or not isinstance(outcome, dict) or outcome.get("valid") is not True:
                continue
            winner = str(outcome.get("winning_outcome") or "")
            yes = str(market.get("yes_token") or "")
            no = str(market.get("no_token") or "")
            winner_token = yes if winner == "YES" else no if winner == "NO" else ""
            loser_token = no if winner == "YES" else yes if winner == "NO" else ""
            determined_ns = int(outcome.get("determined_wall_ns") or 0)
            if not winner_token or not loser_token or determined_ns <= 0:
                continue
            determined_ms = determined_ns // 1_000_000
            cycle_key = f"{market_id}:{determined_ms}"
            if cycle_key in self.seen or cycle_key in self.pending:
                continue
            self.pending[cycle_key] = {
                "cycle_key": cycle_key,
                "market_id": market_id,
                "market": market,
                "semantic_fingerprint": semantic_fingerprint(market),
                "outcome": outcome,
                "winner": winner,
                "winner_token": winner_token,
                "loser_token": loser_token,
                "determined_ms": determined_ms,
                "arrival_ms": determined_ms + self.args.paper_arrival_delay_ms,
            }

    def _base_row(self, item: dict[str, Any], kind: str) -> dict[str, Any]:
        market = item["market"]
        return {
            "schema": SCHEMA,
            "model_sha": self.args.model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "strategy": "SETTLEMENT_SOURCE_ARB",
            "kind": kind,
            "cycle_key": item["cycle_key"],
            "asset": str(market.get("asset") or ""),
            "horizon": str(market.get("horizon") or ""),
            "market_id": item["market_id"],
            "market_end_ms": int(market.get("end_timestamp_ms") or 0),
            "semantic_fingerprint": item.get("semantic_fingerprint"),
            "winning_outcome": item["winner"],
            "outcome_determined_wall_ms": item["determined_ms"],
            "paper_arrival_wall_ms": item["arrival_ms"],
            "paper_arrival_delay_ms": self.args.paper_arrival_delay_ms,
            "reference_price_decimal": item["outcome"].get("reference_price_decimal"),
            "final_price_decimal": item["outcome"].get("final_price_decimal"),
            "redemption_reserve_per_share": self.args.redemption_reserve_per_share,
            "minimum_locked_edge_per_share": self.args.minimum_locked_edge_per_share,
            "state": "CENSORED_BOOK_UNAVAILABLE",
            "requested_shares": self.args.target_shares,
            "filled_shares": 0.0,
            "locked_edge_per_share": None,
            "locked_pnl": None,
        }

    def evaluate_pending(self) -> None:
        for cycle_key in list(self.pending):
            item = self.pending[cycle_key]
            arrival_ms = int(item["arrival_ms"])
            if self.book.watermark_ms < arrival_ms:
                continue
            market = item["market"]
            current = self.market_cache.get(item["market_id"])
            if current is None or semantic_fingerprint(current) != item.get("semantic_fingerprint"):
                rows=[]
                for kind in ("BUY_WINNER","SELL_LOSER"):
                    row=self._base_row(item,kind)
                    row["state"]="SEMANTIC_RESET"
                    rows.append(row)
                with self.args.output.open("a",encoding="utf-8") as handle:
                    for row in rows:
                        handle.write(json.dumps(row,sort_keys=True)+"\n")
                        self.rows.append(row)
                self.seen.add(cycle_key)
                self.pending.pop(cycle_key,None)
                continue
            params = fee_params(market)
            rows: list[dict[str, Any]] = []
            winner_cut = self.book.asof(item["market_id"], item["winner_token"], arrival_ms)
            loser_cut = self.book.asof(item["market_id"], item["loser_token"], arrival_ms)

            if params is None:
                for kind in ("BUY_WINNER", "SELL_LOSER"):
                    row = self._base_row(item, kind)
                    row["state"] = "CENSORED_FEE_UNVERIFIED"
                    rows.append(row)
            else:
                rate, exponent = params

                buy = self._base_row(item, "BUY_WINNER")
                buy["winning_token"] = item["winner_token"]
                if winner_cut is not None:
                    try:
                        ask = float(winner_cut["best_ask"])
                        depth = float(winner_cut.get("ask_depth_l1") or 0.0)
                        receive_ms = int(winner_cut["receive_wall_ms"])
                    except (KeyError, TypeError, ValueError, OverflowError):
                        ask = math.nan; depth = 0.0; receive_ms = 0
                    age = arrival_ms - receive_ms
                    fee = fee_per_share(ask, rate, exponent)
                    gross = 1.0 - ask - fee if math.isfinite(fee) else math.nan
                    edge = gross - self.args.redemption_reserve_per_share if math.isfinite(gross) else math.nan
                    fill = min(self.args.target_shares, max(0.0, depth))
                    buy.update({
                        "book_receive_wall_ms": receive_ms,
                        "book_age_at_arrival_ms": age,
                        "entry_ask": ask if math.isfinite(ask) else None,
                        "entry_fee_per_share": fee if math.isfinite(fee) else None,
                        "gross_edge_per_share": gross if math.isfinite(gross) else None,
                        "locked_edge_per_share": edge if math.isfinite(edge) else None,
                        "filled_shares": fill if 0 <= age <= self.args.maximum_book_age_ms else 0.0,
                    })
                    if not math.isfinite(ask) or not 0 < ask < 1 or not math.isfinite(fee):
                        buy["state"] = "CENSORED_INVALID_BOOK"
                    elif age < 0 or age > self.args.maximum_book_age_ms:
                        buy["state"] = "CENSORED_STALE_BOOK"
                    elif fill + 1e-12 < self.args.minimum_fill_shares:
                        buy["state"] = "NO_TRADE_DEPTH"
                    elif edge <= self.args.minimum_locked_edge_per_share:
                        buy["state"] = "NO_TRADE_EDGE"
                    else:
                        buy["state"] = "PAPER_LOCKED_ARBITRAGE"
                        buy["locked_pnl"] = fill * edge
                rows.append(buy)

                sell = self._base_row(item, "SELL_LOSER")
                sell["losing_token"] = item["loser_token"]
                sell["requires_prefunded_complete_set"] = True
                if loser_cut is not None:
                    try:
                        bid = float(loser_cut["best_bid"])
                        depth = float(loser_cut.get("bid_depth_l1") or 0.0)
                        receive_ms = int(loser_cut["receive_wall_ms"])
                    except (KeyError, TypeError, ValueError, OverflowError):
                        bid = math.nan; depth = 0.0; receive_ms = 0
                    age = arrival_ms - receive_ms
                    fee = fee_per_share(bid, rate, exponent)
                    gross = bid - fee if math.isfinite(fee) else math.nan
                    edge = gross - self.args.redemption_reserve_per_share if math.isfinite(gross) else math.nan
                    fill = min(self.args.target_shares, max(0.0, depth))
                    sell.update({
                        "book_receive_wall_ms": receive_ms,
                        "book_age_at_arrival_ms": age,
                        "exit_bid": bid if math.isfinite(bid) else None,
                        "exit_fee_per_share": fee if math.isfinite(fee) else None,
                        "gross_edge_per_share": gross if math.isfinite(gross) else None,
                        "locked_edge_per_share": edge if math.isfinite(edge) else None,
                        "filled_shares": fill if 0 <= age <= self.args.maximum_book_age_ms else 0.0,
                    })
                    if not math.isfinite(bid) or not 0 < bid < 1 or not math.isfinite(fee):
                        sell["state"] = "CENSORED_INVALID_BOOK"
                    elif age < 0 or age > self.args.maximum_book_age_ms:
                        sell["state"] = "CENSORED_STALE_BOOK"
                    elif fill + 1e-12 < self.args.minimum_fill_shares:
                        sell["state"] = "NO_TRADE_DEPTH"
                    elif edge <= self.args.minimum_locked_edge_per_share:
                        sell["state"] = "NO_TRADE_EDGE"
                    else:
                        sell["state"] = "PAPER_LOCKED_ARBITRAGE"
                        sell["locked_pnl"] = fill * edge
                rows.append(sell)

            with self.args.output.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                    self.rows.append(row)
            self.seen.add(cycle_key)
            self.pending.pop(cycle_key, None)

    def publish(self) -> None:
        states = Counter(str(r.get("state") or "UNKNOWN") for r in self.rows)
        trades = [r for r in self.rows if r.get("state") == "PAPER_LOCKED_ARBITRAGE"]
        by_kind: dict[str, dict[str, Any]] = {}
        for row in self.rows:
            kind = str(row.get("kind") or "UNKNOWN")
            bucket = by_kind.setdefault(kind, {"cycles": 0, "paper_locked_arbitrages": 0, "locked_pnl": 0.0})
            bucket["cycles"] += 1
            if row.get("state") == "PAPER_LOCKED_ARBITRAGE":
                bucket["paper_locked_arbitrages"] += 1
                bucket["locked_pnl"] += float(row.get("locked_pnl") or 0.0)
        edges = [float(r["locked_edge_per_share"]) for r in trades
                 if isinstance(r.get("locked_edge_per_share"), (int, float))]
        value = {
            "schema": STATUS_SCHEMA,
            "model_sha": self.args.model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "strategy": "SETTLEMENT_SOURCE_ARB",
            "timestamp_ms": time.time_ns() // 1_000_000,
            "cycles": len(self.rows),
            "paper_locked_arbitrages": len(trades),
            "locked_pnl": sum(float(r.get("locked_pnl") or 0.0) for r in trades),
            "mean_locked_edge_per_share": sum(edges) / len(edges) if edges else None,
            "pending_outcomes": len(self.pending),
            "cached_markets": len(self.market_cache),
            "states": dict(states),
            "by_kind": by_kind,
            "paper_arrival_delay_ms": self.args.paper_arrival_delay_ms,
            "maximum_book_age_ms": self.args.maximum_book_age_ms,
            "state": "COLLECTING",
        }
        tmp = self.args.status.with_name(self.args.status.name + ".tmp")
        tmp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.args.status)

    def run(self) -> None:
        while True:
            self.book.poll()
            self.ingest_outcomes()
            self.evaluate_pending()
            self.publish()
            time.sleep(max(0.001, self.args.interval_ms / 1000.0))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--oracle-status", type=Path, required=True)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--book-tape", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--target-shares", type=float, default=5.0)
    ap.add_argument("--minimum-fill-shares", type=float, default=1.0)
    ap.add_argument("--redemption-reserve-per-share", type=float, default=0.0005)
    ap.add_argument("--minimum-locked-edge-per-share", type=float, default=0.0005)
    ap.add_argument("--paper-arrival-delay-ms", type=int, default=50)
    ap.add_argument("--maximum-book-age-ms", type=int, default=100)
    ap.add_argument("--interval-ms", type=int, default=5)
    args = ap.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("invalid model sha")
    if not (args.target_shares > 0 and args.minimum_fill_shares > 0
            and args.minimum_fill_shares <= args.target_shares):
        raise SystemExit("invalid size")
    if not (0 <= args.redemption_reserve_per_share < 1
            and 0 <= args.minimum_locked_edge_per_share < 1):
        raise SystemExit("invalid edge reserve")
    if not (0 <= args.paper_arrival_delay_ms <= 5000
            and 1 <= args.maximum_book_age_ms <= 5000
            and 1 <= args.interval_ms <= 1000):
        raise SystemExit("invalid timing")
    Shadow(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
