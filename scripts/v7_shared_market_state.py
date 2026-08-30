#!/usr/bin/env python3
"""Read and validate the canonical C++ WebSocket market-state snapshot.

The producer writes one atomic multi-token image. Consumers may use REST for
metadata/bootstrap, but execution freshness comes from this image whenever all
required tokens have continuous WebSocket lineage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Iterable

SCHEMA = "polymarket_v7_shared_market_state_v1"
_SHA = re.compile(r"^[0-9a-f]{40}$")


class SharedStateError(ValueError):
    pass


def _finite(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SharedStateError("non_finite_number") from exc
    if not math.isfinite(result):
        raise SharedStateError("non_finite_number")
    return result


def _levels(value: Any, *, bids: bool) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    if not isinstance(value, list):
        raise SharedStateError("levels:not_array")
    for raw in value:
        if not isinstance(raw, dict):
            raise SharedStateError("level:not_object")
        price, size = _finite(raw.get("price")), _finite(raw.get("size"))
        if not 0.0 < price < 1.0 or size <= 0.0:
            raise SharedStateError("level:invalid")
        output.append((price, size))
    output.sort(key=lambda row: row[0], reverse=bids)
    if not output:
        raise SharedStateError("levels:empty")
    return output


def validate_payload(raw: dict[str, Any], *, expected_sha: str,
                     now_ms: int, max_publish_age_ms: int = 2_500) -> dict[str, Any]:
    if not _SHA.fullmatch(expected_sha):
        raise SharedStateError("expected_sha:not_exact")
    if raw.get("schema") != SCHEMA or raw.get("model_sha") != expected_sha:
        raise SharedStateError("snapshot:identity")
    if raw.get("paper_only") is not True or raw.get("authenticated_execution") is not False:
        raise SharedStateError("snapshot:safety")
    if raw.get("real_order_submission") is not False:
        raise SharedStateError("snapshot:real_order_submission")
    timestamp = int(raw.get("timestamp_ms") or 0)
    if timestamp <= 0 or timestamp > now_ms + 1_500:
        raise SharedStateError("snapshot:clock")
    if now_ms - timestamp > max(1, int(max_publish_age_ms)):
        raise SharedStateError("snapshot:stale_publish")
    snapshot_id = str(raw.get("snapshot_id") or "")
    generation = int(raw.get("generation") or 0)
    if not snapshot_id or generation < 0:
        raise SharedStateError("snapshot:sequence")
    books: dict[str, dict[str, Any]] = {}
    for item in raw.get("books", []):
        if not isinstance(item, dict):
            raise SharedStateError("book:not_object")
        token = str(item.get("token_id") or "")
        exchange = int(item.get("exchange_ts_ms") or 0)
        received = int(item.get("source_receive_ts_ms") or item.get("receive_ts_ms") or 0)
        published = int(item.get("snapshot_published_ms") or timestamp)
        version = int(item.get("state_version") or 0)
        epoch = int(item.get("lineage_epoch") or 0)
        if not token or token in books or exchange <= 0 or received <= 0 or version <= 0 or epoch < 0:
            raise SharedStateError("book:identity_or_clock")
        if (exchange > received + 1_500 or received > published + 1_500
                or published != timestamp):
            raise SharedStateError("book:causality")
        last_book_change = int(item.get("last_book_change_receive_ms") or received)
        last_trade_receive = int(item.get("last_trade_receive_ms") or 0)
        last_trade_exchange = int(item.get("last_trade_exchange_ms") or 0)
        if (last_book_change < 0 or last_book_change > published + 1_500
                or last_trade_receive < 0 or last_trade_receive > published + 1_500
                or last_trade_exchange < 0
                or (last_trade_exchange > 0 and last_trade_receive <= 0)
                or last_trade_exchange > last_trade_receive + 1_500):
            raise SharedStateError("book:event_clock")
        books[token] = {
            "token": token,
            "market_id": str(item.get("market_id") or ""),
            "condition_id": str(item.get("condition_id") or ""),
            "event_id": str(item.get("event_id") or ""),
            "outcome": str(item.get("outcome") or ""),
            "bids": _levels(item.get("bids"), bids=True),
            "asks": _levels(item.get("asks"), bids=False),
            "min_order": max(0.0, _finite(item.get("min_order_size", 0.0))),
            "tick_size": max(0.0, _finite(item.get("tick_size", 0.0))),
            "exchange_ts_ms": exchange,
            # Transport freshness and economic event time are intentionally
            # separate.  A continuously connected quiet book remains usable at
            # the newly published atomic cut, but publishing that cut is not a
            # new market observation.
            "received_ms": timestamp,
            "source_receive_ts_ms": received,
            "snapshot_published_ms": timestamp,
            "state_version": version,
            "lineage_epoch": epoch,
            "lineage_continuous": item.get("lineage_continuous") is True,
            "economic_novelty": item.get("economic_novelty") is True,
            "last_book_change_receive_ms": last_book_change,
            "last_trade_receive_ms": last_trade_receive,
            "last_trade_exchange_ms": last_trade_exchange,
            "provenance": str(item.get("provenance") or ""),
            "fee_verified": item.get("fee_verified") is True,
            "fee_rate": max(0.0, _finite(item.get("fee_rate", 0.0))),
            "fee_exponent": max(0.0, _finite(item.get("fee_exponent", 1.0))),
            "fee_taker_only": item.get("fee_taker_only") is True,
            "bus_snapshot_id": snapshot_id,
            "bus_generation": generation,
        }
    if not books:
        raise SharedStateError("snapshot:no_books")
    return {
        "snapshot_id": snapshot_id,
        "generation": generation,
        "timestamp_ms": timestamp,
        "model_sha": expected_sha,
        "books": books,
    }


def load_snapshot(path: Path, *, expected_sha: str, now_ms: int | None = None,
                  max_publish_age_ms: int = 2_500) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SharedStateError("snapshot:unreadable") from exc
    if not isinstance(raw, dict):
        raise SharedStateError("snapshot:not_object")
    return validate_payload(
        raw, expected_sha=expected_sha,
        now_ms=now_ms if now_ms is not None else time.time_ns() // 1_000_000,
        max_publish_age_ms=max_publish_age_ms,
    )


def synchronized_books(snapshot: dict[str, Any], tokens: Iterable[str], *,
                       require_continuous: bool = True) -> dict[str, dict[str, Any]]:
    wanted = list(dict.fromkeys(str(token) for token in tokens if str(token)))
    books = snapshot.get("books") if isinstance(snapshot.get("books"), dict) else {}
    selected = {token: books[token] for token in wanted if token in books}
    if len(selected) != len(wanted):
        raise SharedStateError("bundle:missing_token")
    ids = {str(book.get("bus_snapshot_id") or "") for book in selected.values()}
    if len(ids) != 1 or "" in ids:
        raise SharedStateError("bundle:not_atomic")
    if require_continuous and any(not book.get("lineage_continuous") for book in selected.values()):
        raise SharedStateError("bundle:lineage_not_continuous")
    return selected


@dataclass
class SharedStateCursor:
    generation: int = -1
    versions: dict[str, tuple[int, int]] = field(default_factory=dict)

    def accept(self, snapshot: dict[str, Any]) -> None:
        generation = int(snapshot.get("generation") or 0)
        if generation < self.generation:
            raise SharedStateError("snapshot:generation_regression")
        for token, book in snapshot.get("books", {}).items():
            epoch = int(book.get("lineage_epoch") or 0)
            version = int(book.get("state_version") or 0)
            prior = self.versions.get(token)
            if prior is not None and epoch < prior[0]:
                raise SharedStateError("book:epoch_regression")
            if prior is not None and epoch == prior[0] and version < prior[1]:
                raise SharedStateError("book:version_regression")
            self.versions[token] = (epoch, version)
        self.generation = generation
