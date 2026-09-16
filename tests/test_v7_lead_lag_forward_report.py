from __future__ import annotations
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from v7_execution_ledger import LedgerEvent
from v7_lead_lag_forward_report import summarize

def event(kind,market,i,pnl=None,won=None):
    md={'model_family':'lead_lag_taker_v1','protocol_hash':'f'*64,'won':won}
    kw=dict(event_type=kind,strategy='CRYPTO_SETTLEMENT_ENGINE',model_sha='a'*40,recorded_ts_ms=1000+i,
            market_id=market,order_id='o'+market,position_id='p'+market,token_id='t'+market,side='BUY',metadata=md)
    if kind=='FILL':kw.update(fill_id='x'+market,fill_price=.5,filled_size=5.,fee=.01)
    if kind=='FINAL':kw.update(fill_id='x'+market,final_pnl=pnl,realized_cashflow=5. if won else 0.)
    return LedgerEvent(**kw)

def test_report_uses_independent_market_finals_and_never_promotes():
    events=[]
    for i in range(100):
        m=f'm{i}'; events += [event('ORDER_SUBMITTED',m,i),event('FILL',m,i),event('FINAL',m,i,pnl=.1,won=True)]
    manifest={'protocol_hash':'f'*64,'code_sha':'a'*40,'target_independent_markets':100,'minimum_positive_windows_before_canary':3}
    r=summarize(events,manifest,{'entries':100,'settled':100},draws=1000)
    assert r['settled_markets']==100 and r['total_pnl']>0
    assert r['positive_complete_blocks']==4
    assert r['research_gate']['ready_for_manual_canary_review'] is True
    assert r['automatic_promotion'] is False and r['real_order_submission'] is False

if __name__=='__main__':test_report_uses_independent_market_finals_and_never_promotes();print('lead-lag report test passed')
