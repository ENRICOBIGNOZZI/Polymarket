import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_economic_decision_report import AUTH,diagnose,quality_metric,memo,ranked_regions


def fixture():
    fill={'price':'0.5','quantity':'2','probability':'.6','arrival_probability':None,'model_hash':'m',
          'placement_action':'JOIN','placement_features':None,'fill_timestamp_ms':1000,'markouts':[]}
    positions=[{'component':'professional_maker','market_id':'M','ledger_final_pnl':'-3','gross_trading_pnl':'-2',
                'costs':'1','predicted_margin':'.2','outcome_surprise':'-2.2','fill_details':[fill]},
               {'component':'crypto_informed_taker','market_id':'T','ledger_final_pnl':'1','gross_trading_pnl':'1',
                'costs':'0','predicted_margin':'.2','outcome_surprise':'.8','fill_details':[]}]
    attribution={**AUTH,'positions':positions,'recorded_at_ns':10_000_000_000,'source_code_shas':['a'*40],
        'canonical_final_pnl':'-2','canonical_final_positions':2,'reconciled_positions':2,'unattributed_ledger_pnl':'0',
        'strata_by_component_probe_model':{},'opportunity_funnel':{},
        'maker_outcomes_by_sha':{'a'*40:{'NO_OPPOSITE_FLOW':12,'QUEUE_NOT_DEPLETED':3,'FILLED':1}}}
    cohort={'manifest_sha256':'m'*64,'settlement_model_hash':'s'*64,'protocol_id':'p',
            'selected_contracts':2,'resolved_selected_contracts':1,'censored_labels':{'GAP':3},
            'maker_coverage':{'JOIN_5S|BOOK_CONTINUITY_CENSORED':3}}
    return attribution,{'cohorts':[cohort]}


class DecisionReportTests(unittest.TestCase):
    def test_archived_results_remain_visible_without_becoming_current_cash(self):
        from v7_profit_attribution import analyze
        from test_v7_profit_attribution import fixture as ledger_fixture
        attribution,experiments=fixture();historical=analyze(ledger_fixture())
        report=diagnose(attribution,experiments,{},historical=historical)
        json.dumps(report,allow_nan=False)
        self.assertEqual(report['canonical']['net_pnl_usd'],'-2')
        self.assertEqual(str(report['historical_canonical']['net_pnl_usd']),'2.27')
        self.assertIn('MAKER_EXECUTION',report['intervention_ranking']['provisional_next_test'])
        self.assertIn('Archived evidence remains visible',memo(report))
        corrupted=copy.deepcopy(historical);corrupted['canonical_final_pnl']='1000'
        with self.assertRaisesRegex(ValueError,'historical position population'):
            diagnose(attribution,experiments,{},historical=corrupted)

    def test_legacy_final_without_fills_remains_unknown_and_reportable(self):
        from v7_profit_attribution import analyze
        from test_v7_profit_attribution import fixture as ledger_fixture
        final=ledger_fixture()[-1];final['final_pnl']=-2
        attribution=analyze([final]);report=diagnose(attribution,{'cohorts':[]},{})
        self.assertEqual(str(report['canonical']['net_pnl_usd']),'-2')
        self.assertIsNone(report['canonical']['gross_pnl_usd'])
        self.assertIsNone(report['canonical']['costs_usd'])
        metric=report['data_quality']['metrics']['unreconciled_positions']
        self.assertEqual(metric['count'],1)
        self.assertEqual(metric['affected_contracts'],1)
        self.assertIsNone(metric['affected_economic_notional_usd'])
        self.assertEqual(report['diagnosis'][-1]['finding'],'NOT_RULED_OUT')

    def test_inconsistent_canonical_population_is_not_published_as_a_diagnosis(self):
        attribution,experiments=fixture();attribution['canonical_final_pnl']='999'
        with self.assertRaisesRegex(ValueError,'reconcile'):diagnose(attribution,experiments,{})
        attribution,experiments=fixture();attribution['positions'].append(copy.deepcopy(attribution['positions'][0]))
        with self.assertRaisesRegex(ValueError,'reconcile'):diagnose(attribution,experiments,{})

    def test_region_ranking_excludes_tiny_winners_and_preserves_registration_limits(self):
        signal={'registration_status':'POST_HOC_EXPLORATORY_GRID_ON_PRESERVED_LEGACY_PROTOCOL',
            'cells':{name:{'brier_improvement_over_pm':{'mean':mean,'contracts':n}} for name,mean,n in
                     [('ALL',.4,30),('tte|0',.02,20),('volatility_ticks|0',-.03,18),('margin|9',.99,2)]}}
        result=ranked_regions(signal)
        self.assertEqual(result['strongest'][0]['region'],'tte|0')
        self.assertEqual(result['weakest'][0]['region'],'volatility_ticks|0')
        self.assertEqual(result['weakest'][0]['definition_status'],signal['registration_status'])
        self.assertEqual(result['eligible_cells'],2)

    def test_priority_follows_observed_loss_component_and_cost_identity(self):
        attribution,experiments=fixture();r=diagnose(attribution,experiments,{})
        self.assertIn('MAKER_EXECUTION',r['intervention_ranking']['provisional_next_test'])
        self.assertEqual(r['canonical']['net_pnl_usd'],'-2')
        self.assertEqual(r['diagnosis'][-1]['finding'],'RULED_OUT_IN_THIS_SAMPLE')
        changed=copy.deepcopy(attribution)
        changed['positions'][0]['component']='crypto_informed_taker'
        changed['positions'][1]['component']='professional_maker'
        r2=diagnose(changed,experiments,{})
        self.assertIn('PM_VS_FROZEN_MODEL',r2['intervention_ranking']['provisional_next_test'])
        self.assertNotEqual(r2['diagnosis'][0]['finding'],r['diagnosis'][0]['finding'])
        cost_only=copy.deepcopy(attribution);cost_only['positions'][0]['gross_trading_pnl']='2'
        cost_only['positions'][0]['costs']='5'
        r3=diagnose(cost_only,experiments,{})
        self.assertIn('COST_STRESS',r3['intervention_ranking']['provisional_next_test'])
        self.assertEqual(r3['diagnosis'][-1]['finding'],'NOT_RULED_OUT')

    def test_missing_probabilities_have_exact_exposure_but_not_yet_due_markouts_are_not_missing(self):
        a,e=fixture();report=diagnose(a,e,{})
        metric=report['data_quality']['metrics']['missing_arrival_probability']
        self.assertEqual(metric['count'],1);self.assertEqual(metric['fraction'],1)
        self.assertEqual(metric['affected_economic_notional_usd'],'1.0')
        self.assertEqual(metric['first_occurrence_ms'],1000)
        self.assertEqual(report['data_quality']['metrics']['missing_maker_markout_30s']['count'],0)
        self.assertIsNone(report['data_quality']['metrics']['missing_maker_markout_30s']['fraction'])
        self.assertEqual(len(report['maker_profit_causes']),8)
        self.assertFalse(report['automatic_promotion'])
        self.assertIn('11. **Do not change yet:**',memo(report))

    def test_unknown_exposure_and_reset_counters_are_not_fabricated(self):
        previous=quality_metric(10,20,scope='session-A')
        reset=quality_metric(2,20,scope='session-B',previous=previous)
        self.assertIsNone(reset['affected_economic_notional_usd'])
        self.assertIsNone(reset['first_occurrence_ms'])
        self.assertIsNone(reset['trend']['delta_since_previous_report'])
        next_value=quality_metric(13,20,scope='session-A',previous=previous)
        self.assertEqual(next_value['trend']['delta_since_previous_report'],3)


if __name__=='__main__':unittest.main()
