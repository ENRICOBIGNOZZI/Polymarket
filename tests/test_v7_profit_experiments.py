import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_profit_experiments import ProfitExperiments,replay_anchor,AUTH,LedgerTail,preserve_source
from v7_profit_protocol import freeze
from v7_profit_report import summarize,settlement,interval
from test_v7_causal_book import CausalBookTests,SHA

class ExperimentTests(CausalBookTests):
    def test_book_paths_are_lossless_immutable_sidecars(self):
        import gzip
        from v7_profit_protocol import digest
        root=Path(self.directory.name);source={'path':[{'sequence':i,'price':.5} for i in range(1000)]}
        reference=preserve_source(root,source);path=root/reference['source_path'];before=path.read_bytes()
        self.assertEqual(json.loads(gzip.decompress(before)),source)
        self.assertEqual(reference['source_sha256'],digest(source))
        self.assertLess(reference['source_compressed_bytes'],reference['source_uncompressed_bytes'])
        self.assertEqual(preserve_source(root,source),reference)
        self.assertEqual(path.read_bytes(),before)
        path.write_bytes(gzip.compress(b'{}'))
        with self.assertRaisesRegex(ValueError,'source hash mismatch'):preserve_source(root,source)
    def test_forward_selection_delays_restart_and_fixed_signal(self):
        root=Path(self.directory.name);self.book.retention_ms=60000
        self.seed()
        for history in self.book.history.values():
            for row in history:row.update(ask_depth_l1=10,bid_depth_l1=10)
        collector=ProfitExperiments(root,root/'study',ROOT/'config/v7_profit_experiment.json',self.book,SHA,root/'binary')
        collector.manifest=freeze(collector.manifest_path,collector.protocol,SHA,'b'*64,(self.base-600000)*1000000)
        origin={'origin_observed_wall_ns':(self.base+10)*1000000,'market_id':'market','yes_token':'yes','no_token':'no','rich_feature_sha256':'c'*64}
        fair={'yes':.7,'tte_seconds':100,'lower':0,'upper':1,'probability_interval_validated':False,'probability_model_hash':'b'*64}
        market={'fees_enabled_explicit':True,'fee_schedule':{'rate':.07,'exponent':1}}
        collector.select_signal(origin,fair,market,self.status());self.assertEqual(len(collector.selected),1)
        collector.select_signal(origin,fair,market,self.status());self.assertEqual(len(collector.selected),1)
        collector.label_signals(self.status(),time.time_ns())
        observations=[json.loads(l) for l in (root/'study/observations.jsonl').read_text().splitlines()]
        zero=next(r for r in observations if r['kind']=='DELAY_LABEL' and r['delay_ms']==0)
        later=next(r for r in observations if r['kind']=='DELAY_LABEL' and r['delay_ms']==100)
        self.assertEqual(zero['fixed_signal_probability'],later['fixed_signal_probability'])
        self.assertGreater(zero['point_net_margin'],later['point_net_margin'])
        self.assertEqual(next(r for r in observations if r.get('delay_ms')==500)['state'],'BOOK_CONTINUITY_OR_WATERMARK_CENSORED')
        restarted=ProfitExperiments(root,root/'study',ROOT/'config/v7_profit_experiment.json',self.book,SHA,root/'binary')
        restarted.select_signal(origin,fair,market,self.status());self.assertEqual(restarted.counts['SIGNAL_SELECTION'],1)
        labels={'market':{'tokens':['yes','no'],'winning_token_id':'yes'}}
        report=summarize(observations,collector.manifest,labels)
        self.assertEqual(report['resolved_selected_contracts'],1)
        estimated=[v for v in report['signal_cells'].values() if v['contracts']]
        self.assertTrue(all(v['interval'] is None for v in estimated))
        self.assertTrue(all(v['contracts']==1 for v in estimated))
        wrong=copy.deepcopy(observations);wrong[0]['origin_ns']=1
        with self.assertRaisesRegex(ValueError,'contamination'):summarize(wrong,collector.manifest,labels)

    def test_settlement_requires_actual_market_closed_and_binary_tokens(self):
        raw={'id':'123','closed':True,'clobTokenIds':'["yes","no"]','outcomePrices':'["0","1"]'}
        result=settlement('123',{'no'},lambda _:raw);self.assertEqual(result['winning_token_id'],'no')
        self.assertIsNone(settlement('123',{'other'},lambda _:raw))
        self.assertIsNone(settlement('other',{'no'},lambda _:raw))
        self.assertIsNone(settlement('123',{'no'},lambda _:{**raw,'closed':False}))
        self.assertIsNone(settlement('123',{'no'},lambda _:{**raw,'outcomePrices':'["0.01","0.99"]'}))

    def test_partial_ledger_append_does_not_lose_record(self):
        path=Path(self.directory.name)/'ledger';tail=LedgerTail(path)
        path.write_text('{"a":1}');self.assertEqual(tail.poll(),[])
        with path.open('a') as f:f.write('\n')
        self.assertEqual(tail.poll(),[{'a':1}]);self.assertEqual(tail.poll(),[])

    def test_model_change_is_not_silently_accepted(self):
        root=Path(self.directory.name)
        collector=ProfitExperiments(root,root/'study',ROOT/'config/v7_profit_experiment.json',self.book,SHA,root/'binary')
        collector.manifest=freeze(collector.manifest_path,collector.protocol,SHA,'b'*64,1)
        collector.tick({'fair':{'probability_model_hash':'c'*64}}, {'origin_observed_wall_ns':time.time_ns()}, {})
        self.assertEqual(collector.last_error,'FROZEN_MODEL_CHANGED_OBSERVATION_REJECTED')

if __name__=='__main__':unittest.main()
