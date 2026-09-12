import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))

from v7_profit_cohorts import ProfitCohorts
from v7_profit_protocol import FIVE_MIN_NS


class FakeBook:
    gaps=0
    session='s'
    epoch=1


class ProfitCohortReplicationTests(unittest.TestCase):
    def manager(self,root):
        run=Path(root)/'run';out=Path(root)/'durable'
        run.mkdir(exist_ok=True);out.mkdir(exist_ok=True)
        return ProfitCohorts(run,out,ROOT/'config/v7_profit_experiment.json',FakeBook(),'a'*40,Path('/tmp/no-binary'))

    def origin(self,ns):
        return {'origin_observed_wall_ns':ns,'feature_schema_version':'btc-m5-rich-external-causal-v2'}

    def fair(self,model_id):
        return {'fair':{'valid':True,'probability_model_hash':'b'*64,'probability_model_id':model_id}}

    def test_old_non_residual_model_cannot_open_v6_signal_cohort(self):
        with tempfile.TemporaryDirectory() as d:
            manager=self.manager(d)
            active=manager._signal_active(self.fair('btc-m5-rich-logit-old'),self.origin(10*FIVE_MIN_NS))
            self.assertIsNone(active)
            self.assertEqual(manager.cohorts,{})

    def test_three_windows_are_frozen_contiguous_same_hash_and_restart_stable(self):
        with tempfile.TemporaryDirectory() as d:
            manager=self.manager(d)
            observed=10*FIVE_MIN_NS+1
            active=manager._signal_active(self.fair('btc-m5-rich-logit-residual-deadbeef'),self.origin(observed))
            self.assertIsNone(active)  # preregistration happens before the next boundary
            self.assertEqual(len(manager.cohorts),3)
            cohorts=sorted(manager.cohorts.values(),key=lambda c:(c.manifest['cohort_identity'])['residual_replication_index'])
            duration=8*3_600_000_000_000
            starts=[c.manifest['forward_start_ns'] for c in cohorts]
            self.assertEqual(starts,[starts[0],starts[0]+duration,starts[0]+2*duration])
            self.assertEqual({c.manifest['frozen_model_hash'] for c in cohorts},{'b'*64})
            self.assertEqual([c.manifest['cohort_identity']['residual_replication_index'] for c in cohorts],[0,1,2])
            active=manager._signal_active(self.fair('btc-m5-rich-logit-residual-deadbeef'),self.origin(starts[0]+1))
            self.assertIs(active,cohorts[0])

            restarted=self.manager(d)
            existing=sorted(restarted.cohorts.values(),key=lambda c:c.manifest['cohort_identity']['residual_replication_index'])
            self.assertEqual([c.manifest['forward_start_ns'] for c in existing],starts)
            restarted.ensure_residual_replications('b'*64,'btc-m5-rich-external-causal-v2',starts[1]+123)
            self.assertEqual(len(restarted.cohorts),3)
            self.assertEqual(sorted(c.manifest['forward_start_ns'] for c in restarted.cohorts.values()),starts)

    def test_windows_do_not_overlap(self):
        with tempfile.TemporaryDirectory() as d:
            manager=self.manager(d)
            manager._signal_active(self.fair('btc-m5-rich-logit-residual-x'),self.origin(20*FIVE_MIN_NS+1))
            cohorts=sorted(manager.cohorts.values(),key=lambda c:c.manifest['forward_start_ns'])
            for left,right in zip(cohorts,cohorts[1:]):
                self.assertEqual(left.manifest['confirmatory_end_ns'],right.manifest['forward_start_ns'])


if __name__=='__main__':unittest.main()
