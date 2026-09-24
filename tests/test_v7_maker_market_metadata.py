from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from research.walk_forward_v3.maker_market_metadata import identify_context, observed_markets


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

    def test_raw_causal_book_fallback_discovers_market(self):
        import json,tempfile
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"paper"
            books=root/"research"/"repricing_book"/"book_observations"
            books.mkdir(parents=True)
            row={
                "schema":"polymarket_v7_causal_book_observation_v1",
                "paper_only":True,"authenticated_execution":False,
                "real_order_submission":False,
                "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
                "receive_wall_ms":2000,
                "market_id":"m1","token_id":"t1",
            }
            (books/"current.jsonl").write_text(json.dumps(row)+"\n",encoding="utf-8")
            found,diag=observed_markets(root,1_000_000_000)
            self.assertIn("m1",found)
            self.assertEqual(found["m1"]["tokens_seen"],["t1"])
            self.assertEqual(diag["source"],"RAW_CAUSAL_BOOK")

    def test_raw_causal_book_fallback_reads_gzip_segment(self):
        import gzip,json,tempfile
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"paper"
            books=root/"research"/"repricing_book"/"book_observations"
            books.mkdir(parents=True)
            row={
                "schema":"polymarket_v7_causal_book_observation_v1",
                "paper_only":True,"authenticated_execution":False,
                "real_order_submission":False,
                "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
                "receive_wall_ms":2000,
                "market_id":"m-gz","token_id":"t-gz",
            }
            with gzip.open(books/"segment.jsonl.gz","wt",encoding="utf-8") as handle:
                handle.write(json.dumps(row)+"\n")
            found,diag=observed_markets(root,1_000_000_000)
            self.assertIn("m-gz",found)
            self.assertEqual(found["m-gz"]["tokens_seen"],["t-gz"])
            self.assertEqual(diag["source"],"RAW_CAUSAL_BOOK")

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
