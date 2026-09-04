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
        receipt={"schema":"polymarket_v7_global_opportunity_decision_v1","owner":"V7_GLOBAL_PORTFOLIO_COORDINATOR","engine_id":"CRYPTO_SETTLEMENT_ENGINE","action":"TAKE","selected_replay_key":"explore","new_risk_authorized":False,"paper_exploration_authorized":True,"paper_exploration_probe_authorized":False,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,"crypto_context":{"asset":"BTC","horizon":"M5","authority":"PAPER_EXPLORATION"}}
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



def probe_envelope():
    value=exploration()
    value["deterministic_replay_key"]="probe"
    value["conservative_expected_wealth_change"]=-0.25
    value["portfolio_exposure_delta"]=0.75
    value["exploration"]={
        "mode":"PAPER_BOOTSTRAP_PROBE",
        "point_expected_wealth_change":0.20,
        "maximum_probe_loss":0.80,
        "probe_loss_cap":1.00,
        "information_score":0.15,
        "promotion_eligible":False,
        "robust_candidate":False,
        "arrival_revalidated":True,
        "model_id":"btc_m5_same_oracle_diffusion_bootstrap_v1",
        "model_hash":"e"*64,
    }
    value["reasons"]=["PAPER_EXPLORATION_INFORMATION_GAIN_PROBE"]
    return value


def test_bounded_probe_is_selected_only_after_robust_candidates_are_absent():
    probe=probe_envelope()
    parsed=OpportunityEnvelope.parse(probe)
    assert parsed.is_probe is True
    decision=coordinate([probe],now_ns=150,paper_exploration_authorized=True)
    assert decision["action"]=="TAKE"
    assert decision["new_risk_authorized"] is False
    assert decision["paper_exploration_authorized"] is True
    assert decision["paper_exploration_probe_authorized"] is True
    assert decision["reasons"]==["PAPER_EXPLORATION_INFORMATION_GAIN_PROBE"]
    robust=exploration(); robust["conservative_expected_wealth_change"]=0.01
    decision=coordinate([probe,robust],now_ns=150,paper_exploration_authorized=True)
    assert decision["selected_replay_key"]==robust["deterministic_replay_key"]
    assert decision["paper_exploration_probe_authorized"] is False


def test_probe_caps_and_real_money_drift_fail_closed():
    for mutation in ("loss", "promotion", "model"):
        value=probe_envelope()
        if mutation=="loss": value["exploration"]["maximum_probe_loss"]=5.01; value["exploration"]["probe_loss_cap"]=5.01
        elif mutation=="promotion": value["exploration"]["promotion_eligible"]=True
        else: value["exploration"]["model_id"]="unregistered"
        try: OpportunityEnvelope.parse(value)
        except OpportunityError as exc: assert str(exc)=="paper_exploration_probe_invalid"
        else: raise AssertionError(f"unsafe probe accepted: {mutation}")


def test_ledger_probe_requires_matching_probe_receipt():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        receipt={"schema":"polymarket_v7_global_opportunity_decision_v1","owner":"V7_GLOBAL_PORTFOLIO_COORDINATOR","engine_id":"CRYPTO_SETTLEMENT_ENGINE","action":"TAKE","selected_replay_key":"probe","new_risk_authorized":False,"paper_exploration_authorized":True,"paper_exploration_probe_authorized":True,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,"crypto_context":{"asset":"BTC","horizon":"M5","authority":"PAPER_EXPLORATION"}}
        meta={"coordinator_receipt":receipt,"paper_exploration":True,"paper_bootstrap_probe":True,"economic_authority":"PAPER_EXPLORATION"}
        event=LedgerEvent(event_type="FILL",strategy="CRYPTO_INFORMED_TAKER",model_sha=SHA,order_id="probe-o",fill_id="probe-f",side="BUY",token_id="t",exchange_ts_ms=1000,receive_ts_ms=1100,fill_price=.1,filled_size=5,fee=0,fee_source="test:authoritative",metadata=meta)
        spool_event(root,event); result=drain_spool(root,model_sha=SHA)
        assert result["appended"]==1 and result["quarantined"]==0
        receipt["paper_exploration_probe_authorized"]=False
        event2=LedgerEvent(event_type="FILL",strategy="CRYPTO_INFORMED_TAKER",model_sha=SHA,order_id="probe-o2",fill_id="probe-f2",side="BUY",token_id="t",exchange_ts_ms=1000,receive_ts_ms=1100,fill_price=.1,filled_size=5,fee=0,fee_source="test:authoritative",metadata=meta)
        spool_event(root,event2); result=drain_spool(root,model_sha=SHA)
        assert result["quarantined"]==1


def test_live_router_has_distinct_probe_candidate_and_arrival_revalidation_paths():
    import v7_external_fair_paper_router as router
    policy=json.loads((ROOT/"config/v7_external_fair.json").read_text())
    probe=router.validate_probe_policy(policy["paper_exploration_probe"])
    now=router.time.monotonic_ns()
    yes=router.Book("yes",((.88,100.0),),((.90,100.0),),.01,5.0,1000,1000,"y")
    no=router.Book("no",((.09,100.0),),((.11,100.0),),.01,5.0,1000,1000,"n")
    status={
        "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
        "contract":{"verified":True,"rules_hash_recognized":True},
        "settlement_reference":{"valid":True},
        "oracle":{"healthy":True,"continuity":"LIVE_CONTINUOUS"},
        "external":{"healthy":True},
        "market":{"yes_token":"yes","no_token":"no","fee_schedule":{"rate":0.0,"exponent":1,"takerOnly":True}},
        "fair":{"valid":True,"paper_exploration_bootstrap":True,"promotion_eligible":False,"real_money_authority":False,"probability_model_id":"btc_m5_same_oracle_diffusion_bootstrap_v1","probability_model_hash":"f"*64,"yes":.775,"lower":.567,"upper":.911,"tte_seconds":120.0,"calculated_monotonic_ns":now-1,"valid_until_monotonic_ns":now+10_000_000_000},
    }
    assert router.robust_candidates(status,{"yes":yes,"no":no},policy["taker"])==[]
    rows=router.paper_probe_candidates(status,{"yes":yes,"no":no},policy["taker"],probe)
    assert len(rows)==1 and rows[0]["outcome"]=="NO"
    assert rows[0]["point_ev"]>0 and rows[0]["robust_ev"]<0
    text=(ROOT/"scripts/v7_external_fair_paper_router.py").read_text()
    assert "paper_probe_candidates(arrival_status" in text
    assert "paper_exploration_probe_authorized" in text
