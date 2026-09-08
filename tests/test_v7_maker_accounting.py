from pathlib import Path
import copy, sys, unittest, tempfile
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_execution_ledger import LedgerEvent
from v7_maker_accounting import project_maker,settlement_event
SHA='a'*40

def event(kind,market='m1',quantity=5.,fill='f1'):
    metadata={'paper_exploration':True,'economic_authority':'PAPER_EXPLORATION',
        'paper_bootstrap_probe':True,'coordinator_receipt':{
            'owner':'V7_GLOBAL_PORTFOLIO_COORDINATOR','action':'MAKE','paper_only':True,
            'paper_exploration_authorized':True,'authenticated_execution':False,'real_order_submission':False}}
    return LedgerEvent(event_type=kind,strategy='MICRO_MAKER_PRO',model_sha=SHA,
        recorded_ts_ms=1000,record_id=kind+'-'+market+'-'+fill,order_id='1',
        market_id=market,event_id='e1',token_id=market+'-yes',side='BUY',
        intended_action='MAKE',intended_size=5.,fill_id=fill if kind=='FILL' else None,
        fill_price=.4 if kind=='FILL' else None,filled_size=quantity if kind=='FILL' else None,
        fee=0. if kind=='FILL' else None,fee_rate=0.,fee_source='POLYMARKET_MAKER_ZERO',
        decision_ts_ms=1000,receive_ts_ms=1000,exchange_ts_ms=999,
        book_snapshot_id='book',limit_price=.4,metadata=metadata)

class MakerAccountingTests(unittest.TestCase):
    def test_fill_debits_shared_cash_and_creates_inventory(self):
        p=project_maker([event('ORDER_SUBMITTED'),event('FILL')],{})
        self.assertEqual(p['issues'],[]);self.assertEqual(p['entry_debit'],2.)
        self.assertEqual(len(p['positions']),1);self.assertEqual(p['marked_open_value'],0.)
        self.assertEqual(p['pending_orders'],0)
    def test_same_old_native_id_different_market_never_collides(self):
        es=[event(k,m) for m in ['m1','m2'] for k in ['ORDER_SUBMITTED','FILL']]
        p=project_maker(es,{})
        self.assertEqual(p['issues'],[]);self.assertEqual(p['fills'],2)
        self.assertEqual(len(p['positions']),2);self.assertEqual(p['entry_debit'],4.)
    def test_partial_fills_have_separate_positions(self):
        es=[event('ORDER_SUBMITTED'),event('FILL',quantity=2.,fill='p1'),event('FILL',quantity=3.,fill='p2')]
        p=project_maker(es,{})
        self.assertEqual(p['issues'],[]);self.assertEqual(len(p['positions']),2)
        self.assertEqual(p['entry_debit'],2.)
    def test_overfill_and_orphan_are_not_silent(self):
        self.assertTrue(project_maker([event('ORDER_SUBMITTED'),event('FILL',quantity=6.)],{})['issues'])
        self.assertTrue(project_maker([event('FILL')],{})['issues'])
    def test_open_market_or_rounded_price_never_final(self):
        p=next(iter(project_maker([event('ORDER_SUBMITTED'),event('FILL')],{})['positions'].values()))
        raw={'id':'m1','closed':False,'clobTokenIds':['m1-yes','m1-no'],'outcomePrices':[1.,0.]}
        self.assertIsNone(settlement_event(p,raw,400_000))
        raw.update(closed=True,outcomePrices=[.9995,.0005]);self.assertIsNone(settlement_event(p,raw,400_000))
        raw.update(id='other',outcomePrices=[1.,0.]);self.assertIsNone(settlement_event(p,raw,400_000))
    def test_verified_settlement_closes_cash_and_inventory(self):
        es=[event('ORDER_SUBMITTED'),event('FILL')]
        p=next(iter(project_maker(es,{})['positions'].values()))
        raw={'id':'m1','closed':True,'clobTokenIds':['m1-yes','m1-no'],'outcomePrices':[1.,0.]}
        f=settlement_event(p,raw,400_000);f.validate()
        self.assertEqual(f.final_pnl,3.);self.assertEqual(f.realized_cashflow,5.)
        projection=project_maker(es+[f],{})
        self.assertEqual(projection['issues'],[]);self.assertEqual(projection['positions'],{})
        self.assertEqual(projection['realized_pnl'],3.)
        self.assertEqual(f.record_id,settlement_event(p,raw,500_000).record_id)
    def test_existing_router_account_and_settlement_owner_include_maker(self):
        from v7_external_fair_paper_router import (reconstruct_paper_exploration_account,
            spool_event, PaperRouter)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for e in [event('ORDER_SUBMITTED'),event('FILL')]:spool_event(root,e)
            account=reconstruct_paper_exploration_account(root,SHA,4000.)
            self.assertTrue(account['complete'],account['issues'])
            self.assertEqual(account['cash'],3998.);self.assertEqual(account['open_positions'],1)
            router=object.__new__(PaperRouter);router.root=root;router.sha=SHA
            router.state={'positions':account['positions']};router.gamma_url='https://invalid.example'
            raw={'id':'m1','closed':True,'clobTokenIds':['m1-yes','m1-no'],'outcomePrices':[1.,0.]}
            with mock.patch('v7_external_fair_paper_router.request_json',return_value=raw):
                self.assertEqual(router.observe_positions(),1)
            settled=reconstruct_paper_exploration_account(root,SHA,4000.)
            self.assertTrue(settled['complete'],settled['issues'])
            self.assertEqual(settled['cash'],4003.);self.assertEqual(settled['equity'],4003.)
            self.assertEqual(settled['open_positions'],0);self.assertEqual(settled['terminal_positions'],1)
            self.assertEqual(settled['realized_pnl'],3.)

    def test_counterfactual_never_in_account(self):
        f=event('FILL');f.metadata['counterfactual']=True
        self.assertEqual(project_maker([f],{})['fills'],0)

if __name__=='__main__':unittest.main()
