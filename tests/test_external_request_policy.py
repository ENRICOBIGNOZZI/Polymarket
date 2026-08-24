#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "external_request_policy", ROOT / "scripts" / "external_request_policy.py"
)
assert SPEC and SPEC.loader
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


class ExternalRequestPolicyTests(unittest.TestCase):
    def test_absolute_clob_history_drops_interval(self) -> None:
        original = (
            "https://clob.polymarket.com/prices-history?"
            "market=123&startTs=1700000000&endTs=1700086400&interval=1h&fidelity=60"
        )
        rewritten = policy.rewrite_external_url(original)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(rewritten).query))
        self.assertEqual(query["market"], "123")
        self.assertEqual(query["startTs"], "1700000000")
        self.assertEqual(query["endTs"], "1700086400")
        self.assertEqual(query["fidelity"], "60")
        self.assertNotIn("interval", query)

    def test_interval_only_clob_history_is_unchanged(self) -> None:
        url = "https://clob.polymarket.com/prices-history?market=123&interval=1h&fidelity=60"
        self.assertEqual(policy.rewrite_external_url(url), url)

    def test_kalshi_market_discovery_excludes_mve(self) -> None:
        original = "https://external-api.kalshi.com/trade-api/v2/markets?status=open&limit=1000"
        rewritten = policy.rewrite_external_url(original)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(rewritten).query))
        self.assertEqual(query["status"], "open")
        self.assertEqual(query["limit"], "1000")
        self.assertEqual(query["mve_filter"], "exclude")

    def test_existing_kalshi_mve_filter_is_forced_fail_closed(self) -> None:
        original = (
            "https://external-api.kalshi.com/trade-api/v2/markets?"
            "status=open&limit=100&mve_filter=only&cursor=next"
        )
        rewritten = policy.rewrite_external_url(original)
        pairs = urllib.parse.parse_qsl(urllib.parse.urlsplit(rewritten).query)
        self.assertEqual([value for key, value in pairs if key == "mve_filter"], ["exclude"])
        self.assertIn(("cursor", "next"), pairs)

    def test_gdelt_requests_are_paced(self) -> None:
        clock = [100.0]
        sleeps: list[float] = []
        seen: list[str] = []

        def monotonic() -> float:
            return clock[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        def delegate(url: str, *, timeout: float, retries: int):
            seen.append(url)
            return {"ok": True, "timeout": timeout, "retries": retries}

        request = policy.wrap_request_json(
            delegate,
            gdelt_min_interval_seconds=1.25,
            monotonic=monotonic,
            sleep=sleep,
        )
        url = "https://api.gdeltproject.org/api/v2/doc/doc?query=test&format=json"
        request(url, timeout=30.0, retries=2)
        request(url, timeout=30.0, retries=2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(sleeps, [1.25])

    def test_non_target_url_is_unchanged(self) -> None:
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false"
        self.assertEqual(policy.rewrite_external_url(url), url)


if __name__ == "__main__":
    unittest.main()
