import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from v7_profit_cohorts import ProfitCohorts
from v7_evidence_store import EvidenceStore
from test_v7_causal_book import CausalBookTests,SHA


class CohortTests(CausalBookTests):
    def seed_depth(self):
        self.book.invalidate();self.seed()
        for history in self.book.history.values():
            for row in history:row.update(ask_depth_l1=10,bid_depth_l1=10)

    def origin(self):
        return {'origin_observed_wall_ns':(self.base+10)*1_000_000,'market_id':'market',
          'yes_token':'yes','no_token':'no','rich_feature_sha256':'c'*64,'feature_schema_version':'features-v1'}

    def fair(self,model):
        return {'fair':{'valid':True,'yes':.7,'tte_seconds':100,'lower':0,'upper':1,
          'probability_interval_validated':False,'probability_model_hash':model},
          'market':{'fees_enabled_explicit':True,'fee_schedule':{'rate':.07,'exponent':1}}}

    def test_model_independent_origin_does_not_create_invalid_model_cohort(self):
        root=Path(self.directory.name);live=root/'live';live.mkdir()
        manager=ProfitCohorts(live,root/'durable/profit',ROOT/'config/v7_profit_experiment.json',self.book,SHA,root/'binary')
        self.seed_depth()
        manager.tick({'fair':{'valid':False}},self.origin(),self.status())
        self.assertEqual(manager.cohorts,{})

    def test_new_model_new_future_cohort_and_original_data_remain_after_restart(self):
        root=Path(self.directory.name);live=root/'live';output=root/'durable/profit';live.mkdir()
        manager=ProfitCohorts(live,output,ROOT/'config/v7_profit_experiment.json',self.book,SHA,root/'binary')
        with patch('time.time_ns',return_value=(self.base-600_000)*1_000_000):
            a=manager.cohort('a'*64,'features-v1')
        self.seed_depth();manager.tick(self.fair('a'*64),self.origin(),self.status())
        a_source=a.output/'observations.jsonl';a_bytes=a_source.read_bytes()
        self.assertEqual(a.counts['SIGNAL_SELECTION'],1)
        manager.tick(self.fair('b'*64),self.origin(),self.status())
        b=next(c for c in manager.cohorts.values() if c.manifest['frozen_model_hash']=='b'*64)
        self.assertEqual(b.counts['SIGNAL_SELECTION'],0)  # no backdated preregistration
        self.assertGreater(b.manifest['forward_start_ns'],self.origin()['origin_observed_wall_ns'])
        # Simulated cutover: archive source run and restart the same collector
        # over durable cohorts, then deliver a genuinely later causal book.
        live.rename(root/'archive-A');live.mkdir()
        manager=ProfitCohorts(live,output,ROOT/'config/v7_profit_experiment.json',self.book,SHA,root/'binary')
        self.base=b.manifest['forward_start_ns']//1_000_000+5000
        with patch('time.time_ns',return_value=(self.base+5000)*1_000_000):
            self.seed_depth();manager.tick(self.fair('b'*64),self.origin(),self.status())
        self.assertEqual(a_source.read_bytes(),a_bytes)
        self.assertEqual(len(manager.cohorts),2)
        b=next(c for c in manager.cohorts.values() if c.manifest['frozen_model_hash']=='b'*64)
        self.assertEqual(b.counts['SIGNAL_SELECTION'],1)
        # Analysis can reconstruct and independently attribute both generations.
        with EvidenceStore(root/'store',chunk_bytes=127) as store:
            for c in manager.cohorts.values():
                r=store.capture(c.output/'observations.jsonl',partition=c.manifest['frozen_model_hash'],
                    relative='observations.jsonl',contract={'feature_schema':'features-v1'},append=True)
                rows=list(store.json_rows(r['revision']))
                self.assertEqual({x['model_hash'] for x in rows if x['kind']=='SIGNAL_SELECTION'},
                                 {c.manifest['frozen_model_hash']})

if __name__=='__main__':unittest.main()
