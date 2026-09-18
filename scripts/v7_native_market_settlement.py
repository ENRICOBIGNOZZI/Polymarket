#!/usr/bin/env python3
"""Settle completed native PAPER market sessions from public Gamma evidence.

No execution authority lives here. The process reads canonical PAPER fills,
requires Gamma to report the market closed with an unambiguous winning token,
then emits one idempotent FINAL event into the existing single-writer spool.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import spool_event

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SESSION_SCHEMA = "polymarket_v7_native_market_session_v1"
STATUS_SCHEMA = "polymarket_v7_native_market_settlement_status_v1"


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
        except json.JSONDecodeError:
            return []
        return out if isinstance(out, list) else []
    return []


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def request_json(url: str, timeout: float = 4.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-native-paper-settlement/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def read_sessions(path: Path, *, model_sha: str) -> dict[str, dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return sessions
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(row, dict)
                or row.get("schema") != SESSION_SCHEMA
                or row.get("model_sha") != model_sha
                or row.get("paper_only") is not True
                or row.get("authenticated_execution") is not False
                or row.get("real_order_submission") is not False
            ):
                continue
            market_id = str(row.get("market_id") or "")
            if market_id:
                sessions[market_id] = row
    return sessions


def iter_native_fills(run_root: Path, *, model_sha: str, market_id: str):
    seen: set[str] = set()
    paths = [run_root / "ledger" / "execution.jsonl"]
    spool = run_root / "ledger" / "spool"
    if spool.is_dir():
        paths.extend(sorted(spool.glob("*.json")))
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("event_type") != "FILL":
                continue
            record_id = str(row.get("record_id") or "")
            if not record_id or record_id in seen:
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            receipt = metadata.get("native_settlement_receipt")
            if (
                not isinstance(receipt, dict)
                or row.get("strategy") != "CRYPTO_SETTLEMENT_ENGINE"
                or row.get("model_sha") != model_sha
                or row.get("market_id") != market_id
                or row.get("paper_only") is not True
                or row.get("authenticated_execution") is not False
                or receipt.get("owner") != "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"
                or receipt.get("execution_mode") != "PAPER_SIMULATED"
            ):
                continue
            seen.add(record_id)
            yield row


def market_pnl(fills: list[dict[str, Any]], winning_token_id: str) -> tuple[float, float, float, dict[str, float]]:
    cash = 0.0
    total_fee = 0.0
    positions: dict[str, float] = {}
    for row in fills:
        token = str(row.get("token_id") or "")
        side = str(row.get("side") or "").upper()
        qty = finite(row.get("filled_size"))
        price = finite(row.get("fill_price"))
        fee = finite(row.get("fee"), 0.0)
        if not token or side not in {"BUY", "SELL"} or not (qty > 0.0) or not (0.0 <= price <= 1.0) or fee < 0.0:
            raise ValueError("invalid native fill economics")
        signed = qty if side == "BUY" else -qty
        positions[token] = positions.get(token, 0.0) + signed
        cash += (-qty * price if side == "BUY" else qty * price) - fee
        total_fee += fee
    if any(value < -1e-9 for value in positions.values()):
        raise ValueError("native paper settlement would require uncovered short inventory")
    payout = max(0.0, positions.get(winning_token_id, 0.0))
    pnl = cash + payout
    return pnl, payout, total_fee, positions


def winner_from_gamma(raw: Any) -> tuple[str, str] | None:
    if not isinstance(raw, dict) or raw.get("closed") is not True:
        return None
    tokens = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(value) for value in parse_array(raw.get("outcomes"))]
    prices = [finite(value) for value in parse_array(raw.get("outcomePrices"))]
    winners = [index for index, price in enumerate(prices) if math.isfinite(price) and price >= 1.0 - 1e-9]
    if len(winners) != 1:
        return None
    index = winners[0]
    if index >= len(tokens) or not tokens[index]:
        return None
    return tokens[index], outcomes[index] if index < len(outcomes) else ""


class Settler:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.run_root = args.run_root.resolve()
        self.sessions = self.run_root / "control" / "native_market_sessions.jsonl"
        self.state_path = self.run_root / "control" / "native_market_settlement_state.json"
        self.status_path = self.run_root / "control" / "native_market_settlement_status.json"
        self.kill = self.run_root / "control" / "KILL"
        state = load(self.state_path)
        self.settled = set(state.get("settled_markets", [])) if state.get("model_sha") == args.model_sha else set()
        self.pnl = float(state.get("realized_pnl") or 0.0) if state.get("model_sha") == args.model_sha else 0.0

    def persist(self) -> None:
        atomic_json(self.state_path, {
            "schema": "polymarket_v7_native_market_settlement_state_v1",
            "model_sha": self.args.model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "settled_markets": sorted(self.settled),
            "realized_pnl": self.pnl,
        })

    def publish(self, state: str, **extra: Any) -> None:
        atomic_json(self.status_path, {
            "schema": STATUS_SCHEMA,
            "timestamp_ms": int(time.time() * 1000),
            "state": state,
            "model_sha": self.args.model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": False,
            "settled_market_count": len(self.settled),
            "realized_pnl": self.pnl,
            **extra,
        })

    def settle_one(self, session: dict[str, Any]) -> bool:
        market_id = str(session["market_id"])
        try:
            raw = request_json(
                f"{self.args.gamma_url.rstrip('/')}/markets/{urllib.parse.quote(market_id)}",
                timeout=self.args.timeout,
            )
        except Exception as exc:
            self.publish("WAITING_GAMMA", market_id=market_id, blocker=type(exc).__name__)
            return False
        winner = winner_from_gamma(raw)
        if winner is None:
            self.publish("WAITING_CLOSED_MARKET", market_id=market_id)
            return False
        winning_token, winning_outcome = winner
        if winning_token not in {str(session.get("yes_token") or ""), str(session.get("no_token") or "")}:
            self.publish("SETTLEMENT_TOKEN_MISMATCH", market_id=market_id)
            return False

        fills = list(iter_native_fills(
            self.run_root, model_sha=self.args.model_sha, market_id=market_id))
        if not fills:
            self.settled.add(market_id)
            self.persist()
            self.publish("NO_POSITION_SETTLED", market_id=market_id)
            return True
        try:
            pnl, payout, fee, positions = market_pnl(fills, winning_token)
        except ValueError as exc:
            self.publish("SETTLEMENT_ACCOUNTING_FAILED", market_id=market_id, blocker=str(exc))
            return False

        observed_ms = int(time.time() * 1000)
        receipt = {
            "schema": "polymarket_v7_native_market_settlement_receipt_v1",
            "owner": "V7_NATIVE_PAPER_MARKET_SETTLEMENT",
            "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
            "model_sha": self.args.model_sha,
            "market_id": market_id,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "source": "GAMMA_CLOSED_MARKET",
            "market_closed": True,
            "settlement_observed_ms": observed_ms,
            "winning_token_id": winning_token,
            "winning_outcome": winning_outcome,
        }
        record_id = f"native-final:{self.args.model_sha[:12]}:{market_id}"
        event = LedgerEvent(
            event_type="FINAL",
            strategy="CRYPTO_SETTLEMENT_ENGINE",
            model_sha=self.args.model_sha,
            record_id=record_id,
            recorded_ts_ms=observed_ms,
            model_version="native-paper-engine",
            position_id=f"native-market:{market_id}",
            market_id=market_id,
            event_id=str(session.get("event_id") or ""),
            final_pnl=pnl,
            realized_cashflow=payout,
            fee=fee,
            slippage=0.0,
            unwind_loss=0.0,
            capital_cost=0.0,
            latency_cost=0.0,
            capital_duration_ms=max(0, observed_ms - int(session.get("started_ms") or observed_ms)),
            metadata={
                "component": "native_market_settlement",
                "model_family": "native_paper_engine",
                "realized": True,
                "cost_vector_complete": True,
                "native_market_settlement_receipt": receipt,
                "terminal_id": record_id,
                "net_token_positions": positions,
                "fill_count": len(fills),
            },
        )
        spool_event(self.run_root, event)
        self.settled.add(market_id)
        self.pnl += pnl
        self.persist()
        self.publish("SETTLED", market_id=market_id, final_pnl=pnl, winning_token_id=winning_token)
        return True

    def run(self) -> int:
        if not SHA40.fullmatch(self.args.model_sha):
            raise SystemExit("model_sha:not_exact")
        self.publish("RUNNING")
        while not self.kill.exists():
            sessions = read_sessions(self.sessions, model_sha=self.args.model_sha)
            pending = [
                row for market, row in sessions.items()
                if market not in self.settled and int(row.get("ended_ms") or 0) > 0
            ]
            progressed = False
            for row in sorted(pending, key=lambda value: int(value.get("ended_ms") or 0)):
                progressed = self.settle_one(row) or progressed
            if not pending:
                self.publish("RUNNING")
            time.sleep(max(1.0, self.args.interval if not progressed else 1.0))
        self.publish("STOPPED")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=4.0)
    args = parser.parse_args()
    return Settler(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
