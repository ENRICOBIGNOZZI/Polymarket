from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from research.walk_forward_v3.maker_market_metadata import identify_context


class MakerMarketMetadataTests(unittest.TestCase):
    def registry(self):
        return {
            "contexts":[
                {"enabled":True,"asset":"BTC","horizon":"M5","horizon_seconds":300,
                 "polymarket":{"slug_kind":"UNIX_WINDOW","slug_prefix":"btc","horizon_slug":"5m"}},
                {"enabled":True,"asset":"BTC","horizon":"H1","horizon_seconds":3600,
                 "polymarket":{"slug_kind":"HOURLY_ET","slug_prefix":"bitcoin","horizon_slug":"1h"}},
                {"enabled":True,"asset":"BTC","horizon":"D1","horizon_seconds":86400,
                 "polymarket":{"slug_kind":"DAILY_ET","slug_prefix":"bitcoin","horizon_slug":"1d"}},
            ]
        }

    def test_unix_window_context_and_time(self):
        ctx,start,end=identify_context(
            "btc-updown-5m-1790285100",self.registry(),
            {"endDate":"2026-09-24T21:30:00Z"})
        self.assertEqual((ctx["asset"],ctx["horizon"]),("BTC","M5"))
        self.assertEqual(start,1790285100)
        self.assertEqual(end-start,300)

    def test_hourly_and_daily_context_use_static_horizon(self):
        ctx,start,end=identify_context(
            "bitcoin-up-or-down-september-24-2026-5pm-et",self.registry(),
            {"endDate":"2026-09-24T22:00:00Z"})
        self.assertEqual(ctx["horizon"],"H1")
        self.assertEqual(end-start,3600)
        ctx,start,end=identify_context(
            "bitcoin-up-or-down-on-september-25-2026",self.registry(),
            {"endDate":"2026-09-25T16:00:00Z"})
        self.assertEqual(ctx["horizon"],"D1")
        self.assertEqual(end-start,86400)


if __name__=="__main__":
    unittest.main()
