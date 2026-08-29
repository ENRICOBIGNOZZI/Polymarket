from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from scripts import v6_market_proxy as proxy


class V6MarketProxyDeadlineTests(unittest.TestCase):
    def make_proxy(self) -> proxy.Proxy:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        return proxy.Proxy(
            "https://gamma.invalid",
            "https://clob.invalid",
            root / "cache.json",
            root / "status.json",
        )

    def tearDown(self) -> None:
        tmp = getattr(self, "tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def test_cached_market_pages_honor_offset(self) -> None:
        p = self.make_proxy()
        p.rows = [
            {
                "id": str(i),
                "conditionId": str(i),
                "liquidityNum": 1000 - i,
            }
            for i in range(250)
        ]
        p.ts = time.time()
        p.source = "clob_fallback"

        first = p.markets({"limit": ["100"], "offset": ["0"]})
        second = p.markets({"limit": ["100"], "offset": ["100"]})
        third = p.markets({"limit": ["100"], "offset": ["200"]})
        end = p.markets({"limit": ["100"], "offset": ["300"]})

        self.assertEqual(first[0]["id"], "0")
        self.assertEqual(second[0]["id"], "100")
        self.assertEqual(third[0]["id"], "200")
        self.assertEqual(len(third), 50)
        self.assertEqual(end, [])

    def test_upstream_deadlines_fit_inside_cpp_client_deadline(self) -> None:
        # The C++ HttpClient has a 30 second request deadline. A proxy must not
        # spend most of that deadline retrying one unavailable upstream.
        self.assertLessEqual(proxy.GAMMA_TIMEOUT_SECONDS, 2.0)
        self.assertLessEqual(proxy.CLOB_TIMEOUT_SECONDS, 3.0)
        self.assertLessEqual(proxy.CLOB_DISCOVERY_BUDGET_SECONDS, 8.0)
        self.assertLessEqual(proxy.FALLBACK_MARKETS, 300)
        self.assertGreaterEqual(proxy.BOOK_WORKERS, 4)

    def test_known_cached_market_skips_gamma(self) -> None:
        p = self.make_proxy()
        p.rows = [{"id": "m1", "conditionId": "c1", "liquidityNum": 10.0}]
        p.ts = time.time()
        self.assertEqual(p.one("m1")["conditionId"], "c1")
        self.assertEqual(p.one("c1")["id"], "m1")


if __name__ == "__main__":
    unittest.main()
