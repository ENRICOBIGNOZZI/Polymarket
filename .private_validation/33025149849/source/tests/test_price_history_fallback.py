from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PriceHistoryFallbackContractTest(unittest.TestCase):
    def _body(self) -> str:
        source = (ROOT / "src" / "api.cpp").read_text(encoding="utf-8")
        start = source.index("PolymarketApi::fetch_price_history")
        end = source.index("PolymarketApi::fetch_recent_trades", start)
        return source[start:end]

    def test_partial_or_empty_batch_response_falls_back_per_token(self) -> None:
        body = self._body()

        self.assertIn('/batch-prices-history', body)
        self.assertIn('/prices-history?market=', body)
        self.assertIn('before_sizes', body)
        self.assertIn('if (after_batch > before_sizes[i - pos]) continue;', body)
        self.assertLess(body.index('/batch-prices-history'), body.index('/prices-history?market='))

    def test_long_absolute_ranges_are_chunked_before_request(self) -> None:
        body = self._body()

        self.assertIn('max_history_window_seconds = 14LL * 24LL * 60LL * 60LL', body)
        self.assertIn('for (std::int64_t window_start = start_ts; window_start < end_ts;)', body)
        self.assertIn('const auto window_end = std::min(end_ts, window_start + max_history_window_seconds);', body)
        self.assertIn('{"start_ts", window_start}', body)
        self.assertIn('{"end_ts", window_end}', body)
        self.assertIn('<< "&startTs=" << window_start << "&endTs=" << window_end', body)
        self.assertIn('window_start = window_end;', body)

    def test_batch_and_single_market_failures_are_isolated_but_rate_limits_fail_closed(self) -> None:
        body = self._body()

        self.assertGreaterEqual(body.count("try {"), 2)
        self.assertIn("A malformed or transient batch response", body)
        self.assertIn("Missing history for one asset", body)
        self.assertIn('if (r.status == 429)', body)
        self.assertIn('if (one.status == 429)', body)
        self.assertIn('CLOB batch price history HTTP 429', body)
        self.assertIn('CLOB price history HTTP 429', body)


if __name__ == "__main__":
    unittest.main()
