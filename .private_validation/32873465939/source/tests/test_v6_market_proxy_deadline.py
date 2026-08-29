from __future__ import annotations

import gzip
import json
import tempfile
import time
import unittest
from unittest import mock
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
        # The C++ HttpClient has a 30 second request deadline. The proxy leaves
        # at least five seconds outside the bounded Gamma/discovery/book budget.
        self.assertLessEqual(proxy.GAMMA_TIMEOUT_SECONDS, 2.0)
        self.assertLessEqual(proxy.CLOB_TIMEOUT_SECONDS, 6.0)
        self.assertLessEqual(proxy.CLOB_DISCOVERY_BUDGET_SECONDS, 14.0)
        self.assertLessEqual(
            proxy.GAMMA_TIMEOUT_SECONDS
            + proxy.CLOB_DISCOVERY_BUDGET_SECONDS
            + proxy.CLOB_TIMEOUT_SECONDS,
            25.0,
        )
        self.assertLessEqual(proxy.FALLBACK_MARKETS, 300)
        self.assertGreaterEqual(proxy.BOOK_WORKERS, 4)

    def test_req_advertises_and_decodes_gzip(self) -> None:
        class Response:
            headers = {"Content-Encoding": "gzip"}

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            @staticmethod
            def read() -> bytes:
                return gzip.compress(json.dumps({"ok": True}).encode())

        with mock.patch("scripts.v6_market_proxy.urllib.request.urlopen", return_value=Response()) as open_url:
            self.assertEqual(proxy.req("https://clob.invalid/sampling-markets"), {"ok": True})

        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Accept-encoding"), "gzip")

    def test_known_cached_market_skips_gamma(self) -> None:
        p = self.make_proxy()
        p.rows = [{"id": "m1", "conditionId": "c1", "liquidityNum": 10.0}]
        p.ts = time.time()
        self.assertEqual(p.one("m1")["conditionId"], "c1")
        self.assertEqual(p.one("c1")["id"], "m1")


    @staticmethod
    def candidate(condition_id: str) -> dict[str, object]:
        return {
            "condition_id": condition_id,
            "market_slug": condition_id,
            "question": condition_id,
            "active": True,
            "closed": False,
            "archived": False,
            "accepting_orders": True,
            "enable_order_book": True,
            "tokens": [
                {"token_id": condition_id + "-yes", "outcome": "Yes"},
                {"token_id": condition_id + "-no", "outcome": "No"},
            ],
        }

    @staticmethod
    def liquid_books(condition_id: str) -> dict[str, dict[str, object]]:
        return {
            condition_id + "-yes": {
                "bids": [{"price": "0.40", "size": "100"}],
                "asks": [{"price": "0.60", "size": "100"}],
            },
            condition_id + "-no": {
                "bids": [{"price": "0.40", "size": "100"}],
                "asks": [{"price": "0.60", "size": "100"}],
            },
        }

    def test_sampling_market_candidates_paginate_without_gamma(self) -> None:
        p = self.make_proxy()
        requests: list[str] = []
        original = proxy.req

        def fake_req(url: str, payload: object = None, timeout: float = 0.0) -> object:
            del payload, timeout
            requests.append(url)
            if len(requests) == 1:
                return {
                    "data": [self.candidate("sample-1"), {"closed": True}],
                    "next_cursor": "cursor-2",
                }
            return {"data": [self.candidate("sample-2")], "next_cursor": "LTE="}

        proxy.req = fake_req
        try:
            rows = p.clob_candidates(2, "/sampling-markets", time.monotonic() + 2.0)
        finally:
            proxy.req = original

        self.assertEqual([row["condition_id"] for row in rows], ["sample-1", "sample-2"])
        self.assertTrue(all("/sampling-markets" in url for url in requests))
        self.assertIn("next_cursor=cursor-2", requests[1])

    def test_sampling_fallback_requires_books_before_legacy_markets(self) -> None:
        p = self.make_proxy()
        attempted: list[str] = []

        def fake_candidates(n: int, path: str, deadline: float) -> list[dict[str, object]]:
            del n, deadline
            attempted.append(path)
            return [self.candidate("sample" if path == "/sampling-markets" else "legacy")]

        def fake_books(candidates: list[dict[str, object]]) -> dict[str, dict[str, object]]:
            condition_id = str(candidates[0]["condition_id"])
            return {} if condition_id == "sample" else self.liquid_books(condition_id)

        p.clob_candidates = fake_candidates  # type: ignore[method-assign]
        p.books = fake_books  # type: ignore[method-assign]
        rows = p.clob_rows(1.0)

        self.assertEqual(attempted, ["/sampling-markets", "/markets"])
        self.assertEqual(p.clob_source, "clob_markets")
        self.assertEqual([row["conditionId"] for row in rows], ["legacy"])

if __name__ == "__main__":
    unittest.main()
