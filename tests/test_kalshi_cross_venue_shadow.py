import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "kalshi_cross_venue_shadow.py"
spec = importlib.util.spec_from_file_location("kalshi_cross_venue_shadow", SCRIPT)
matcher = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = matcher
spec.loader.exec_module(matcher)


class KalshiCrossVenueShadowTest(unittest.TestCase):
    def pm(self, question: str, *, market_id: str = "pm1", end_ts: int = 2_000_000_000):
        return matcher.PmMarket(
            market_id=market_id,
            condition_id="condition-" + market_id,
            slug="slug-" + market_id,
            question=question,
            description=question + " according to the official release.",
            end_ts=end_ts,
            liquidity=10_000.0,
            volume24h=5_000.0,
            yes_token="yes-" + market_id,
            no_token="no-" + market_id,
        )

    def kalshi(self, title: str, *, ticker: str = "K1", end_ts: int = 2_000_000_000):
        return matcher.KMarket(
            ticker=ticker,
            event_ticker="E1",
            title=title,
            subtitle="",
            rules=title + " according to the official release.",
            settlement_ts=end_ts,
            yes_bid=0.49,
            yes_ask=0.51,
            volume=10_000.0,
            open_interest=1_000.0,
        )

    def quote(self):
        return matcher.PmQuote(midpoint=0.45, spread=0.02, best_bid=0.44, best_ask=0.46)

    def match(self, pm_markets, kalshi_markets, **overrides):
        kwargs = dict(
            min_score=0.82,
            min_margin=0.05,
            max_kalshi_spread=0.08,
            max_pm_spread=0.15,
            min_confidence=0.45,
        )
        kwargs.update(overrides)
        quotes = {pm.market_id: self.quote() for pm in pm_markets}
        return matcher.match_markets(pm_markets, quotes, kalshi_markets, **kwargs)

    def test_identical_contract_generates_engine_compatible_shadow_signal(self):
        question = "Will the Federal Reserve raise rates by 25 bps after the September 2026 meeting?"
        diagnostics, signals = self.match([self.pm(question)], [self.kalshi(question)])
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["accepted"], 1)
        self.assertEqual(diagnostics[0]["rejection_reason"], "")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["market_key"], "pm1")
        self.assertEqual(signals[0]["source"], "kalshi:K1")
        self.assertAlmostEqual(signals[0]["q_yes"], 0.50)
        self.assertGreater(signals[0]["confidence"], 0.45)

    def test_different_numeric_threshold_is_rejected(self):
        pm = self.pm("Will the Federal Reserve raise rates by 25 bps after the September 2026 meeting?")
        kalshi = self.kalshi("Will the Federal Reserve raise rates by 50 bps after the September 2026 meeting?")
        similarity = matcher.contract_similarity(pm, kalshi)
        self.assertFalse(similarity.numbers_match)
        self.assertEqual(similarity.rejection, "critical_numbers_mismatch")

    def test_opposite_logical_direction_is_rejected(self):
        pm = self.pm("Will US CPI be above 3 percent in December 2026?")
        kalshi = self.kalshi("Will US CPI be below 3 percent in December 2026?")
        similarity = matcher.contract_similarity(pm, kalshi)
        self.assertFalse(similarity.orientation_match)
        self.assertEqual(similarity.rejection, "logical_orientation_mismatch")

    def test_ambiguous_duplicate_contracts_do_not_generate_signal(self):
        question = "Will OpenAI complete an IPO before December 31 2026?"
        diagnostics, signals = self.match(
            [self.pm(question)],
            [self.kalshi(question, ticker="K1"), self.kalshi(question, ticker="K2")],
        )
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["accepted"], 0)
        self.assertEqual(diagnostics[0]["rejection_reason"], "ambiguous_match")
        self.assertEqual(signals, [])

    def test_settlement_dates_more_than_ten_days_apart_are_rejected(self):
        question = "Will Bitcoin be above 100000 dollars on December 31 2026?"
        similarity = matcher.contract_similarity(
            self.pm(question, end_ts=2_000_000_000),
            self.kalshi(question, end_ts=2_000_000_000 + 11 * 86400),
        )
        self.assertEqual(similarity.rejection, "settlement_date_mismatch")

    def test_kalshi_parser_supports_2026_dollar_fields_and_no_bid_ask_parity(self):
        parsed = matcher.parse_kalshi_market(
            {
                "ticker": "KX",
                "event_ticker": "E",
                "title": "Will X happen?",
                "status": "active",
                "yes_bid_dollars": "0.4200",
                "no_bid_dollars": "0.5600",
                "volume_fp": "12.50",
                "open_interest_fp": "7.00",
                "settlement_ts": 2_000_000_000,
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertAlmostEqual(parsed.yes_bid, 0.42)
        self.assertAlmostEqual(parsed.yes_ask, 0.44)
        self.assertAlmostEqual(parsed.midpoint, 0.43)
        self.assertAlmostEqual(parsed.volume, 12.5)


if __name__ == "__main__":
    unittest.main()
