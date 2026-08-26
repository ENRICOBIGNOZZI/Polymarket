from __future__ import annotations

import importlib.util
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "v6_market_proxy.py"
SPEC = importlib.util.spec_from_file_location("v6_market_proxy_reconciled", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
proxy_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy_mod)


def market(mid: str, liquidity: float = 100.0) -> dict[str, object]:
    return {
        "id": mid,
        "conditionId": f"condition-{mid}",
        "liquidityNum": liquidity,
    }


def cache_payload(mid: str, timestamp: int) -> dict[str, object]:
    row = market(mid)
    return {
        "schema": proxy_mod.CACHE_SCHEMA,
        "timestamp": timestamp,
        "markets": [row],
        "gamma_to_condition": {mid: row["conditionId"]},
    }


def write_atomic(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_name(path.name + ".incoming")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class V6AtomicCacheReloadTests(unittest.TestCase):
    def test_atomic_replacement_reloads_even_when_mtime_does_not_increase(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "market_proxy_cache.json"
            status = root / "market_proxy_status.json"
            now = int(time.time())
            write_atomic(cache, cache_payload("old", now))
            first_stat = cache.stat()

            proxy = proxy_mod.Proxy("https://gamma.invalid", "https://clob.invalid", cache, status)
            self.assertEqual(proxy.rows[0]["id"], "old")

            replacement = cache.with_name("relay.json")
            replacement.write_text(json.dumps(cache_payload("new", now)) + "\n", encoding="utf-8")
            # Reproduce the production failure: a new inode can carry an mtime equal
            # to or older than the inode the long-running proxy already observed.
            older_ns = max(1, first_stat.st_mtime_ns - 1_000_000_000)
            os.utime(replacement, ns=(older_ns, older_ns))
            os.replace(replacement, cache)
            second_stat = cache.stat()
            self.assertNotEqual(first_stat.st_ino, second_stat.st_ino)
            self.assertLessEqual(second_stat.st_mtime_ns, first_stat.st_mtime_ns)

            page = proxy.cached_page(100, 0, 0.0, proxy_mod.STALE_CACHE_SECONDS)
            self.assertIsNotNone(page)
            assert page is not None
            self.assertEqual([row["id"] for row in page], ["new"])

    def test_future_dated_relay_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "market_proxy_cache.json"
            status = root / "market_proxy_status.json"
            now = int(time.time())
            write_atomic(cache, cache_payload("good", now))
            proxy = proxy_mod.Proxy("https://gamma.invalid", "https://clob.invalid", cache, status)

            write_atomic(
                cache,
                cache_payload("future", now + int(proxy_mod.MAX_CACHE_FUTURE_SKEW_SECONDS) + 300),
            )
            self.assertFalse(proxy.load())
            self.assertEqual(proxy.rows[0]["id"], "good")

    def test_local_save_does_not_hide_concurrent_relay_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "market_proxy_cache.json"
            status = root / "market_proxy_status.json"
            now = int(time.time())
            write_atomic(cache, cache_payload("initial", now))
            proxy = proxy_mod.Proxy("https://gamma.invalid", "https://clob.invalid", cache, status)

            original_atomic = proxy_mod._impl.atomic

            def racing_atomic(path: Path, obj: object) -> None:
                original_atomic(path, obj)
                write_atomic(path, cache_payload("relay-wins", int(time.time())))

            proxy_mod._impl.atomic = racing_atomic
            try:
                proxy.save([market("local")])
            finally:
                proxy_mod._impl.atomic = original_atomic

            self.assertIsNone(proxy.cache_signature)
            page = proxy.cached_page(100, 0, 0.0, proxy_mod.STALE_CACHE_SECONDS)
            self.assertIsNotNone(page)
            assert page is not None
            self.assertEqual([row["id"] for row in page], ["relay-wins"])

    def test_reconciliation_preserves_nonblocking_stale_cache_reader(self) -> None:
        self.assertTrue(hasattr(proxy_mod.Proxy, "_refresh_stale_cache_in_background"))

    def test_runtime_server_enables_reuseaddr_for_rapid_validated_restart(self) -> None:
        self.assertIs(proxy_mod._impl.ThreadingHTTPServer, proxy_mod.ReusableThreadingHTTPServer)
        self.assertTrue(proxy_mod.ReusableThreadingHTTPServer.allow_reuse_address)
        server = proxy_mod.ReusableThreadingHTTPServer(
            ("127.0.0.1", 0), proxy_mod.H, bind_and_activate=False
        )
        try:
            server.server_bind()
            self.assertEqual(
                server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR),
                1,
            )
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
