from __future__ import annotations
import json, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts")); sys.path.insert(0,str(ROOT/"tests"))
from v7_opportunity import OpportunityEnvelope, OpportunityError, coordinate
from v7_global_portfolio_coordinator import process_cut
from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import spool_event, drain_spool
from test_v7_opportunity import envelope
SHA="1"*40

def exploration():
    v=envelope(authority="PAPER_EXPLORATION", key="explore")
    v["uncertainty"]["status"]="IMMATURE"; v["calibration_status"]="IMMATURE"
    v["latency"]["profile_valid"]=False; v["latency"]["arrival_ns"]=2_000_000
    return v

def test_exploration_is_btc_m5_only_and_not_mature_new_risk():
    v=exploration(); assert OpportunityEnvelope.parse(v).action=="TAKE"
    d=coordinate([v],now_ns=150,paper_exploration_authorized=True)
    assert d["action"]=="TAKE" and d["paper_exploration_authorized"] is True
    assert d["new_risk_authorized"] is False
    bad=exploration(); bad["crypto_context"]["asset"]="ETH"
    try: OpportunityEnvelope.parse(bad)
    except OpportunityError as e: assert str(e)=="paper_exploration_evidence_incomplete"
    else: raise AssertionError("ETH exploration accepted")

def test_coordinator_writes_separate_exploration_receipt():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); (root/"opportunities/inbox").mkdir(parents=True)
        (root/"opportunities/inbox/e.json").write_text(json.dumps(exploration()))
        d=process_cut(root,now_ns=150)["last_decision"]
        assert d["paper_exploration_authorized"] is True and d["new_risk_authorized"] is False
        assert list((root/"opportunities/receipts").glob("*.json"))

def test_ledger_accepts_only_explicit_paper_exploration_receipt():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        receipt={"schema":"polymarket_v7_global_opportunity_decision_v1","owner":"V7_GLOBAL_PORTFOLIO_COORDINATOR","engine_id":"CRYPTO_SETTLEMENT_ENGINE","action":"TAKE","selected_replay_key":"explore","new_risk_authorized":False,"paper_exploration_authorized":True,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,"crypto_context":{"asset":"BTC","horizon":"M5","authority":"PAPER_EXPLORATION"}}
        meta={"coordinator_receipt":receipt,"paper_exploration":True,"economic_authority":"PAPER_EXPLORATION"}
        ev=LedgerEvent(event_type="FILL",strategy="CRYPTO_INFORMED_TAKER",model_sha=SHA,order_id="o",fill_id="f",side="BUY",token_id="t",exchange_ts_ms=1000,receive_ts_ms=1100,fill_price=.5,filled_size=1,fee=0,fee_source="test:authoritative",metadata=meta)
        spool_event(root,ev); r=drain_spool(root,model_sha=SHA); assert r["appended"]==1 and r["quarantined"]==0
        receipt["real_order_submission"]=True
        ev2=LedgerEvent(event_type="FILL",strategy="CRYPTO_INFORMED_TAKER",model_sha=SHA,order_id="o2",fill_id="f2",side="BUY",token_id="t",exchange_ts_ms=1000,receive_ts_ms=1100,fill_price=.5,filled_size=1,fee=0,fee_source="test:authoritative",metadata=meta)
        spool_event(root,ev2); r=drain_spool(root,model_sha=SHA); assert r["quarantined"]==1

def test_router_requires_arrival_and_receipt_before_canonical_fill():
    text=(ROOT/"scripts/v7_external_fair_paper_router.py").read_text()
    assert "arrival_revalidated" in text
    assert "wait_for_exploration_receipt" in text
    assert "PAPER_EXPLORATION_NOT_SELECTED" in text
    assert "replay_key = self.emit_shadow_ingress" in text
    assert "paper_exploration" in text
