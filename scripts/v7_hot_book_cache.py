#!/usr/bin/env python3
"""Read-only mmap view of the canonical local Polymarket L10 hot-book cache."""
from __future__ import annotations

from dataclasses import dataclass
import mmap
from pathlib import Path
import struct
import time

MAGIC = b"V7HBK001"
VERSION = 1
HEADER_BYTES = 64
SLOT_BYTES = 512
MAX_SLOTS = 128
MARKET_OFFSET = 64
MARKET_BYTES = 96
TOKEN_OFFSET = 160
TOKEN_BYTES = 96
BID_OFFSET = 256
ASK_OFFSET = 376
LEVEL_BYTES = 12
MAX_LEVELS = 10


@dataclass(frozen=True)
class HotBook:
    token_id: str
    market_id: str
    state_version: int
    exchange_event_ns: int
    receive_monotonic_ns: int
    receive_wall_ms: int
    tick_size: float
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    snapshot_id: str


class HotBookCacheReader:
    def __init__(self, path: Path, model_sha: str):
        if len(model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in model_sha):
            raise ValueError("exact model SHA required")
        self.path = Path(path)
        self.model_sha = model_sha
        self._file = None
        self._mmap: mmap.mmap | None = None
        self._slots = 0
        self._inode: tuple[int, int] | None = None
        self._token_slots: dict[str, int] = {}

    def close(self) -> None:
        if self._mmap is not None:
            self._mmap.close(); self._mmap = None
        if self._file is not None:
            self._file.close(); self._file = None
        self._slots = 0; self._inode = None; self._token_slots.clear()

    def _ensure_open(self) -> bool:
        try:
            stat = self.path.stat()
        except OSError:
            self.close(); return False
        identity = (stat.st_dev, stat.st_ino)
        if self._mmap is not None and identity == self._inode:
            return True
        self.close()
        try:
            handle = self.path.open("rb", buffering=0)
            view = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        except OSError:
            try: handle.close()
            except Exception: pass
            return False
        if len(view) < HEADER_BYTES or view[:8] != MAGIC:
            view.close(); handle.close(); return False
        version, slots = struct.unpack_from("<II", view, 8)
        sha = bytes(view[16:56]).decode("ascii", "ignore").rstrip("\x00")
        expected = HEADER_BYTES + (slots + 1) * SLOT_BYTES
        if version != VERSION or not 0 < slots < MAX_SLOTS or sha != self.model_sha or len(view) != expected:
            view.close(); handle.close(); return False
        self._file, self._mmap, self._slots, self._inode = handle, view, slots, identity
        self._reindex()
        return True

    def _slot_bytes(self, slot: int, attempts: int = 3) -> bytes | None:
        view = self._mmap
        if view is None or slot <= 0 or slot > self._slots:
            return None
        offset = HEADER_BYTES + slot * SLOT_BYTES
        for _ in range(attempts):
            before, = struct.unpack_from("<Q", view, offset)
            if before == 0 or before & 1:
                continue
            payload = bytes(view[offset:offset + SLOT_BYTES])
            after, = struct.unpack_from("<Q", view, offset)
            copied, = struct.unpack_from("<Q", payload, 0)
            if before == after == copied and not (after & 1):
                return payload
        return None

    @staticmethod
    def _text(payload: bytes, length_offset: int, data_offset: int, capacity: int) -> str | None:
        length = payload[length_offset]
        if length <= 0 or length > capacity:
            return None
        try:
            return payload[data_offset:data_offset + length].decode("ascii")
        except UnicodeDecodeError:
            return None

    def _reindex(self) -> None:
        self._token_slots.clear()
        for slot in range(1, self._slots + 1):
            payload = self._slot_bytes(slot)
            if payload is None:
                continue
            token = self._text(payload, 57, TOKEN_OFFSET, TOKEN_BYTES)
            if token:
                self._token_slots[token] = slot

    def read(self, token_id: str, *, maximum_age_ms: int = 250,
             now_ms: int | None = None) -> HotBook | None:
        if maximum_age_ms <= 0 or not token_id or not self._ensure_open():
            return None
        slot = self._token_slots.get(token_id)
        if slot is None:
            self._reindex(); slot = self._token_slots.get(token_id)
            if slot is None:
                return None
        payload = self._slot_bytes(slot)
        if payload is None:
            return None
        market = self._text(payload, 56, MARKET_OFFSET, MARKET_BYTES)
        token = self._text(payload, 57, TOKEN_OFFSET, TOKEN_BYTES)
        if market is None or token != token_id:
            self._reindex(); return None
        instrument, state_version = struct.unpack_from("<QQ", payload, 8)
        exchange_ns, receive_mono_ns, receive_wall_ms = struct.unpack_from("<qqq", payload, 24)
        tick_e4, = struct.unpack_from("<i", payload, 48)
        bid_count, ask_count, lineage, valid = payload[52], payload[53], payload[54], payload[55]
        if (instrument != slot or state_version <= 0 or exchange_ns <= 0 or receive_mono_ns <= 0
                or receive_wall_ms <= 0 or tick_e4 <= 0 or bid_count > MAX_LEVELS
                or ask_count > MAX_LEVELS or not lineage or not valid):
            return None
        current = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
        age = current - receive_wall_ms
        if age < -5 or age > maximum_age_ms:
            return None
        def levels(offset: int, count: int) -> tuple[tuple[float, float], ...]:
            rows = []
            for index in range(count):
                price_e4, quantity = struct.unpack_from("<iq", payload, offset + index * LEVEL_BYTES)
                if price_e4 <= 0 or quantity <= 0:
                    return ()
                rows.append((price_e4 / 10_000.0, quantity / 1_000_000.0))
            return tuple(rows)
        bids, asks = levels(BID_OFFSET, bid_count), levels(ASK_OFFSET, ask_count)
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            return None
        return HotBook(
            token, market, state_version, exchange_ns, receive_mono_ns, receive_wall_ms,
            tick_e4 / 10_000.0, bids, asks,
            f"hot-book:{token}:{state_version}:{receive_mono_ns}",
        )

    def __enter__(self) -> "HotBookCacheReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
