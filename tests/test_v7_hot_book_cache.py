from __future__ import annotations

import mmap
from pathlib import Path
import struct
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_hot_book_cache import HotBookCacheReader, HEADER_BYTES, SLOT_BYTES


def fixture(path: Path, *, sequence: int = 2, valid: int = 1, lineage: int = 1,
            receive_wall_ms: int | None = None) -> str:
    token = "82557822733541051116003757150499858511"
    market = "4618098"
    slots = 2
    payload = bytearray(HEADER_BYTES + (slots + 1) * SLOT_BYTES)
    payload[:8] = b"V7HBK001"
    struct.pack_into("<II", payload, 8, 1, slots)
    payload[16:56] = b"a" * 40
    base = HEADER_BYTES + SLOT_BYTES
    struct.pack_into("<QQ", payload, base, sequence, 1)
    struct.pack_into("<Q", payload, base + 16, 77)
    struct.pack_into("<qqq", payload, base + 24, 1_789_650_000_123_000, 123_456_789,
                     receive_wall_ms if receive_wall_ms is not None else time.time_ns() // 1_000_000)
    struct.pack_into("<i", payload, base + 48, 10)
    payload[base + 52:base + 58] = bytes([2, 2, lineage, valid, len(market), len(token)])
    payload[base + 64:base + 64 + len(market)] = market.encode()
    payload[base + 160:base + 160 + len(token)] = token.encode()
    struct.pack_into("<iqiq", payload, base + 256, 4990, 3_000_000, 4980, 5_000_000)
    struct.pack_into("<iqiq", payload, base + 376, 5010, 4_000_000, 5020, 6_000_000)
    struct.pack_into("<q", payload, base + 496, 5_000_000)
    path.write_bytes(payload)
    return token


def test_valid_snapshot_and_exact_l10() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "book.bin"; token = fixture(path)
        with HotBookCacheReader(path, "a" * 40) as reader:
            book = reader.read(token, maximum_age_ms=100)
            assert book is not None
            assert book.market_id == "4618098" and book.state_version == 77
            assert book.bids == ((.499, 3.0), (.498, 5.0))
            assert book.asks == ((.501, 4.0), (.502, 6.0))
            assert book.tick_size == .001
            assert book.min_order_size == 5.0


def test_odd_sequence_lineage_invalid_and_stale_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "book.bin"
        token = fixture(path, sequence=3)
        assert HotBookCacheReader(path, "a" * 40).read(token) is None
        token = fixture(path, lineage=0)
        assert HotBookCacheReader(path, "a" * 40).read(token) is None
        token = fixture(path, receive_wall_ms=(time.time_ns() // 1_000_000) - 1_000)
        assert HotBookCacheReader(path, "a" * 40).read(token, maximum_age_ms=100) is None


def test_wrong_sha_and_truncated_file_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "book.bin"; token = fixture(path)
        assert HotBookCacheReader(path, "b" * 40).read(token) is None
        path.write_bytes(path.read_bytes()[:-1])
        assert HotBookCacheReader(path, "a" * 40).read(token) is None
