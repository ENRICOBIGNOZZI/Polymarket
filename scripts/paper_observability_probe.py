#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from pathlib import Path
from typing import Any

from v6_micro_taker import Book, Market, append_csv, atomic_json, discover, fee_per_share, fetch_books, finite

FILL_FIELDS = ["timestamp", "market_id", "slug", "action", "side", "shares", "price", "fee", "pnl"]
EQUITY_FIELDS = [
    "timestamp", "cash", "equity", "drawdown", "open_positions", "signals", "opened",
    "best_edge", "labeled_samples", "realized_pnl_total", "target_staleness_max_seconds",
]


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_probe_config(path: Path) -> dict[str, Any]:
    raw = load_json(path, {})
    if not isinstance(raw, dict) or raw.get("paper_only") is not True:
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "notional_usd": min(5.0, max(0.0, finite(raw.get("notional_usd"), 5.0))),
        "cooldown_seconds": max(1, int(finite(raw.get("cooldown_seconds"), 60.0))),
        "min_price": min(0.49, max(0.001, finite(raw.get("min_price"), 0.05))),
        "max_price": max(0.51, min(0.999, finite(raw.get("max_price"), 0.95))),
        "max_top_level_fraction": min(0.25, max(0.01, finite(raw.get("max_top_level_fraction"), 0.25))),
        "max_roundtrips": min(20, max(0, int(finite(raw.get("max_roundtrips"), 20.0)))),
    }


def csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size <= 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def roundtrip_friction(market: Market, book: Book, slip: float) -> tuple[float, float, float] | None:
    bid, ask = book.bid(), book.ask()
    if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask >= 1 or ask <= bid:
        return None
    entry = min(0.999999, ask * (1.0 + slip))
    exit_price = max(1e-6, bid * (1.0 - slip))
    entry_fee = fee_per_share(entry, market.fee_rate, market.fee_exp)
    exit_fee = fee_per_share(exit_price, market.fee_rate, market.fee_exp)
    cost = entry + entry_fee
    proceeds = exit_price - exit_fee
    return max(0.0, cost - proceeds) / max(cost, 1e-9), entry, entry_fee


def select_candidate(
    markets: list[Market], books: dict[str, Book], *, slip: float, min_price: float, max_price: float
) -> tuple[float, Market, str, Book, float, float] | None:
    ranked: list[tuple[float, float, Market, str, Book, float, float]] = []
    for market in markets:
        for side, token in (("YES", market.yes), ("NO", market.no)):
            book = books.get(token)
            if book is None or not book.asks:
                continue
            ask = book.ask()
            if not math.isfinite(ask) or ask < min_price or ask > max_price:
                continue
            friction = roundtrip_friction(market, book, slip)
            if friction is None:
                continue
            relative_friction, entry, entry_fee = friction
            displayed_notional = float(book.asks[0][1]) * max(entry + entry_fee, 1e-9)
            if displayed_notional <= 0:
                continue
            ranked.append((relative_friction, -displayed_notional, market, side, book, entry, entry_fee))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    relative_friction, _neg_depth, market, side, book, entry, entry_fee = ranked[0]
    return relative_friction, market, side, book, entry, entry_fee


def sync_closed_probe(run_dir: Path, state: dict[str, Any], probe_state: dict[str, Any]) -> int:
    market_id = str(probe_state.get("open_market_id") or "")
    if not market_id:
        return 0
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    if market_id in positions:
        return 0
    entry_ts = int(finite(probe_state.get("open_entry_ts"), 0.0))
    row = None
    for candidate in reversed(csv_rows(run_dir / "fills.csv")):
        if candidate.get("action") != "SELL" or str(candidate.get("market_id") or "") != market_id:
            continue
        if int(finite(candidate.get("timestamp"), 0.0)) < entry_ts:
            continue
        row = candidate
        break
    if row is not None:
        append_csv(run_dir / "observability_probe_fills.csv", FILL_FIELDS, row)
        probe_state["last_exit_pnl"] = finite(row.get("pnl"), 0.0)
        probe_state["last_exit_ts"] = int(finite(row.get("timestamp"), 0.0))
    probe_state["exits"] = int(finite(probe_state.get("exits"), 0.0)) + 1
    probe_state["open_market_id"] = ""
    probe_state["open_entry_ts"] = 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded PAPER fill-to-PnL observability probe")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--probe-config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=250)
    parser.add_argument("--min-liquidity", type=float, default=25.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args()

    probe = load_probe_config(args.probe_config)
    state_path = args.run_dir / "state.json"
    status_path = args.run_dir / "status.json"
    probe_state_path = args.run_dir / "observability_probe_state.json"
    state = load_json(state_path, {})
    probe_state = load_json(
        probe_state_path,
        {"paper_only": True, "entries": 0, "exits": 0, "last_entry_ts": 0, "open_market_id": ""},
    )
    if not isinstance(state, dict):
        state = {}
    if not isinstance(probe_state, dict):
        probe_state = {"paper_only": True, "entries": 0, "exits": 0, "last_entry_ts": 0, "open_market_id": ""}

    closed = sync_closed_probe(args.run_dir, state, probe_state)
    status = load_json(status_path, {})
    if not isinstance(status, dict):
        status = {}
    status.update(
        {
            "observability_probe_enabled": bool(probe["enabled"]),
            "observability_probe_entries": int(finite(probe_state.get("entries"), 0.0)),
            "observability_probe_exits": int(finite(probe_state.get("exits"), 0.0)),
            "observability_probe_max_roundtrips": int(probe["max_roundtrips"]),
        }
    )
    atomic_json(probe_state_path, probe_state)
    if status:
        atomic_json(status_path, status)

    now = int(time.time())
    entries = int(finite(probe_state.get("entries"), 0.0))
    if not probe["enabled"] or probe["notional_usd"] <= 0 or probe["max_roundtrips"] <= 0:
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "disabled"}, sort_keys=True))
        return 0
    if entries >= int(probe["max_roundtrips"]):
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "roundtrip_budget_exhausted"}, sort_keys=True))
        return 0
    if not state_path.exists() or not state:
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "micro_state_missing"}, sort_keys=True))
        return 0
    if bool(state.get("killed")):
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "killed"}, sort_keys=True))
        return 0
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    if positions:
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "positions_already_open"}, sort_keys=True))
        return 0
    if int(finite(state.get("opened"), 0.0)) > 0:
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "alpha_opened_this_tick"}, sort_keys=True))
        return 0
    if now - int(finite(probe_state.get("last_entry_ts"), 0.0)) < int(probe["cooldown_seconds"]):
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "cooldown"}, sort_keys=True))
        return 0

    cfg = load_json(args.config, {})
    if not isinstance(cfg, dict) or not cfg.get("gamma_url") or not cfg.get("clob_url"):
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "invalid_market_config"}, sort_keys=True))
        return 0
    try:
        markets = discover(str(cfg["gamma_url"]), max(1, int(args.markets)), max(0.0, float(args.min_liquidity)))
        books = fetch_books(str(cfg["clob_url"]), markets)
    except Exception as exc:
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": f"market_data:{type(exc).__name__}:{exc}"}, sort_keys=True))
        return 0

    slip = max(0.0, float(args.slippage_bps)) / 10000.0
    candidate = select_candidate(
        markets,
        books,
        slip=slip,
        min_price=float(probe["min_price"]),
        max_price=float(probe["max_price"]),
    )
    if candidate is None:
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "no_executable_candidate"}, sort_keys=True))
        return 0

    relative_friction, market, side, book, entry, fee_per_unit = candidate
    cash = finite(state.get("cash"), 0.0)
    displayed_shares = float(book.asks[0][1]) * float(probe["max_top_level_fraction"])
    target_shares = min(float(probe["notional_usd"]) / max(entry + fee_per_unit, 1e-9), displayed_shares)
    if target_shares < float(book.min_order):
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "below_min_order"}, sort_keys=True))
        return 0
    fee = fee_per_unit * target_shares
    cost = entry * target_shares + fee
    if cost <= 0 or cost > cash:
        print(json.dumps({"probe_opened": 0, "probe_closed": closed, "reason": "insufficient_cash"}, sort_keys=True))
        return 0

    positions[market.id] = {
        "side": side,
        "shares": target_shares,
        "entry_price": entry,
        "cost": cost,
        "entry_ts": now,
        "mode": "observability_probe",
    }
    cash -= cost
    bid = book.bid()
    equity = cash + target_shares * (bid if math.isfinite(bid) else entry)
    peak = max(finite(state.get("peak"), equity), equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0 else 0.0
    max_drawdown = finite(cfg.get("max_drawdown"), 0.15)
    killed = bool(state.get("killed")) or drawdown >= max_drawdown

    state.update(
        {
            "timestamp": now,
            "cash": cash,
            "equity": equity,
            "peak": peak,
            "drawdown": drawdown,
            "killed": killed,
            "positions": positions,
            "opened": int(finite(state.get("opened"), 0.0)) + 1,
            "observability_probe_opened_last_tick": 1,
            "observability_probe_last_entry_ts": now,
        }
    )
    atomic_json(state_path, state)

    status = load_json(status_path, {})
    if not isinstance(status, dict):
        status = {}
    status.update(
        {
            "cash": cash,
            "equity": equity,
            "peak_equity": peak,
            "drawdown": drawdown,
            "gross_exposure": cost,
            "open_positions": 1,
            "killed": killed,
            "opened": int(finite(state.get("opened"), 0.0)),
            "observability_probe_enabled": True,
            "observability_probe_opened_last_tick": 1,
            "observability_probe_last_entry_ts": now,
            "observability_probe_roundtrip_friction": relative_friction,
            "observability_probe_entries": entries + 1,
            "observability_probe_exits": int(finite(probe_state.get("exits"), 0.0)),
            "observability_probe_max_roundtrips": int(probe["max_roundtrips"]),
        }
    )
    atomic_json(status_path, status)

    fill = {
        "timestamp": now,
        "market_id": market.id,
        "slug": market.slug,
        "action": "BUY",
        "side": side,
        "shares": target_shares,
        "price": entry,
        "fee": fee,
        "pnl": 0.0,
    }
    append_csv(args.run_dir / "fills.csv", FILL_FIELDS, fill)
    append_csv(args.run_dir / "observability_probe_fills.csv", FILL_FIELDS, fill)
    append_csv(
        args.run_dir / "equity.csv",
        EQUITY_FIELDS,
        {
            "timestamp": now,
            "cash": cash,
            "equity": equity,
            "drawdown": drawdown,
            "open_positions": 1,
            "signals": int(finite(state.get("signals"), 0.0)),
            "opened": int(finite(state.get("opened"), 0.0)),
            "best_edge": finite(state.get("best_edge"), 0.0),
            "labeled_samples": int(finite(state.get("labeled_samples"), 0.0)),
            "realized_pnl_total": finite(state.get("realized_pnl_total"), 0.0),
            "target_staleness_max_seconds": state.get("target_staleness_max_seconds"),
        },
    )

    probe_state.update(
        {
            "paper_only": True,
            "entries": entries + 1,
            "last_entry_ts": now,
            "open_market_id": market.id,
            "open_entry_ts": now,
            "last_side": side,
            "last_roundtrip_friction": relative_friction,
            "max_roundtrips": int(probe["max_roundtrips"]),
        }
    )
    atomic_json(probe_state_path, probe_state)
    print(
        json.dumps(
            {
                "probe_opened": 1,
                "probe_closed": closed,
                "market_id": market.id,
                "side": side,
                "notional_usd": cost,
                "roundtrip_friction": relative_friction,
                "entries": entries + 1,
                "max_roundtrips": int(probe["max_roundtrips"]),
                "equity": equity,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
