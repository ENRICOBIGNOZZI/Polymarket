from __future__ import annotations
import gzip, json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT/'scripts'
if str(SCRIPTS) not in sys.path: sys.path.insert(0,str(SCRIPTS))
import v7_archive_crypto_universe as archive

SHA='d'*40


def live_universe(ts_ms: int=1_000_000) -> dict:
    rows=[]
    for i,(asset,horizon) in enumerate((('BTC','M5'),('ETH','M15'),('SOL','M5'),('XRP','M15')),1):
        rows.append({
            'market_id':str(i),'condition_id':f'c{i}','asset':asset,'horizon':horizon,
            'settlement_semantic_hash':str(i)*64,'clob_token_ids':[f'y{i}',f'n{i}'],
        })
    return {
        'schema':archive.SOURCE_SCHEMA,'timestamp_ms':ts_ms,'model_sha':SHA,
        'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
        'execution_authority':False,'markets':rows,
    }


class CryptoUniverseArchiveTest(unittest.TestCase):
    def test_load_requires_exact_sha_and_crypto_identity(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(archive.time,'time_ns',return_value=1_000_000*1_000_000):
            path=Path(tmp)/'current.json'; path.write_text(json.dumps(live_universe()))
            value=archive.load_crypto_universe(path,model_sha=SHA,maximum_age_seconds=180)
            self.assertEqual({row['asset'] for row in value['markets']},{'BTC','ETH','SOL','XRP'})
            with self.assertRaisesRegex(ValueError,'crypto_universe_sha'):
                archive.load_crypto_universe(path,model_sha='e'*40,maximum_age_seconds=180)

    def test_noncrypto_asset_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(archive.time,'time_ns',return_value=1_000_000*1_000_000):
            value=live_universe(); value['markets'][0]['asset']='SPORTS'
            path=Path(tmp)/'current.json'; path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,'crypto_universe_identity'):
                archive.load_crypto_universe(path,model_sha=SHA,maximum_age_seconds=180)

    def test_archive_has_no_discovery_authority(self):
        source=live_universe()
        value=archive.archive_value(source,model_sha=SHA,captured_ts_ms=1_000_100)
        self.assertEqual(value['schema'],archive.ARCHIVE_SCHEMA)
        self.assertEqual(value['source'],'canonical_live_crypto_universe')
        self.assertFalse(value['execution_authority'])
        self.assertEqual(value['market_count'],4)
        self.assertEqual(value['assets'],['BTC','ETH','SOL','XRP'])
        self.assertEqual(len(value['membership_sha256']),64)

    def test_archive_is_immutable_and_latest_matches(self):
        value=archive.archive_value(live_universe(),model_sha=SHA,captured_ts_ms=2_000_000)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=archive.write_archive(root,value); same=archive.write_archive(root,value)
            self.assertEqual(path,same)
            latest=json.loads(gzip.decompress((root/'latest.json.gz').read_bytes()).decode())
            self.assertEqual(latest,value)

    def test_workflow_archives_live_crypto_universe_not_gamma(self):
        text=(ROOT/'.github/workflows/v7-point-in-time-universe-archive.yml').read_text()
        self.assertIn('v7_archive_crypto_universe.py',text)
        self.assertIn('runs/paper_v7_live/universe/current.json',text)
        self.assertIn('polymarket_v7_point_in_time_crypto_universe_v1',text)
        self.assertNotIn('/markets/keyset',text)
        self.assertNotIn('v7_archive_market_universe.py',text)

    def test_cpp_api_has_no_global_discover_markets_entrypoint(self):
        header=(ROOT/'include/pm/api.hpp').read_text()
        source=(ROOT/'src/api.cpp').read_text()
        self.assertNotIn('discover_markets(',header)
        self.assertNotIn('PolymarketApi::discover_markets',source)
        self.assertNotIn('/markets/keyset',source)


if __name__=='__main__': unittest.main()
