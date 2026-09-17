#!/usr/bin/env python3
from __future__ import annotations
import json, sys, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
from v7_portfolio_guard import assess
from v7_execution_ledger import LedgerEvent
SHA='a'*40

def allocation(path:Path,crypto:float=60.0,reserve:float=40.0)->Path:
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps({'schema':'polymarket_v7_capital_allocation_v3','paper_only':True,'authenticated_execution':False,'real_order_submission':False,'capital_authority_owner':'V7_CANONICAL_ALLOCATOR','capital_authority_owner_count':1,'account_starting_capital':crypto+reserve,'engine_budgets':{'CRYPTO_SETTLEMENT_ENGINE':crypto},'reserve_budget':reserve})); return path
def canonical_runtime(root:Path):
    p=root/'control/runtime_status.json'; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps({'schema':'polymarket_v7_runtime_status_v3','model_sha':SHA,'paper_only':True,'authenticated_execution':False,'real_order_submission':False}))
def write_ledger(root:Path,events):
    p=root/'ledger/execution.jsonl'; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(''.join(json.dumps(e.to_dict(),sort_keys=True)+'\n' for e in events))
def fill(order,fill_id,price=.4,size=5.0,fee=.1,component='crypto_informed_taker'):
    return LedgerEvent(event_type='FILL',strategy='CRYPTO_SETTLEMENT_ENGINE',model_sha=SHA,order_id=order,fill_id=fill_id,token_id='token',side='BUY',fill_price=price,filled_size=size,fee=fee,fee_source='TEST_AUTHORITATIVE',exchange_ts_ms=1000,receive_ts_ms=1001,recorded_ts_ms=1002,metadata={'component':component,'coordinator_receipt':{'owner':'V7_GLOBAL_PORTFOLIO_COORDINATOR'}})
def final(order,pnl,component='crypto_informed_taker',record_id=None):
    kw={} if record_id is None else {'record_id':record_id}; return LedgerEvent(event_type='FINAL',strategy='CRYPTO_SETTLEMENT_ENGINE',model_sha=SHA,order_id=order,final_pnl=pnl,recorded_ts_ms=1003,metadata={'component':component,'coordinator_receipt':{'owner':'V7_GLOBAL_PORTFOLIO_COORDINATOR'}},**kw)

class PortfolioGuardTests(unittest.TestCase):
    def test_crypto_equity_is_accounted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); p=root/'external_fair/paper_router_status.json'; p.parent.mkdir(parents=True); p.write_text(json.dumps({'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'equity':55.0,'killed':False}))
            report=assess(root,allocation(root/'manifest.json'),max_drawdown=.15); self.assertFalse(report['killed']); self.assertEqual(report['equity'],95.0); self.assertEqual(set(report['engines']),{'CRYPTO_SETTLEMENT_ENGINE'})
    def test_account_drawdown_triggers_global_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); p=root/'external_fair/paper_router_status.json'; p.parent.mkdir(parents=True); p.write_text(json.dumps({'paper_only':True,'authenticated_execution':False,'equity':30.0,'killed':False}))
            report=assess(root,allocation(root/'manifest.json'),max_drawdown=.15); self.assertTrue(report['killed']); self.assertEqual(report['equity'],70.0); self.assertTrue((root/'control/KILL').exists())
    def test_missing_engine_status_preserves_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); report=assess(root,allocation(root/'manifest.json'),max_drawdown=.15); self.assertEqual(report['equity'],100.0); self.assertEqual(report['engines']['CRYPTO_SETTLEMENT_ENGINE']['source'],'not_started')
    def test_component_observer_equity_never_enters_account_equity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); p=root/'micro_maker/status.json'; p.parent.mkdir(parents=True); p.write_text(json.dumps({'paper_only':True,'authenticated_execution':False,'equity':9999.0})); report=assess(root,allocation(root/'manifest.json'),max_drawdown=.15); self.assertEqual(report['equity'],100.0)
    def test_unsafe_engine_kills_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); p=root/'external_fair/paper_router_status.json'; p.parent.mkdir(parents=True); p.write_text(json.dumps({'paper_only':True,'authenticated_execution':True,'equity':60.0})); report=assess(root,allocation(root/'manifest.json'),max_drawdown=.15); self.assertTrue(report['killed'])
    def test_canonical_ledger_unifies_maker_and_taker_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); canonical_runtime(root); write_ledger(root,[fill('lead','f1',fee=.05),final('lead',4.6),fill('maker','f2',price=.32,fee=0.0,component='professional_maker'),final('maker',3.4,component='professional_maker')]); report=assess(root,allocation(root/'manifest.json'),max_drawdown=.15); self.assertAlmostEqual(report['engines']['CRYPTO_SETTLEMENT_ENGINE']['equity'],68.0); self.assertAlmostEqual(report['equity'],108.0)
    def test_canonical_open_fill_is_conservatively_debited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); canonical_runtime(root); write_ledger(root,[fill('open','f1')]); report=assess(root,allocation(root/'manifest.json'),max_drawdown=.15); self.assertAlmostEqual(report['engines']['CRYPTO_SETTLEMENT_ENGINE']['equity'],57.9); self.assertAlmostEqual(report['equity'],97.9)
    def test_duplicate_canonical_final_kills_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); canonical_runtime(root); write_ledger(root,[fill('one','f1'),final('one',1.0,record_id='a'),final('one',1.0,record_id='b')]); report=assess(root,allocation(root/'manifest.json'),max_drawdown=.15); self.assertTrue(report['killed'])
if __name__=='__main__': unittest.main()
