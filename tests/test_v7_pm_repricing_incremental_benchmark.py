import pathlib, sys, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_pm_repricing_incremental_benchmark import pm_features, raw_features, chronological_split

class IncrementalBenchmarkTests(unittest.TestCase):
    def row(self):
        def book(token,imb):
            return {'token_id':token,'best_bid':.49 if token=='Y' else .50,'best_ask':.50 if token=='Y' else .51,
                    'bid_depth_l1':100.,'ask_depth_l1':80.,'tick_size':.01,
                    'placement_features':{'imbalance':imb,'ofi':.2,'short_return_ticks':.1,'ew_vol_ticks':.3,
                        'trade_intensity':.4,'cancel_intensity':.5,'aggressive_buy_prints_per_second':.6,
                        'aggressive_sell_prints_per_second':.2}}
        return {'market_id':'m','yes_token':'Y','no_token':'N','origin_pm_yes':.495,
                'origin_book_cuts':[book('Y',.1),book('N',-.1)],
                'rich_model_features':{'return_100ms_bp':3.0,'binance_perp_basis_bp':2.0}}

    def test_pm_and_external_feature_domains_are_separate(self):
        row=self.row(); pm=pm_features(row)
        self.assertIsNotNone(pm); self.assertIn('yes_imbalance',pm); self.assertNotIn('return_100ms_bp',pm)
        only_pm=raw_features(row,'PM_MICRO_ONLY'); external=raw_features(row,'EXTERNAL_ONLY')
        combined=raw_features(row,'PM_PLUS_EXTERNAL')
        self.assertTrue(all(not k.startswith('ext__') for k in only_pm))
        self.assertTrue(all(k.startswith('ext__') for k in external))
        self.assertIn('pm__yes_imbalance',combined); self.assertIn('ext__return_100ms_bp',combined)
    def test_chronological_split_keeps_whole_markets(self):
        rows=[]
        for i in range(30):
            for h in (100,250):
                rows.append({'market_id':f'm{i:02d}','origin_observed_wall_ns':i+1,'horizon_ms':h})
        parts=chronological_split(rows)
        sets={k:{r['market_id'] for r in v} for k,v in parts.items()}
        self.assertFalse(sets['train'] & sets['validation'])
        self.assertFalse(sets['train'] & sets['audit'])
        self.assertFalse(sets['validation'] & sets['audit'])
        self.assertEqual(set.union(*sets.values()),{f'm{i:02d}' for i in range(30)})

if __name__=='__main__': unittest.main()
