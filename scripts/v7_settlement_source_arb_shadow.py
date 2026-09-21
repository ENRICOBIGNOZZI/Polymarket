#!/usr/bin/env python3
"""Zero-authority PAPER shadow for exact settlement-source arbitrage.

For M5/M15 crypto markets the official source is the public Chainlink 60s TWAP
stream. A candidate exists only after the exact end-boundary observation makes
the winning token deterministic. The shadow then evaluates the first causally
available Polymarket book after a configured PAPER arrival delay.

No forecasting. No ML. No Binance predictor. No real order submission.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

from v7_causal_book import BookTimeline

SCHEMA = "polymarket_v7_settlement_source_arb_cycle_v1"
STATUS_SCHEMA = "polymarket_v7_settlement_source_arb_status_v1"


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


def markets(selection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if (
        selection.get("schema") != "polymarket_v7_multi_crypto_book_selection_v1"
        or selection.get("paper_only") is not True
        or selection.get("authenticated_execution") is not False
        or selection.get("real_order_submission") is not False
        or selection.get("execution_authority") is not False
    ):
        return {}
    out = {}
    for row in selection.get("markets") or []:
        if not isinstance(row, dict) or row.get("horizon") not in {"M5", "M15"}:
            continue
        mid = str(row.get("market_id") or "")
        yes = str(row.get("yes_token") or "")
        no = str(row.get("no_token") or "")
        if mid and yes and no and yes != no:
            out[mid] = row
    return out


class Shadow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.book = BookTimeline(args.book_tape, args.model_sha, retention_ms=60_000)
        self.seen: set[str] = set()
        self.pending: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.status.parent.mkdir(parents=True, exist_ok=True)
        self._restore()

    def _restore(self) -> None:
        try:
            for line in self.args.output.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row.get("schema") == SCHEMA and row.get("model_sha") == self.args.model_sha:
                    self.rows.append(row)
                    self.seen.add(str(row.get("market_id") or ""))
        except (OSError, json.JSONDecodeError):
            pass

    def ingest_outcomes(self) -> None:
        oracle = load(self.args.oracle_status)
        selection = load(self.args.selection)
        if (
            oracle.get("schema") != "polymarket_v7_multi_crypto_oracle_hub_v2"
            or oracle.get("model_sha") != self.args.model_sha
            or oracle.get("paper_only") is not True
            or oracle.get("authenticated_execution") is not False
            or oracle.get("real_order_submission") is not False
            or oracle.get("execution_authority") is not False
        ):
            return
        by_market = markets(selection)
        outcomes = oracle.get("settlement_outcomes")
        if not isinstance(outcomes, dict):
            return
        for market_id, outcome in outcomes.items():
            if market_id in self.seen or market_id in self.pending:
                continue
            market = by_market.get(str(market_id))
            if market is None or not isinstance(outcome, dict) or outcome.get("valid") is not True:
                continue
            winner = str(outcome.get("winning_outcome") or "")
            token = str(market.get("yes_token") if winner == "YES" else market.get("no_token") if winner == "NO" else "")
            determined_ns = int(outcome.get("determined_wall_ns") or 0)
            if not token or determined_ns <= 0:
                continue
            determined_ms = determined_ns // 1_000_000
            self.pending[market_id] = {
                "market": market,
                "outcome": outcome,
                "winner": winner,
                "token": token,
                "determined_ms": determined_ms,
                "arrival_ms": determined_ms + self.args.paper_arrival_delay_ms,
            }

    def evaluate_pending(self) -> None:
        for market_id in list(self.pending):
            item = self.pending[market_id]
            arrival_ms = int(item["arrival_ms"])
            if self.book.watermark_ms < arrival_ms:
                continue
            market = item["market"]
            cut = self.book.asof(market_id, item["token"], arrival_ms)
            row: dict[str, Any] = {
                "schema": SCHEMA,
                "model_sha": self.args.model_sha,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "real_capital_at_risk": False,
                "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
                "strategy": "SETTLEMENT_SOURCE_ARB",
                "asset": str(market.get("asset") or ""),
                "horizon": str(market.get("horizon") or ""),
                "market_id": market_id,
                "winning_outcome": item["winner"],
                "winning_token": item["token"],
                "outcome_determined_wall_ms": item["determined_ms"],
                "paper_arrival_wall_ms": arrival_ms,
                "paper_arrival_delay_ms": self.args.paper_arrival_delay_ms,
                "reference_price_decimal": item["outcome"].get("reference_price_decimal"),
                "final_price_decimal": item["outcome"].get("final_price_decimal"),
                "state": "CENSORED_BOOK_UNAVAILABLE",
                "requested_shares": self.args.target_shares,
                "filled_shares": 0.0,
                "entry_ask": None,
                "entry_fee_per_share": None,
                "redemption_reserve_per_share": self.args.redemption_reserve_per_share,
                "locked_edge_per_share": None,
                "locked_pnl": None,
            }
            if cut is not None:
                try:
                    ask = float(cut["best_ask"])
                    depth = float(cut.get("ask_depth_l1") or 0.0)
                    receive_ms = int(cut["receive_wall_ms"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    ask = math.nan; depth = 0.0; receive_ms = 0
                age = arrival_ms - receive_ms
                fee = fee_per_share(ask, self.args.taker_fee_rate, self.args.taker_fee_exponent)
                edge = 1.0 - ask - fee - self.args.redemption_reserve_per_share                     if math.isfinite(fee) else math.nan
                fill = min(self.args.target_shares, max(0.0, depth))
                row.update({
                    "book_receive_wall_ms": receive_ms,
                    "book_age_at_arrival_ms": age,
                    "entry_ask": ask if math.isfinite(ask) else None,
                    "entry_fee_per_share": fee if math.isfinite(fee) else None,
                    "locked_edge_per_share": edge if math.isfinite(edge) else None,
                    "filled_shares": fill if age >= 0 and age <= self.args.maximum_book_age_ms else 0.0,
                })
                if not math.isfinite(ask) or not 0 < ask < 1 or not math.isfinite(fee):
                    row["state"] = "CENSORED_INVALID_BOOK"
                elif age < 0 or age > self.args.maximum_book_age_ms:
                    row["state"] = "CENSORED_STALE_BOOK"
                elif fill + 1e-12 < self.args.minimum_fill_shares:
                    row["state"] = "NO_TRADE_DEPTH"
                elif edge <= self.args.minimum_locked_edge_per_share:
                    row["state"] = "NO_TRADE_EDGE"
                else:
                    row["state"] = "PAPER_LOCKED_ARBITRAGE"
                    row["locked_pnl"] = fill * edge
            with self.args.output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            self.rows.append(row)
            self.seen.add(market_id)
            self.pending.pop(market_id, None)

    def publish(self) -> None:
        trades = [r for r in self.rows if r.get("state") == "PAPER_LOCKED_ARBITRAGE"]
        pnl = sum(float(r.get("locked_pnl") or 0.0) for r in trades)
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
            "locked_pnl": pnl,
            "mean_locked_edge_per_share": sum(edges) / len(edges) if edges else None,
            "pending_outcomes": len(self.pending),
            "paper_arrival_delay_ms": self.args.paper_arrival_delay_ms,
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
    ap.add_argument("--taker-fee-rate", type=float, required=True)
    ap.add_argument("--taker-fee-exponent", type=float, required=True)
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
    if not (0 <= args.taker_fee_rate <= 1 and args.taker_fee_exponent >= 0):
        raise SystemExit("invalid fee")
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
