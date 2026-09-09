import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_profit_inference import interval
from v7_profit_signal_analysis import summarize_signal,confirmatory

PROTOCOL=json.loads((Path(__file__).resolve().parents[1]/'config/v7_profit_experiment.json').read_text())

class InferenceTests(unittest.TestCase):
    def test_extreme_bonferroni_tail_is_suppressed_instead_of_pseudo_precision(self):
        result=interval([float(i%2) for i in range(150)],PROTOCOL,400)
        self.assertEqual(result['state'],'INSUFFICIENT_MONTE_CARLO_TAIL_RESOLUTION')
        self.assertEqual(result['expected_draws_in_each_adjusted_tail'],1.25)
        self.assertIsNone(result['interval'])

    def test_consecutive_contract_dependence_needs_enough_blocks(self):
        result=interval([.1]*30,PROTOCOL,3)
        self.assertEqual(result['state'],'INSUFFICIENT_TEMPORAL_BLOCKS')
        self.assertIsNone(result['interval'])
        # Three long contiguous regimes: uncertainty must reflect block order,
        # not pretend the 120 contracts are IID quote-level observations.
        values=[-1.]*40+[0.]*40+[1.]*40
        result=interval(values,PROTOCOL,3)
        self.assertLess(result['interval'][0],0)
        self.assertGreater(result['interval'][1],0)
        self.assertEqual(set(result['block_sensitivity_intervals']),{'3','6','12'})
        self.assertGreater(result['block_sensitivity_intervals']['12'][1],result['block_sensitivity_intervals']['3'][1])

    def test_contract_equal_weight_delay_pair_counts_and_missing_regimes(self):
        selected={};delays={}
        for market,count,p in [('A',10,.8),('B',1,.6)]:
            for i in range(count):
                key=market+str(i);row={'market_id':market,'selection_key':key,'origin_ns':1000+i,'token_id':'yes',
                    'outcome':'YES','margin_bin':1,'tte_bin':1,'model_probability':p,'pm_probability':.5,
                    'origin_book':{'best_bid':.4,'best_ask':.5,'ask_depth_l1':10,'tick_size':.01,'features_valid':False,
                        'placement_features':{'ew_vol_ticks':0}}}
                selected[key]=row
                for delay in [0,100,250,500,1000]:
                    ask=.5+delay/100000
                    delays[(key,delay)]={'state':'OBSERVED','fixed_signal_probability':p,'book_cut':{'best_ask':ask},
                        'fee_per_share':.01,'risk_allowance_per_share':.001,'point_net_margin':p-ask-.011}
        manifest={'protocol':PROTOCOL,'forward_start_ns':100,'confirmatory_end_ns':10000}
        labels={m:{'tokens':['yes','no'],'winning_token_id':'yes'} for m in ['A','B']}
        result=summarize_signal(selected,delays,manifest,labels,5000)
        self.assertAlmostEqual(result['cells']['ALL']['brier_model']['mean'],.1)
        pair=result['fixed_signal_delay']['ALL']['EV_change_0_to_100']
        self.assertEqual(pair['observations'],11)
        self.assertEqual(pair['contracts'],2)
        self.assertAlmostEqual(pair['mean'],-.001)
        self.assertEqual(result['input_coverage']['volatility_ticks|missing'],11)
        self.assertEqual(result['cells']['external_disagreement_bp|UNKNOWN']['brier_model']['contracts'],2)
        frozen=confirmatory(result['primary_contract_values'],result['contract_origin_ns'],manifest,5000)
        self.assertEqual(frozen['state'],'FORWARD_WINDOW_OPEN')
        self.assertNotIn('interval',frozen['endpoints']['model_brier_improvement_over_pm'])
        changed=copy.deepcopy(delays);changed[('A0',100)]['fixed_signal_probability']=.9
        with self.assertRaisesRegex(ValueError,'frozen probability'):summarize_signal(selected,changed,manifest,labels,5000)

if __name__=='__main__':unittest.main()
