import copy
from decimal import Decimal
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_profit_attribution import analyze, read_sources, opportunity_funnel, learning_coverage

SHA='a'*40

def fixture():
    def record(kind, identity, **values):
        return {'event_type':kind,'record_id':identity,'paper_only':True,'authenticated_execution':False,
            'model_sha':SHA,'position_id':'position','market_id':'market','event_id':'event','token_id':'NO-token',
            'order_id':'order','side':'BUY','metadata':{},**values}
    order=record('ORDER_SUBMITTED','o',intended_size=3,limit_price=.2,decision_ts_ms=1000,
        metadata={'component':'crypto_informed_taker','point_probability':.3,
                  'probability_model_id':'frozen','probability_model_hash':'b'*64})
    one=record('FILL','f1',fill_id='f1',filled_size=1,fill_price=.2,fee=.01)
    two=record('FILL','f2',fill_id='f2',filled_size=2,fill_price=.25,fee=.02)
    final=record('FINAL','final',final_pnl=2.27,fee=0,capital_cost=0,latency_cost=0,unwind_loss=0,
        metadata={'component':'crypto_informed_taker','entry_debit':.73,'settlement_payout':3,
                  'winning_token_id':'NO-token','paper_bootstrap_probe':True})
    return [order,one,two,final]

class AttributionTests(unittest.TestCase):
    def test_exact_embedded_taker_receipt_joins_namespaced_replay_without_guessing(self):
        values=fixture();order=values[0];order['candidate_id']='candidate'
        item={'model_sha':SHA,'market_id':'market','token_id':'NO-token','replay_key':'crypto:BTC:candidate'}
        receipt={'selected_replay_key':item['replay_key'],'opportunity_inputs':[item]}
        order['metadata']['coordinator_receipt']=receipt
        result=opportunity_funnel(values,[receipt])
        self.assertEqual(result['distinct_opportunities'],1)
        self.assertEqual(result['with_operational_fills'],1)
        self.assertEqual(result['stages']['settled'],1)
        self.assertEqual(result['stages']['profitable_unambiguously_attributed'],1)
        item['token_id']='wrong-token'
        self.assertEqual(opportunity_funnel(values,[receipt])['distinct_opportunities'],2)

    def test_explicit_replay_conflict_fails_and_unobserved_arrival_stays_missing(self):
        values=fixture();order=values[0]
        item={'model_sha':SHA,'market_id':'market','token_id':'NO-token','replay_key':'key'}
        order['metadata'].update(opportunity_replay_key='other',coordinator_receipt={
            'selected_replay_key':'key','opportunity_inputs':[item]})
        with self.assertRaisesRegex(ValueError,'identity conflict'):opportunity_funnel(values)
        order['metadata'].pop('opportunity_replay_key')
        detail=analyze(values)['positions'][0]['fill_details'][0]
        self.assertIsNone(detail['arrival_probability'])
        self.assertIsNone(detail['arrival_pm_probability'])
        self.assertEqual(detail['order_record_id'],'o')

    def test_markout_coverage_counts_fill_ids_not_observer_attempts(self):
        values=fixture();values[0]['metadata']['component']='professional_maker'
        mark={**values[1],'event_type':'MARKOUT','markouts':{'1s':-.01},'filled_size':None}
        coverage=learning_coverage(values+[mark,mark])['fill_conditioned_markout_coverage']
        self.assertEqual(coverage['1s']['observed'],1)
        self.assertEqual(coverage['1s']['positive_quantity_fills'],2)
        self.assertEqual(coverage['5s']['missing_or_nonfinite_after_horizon'],2)
    def test_legacy_conservative_bound_is_not_mistaken_for_point_forecast(self):
        values=fixture();values[0]['metadata'].update(fair_yes=.6,outcome='NO',point_probability=.1,
            pm_mid=.7,arrival_pm_mid=.65,fair_lower=.2,fair_upper=.9)
        result=analyze(values)['positions'][0]
        self.assertEqual(result['predicted_margin'],Decimal('.5'))
        self.assertEqual(result['fill_details'][0]['probability'],Decimal('.4'))
        self.assertEqual(result['fill_details'][0]['decision_pm_probability'],Decimal('.3'))
        self.assertEqual(result['fill_details'][0]['arrival_pm_probability'],Decimal('.35'))
        self.assertEqual(result['fill_details'][0]['probability_lower'],Decimal('.1'))
        self.assertEqual(result['fill_details'][0]['probability_upper'],Decimal('.8'))
    def test_partial_fills_reconcile_actual_no_token_and_fees_once(self):
        report=analyze(fixture());p=report['positions'][0]
        self.assertEqual(p['predicted_margin'],Decimal('.2'))
        self.assertEqual(p['outcome_surprise'],Decimal('2.1'))
        self.assertEqual(p['costs'],Decimal('.03'))
        self.assertEqual(p['decomposition_pnl'],Decimal('2.27'))
        self.assertEqual(p['ledger_reconciliation_residual'],0)
        self.assertTrue(p['paper_probe'])
        self.assertEqual(report['reconciled_positions'],1)

    def test_missing_probability_is_not_inferred_from_pnl(self):
        values=fixture();values[0]['metadata'].pop('point_probability')
        p=analyze(values)['positions'][0]
        self.assertTrue(p['reconciled_to_microdollar'])
        self.assertIsNone(p['predicted_margin'])
        self.assertIn('MISSING_DECISION_PROBABILITY',p['missing_or_inconsistent'])

    def test_missing_costs_and_wrong_payout_prevent_complete_accounting_claim(self):
        values=fixture();values[-1]['fee']=None
        p=analyze(values)['positions'][0]
        self.assertFalse(p['reconciled_to_microdollar'])
        self.assertIsNone(p['costs'])
        values=fixture();values[-1]['final_pnl']=4
        p=analyze(values)['positions'][0]
        self.assertIn('CASH_IDENTITY_NOT_RECONCILED',p['missing_or_inconsistent'])

    def test_checkpoint_overlap_deduplicates_and_conflicts_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=Path(tmp)/'prefix.jsonl';b=Path(tmp)/'full.jsonl.gz'
            rows=fixture();a.write_text(''.join(json.dumps(row)+'\n' for row in rows[:2]))
            with gzip.open(b,'wt') as stream:stream.write(''.join(json.dumps(row)+'\n' for row in rows))
            unique,sources=read_sources([a,b]);self.assertEqual(len(unique),4)
            self.assertEqual(analyze(unique)['canonical_final_pnl'],Decimal('2.27'))
            bad=copy.deepcopy(rows[0]);bad['intended_size']=100;a.write_text(json.dumps(bad)+'\n')
            with self.assertRaisesRegex(ValueError,'conflicting'):read_sources([a,b])

    def test_other_tokens_and_counterfactuals_do_not_become_position_returns(self):
        values=fixture();values[1]['token_id']='YES-token'
        p=analyze(values)['positions'][0]
        self.assertNotIn('decomposition_pnl',p)
        self.assertIn('NOT_A_SINGLE_TOKEN_BUY_POSITION',p['missing_or_inconsistent'])
        values=fixture()
        for row in values:row['metadata']['counterfactual']=True
        self.assertEqual(analyze(values)['canonical_final_positions'],0)

    def test_attempts_are_not_distinct_opportunities(self):
        rows=fixture();rows[0]['opportunity_id']='key'
        cut={'opportunity_inputs':[{'model_sha':SHA,'replay_key':'key'}],'selected_replay_key':'key'}
        attempt={'model_sha':SHA,'replay_key':'key','reason':'OLD_SELECTION'}
        result=opportunity_funnel(rows,[cut,cut,cut],[attempt,attempt])
        self.assertEqual(result['distinct_opportunities'],1)
        group=result['opportunities'][0]
        self.assertEqual(group['coordinator_attempts'],3)
        self.assertEqual(group['authorization_rejection_attempts'],2)
        self.assertEqual(len(group['fill_ids']),2)
        self.assertEqual(group['operational_filled_shares'],3)

if __name__=='__main__':unittest.main()
