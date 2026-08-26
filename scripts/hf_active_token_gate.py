#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class TokenStats:
    token_id: str
    trades: int = 0
    buy_shares: float = 0.0
    sell_shares: float = 0.0
    last_event_ts: int = 0
    last_received_ms: int = 0

    @property
    def total_shares(self) -> float:
        return self.buy_shares + self.sell_shares

    @property
    def sell_share(self) -> float:
        return self.sell_shares / self.total_shares if self.total_shares > 0.0 else 0.0

    @property
    def imbalance(self) -> float:
        return (self.sell_shares - self.buy_shares) / self.total_shares if self.total_shares > 0.0 else 0.0

    @property
    def score(self) -> float:
        # Prefer fresh, fill-relevant SELL flow while penalising one-sided toxicity.
        balance = max(0.0, 1.0 - abs(self.imbalance))
        return math.log1p(max(0.0, self.sell_shares)) * math.sqrt(max(1, self.trades)) * (0.25 + 0.75 * balance)


def _f(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _i(value: str | None, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return default


def load_recent_stats(tape: Path, as_of_ms: int, lookback_seconds: int) -> dict[str, TokenStats]:
    stats: dict[str, TokenStats] = {}
    if not tape.exists() or tape.stat().st_size == 0:
        return stats
    as_of_s = as_of_ms // 1000
    min_event_ts = as_of_s - max(1, lookback_seconds)
    with tape.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            event_ts = _i(row.get("timestamp"))
            received_ms = _i(row.get("received_ms"))
            token = (row.get("asset_id") or "").strip()
            if not token or event_ts <= 0 or received_ms <= 0:
                continue
            # Local receive time is the causal information gate. Event time is the
            # economic-recency clock. A late-indexed old trade must not masquerade
            # as fresh flow merely because it was received recently.
            if received_ms > as_of_ms or event_ts > as_of_s or event_ts < min_event_ts:
                continue
            side = (row.get("side") or "").upper()
            size = max(0.0, _f(row.get("size")))
            if size <= 0.0 or side not in {"BUY", "SELL"}:
                continue
            s = stats.setdefault(token, TokenStats(token_id=token))
            s.trades += 1
            if side == "SELL":
                s.sell_shares += size
            else:
                s.buy_shares += size
            s.last_event_ts = max(s.last_event_ts, event_ts)
            s.last_received_ms = max(s.last_received_ms, received_ms)
    return stats


def choose_tokens(
    stats: dict[str, TokenStats],
    *,
    min_trades: int,
    min_sell_shares: float,
    min_sell_share: float,
    max_sell_share: float,
    max_tokens: int,
) -> list[TokenStats]:
    eligible = [
        s
        for s in stats.values()
        if s.trades >= min_trades
        and s.sell_shares >= min_sell_shares
        and s.total_shares > 0.0
        and s.sell_share >= min_sell_share
        and s.sell_share <= max_sell_share
    ]
    eligible.sort(key=lambda s: (s.score, s.last_event_ts, s.sell_shares, s.token_id), reverse=True)
    return eligible[: max(1, max_tokens)]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def run_once(args: argparse.Namespace) -> dict:
    as_of_ms = int(args.as_of_ms) if args.as_of_ms is not None else int(time.time() * 1000)
    stats = load_recent_stats(Path(args.tape), as_of_ms, args.lookback_seconds)
    chosen = choose_tokens(
        stats,
        min_trades=args.min_trades,
        min_sell_shares=args.min_sell_shares,
        min_sell_share=args.min_sell_share,
        max_sell_share=args.max_sell_share,
        max_tokens=args.max_tokens,
    )
    atomic_write_text(Path(args.output), "".join(f"{s.token_id}\n" for s in chosen))
    payload = {
        "schema": "hf_active_token_gate_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "as_of_ms": as_of_ms,
        "lookback_seconds": args.lookback_seconds,
        "thresholds": {
            "min_trades": args.min_trades,
            "min_sell_shares": args.min_sell_shares,
            "min_sell_share": args.min_sell_share,
            "max_sell_share": args.max_sell_share,
            "max_tokens": args.max_tokens,
        },
        "tokens_with_recent_causal_flow": len(stats),
        "eligible_tokens": len(chosen),
        "selected": [
            {
                **asdict(s),
                "total_shares": s.total_shares,
                "sell_share": s.sell_share,
                "imbalance": s.imbalance,
                "score": s.score,
            }
            for s in chosen
        ],
    }
    if args.status:
        atomic_write_text(Path(args.status), json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a causal, activity-aware token allowlist for HF maker research")
    p.add_argument("--tape", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--status")
    p.add_argument("--lookback-seconds", type=int, default=120)
    p.add_argument("--min-trades", type=int, default=2)
    p.add_argument("--min-sell-shares", type=float, default=5.0)
    p.add_argument("--min-sell-share", type=float, default=0.05)
    p.add_argument("--max-sell-share", type=float, default=0.80)
    p.add_argument("--max-tokens", type=int, default=250)
    p.add_argument("--as-of-ms", type=int)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=float, default=10.0)
    args = p.parse_args()
    if args.lookback_seconds <= 0:
        p.error("--lookback-seconds must be positive")
    if args.min_trades < 1:
        p.error("--min-trades must be >= 1")
    if args.min_sell_shares < 0.0:
        p.error("--min-sell-shares must be non-negative")
    if not 0.0 <= args.min_sell_share <= args.max_sell_share <= 1.0:
        p.error("sell-share thresholds must satisfy 0 <= min <= max <= 1")
    if args.max_tokens < 1:
        p.error("--max-tokens must be >= 1")
    return args


def main() -> int:
    args = parse_args()
    while True:
        payload = run_once(args)
        print(json.dumps(payload, sort_keys=True))
        if not args.loop:
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
