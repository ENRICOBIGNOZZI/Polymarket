#!/usr/bin/env python3
"""Combine all paper/shadow alpha sleeves into one ranked opportunity book.

The book is intentionally larger than the portfolio. It ranks up to N research
candidates while separately flagging the subset that is trade-eligible under
each source's execution evidence. Portfolio/risk gates remain downstream and
unchanged.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Iterable

FIELDS = [
    "rank",
    "source",
    "strategy",
    "source_id",
    "event_id",
    "market_id",
    "side",
    "eligible",
    "hard_arbitrage",
    "raw_edge",
    "net_edge",
    "capital_required",
    "expected_profit",
    "score",
    "legs",
]


def fnum(row: dict[str, str], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        try:
            value = float(row.get(key, "") or "")
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return default


def text(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


def yes(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (FileNotFoundError, OSError, csv.Error):
        return []


def active_experts_from_config(path: Path) -> set[str] | None:
    """Return experts with positive base ensemble weight.

    ``None`` means the config could not be read and preserves the diagnostic
    labels rather than pretending to know which experts were active.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    weights = payload.get("expert_weights")
    if not isinstance(weights, dict):
        return None
    active: set[str] = set()
    for name, value in weights.items():
        try:
            weight = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(weight) and weight > 0.0:
            active.add(str(name).strip().lower())
    return active


def fast_rows(path: Path) -> Iterable[dict[str, object]]:
    """Expose fast-layer observations without promoting passive fill assumptions.

    The fast engine uses ``executable`` for two different concepts:

    * crossing hard-arbitrage baskets whose displayed depth can be walked now;
    * passive two-sided maker definitions that can be posted now but whose
      economics require both legs to fill later.

    Only the first class is directly trade-eligible here. Passive maker rows
    remain visible in the broad research frontier and must earn eligibility via
    forward paired-fill evidence in the dedicated execution/maker research
    path. This prevents a large post-only spread from masquerading as captured
    PnL merely because both quotes are syntactically placeable.
    """
    for row in read_csv(path):
        raw = fnum(row, "raw_edge_per_share")
        net = fnum(row, "net_edge_per_share")
        capital = max(0.0, fnum(row, "capital_required"))
        profit = fnum(row, "expected_profit", default=net * capital)
        source_executable = yes(row.get("executable", ""))
        hard = yes(row.get("hard_arbitrage", ""))
        trade_eligible = source_executable and hard and net > 0.0 and capital > 0.0
        if not source_executable and raw <= 0.0:
            continue
        yield {
            "source": "fast",
            "strategy": text(row, "kind") or "FAST",
            "source_id": text(row, "id"),
            "event_id": text(row, "event_id"),
            "market_id": "",
            "side": "",
            "eligible": int(trade_eligible),
            "hard_arbitrage": int(hard),
            "raw_edge": raw,
            "net_edge": net,
            "capital_required": capital,
            "expected_profit": profit,
            "legs": text(row, "legs"),
        }


def b1_rows(path: Path, max_trade: float) -> Iterable[dict[str, object]]:
    for row in read_csv(path):
        raw = fnum(row, "raw_expected_edge")
        net = fnum(row, "maker_entry_net_edge")
        executable = max(0.0, fnum(row, "executable_notional"))
        capital = min(max_trade, executable)
        if net <= 0.0 and raw <= 0.0:
            continue
        market = text(row, "y_market")
        yield {
            "source": "b1",
            "strategy": "B1",
            "source_id": f"B1:{market}:{text(row, 'x_market')}",
            "event_id": f"PAIR:{market}|{text(row, 'x_market')}",
            "market_id": market,
            "side": text(row, "y_side"),
            "eligible": int(net > 0.0 and capital > 0.0),
            "hard_arbitrage": 0,
            "raw_edge": raw,
            "net_edge": net,
            "capital_required": capital,
            "expected_profit": net * capital,
            "legs": "|".join(
                item
                for item in [
                    f"{text(row, 'y_market')}:{text(row, 'y_side')}:{text(row, 'y_weight')}",
                    f"{text(row, 'x_market')}:{text(row, 'x_side')}:{text(row, 'x_weight')}",
                ]
                if item and not item.startswith("::")
            ),
        }


def b2_rows(path: Path, max_trade: float) -> Iterable[dict[str, object]]:
    for row in read_csv(path):
        raw = fnum(row, "raw_expected_edge")
        net = fnum(row, "maker_entry_net_edge")
        executable = max(0.0, fnum(row, "executable_notional"))
        capital = min(max_trade, executable)
        if net <= 0.0 and raw <= 0.0:
            continue
        market = text(row, "market")
        yield {
            "source": "b2",
            "strategy": "B2",
            "source_id": f"B2:{market}",
            "event_id": f"PCA:{market}",
            "market_id": market,
            "side": text(row, "side"),
            "eligible": int(net > 0.0 and capital > 0.0 and bool(text(row, "coherence_scope"))),
            "hard_arbitrage": 0,
            "raw_edge": raw,
            "net_edge": net,
            "capital_required": capital,
            "expected_profit": net * capital,
            "legs": text(row, "legs"),
        }


def terminal_strategy(experts: str, active_experts: set[str] | None = None) -> str:
    names: list[str] = []
    for item in experts.split("|"):
        name = item.split(":", 1)[0].strip().lower()
        if not name:
            continue
        if active_experts is not None and name not in active_experts:
            continue
        if name not in names:
            names.append(name)
    preferred = [name for name in ("external", "graph", "semantic", "pca", "micro") if name in names]
    return "TERMINAL:" + "+".join(preferred or names or ["ensemble"])


def terminal_rows(
    path: Path,
    max_trade: float,
    now_ts: int,
    max_age_seconds: int,
    active_experts: set[str] | None = None,
) -> Iterable[dict[str, object]]:
    """Expose fresh universal V4 fair-value signals as research candidates.

    `signals.csv` already evaluates executable ask, protocol fee, slippage,
    uncertainty and portfolio sizing. A terminal signal is marked eligible only
    when the engine itself assigned positive desired notional. The file is
    append-only, so only the newest fresh row per market/side is retained.
    """
    latest: dict[tuple[str, str], tuple[int, dict[str, str]]] = {}
    for row in read_csv(path):
        ts = int(max(0.0, fnum(row, "timestamp")))
        if ts <= 0 or now_ts - ts > max_age_seconds or ts - now_ts > 30:
            continue
        market = text(row, "market_id")
        side = text(row, "side")
        if not market or side not in {"YES", "NO"}:
            continue
        key = (market, side)
        previous = latest.get(key)
        if previous is None or ts >= previous[0]:
            latest[key] = (ts, row)

    for (_, _), (_, row) in latest.items():
        raw = fnum(row, "gross_edge")
        net = fnum(row, "net_edge")
        desired = max(0.0, fnum(row, "desired_notional"))
        capital = min(max_trade, desired)
        if raw <= 0.0 and net <= 0.0:
            continue
        market = text(row, "market_id")
        side = text(row, "side")
        experts = text(row, "experts")
        eligible = net > 0.0 and capital > 0.0
        yield {
            "source": "terminal",
            "strategy": terminal_strategy(experts, active_experts),
            "source_id": f"TERMINAL:{market}:{side}",
            "event_id": f"TERMINAL:{market}",
            "market_id": market,
            "side": side,
            "eligible": int(eligible),
            "hard_arbitrage": 0,
            "raw_edge": raw,
            "net_edge": net,
            "capital_required": capital,
            "expected_profit": net * capital,
            "legs": f"{market}:{side}:1",
        }


def score(candidate: dict[str, object]) -> float:
    net = float(candidate["net_edge"])
    raw = float(candidate["raw_edge"])
    profit = float(candidate["expected_profit"])
    capital = max(1.0, float(candidate["capital_required"]))
    eligible_bonus = 1.0 if int(candidate["eligible"]) else 0.0
    hard_bonus = 0.25 if int(candidate["hard_arbitrage"]) else 0.0
    return eligible_bonus + hard_bonus + max(net, 0.0) * 10.0 + max(profit, 0.0) / capital + max(raw, 0.0)


def deduplicate(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    best: dict[tuple[str, str, str], dict[str, object]] = {}
    for candidate in candidates:
        key = (
            str(candidate["strategy"]),
            str(candidate["event_id"]),
            str(candidate["legs"]),
        )
        candidate["score"] = score(candidate)
        previous = best.get(key)
        if previous is None or float(candidate["score"]) > float(previous["score"]):
            best[key] = candidate
    return list(best.values())


def atomic_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v4_live"))
    parser.add_argument("--config", type=Path, default=Path("config/paper_v4.json"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--eligible-output", type=Path, default=None)
    parser.add_argument("--status", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--max-trade-usd", type=float, default=250.0)
    parser.add_argument("--terminal-max-age-seconds", type=int, default=600)
    args = parser.parse_args()

    output = args.output or args.run_root / "global_opportunities.csv"
    eligible_output = args.eligible_output or args.run_root / "global_trade_candidates.csv"
    status_path = args.status or args.run_root / "global_opportunity_status.json"
    limit = max(1, args.limit)
    max_trade = max(0.0, args.max_trade_usd)
    now_ts = int(time.time())
    terminal_max_age = max(60, args.terminal_max_age_seconds)
    active_experts = active_experts_from_config(args.config)

    candidates = list(fast_rows(args.run_root / "fast" / "fast_arb_latest.csv"))
    candidates += list(b1_rows(args.run_root / "stat_arb_pairs.csv", max_trade))
    candidates += list(b2_rows(args.run_root / "stat_arb_pca.csv", max_trade))
    candidates += list(terminal_rows(
        args.run_root / "terminal" / "signals.csv",
        max_trade,
        now_ts,
        terminal_max_age,
        active_experts,
    ))
    candidates = deduplicate(candidates)
    candidates.sort(
        key=lambda row: (
            -int(row["eligible"]),
            -int(row["hard_arbitrage"]),
            -float(row["score"]),
            -float(row["net_edge"]),
            str(row["source_id"]),
        )
    )
    selected = candidates[:limit]
    for rank, row in enumerate(selected, 1):
        row["rank"] = rank
    eligible = [dict(row) for row in selected if int(row["eligible"]) == 1]
    for rank, row in enumerate(eligible, 1):
        row["rank"] = rank

    atomic_csv(output, selected)
    atomic_csv(eligible_output, eligible)
    status = {
        "schema": "polymarket_global_opportunity_book_v2",
        "generated_ts": now_ts,
        "candidate_limit": limit,
        "research_candidates": len(selected),
        "eligible_candidates": len(eligible),
        "hard_arbitrage_candidates": sum(int(row["hard_arbitrage"]) for row in eligible),
        "terminal_max_age_seconds": terminal_max_age,
        "active_experts": sorted(active_experts) if active_experts is not None else [],
        "sources": {
            source: sum(str(row["source"]) == source for row in selected)
            for source in sorted({str(row["source"]) for row in selected})
        },
        "best_net_edge": max((float(row["net_edge"]) for row in eligible), default=0.0),
        "best_expected_profit": max((float(row["expected_profit"]) for row in eligible), default=0.0),
    }
    atomic_json(status_path, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
