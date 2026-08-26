#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import fcntl
import os
import signal
import time
from pathlib import Path

from v7_multileg_broker import Broker, finite


class CoordinatedBroker(Broker):
    def __init__(self, *args, capacity_lock: Path, **kwargs) -> None:
        self.capacity_lock = capacity_lock
        super().__init__(*args, **kwargs)

    def maker_owned_tokens(self) -> set[str]:
        tokens: set[str] = set()
        maker = self.run_dir / "maker"
        for name in ("maker_orders.csv", "maker_positions.csv"):
            try:
                with (maker / name).open(newline="", encoding="utf-8") as handle:
                    for row in csv.DictReader(handle):
                        token = str(row.get("token_id") or "")
                        if token:
                            tokens.add(token)
            except OSError:
                pass
        return tokens

    def active_tokens(self) -> set[str]:
        return {leg.token_id for leg in self.live_legs()} | self.maker_owned_tokens()

    def tick(self) -> None:
        tokens = sorted({leg.token_id for leg in self.live_legs()})
        books = self.books(tokens)
        eq = self.equity(books)
        self.peak = max(self.peak, eq)
        drawdown = max(0.0, 1.0 - eq / self.peak) if self.peak > 0 else 0.0
        self.killed = self.killed or drawdown >= float(self.cfg.get("max_drawdown", 0.15))

        self.capacity_lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.capacity_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.admit(eq)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        tokens = sorted({leg.token_id for leg in self.live_legs()})
        books = self.books(tokens)
        self.apply_trades(self.read_new_tape())
        self.manage(books)
        self.measure_adverse(books)
        eq = self.equity(books)
        self.peak = max(self.peak, eq)
        drawdown = max(0.0, 1.0 - eq / self.peak) if self.peak > 0 else 0.0
        self.killed = self.killed or drawdown >= float(self.cfg.get("max_drawdown", 0.15))
        self.persist()
        from v7_multileg_broker import append_csv, atomic_json
        append_csv(self.run_dir / "multileg_equity.csv", self.EQUITY_FIELDS, {
            "timestamp": int(time.time()), "cash": self.cash, "equity": eq, "reserved_cash": self.reserved_cash(),
            "gross_entry_cash": self.gross_entry_cash(), "peak_equity": self.peak, "drawdown": drawdown,
            "killed": 1 if self.killed else 0,
            "live_bundles": sum(bundle.status not in {"CLOSED", "UNWOUND", "CANCELLED"} for bundle in self.bundles.values()),
        })
        atomic_json(self.run_dir / "v7_broker_status.json", {
            "timestamp": int(time.time()), "paper_only": True, "authenticated_execution": False,
            "cash": self.cash, "equity": eq, "drawdown": drawdown, "killed": self.killed,
            "reserved_cash": self.reserved_cash(), "gross_entry_cash": self.gross_entry_cash(),
            "maker_owned_tokens": len(self.maker_owned_tokens()),
            "bundles": {key: bundle.status for key, bundle in self.bundles.items()},
            "contracts": ["dual_clock_forward_fill", "cross_sleeve_token_capacity_lock", "one_live_owner_per_token", "shared_trade_capacity", "canonical_market_event_risk", "100_percent_completion", "explicit_abort_unwind", "settling_preserves_complete_structural_payoff"],
        })


def main() -> int:
    parser = argparse.ArgumentParser(description="Capacity-coordinated canonical V7 multi-leg PAPER broker")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--intents", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--capacity-lock", type=Path, required=True)
    parser.add_argument("--min-edge", type=float, default=0.00005)
    parser.add_argument("--submit-latency-ms", type=int, default=100)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--adverse-horizon-seconds", type=int, default=45)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    broker = CoordinatedBroker(
        args.config, args.run_dir, args.intents, args.trade_tape,
        args.min_edge, args.submit_latency_ms, args.slippage_bps, args.adverse_horizon_seconds,
        capacity_lock=args.capacity_lock,
    )
    stop = False
    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    while True:
        broker.tick()
        if not args.loop or stop:
            break
        time.sleep(max(0.1, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
