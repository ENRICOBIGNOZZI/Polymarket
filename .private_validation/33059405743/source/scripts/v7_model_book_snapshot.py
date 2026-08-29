#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence


@dataclass(frozen=True)
class CausalBook:
    token_id: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    min_order: float
    exchange_ts_ms: int
    received_ts_ms: int
    snapshot_hash: str

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)


@dataclass(frozen=True)
class SnapshotValidation:
    ok: bool
    reason: str
    snapshot_set_id: str | None
    token_count: int
    oldest_exchange_age_ms: int | None
    oldest_receive_age_ms: int | None
    exchange_skew_ms: int | None
    receive_skew_ms: int | None


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def normalize_exchange_timestamp_ms(value: Any) -> int | None:
    """Normalize seconds/ms/us/ns source timestamps without rejuvenating invalid clocks."""
    raw = finite(value)
    if not math.isfinite(raw) or raw <= 0:
        return None
    if raw < 1e11:       # seconds
        raw *= 1_000.0
    elif raw < 1e14:     # milliseconds
        pass
    elif raw < 1e17:     # microseconds
        raw /= 1_000.0
    elif raw < 1e20:     # nanoseconds
        raw /= 1_000_000.0
    else:
        return None
    out = int(raw)
    return out if out > 0 else None


def _levels(rows: Any, *, reverse: bool) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        price = finite(row.get("price"))
        size = finite(row.get("size"), 0.0)
        if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
            out.append((price, size))
    out.sort(reverse=reverse)
    return out


def parse_causal_book(row: dict[str, Any], *, received_ts_ms: int) -> CausalBook | None:
    token = str(row.get("asset_id") or row.get("token_id") or "").strip()
    bids = _levels(row.get("bids"), reverse=True)
    asks = _levels(row.get("asks"), reverse=False)
    exchange_ts_ms = normalize_exchange_timestamp_ms(row.get("timestamp"))
    snapshot_hash = str(row.get("hash") or row.get("book_hash") or row.get("snapshot_hash") or "").strip()
    if not token or not bids or not asks or exchange_ts_ms is None or not snapshot_hash:
        return None
    bid, bid_size = bids[0]
    ask, ask_size = asks[0]
    if bid >= ask:
        return None
    min_order = max(1.0, finite(row.get("min_order_size"), 1.0))
    return CausalBook(
        token_id=token,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        min_order=min_order,
        exchange_ts_ms=exchange_ts_ms,
        received_ts_ms=int(received_ts_ms),
        snapshot_hash=snapshot_hash,
    )


def fetch_causal_books(
    clob: str,
    tokens: Iterable[str],
    request_json: Callable[[str, Any], Any],
    *,
    batch_size: int = 80,
) -> dict[str, CausalBook]:
    unique = list(dict.fromkeys(str(token) for token in tokens if str(token)))
    out: dict[str, CausalBook] = {}
    for index in range(0, len(unique), max(1, int(batch_size))):
        batch = unique[index : index + max(1, int(batch_size))]
        raw = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in batch])
        received_ts_ms = time.time_ns() // 1_000_000
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            parsed = parse_causal_book(row, received_ts_ms=received_ts_ms)
            if parsed is not None:
                out[parsed.token_id] = parsed
    return out


def validate_coherent_books(
    books: dict[str, CausalBook],
    required_tokens: Sequence[str],
    *,
    now_ms: int | None = None,
    max_age_ms: int = 5_000,
    max_exchange_skew_ms: int = 1_500,
    max_receive_skew_ms: int = 1_500,
) -> SnapshotValidation:
    tokens = tuple(dict.fromkeys(str(token) for token in required_tokens if str(token)))
    now = int(now_ms if now_ms is not None else time.time_ns() // 1_000_000)
    if not tokens:
        return SnapshotValidation(False, "empty_required_token_set", None, 0, None, None, None, None)
    missing = [token for token in tokens if token not in books]
    if missing:
        return SnapshotValidation(False, "missing_required_book", None, len(tokens), None, None, None, None)
    selected = [books[token] for token in tokens]
    if any(not book.snapshot_hash for book in selected):
        return SnapshotValidation(False, "missing_snapshot_hash", None, len(tokens), None, None, None, None)
    exchange = [book.exchange_ts_ms for book in selected]
    receive = [book.received_ts_ms for book in selected]
    if any(value <= 0 for value in exchange):
        return SnapshotValidation(False, "missing_exchange_clock", None, len(tokens), None, None, None, None)
    if any(value <= 0 for value in receive):
        return SnapshotValidation(False, "missing_receive_clock", None, len(tokens), None, None, None, None)
    if any(value > now for value in exchange):
        return SnapshotValidation(False, "future_exchange_clock", None, len(tokens), None, None, None, None)
    if any(value > now for value in receive):
        return SnapshotValidation(False, "future_receive_clock", None, len(tokens), None, None, None, None)
    exchange_ages = [now - value for value in exchange]
    receive_ages = [now - value for value in receive]
    oldest_exchange_age = max(exchange_ages)
    oldest_receive_age = max(receive_ages)
    exchange_skew = max(exchange) - min(exchange)
    receive_skew = max(receive) - min(receive)
    if oldest_exchange_age > max(0, int(max_age_ms)):
        return SnapshotValidation(False, "stale_exchange_book", None, len(tokens), oldest_exchange_age, oldest_receive_age, exchange_skew, receive_skew)
    if oldest_receive_age > max(0, int(max_age_ms)):
        return SnapshotValidation(False, "stale_receive_book", None, len(tokens), oldest_exchange_age, oldest_receive_age, exchange_skew, receive_skew)
    if exchange_skew > max(0, int(max_exchange_skew_ms)):
        return SnapshotValidation(False, "cross_book_exchange_skew", None, len(tokens), oldest_exchange_age, oldest_receive_age, exchange_skew, receive_skew)
    if receive_skew > max(0, int(max_receive_skew_ms)):
        return SnapshotValidation(False, "cross_book_receive_skew", None, len(tokens), oldest_exchange_age, oldest_receive_age, exchange_skew, receive_skew)
    identity_rows = [
        {
            "token_id": book.token_id,
            "hash": book.snapshot_hash,
            "exchange_ts_ms": book.exchange_ts_ms,
            "received_ts_ms": book.received_ts_ms,
            "bid": book.bid,
            "ask": book.ask,
        }
        for book in sorted(selected, key=lambda item: item.token_id)
    ]
    digest = hashlib.sha256(json.dumps(identity_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return SnapshotValidation(True, "coherent_causal_snapshot", digest, len(tokens), oldest_exchange_age, oldest_receive_age, exchange_skew, receive_skew)
