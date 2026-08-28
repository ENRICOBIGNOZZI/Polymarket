#!/usr/bin/env python3
"""Detect post-bootstrap Polymarket market-open milestones into a causal tape.

The initial Gamma snapshot is baseline inventory, never a set of creation
events. Only markets first observed after that baseline emit market-open rows.
This is a research collector with no intent or execution authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from v7_market_open_pipeline import (
    ForwardOpenTape,
    MarketSourceAdapter,
    ingest_market_stream_event,
)

SCHEMA = "polymarket_v7_market_open_tape_v1"
STATE_SCHEMA = "polymarket_v7_market_open_collector_state_v1"
STATUS_SCHEMA = "polymarket_v7_market_open_collector_status_v1"
MILESTONES = (
    "MARKET_CREATED_OBSERVED", "BOOK_ACTIVE_OBSERVED", "FIRST_QUOTE_OBSERVED",
    "FIRST_DEPTH_OBSERVED", "FIRST_TRADE_OBSERVED",
)
BASELINE_MILESTONE = "BASELINE_MARKET"


class MarketOpenCollectorError(ValueError):
    pass


@dataclass(frozen=True)
class MarketObservation:
    market_id: str
    event_id: str
    condition_id: str
    question: str
    rules_hash: str
    source_created_ts_ms: int
    receive_ts_ms: int
    active: bool
    accepting_orders: bool
    best_bid: float | None
    best_ask: float | None
    liquidity: float
    volume: float
    token_ids: tuple[str, ...]

    def validate(self) -> None:
        if (not self.market_id or not self.condition_id or self.source_created_ts_ms <= 0
                or self.receive_ts_ms <= 0):
            raise MarketOpenCollectorError("incomplete_market_observation")
        if self.source_created_ts_ms < 0 or self.source_created_ts_ms > self.receive_ts_ms:
            raise MarketOpenCollectorError("invalid_market_creation_clock")
        if (self.best_bid is None) != (self.best_ask is None):
            raise MarketOpenCollectorError("incomplete_market_quote")
        if self.best_bid is not None and not 0.0 <= self.best_bid <= self.best_ask <= 1.0:
            raise MarketOpenCollectorError("invalid_market_quote")
        if self.liquidity < 0.0 or self.volume < 0.0:
            raise MarketOpenCollectorError("invalid_market_activity")


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _optional_probability(value: Any) -> float | None:
    result = _finite(value, math.nan)
    return result if math.isfinite(result) and 0.0 <= result <= 1.0 else None


def _array(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    return tuple(str(x).strip() for x in value if str(x).strip()) if isinstance(value, list) else ()


def _timestamp_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return 0


def observation(raw: Mapping[str, Any], receive_ts_ms: int) -> MarketObservation:
    rules = str(raw.get("rules") or raw.get("description") or "")
    resolution = str(raw.get("resolutionSource") or raw.get("resolution_source") or "")
    rules_hash = hashlib.sha256(json.dumps(
        {"rules": " ".join(rules.lower().split()), "resolution_source": resolution.strip()},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    event_ids = [str(x.get("id") or "").strip() for x in events if isinstance(x, dict)]
    row = MarketObservation(
        market_id=str(raw.get("id") or "").strip(),
        event_id=next((x for x in event_ids if x), "") or
                 str(raw.get("conditionId") or raw.get("condition_id") or "").strip(),
        condition_id=str(raw.get("conditionId") or raw.get("condition_id") or "").strip(),
        question=str(raw.get("question") or "").strip(), rules_hash=rules_hash,
        source_created_ts_ms=_timestamp_ms(raw.get("createdAt") or raw.get("created_at")),
        receive_ts_ms=int(receive_ts_ms), active=bool(raw.get("active", False)),
        accepting_orders=bool(raw.get("acceptingOrders", raw.get("accepting_orders", False))),
        best_bid=_optional_probability(raw.get("bestBid")),
        best_ask=_optional_probability(raw.get("bestAsk")),
        liquidity=max(0.0, _finite(raw.get("liquidityNum"), _finite(raw.get("liquidity")))),
        volume=max(0.0, _finite(raw.get("volumeNum"), _finite(raw.get("volume")))),
        token_ids=_array(raw.get("clobTokenIds")),
    )
    row.validate(); return row


def fetch_markets(gamma_url: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({
        "active": "true", "closed": "false", "limit": max(1, min(500, limit)),
        "offset": 0, "order": "createdAt", "ascending": "false",
    })
    request = urllib.request.Request(
        gamma_url.rstrip("/") + "/markets?" + query,
        headers={"User-Agent": "PolymarketV7MarketOpenResearch/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    rows = value if isinstance(value, list) else value.get("markets", []) if isinstance(value, dict) else []
    if not isinstance(rows, list):
        raise MarketOpenCollectorError("invalid_gamma_market_response")
    return [dict(x) for x in rows if isinstance(x, dict)]


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": STATE_SCHEMA, "initialized": False, "markets": {}}
    if value.get("schema") != STATE_SCHEMA or not isinstance(value.get("markets"), dict):
        raise MarketOpenCollectorError("market_open_state_schema_mismatch")
    return value


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _milestones(row: MarketObservation) -> tuple[str, ...]:
    result = ["MARKET_CREATED_OBSERVED"]
    if row.active and row.accepting_orders:
        result.append("BOOK_ACTIVE_OBSERVED")
    if row.best_bid is not None and row.best_ask is not None:
        result.append("FIRST_QUOTE_OBSERVED")
    if row.liquidity > 0.0:
        result.append("FIRST_DEPTH_OBSERVED")
    if row.volume > 0.0:
        result.append("FIRST_TRADE_OBSERVED")
    return tuple(result)


def event(row: MarketObservation, milestone: str) -> dict[str, Any]:
    if milestone not in MILESTONES and milestone != BASELINE_MILESTONE:
        raise MarketOpenCollectorError("unknown_market_open_milestone")
    event_id = hashlib.sha256(f"{row.market_id}:{milestone}".encode()).hexdigest()
    return {
        "schema": SCHEMA, "event_id": event_id, "milestone": milestone,
        "market_id": row.market_id, "event_group_id": row.event_id,
        "condition_id": row.condition_id, "question": row.question,
        "rules_hash": row.rules_hash, "source_created_ts_ms": row.source_created_ts_ms,
        "receive_ts_ms": row.receive_ts_ms, "best_bid": row.best_bid, "best_ask": row.best_ask,
        "liquidity": row.liquidity, "volume": row.volume, "token_ids": list(row.token_ids),
        "semantic_verification": "UNVERIFIED", "authority": "RESEARCH",
        "source_id": "polymarket_gamma_catalog",
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
    }


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise MarketOpenCollectorError("gamma_url_must_be_verified_https")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    return f"https://{parsed.hostname.lower()}{port}"


def append_tape(path: Path, rows: Sequence[Mapping[str, Any]], *, gamma_url: str) -> None:
    adapter = MarketSourceAdapter(
        "polymarket_gamma_catalog", (_origin(gamma_url),), "gamma_markets_v1", True,
    )
    tape = ForwardOpenTape(path)
    for original in rows:
        row = dict(original)
        milestone = str(row["milestone"])
        kind = "MARKET_OPEN" if milestone == "MARKET_CREATED_OBSERVED" else milestone
        if kind == "MARKET_OPEN":
            payload = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            stream = ingest_market_stream_event(
                adapter, source_url=gamma_url, source_event_id=str(row["condition_id"]),
                market_id=str(row["market_id"]), event_id=str(row["event_group_id"]),
                event_type="CREATED", published_ts_ms=int(row["source_created_ts_ms"]),
                received_ts_ms=int(row["receive_ts_ms"]), payload=payload,
                advertised_open_ts_ms=int(row["source_created_ts_ms"]),
            )
            row.update({"payload_hash": stream.payload_hash,
                        "adapter_version": adapter.adapter_version,
                        "source_event_id": stream.source_event_id})
        tape.append(kind, int(row["receive_ts_ms"]), row)


def recover_tape(path: Path) -> dict[str, dict[str, Any]]:
    recovered: dict[str, dict[str, Any]] = {}
    for record in ForwardOpenTape(path).read_verified():
            row = record["payload"]
            market_id = str(row.get("market_id") or "")
            milestone = ("MARKET_CREATED_OBSERVED" if record["kind"] == "MARKET_OPEN"
                         else str(row.get("milestone") or record["kind"] or ""))
            if not market_id or (milestone not in MILESTONES and milestone != BASELINE_MILESTONE):
                continue
            state = recovered.setdefault(market_id, {"baseline": False, "milestones": []})
            if milestone == BASELINE_MILESTONE:
                state["baseline"] = True
            else:
                state["milestones"].append(milestone)
            state.update({
                "last_receive_ts_ms": int(row.get("receive_ts_ms") or 0),
                "rules_hash": str(row.get("rules_hash") or ""),
                "condition_id": str(row.get("condition_id") or ""),
            })
    for state in recovered.values():
        state["milestones"] = sorted(set(state["milestones"]))
    return recovered


def collect_once(
    *, gamma_url: str, limit: int, tape_path: Path, state_path: Path,
    fetcher: Callable[[str, int, float], list[dict[str, Any]]] = fetch_markets,
    now_ms: int | None = None, timeout: float = 20.0,
) -> dict[str, Any]:
    received = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    state = load_state(state_path)
    markets = state["markets"]
    if tape_path.exists() and (
        not state_path.exists() or tape_path.stat().st_mtime_ns > state_path.stat().st_mtime_ns
    ):
        for market_id, recovered in recover_tape(tape_path).items():
            current = markets.get(market_id)
            if not isinstance(current, dict):
                markets[market_id] = recovered
                continue
            current["baseline"] = bool(current.get("baseline")) or bool(recovered.get("baseline"))
            current["milestones"] = sorted(set(current.get("milestones", ())).union(
                recovered.get("milestones", ())))
        if markets:
            state["initialized"] = True
    raw_rows = fetcher(gamma_url, limit, timeout)
    observations: list[MarketObservation] = []
    rejected = 0
    for raw in raw_rows:
        try:
            observations.append(observation(raw, received))
        except MarketOpenCollectorError:
            rejected += 1
    initializing = state.get("initialized") is not True
    emitted: list[dict[str, Any]] = []
    new_markets = 0
    baseline_markets = 0
    for row in observations:
        current = markets.get(row.market_id)
        if not isinstance(current, dict):
            current = {"baseline": initializing, "milestones": []}
            markets[row.market_id] = current
            if initializing:
                emitted.append(event(row, BASELINE_MILESTONE)); baseline_markets += 1
            else:
                new_markets += 1
        recorded = set(str(x) for x in current.get("milestones", []))
        for milestone in _milestones(row):
            if milestone in recorded:
                continue
            recorded.add(milestone)
            if not current.get("baseline"):
                emitted.append(event(row, milestone))
        current.update({
            "milestones": sorted(recorded), "last_receive_ts_ms": received,
            "rules_hash": row.rules_hash, "condition_id": row.condition_id,
        })
    append_tape(tape_path, emitted, gamma_url=gamma_url)
    state.update({"initialized": True, "updated_ts_ms": received, "markets": markets})
    atomic_json(state_path, state)
    return {
        "schema": STATUS_SCHEMA, "timestamp_ms": received, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "authority": "RESEARCH", "automatic_promotion": False,
        "bootstrap": initializing, "observed_markets": len(observations),
        "tracked_markets": len(markets), "new_markets": new_markets,
        "baseline_markets": baseline_markets,
        "emitted_milestones": sum(x["milestone"] != BASELINE_MILESTONE for x in emitted),
        "tape_rows_appended": len(emitted), "rejected_observations": rejected,
        "semantic_verified_markets": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)
    while True:
        try:
            result = collect_once(
                gamma_url=args.gamma_url, limit=args.limit, tape_path=args.tape,
                state_path=args.state, timeout=max(.1, args.timeout),
            )
        except Exception as exc:
            result = {
                "schema": STATUS_SCHEMA, "timestamp_ms": time.time_ns() // 1_000_000,
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "authority": "RESEARCH",
                "automatic_promotion": False, "healthy": False,
                "blocker": f"{type(exc).__name__}:{exc}",
            }
        if args.status:
            atomic_json(args.status, result)
        print(json.dumps(result, sort_keys=True), flush=True)
        if not args.loop:
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
