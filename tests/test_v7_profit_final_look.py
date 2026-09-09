import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from v7_profit_protocol import freeze
from v7_profit_experiments import AUTH,ProfitExperiments
from v7_profit_report import report_cohort,settlement,final_window_audit


class FinalLookTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);protocol=json.loads((ROOT/'config/v7_profit_experiment.json').read_text())
        self.manifest=freeze(self.root/'manifest.json',protocol,'a'*40,'b'*64,1_700_000_000_000_000_000)
        self.start=self.manifest['forward_start_ns']+1_000_000_000
        self.now=self.manifest['confirmatory_end_ns']+1_000_000_000
        self.common={**AUTH,'code_sha':'a'*40,'manifest_sha256':self.manifest['manifest_sha256'],'market_id':'m','token_id':'Y','recorded_ns':self.start}
        selected={**self.common,'kind':'SIGNAL_SELECTION','selection_key':'s','origin_ns':self.start,
            'model_hash':'b'*64,'model_probability':.7,'pm_probability':.5,'outcome':'YES','margin_bin':1,'tte_bin':1,
            'origin_book':{'best_bid':.49,'best_ask':.51,'tick_size':.01,'ask_depth_l1':10,'features_valid':False}}
        self.observations=[selected]+[{**self.common,'kind':'DELAY_LABEL','selection_key':'s','delay_ms':d,
            'state':'OBSERVED','fixed_signal_probability':.7,'book_cut':{'best_ask':.51},
            'fee_per_share':.01,'risk_allowance_per_share':.001,'point_net_margin':.179} for d in (0,100,250,500,1000)]
        raw={'id':'m','closed':True,'clobTokenIds':['Y','N'],'outcomePrices':[1.,0.]}
        with patch('time.time_ns',return_value=self.now):self.labels={'m':settlement('m',{'Y'},lambda _:raw)}
        self.write()

    def write(self):
        (self.root/'observations.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in self.observations))
        (self.root/'settlements.json').write_text(json.dumps(self.labels))

    def seal(self,pending=None,now=None):
        owner=SimpleNamespace(manifest=self.manifest,output=self.root,pending=pending or {},maker_pending={})
        ProfitExperiments.seal_confirmatory_window(owner,self.now if now is None else now)

    def report(self,offline=False,producer_seal=True):
        if producer_seal and not offline:self.seal()
        with patch('time.time_ns',return_value=self.now):
            return report_cohort(self.root,fetch_labels=not offline,request_budget=[0])

    def test_final_is_frozen_once_and_later_data_cannot_reselect_the_look(self):
        first=self.report();path=self.root/'confirmatory_final.json';original=path.read_bytes()
        self.assertEqual(first['confirmatory']['state'],'IMMUTABLE_FINAL_PUBLISHED')
        self.assertFalse(first['confirmatory']['subsequent_window_input_changed'])
        self.assertFalse(first['confirmatory']['automatic_promotion'])
        # A later origin from the already-ended window changes the audit scope.
        extra=copy.deepcopy(self.observations)
        for row in extra:row['selection_key']='late-source'
        extra[0]['origin_ns']+=1_000_000
        self.observations+=extra;self.write()
        second=self.report()
        self.assertEqual(path.read_bytes(),original)
        self.assertTrue(second['confirmatory']['subsequent_window_input_changed'])
        self.assertEqual(second['confirmatory']['final_sha256'],first['confirmatory']['final_sha256'])
        self.assertEqual(len(second['confirmatory']['frozen_contract_values']['model_brier_improvement_over_pm']),1)

    def test_incomplete_or_unverified_sources_wait_without_an_inferential_look(self):
        self.observations.pop();self.write();report=self.report()
        self.assertFalse((self.root/'confirmatory_final.json').exists())
        self.assertEqual(len(report['confirmatory']['coverage_audit']['missing_delay_labels']),1)
        self.assertNotIn('interval',report['confirmatory']['endpoints']['model_brier_improvement_over_pm'])
        self.labels['m']['source_sha256']='c'*64
        audit=final_window_audit(self.observations,self.manifest,self.labels,self.now)
        self.assertEqual(audit['invalid_settlement_contracts'],['m'])

    def test_producer_must_close_the_drained_window_before_the_final_look(self):
        self.seal(pending={'s':self.observations[0]})
        path=self.root/'confirmatory_window_closure.json';self.assertFalse(path.exists())
        self.seal(now=self.manifest['confirmatory_end_ns']-1);self.assertFalse(path.exists())
        result=self.report(producer_seal=False)
        self.assertFalse(result['confirmatory']['coverage_audit']['producer_window_closure_verified'])
        self.assertFalse((self.root/'confirmatory_final.json').exists())
        self.seal();self.assertTrue(path.exists())
        self.assertTrue(self.report()['confirmatory']['coverage_audit']['producer_window_closure_verified'])

    def test_terminal_censor_is_frozen_but_prevents_confirmatory_interval(self):
        self.observations[-1].update(state='BOOK_GAP_CENSORED',point_net_margin=None,book_cut=None)
        self.write();report=self.report()
        self.assertEqual(report['confirmatory']['final_analysis_state'],'FINAL_CAUSAL_COVERAGE_INCOMPLETE')
        self.assertEqual(report['confirmatory']['coverage_audit']['signal_primary_censored_origins'],1)
        self.assertTrue(all('interval' not in x for x in report['confirmatory']['endpoints'].values()))

    def test_offline_analysis_never_publishes_a_final_and_frozen_corruption_fails(self):
        self.report(offline=True);self.assertFalse((self.root/'confirmatory_final.json').exists())
        self.report();path=self.root/'confirmatory_final.json';value=json.loads(path.read_text());value['frozen_at_ns']=0
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'checksum'):self.report(offline=True)


if __name__=='__main__':unittest.main()
