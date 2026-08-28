#!/usr/bin/env python3
"""Read-only Kalshi market/book collector for the canonical V7 cross sleeve.

The adapter uses Kalshi's documented unauthenticated REST market-data surface.
REST observations are explicitly labelled POLLING; they are never represented
as exchange event latency.  No mapping, order, balance or execution authority
is inferred from a healthy transport.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import random
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from v7_semantic_mapping import SemanticMappingError, load_verified_mappings


STATUS_SCHEMA = "polymarket_v7_external_input_component_status_v1"
TAPE_SCHEMA = "polymarket_v7_cross_platform_book_tape_v1"
STATE_SCHEMA = "polymarket_v7_cross_platform_collector_state_v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class CrossCollectorError(ValueError):
    pass


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def git_head(repository_root: Path) -> str:
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository_root, text=True,
            stderr=subprocess.STDOUT, timeout=10,
        ).strip().lower()
    except (OSError, subprocess.SubprocessError) as exc:
        raise CrossCollectorError("repository_head_unavailable") from exc
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise CrossCollectorError("repository_head_invalid")
    return value


class PersistentJsonClient:
    """Bounded HTTPS JSON client with connection reuse and measured TTFB."""

    def __init__(self, base_url: str, *, timeout: float = 10.0):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise CrossCollectorError("venue_base_must_be_https")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.prefix = parsed.path.rstrip("/")
        self.timeout = max(0.1, float(timeout))
        self.connection: http.client.HTTPSConnection | None = None
        self.connection_epoch = 0
        self.reconnect_count = 0

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _connect(self) -> http.client.HTTPSConnection:
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout)
            self.connection_epoch += 1
        return self.connection

    def get(self, path: str) -> tuple[Any, dict[str, Any]]:
        if not path.startswith("/"):
            raise CrossCollectorError("venue_path_must_be_absolute")
        started = time.monotonic_ns()
        try:
            connection = self._connect()
            connection.request("GET", self.prefix + path, headers={
                "Accept": "application/json", "Accept-Encoding": "identity",
                "User-Agent": "PolymarketV7Research/1.0",
                "Connection": "keep-alive",
            })
            response = connection.getresponse()
            first_byte = time.monotonic_ns()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            completed = time.monotonic_ns()
            if len(body) > MAX_RESPONSE_BYTES:
                raise CrossCollectorError("venue_response_too_large")
            if response.status != 200:
                raise CrossCollectorError(f"venue_http_status:{response.status}")
            try:
                value = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CrossCollectorError("venue_invalid_json") from exc
            return value, {
                "request_start_monotonic_ns": started,
                "first_byte_monotonic_ns": first_byte,
                "complete_monotonic_ns": completed,
                "ttfb_ms": (first_byte - started) / 1_000_000.0,
                "request_ms": (completed - started) / 1_000_000.0,
                "connection_epoch": self.connection_epoch,
                "body_sha256": hashlib.sha256(body).hexdigest(),
            }
        except (OSError, http.client.HTTPException):
            self.close(); self.reconnect_count += 1
            raise


def _fixed(value: Any) -> float:
    if isinstance(value, bool):
        raise CrossCollectorError("invalid_orderbook_number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CrossCollectorError("invalid_orderbook_number") from exc
    if result < 0.0:
        raise CrossCollectorError("negative_orderbook_number")
    return result


def normalize_kalshi_orderbook(value: Any) -> dict[str, list[list[float]]]:
    if not isinstance(value, dict):
        raise CrossCollectorError("invalid_orderbook_envelope")
    book = value.get("orderbook_fp") or value.get("orderbook")
    if not isinstance(book, dict):
        raise CrossCollectorError("orderbook_missing")
    yes = book.get("yes_dollars") if isinstance(book.get("yes_dollars"), list) else book.get("yes")
    no = book.get("no_dollars") if isinstance(book.get("no_dollars"), list) else book.get("no")
    if not isinstance(yes, list) or not isinstance(no, list):
        raise CrossCollectorError("orderbook_sides_missing")

    def levels(rows: Sequence[Any]) -> list[list[float]]:
        output = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise CrossCollectorError("invalid_orderbook_level")
            price, quantity = _fixed(row[0]), _fixed(row[1])
            if price > 1.0: price /= 100.0
            if not 0.0 <= price <= 1.0 or quantity <= 0.0:
                raise CrossCollectorError("invalid_orderbook_level")
            output.append([price, quantity])
        return output

    yes_bids, no_bids = levels(yes), levels(no)
    yes_asks = [[1.0 - price, quantity] for price, quantity in no_bids]
    yes_bids.sort(key=lambda row: row[0], reverse=True)
    yes_asks.sort(key=lambda row: row[0])
    if yes_bids and yes_asks and yes_bids[0][0] >= yes_asks[0][0]:
        raise CrossCollectorError("crossed_or_locked_venue_book")
    return {"yes_bids": yes_bids, "yes_asks": yes_asks,
            "no_bids": sorted(no_bids, key=lambda row: row[0], reverse=True)}


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": STATE_SCHEMA, "sequences": {}, "last_hash": "0" * 64}
    if value.get("schema") != STATE_SCHEMA or not isinstance(value.get("sequences"), dict):
        raise CrossCollectorError("collector_state_invalid")
    return value


def append_record(path: Path, state: dict[str, Any], payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema": TAPE_SCHEMA, "previous_hash": state.get("last_hash", "0" * 64), **dict(payload)}
    record_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**body, "record_hash": record_hash}, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush(); os.fsync(handle.fileno())
    state["last_hash"] = record_hash
    return record_hash


def _venues(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrossCollectorError("external_input_config_unreadable") from exc
    if (
        value.get("schema") != "polymarket_v7_external_inputs_v1"
        or value.get("version") != 7 or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
    ):
        raise CrossCollectorError("external_input_config_safety_invalid")
    section = value.get("cross_platform") if isinstance(value.get("cross_platform"), dict) else {}
    rows = section.get("venues") if isinstance(section.get("venues"), list) else []
    enabled = [row for row in rows if isinstance(row, dict) and row.get("enabled") is True]
    if len(enabled) != 1 or enabled[0].get("venue") != section.get("second_venue"):
        raise CrossCollectorError("exactly_one_second_venue_required")
    venue = enabled[0]
    if venue.get("venue") != "kalshi" or venue.get("rest_authentication_required") is not False:
        raise CrossCollectorError("unsupported_or_authenticated_second_venue")
    return section, venue


def collect_once(*, repository_root: Path, config_path: Path, mappings_path: Path,
                 tape_path: Path, state_path: Path, status_path: Path,
                 client: PersistentJsonClient | Any | None = None,
                 now_ms: int | None = None) -> dict[str, Any]:
    timestamp = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    monotonic = time.monotonic_ns()
    sha = git_head(repository_root)
    section, venue = _venues(config_path)
    verified = load_verified_mappings(mappings_path, "cross_platform", now_ms=timestamp,
                                      repository_sha=sha)
    own_client = client is None
    client = client or PersistentJsonClient(str(venue["public_rest_base"]))
    state = _load_state(state_path)
    state["updated_at_ms"] = timestamp
    discovered = synced = gaps = parse_failures = dropped = 0
    blocker = ""
    transport_state = "DISCONNECTED"
    latency_samples: list[float] = []
    try:
        limit = max(1, min(1000, int(section.get("max_discovery_markets") or 20)))
        market_data, timing = client.get(f"/markets?limit={limit}&status=open")
        latency_samples.append(float(timing.get("request_ms") or 0.0))
        markets = market_data.get("markets") if isinstance(market_data, dict) else None
        if not isinstance(markets, list):
            raise CrossCollectorError("venue_market_discovery_schema_invalid")
        tickers = []
        for market in markets:
            if not isinstance(market, dict):
                parse_failures += 1; continue
            ticker = str(market.get("ticker") or "")
            if ticker:
                tickers.append(ticker)
                append_record(tape_path, state, {
                    "kind": "MARKET_METADATA", "venue": "kalshi", "contract_id": ticker,
                    "received_at_ms": timestamp, "receive_monotonic_ns": monotonic,
                    "transport": "PUBLIC_REST_POLLING", "source_event_time": None,
                    "polling_latency_not_event_latency": True,
                    "metadata_hash": hashlib.sha256(json.dumps(market, sort_keys=True).encode()).hexdigest(),
                    "connection_epoch": timing.get("connection_epoch", 0),
                    "repository_sha": sha,
                })
        discovered = len(tickers)
        for ticker in tickers:
            try:
                raw, book_timing = client.get(
                    "/markets/" + urllib.parse.quote(ticker, safe="") + "/orderbook?depth=100"
                )
                latency_samples.append(float(book_timing.get("request_ms") or 0.0))
                book = normalize_kalshi_orderbook(raw)
                sequence = int(state["sequences"].get(ticker) or 0) + 1
                state["sequences"][ticker] = sequence
                append_record(tape_path, state, {
                    "kind": "ORDERBOOK_SNAPSHOT", "venue": "kalshi", "contract_id": ticker,
                    "poll_sequence": sequence, "received_at_ms": timestamp,
                    "receive_monotonic_ns": time.monotonic_ns(), "transport": "PUBLIC_REST_POLLING",
                    "source_event_time": None, "polling_latency_not_event_latency": True,
                    "book": book, "body_sha256": book_timing.get("body_sha256", ""),
                    "connection_epoch": book_timing.get("connection_epoch", 0),
                    "repository_sha": sha,
                })
                synced += 1
            except Exception:
                parse_failures += 1
        transport_state = "OPERATIONAL" if synced > 0 else "DEGRADED"
        if not verified:
            blocker = "BLOCKED_NO_VERIFIED_EQUIVALENCE"
    except Exception as exc:
        blocker = f"BLOCKED_SECOND_VENUE_DOWN:{type(exc).__name__}:{exc}"
        transport_state = "DOWN"
    finally:
        if own_client and hasattr(client, "close"):
            client.close()
    atomic_json(state_path, state)
    mapping_active = len(verified) > 0
    status = {
        "schema": STATUS_SCHEMA, "version": 7, "family": "cross_platform",
        "authority": "RESEARCH", "model_sha": sha, "timestamp_ms": timestamp,
        "timestamp": timestamp // 1000, "process_state": "RUNNING",
        "evidence_state": "ACTIVE" if transport_state == "OPERATIONAL" and mapping_active else "BLOCKED_EXTERNAL",
        "implementation_complete": True, "feed_operational": transport_state == "OPERATIONAL",
        "feed_status": transport_state, "mapping_pipeline": True,
        "mapping_status": "VERIFIED_EQUIVALENCES_ACTIVE" if mapping_active else "NO_VERIFIED_EQUIVALENCE",
        "verified_mappings": len(verified), "forward_collection_active": synced > 0,
        "forward_opportunity_tape_active": bool(synced and mapping_active),
        "book_synchronization_active": synced > 0, "fee_model_verified": False,
        "joint_execution_simulator": True, "second_venue": "kalshi",
        "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
        "discovered_markets": discovered, "synchronized_books": synced,
        "feed_age_ms": 0 if synced else None, "last_sequence": max(state["sequences"].values(), default=0),
        "connection_epoch": int(getattr(client, "connection_epoch", 0)),
        "reconnect_count": int(getattr(client, "reconnect_count", 0)),
        "gap_count": gaps, "parse_failure_count": parse_failures,
        "dropped_event_count": dropped, "request_latency_ms": latency_samples,
        "blocker": blocker, "reason_codes": [blocker] if blocker else [],
        "last_attempt_ts": timestamp // 1000,
        "last_success_ts": timestamp // 1000 if transport_state == "OPERATIONAL" else 0,
        "paper_only": True, "research_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False,
        "capital_authority": False, "oms_authority": False,
        "ledger_write_authority": False, "promotion_authority": False,
    }
    atomic_json(status_path, status)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, default=Path("config/v7_external_inputs.json"))
    parser.add_argument("--mappings", type=Path, default=Path("config/v7_external_mappings.json"))
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)
    failures = 0
    while True:
        status = collect_once(
            repository_root=args.repository_root, config_path=args.config,
            mappings_path=args.mappings, tape_path=args.tape, state_path=args.state,
            status_path=args.status,
        )
        print(json.dumps(status, sort_keys=True), flush=True)
        if not args.loop:
            return 0
        failures = failures + 1 if status["feed_status"] == "DOWN" else 0
        delay = min(60.0, max(1.0, args.interval) * (2 ** min(failures, 4)))
        time.sleep(delay * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    raise SystemExit(main())
