from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PriceHistoryFallbackContractTest(unittest.TestCase):
    def test_partial_or_empty_batch_response_falls_back_per_token(self) -> None:
        source = (ROOT / "src" / "api.cpp").read_text(encoding="utf-8")
        start = source.index("PolymarketApi::fetch_price_history")
        end = source.index("PolymarketApi::fetch_recent_trades", start)
        body = source[start:end]

        self.assertIn('/batch-prices-history', body)
        self.assertIn('/prices-history?market=', body)
        self.assertIn('auto existing = out.find(token_ids[i]);', body)
        self.assertIn('if (existing != out.end() && !existing->second.empty()) continue;', body)
        self.assertLess(body.index('/batch-prices-history'), body.index('/prices-history?market='))

        old_failure = (
            'parse_history_array(kv.value().as_array(), out[std::string(kv.key())]);\n'
            '                        }\n'
            '                    }\n'
            '                    continue;'
        )
        self.assertNotIn(old_failure, body)

    def test_batch_and_single_market_failures_are_isolated(self) -> None:
        source = (ROOT / "src" / "api.cpp").read_text(encoding="utf-8")
        start = source.index("PolymarketApi::fetch_price_history")
        end = source.index("PolymarketApi::fetch_recent_trades", start)
        body = source[start:end]

        self.assertGreaterEqual(body.count("try {"), 2)
        self.assertIn("A malformed or transient batch response", body)
        self.assertIn("Missing history for one asset", body)


if __name__ == "__main__":
    unittest.main()
