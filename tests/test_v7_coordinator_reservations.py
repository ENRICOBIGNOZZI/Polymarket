from __future__ import annotations
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts')); sys.path.insert(0,str(ROOT/'tests'))
from v7_coordinator_reservations import ReservationLimits, ReservationProjection, ReservationRequest, OWNER
from v7_lead_lag_replay import ReplayError, ResolutionProof, primitive
from v7_execution_ledger import CanonicalLedgerWriter, LedgerEvent, iter_records
from v7_global_portfolio_coordinator import coordinate_reserved_paper
from test_v7_global_portfolio_coordinator import forward_envelope
NOW=1_800_000_000_000
SHA='a'*40


def request(market='m1', asset='ETH', signal='signal1', maximum='3'):
    return ReservationRequest('synthetic-cohort','b'*64,market,'yes-'+market,signal,'common-shock',
        asset,'M5','CRYPTO_SETTLEMENT_ENGINE','USDC',D(maximum),D(5),D('.50'),NOW+1000,'candidate-'+market)


def receipt(req):
    return dict(schema='polymarket_v7_global_opportunity_decision_v1',owner=OWNER,action='TAKE',
        engine_id=req.strategy,crypto_context={'asset':req.asset,'horizon':req.horizon,
        'authority':'PAPER_EXPLORATION'},selected_replay_key=req.coordinator_replay_key,
        new_risk_authorized=False,paper_exploration_authorized=True,
        paper_exploration_probe_authorized=False,paper_forward_test_authorized=True,
        paper_only=True,authenticated_execution=False,real_order_submission=False,real_capital_at_risk=False)


def projection(cash='100',external=(),limits=None,sha=SHA):
    return ReservationProjection(code_sha=sha,checkpoint_id='SYNTHETIC_RECONCILED_CHECKPOINT',
        starting_cash=D(cash),external_exposures=external,
        limits=limits or ReservationLimits('USDC',*[D(100)]*6),whole_portfolio_reconciled=True)


def fill(req, quantity='2', suffix='1'):
    q=D(quantity); fee=q*D('.0175'); cost=q*D('.50')+fee
    return LedgerEvent('FILL',req.strategy,SHA,record_id='fill-record-'+suffix,recorded_ts_ms=NOW+100,
        order_id=req.key,fill_id='fill-'+suffix,position_id='position-'+req.market_id,
        market_id=req.market_id,token_id=req.token_id,side='BUY',fill_price=.5,filled_size=float(q),
        fee=float(fee),fee_source='SYNTHETIC_TEST_FEE',exchange_ts_ms=NOW+80,receive_ts_ms=NOW+90,
        decision_ts_ms=NOW+90,metadata={'execution_evidence':'SIMULATED','coordinator_receipt':receipt(req),
        'exact_paper_fill':{'gross_shares':str(q),'net_shares':str(q),'cash_debit':str(cost),
                            'cash_fee':str(fee),'shares_fee':'0'}})


def terminal(req, ids=(), state=None):
    return LedgerEvent('ORDER_STATE',req.strategy,SHA,record_id='terminal-'+req.key,recorded_ts_ms=NOW+101,
        order_id=req.key,market_id=req.market_id,token_id=req.token_id,
        order_state=state or ('FAK_PARTIAL_CANCELLED' if ids else 'FAK_UNFILLED_CANCELLED'),
        metadata={'fill_record_ids':list(ids),'coordinator_receipt':receipt(req)})


def final(req, shares='2', cost='1.0350', win=True):
    payout=D(shares)*int(win); pnl=payout-D(cost)
    proof=ResolutionProof(req.market_id,((req.token_id,D(int(win))),('no-'+req.market_id,D(int(not win)))),
                          'OFFICIAL_GAMMA_RESOLUTION','c'*64,(NOW+1900)*1_000_000,'RESOLVED')
    return LedgerEvent('FINAL',req.strategy,SHA,record_id='final-'+req.key,recorded_ts_ms=NOW+2000,
        order_id=req.key,market_id=req.market_id,token_id=req.token_id,fee=0.,
        realized_cashflow=float(payout),final_pnl=float(pnl),metadata={
        'coordinator_receipt':receipt(req),'resolution_proof':primitive(proof),
        'exact_terminal':{'payout':str(payout),'pnl':str(pnl)}})


class ReservationTest(unittest.TestCase):
    def setUp(self):
        self.events=[]; self.p=projection(); self.r=request()
    def reserve(self,p=None,r=None,append=None):
        p=p or self.p; r=r or self.r
        return p.reserve(r,now_ms=NOW,receipt=receipt(r),append=append or self.events.append,entry_gate_open=True)
    def submitted(self):
        self.reserve(); self.p.submit_fence(self.r.key,now_ms=NOW+1,append=self.events.append,entry_gate_open=True)
    def observe(self,event):
        self.events.append(event); self.p.observe(event)
    def opened(self):
        self.submitted(); f=fill(self.r); t=terminal(self.r,(f.record_id,))
        self.observe(f); self.observe(t)
        self.p.complete_fak(self.r.key,fill_record_ids=(f.record_id,),terminal_order_record_id=t.record_id,
                            now_ms=NOW+102,append=self.events.append)

    def test_reserve_is_idempotent_no_second_debit(self):
        a=self.reserve(); b=self.reserve()
        self.assertEqual(a,b); self.assertEqual(len(self.events),1)
        self.assertEqual(self.p.snapshot()['reserved'],'3')

    def test_same_identity_different_terms_rejected(self):
        self.reserve()
        with self.assertRaisesRegex(ReplayError,'REDEFINED'):
            self.reserve(r=replace(self.r,maximum_debit=D(4)))

    def test_gate_default_closed(self):
        with self.assertRaisesRegex(ReplayError,'GATE_CLOSED'):
            self.p.reserve(self.r,now_ms=NOW,receipt=receipt(self.r),append=self.events.append)
        self.assertFalse(self.events)

    def test_no_authorization_reuse_for_other_candidate(self):
        bad=dict(receipt(self.r),selected_replay_key='another')
        with self.assertRaisesRegex(ReplayError,'RECEIPT_BINDING'):
            self.p.reserve(self.r,now_ms=NOW,receipt=bad,append=self.events.append,entry_gate_open=True)

    def test_one_market_across_strategies(self):
        self.reserve()
        with self.assertRaisesRegex(ReplayError,'MARKET_ALREADY'):
            self.reserve(r=replace(self.r,signal_id='another',strategy='OTHER'))

    def test_global_cash_reserves_across_assets(self):
        p=projection(cash='5'); self.reserve(p=p)
        with self.assertRaisesRegex(ReplayError,'GLOBAL_CASH_LIMIT'):
            self.reserve(p=p,r=request('m2','SOL'))

    def test_correlated_parent_shock_limit(self):
        p=projection(limits=replace(self.p.limits,parent_shock=D(4))); self.reserve(p=p)
        with self.assertRaisesRegex(ReplayError,'RISK_LIMIT:parent_shock_id'):
            self.reserve(p=p,r=request('m2','SOL'))

    def test_external_other_lane_exposure_is_counted(self):
        external={'market_id':'old','asset':'BTC','horizon':'M15','strategy':'OTHER',
                  'parent_shock_id':'old-shock','currency':'USDC','cost':'99','state':'OPEN'}
        p=projection(external=[external])
        with self.assertRaisesRegex(ReplayError,'PORTFOLIO_RISK_LIMIT'):
            self.reserve(p=p)

    def test_external_incomplete_currency_and_missing_checkpoint_rejected(self):
        with self.assertRaises(ReplayError): projection(external=[{'cost':'1'}])
        with self.assertRaises(ReplayError):
            ReservationProjection(code_sha=SHA,checkpoint_id='x',starting_cash=D(10),external_exposures=[],
                                   limits=self.p.limits,whole_portfolio_reconciled=False)

    def test_kill_between_authorization_and_submit(self):
        self.reserve()
        with self.assertRaisesRegex(ReplayError,'KILL_DRAIN'):
            self.p.submit_fence(self.r.key,now_ms=NOW+1,append=self.events.append,entry_gate_open=False)
        self.assertEqual(self.p.snapshot()['states'][self.r.key],'RESERVED')

    def test_expiry_while_waiting(self):
        self.reserve()
        with self.assertRaisesRegex(ReplayError,'EXPIRED'):
            self.p.submit_fence(self.r.key,now_ms=NOW+1001,append=self.events.append,entry_gate_open=True)

    def test_fenced_unknown_order_never_released_by_timeout(self):
        self.submitted()
        with self.assertRaisesRegex(ReplayError,'CANNOT_EXPIRE'):
            self.p.release_unsubmitted(self.r.key,now_ms=NOW+10000,reason='timeout',append=self.events.append)
        self.assertEqual(self.p.snapshot()['reserved'],'3')

    def test_crash_restart_does_not_submit_twice(self):
        self.submitted(); p=projection(sha='d'*40)
        for e in self.events:p.observe(e)
        self.assertEqual(p.snapshot(),self.p.snapshot())
        with self.assertRaisesRegex(ReplayError,'ALREADY_FENCED'):
            p.submit_fence(self.r.key,now_ms=NOW+2,append=self.events.append,entry_gate_open=True)

    def test_fsync_succeeded_but_callback_failed_requires_recovery(self):
        def ambiguous(e):
            self.events.append(e); raise OSError('synthetic failure AFTER durable append')
        with self.assertRaises(OSError):self.reserve(append=ambiguous)
        self.assertTrue(self.p.snapshot()['poisoned'])
        with self.assertRaisesRegex(ReplayError,'AMBIGUOUS_DURABILITY'):self.reserve()
        p=projection()
        for e in self.events:p.observe(e)
        self.assertEqual(p.snapshot()['reserved'],'3')
        self.assertEqual(self.reserve(p=p).record_id,self.events[0].record_id)

    def test_partial_fak_cash_identity_and_residual_release(self):
        self.opened(); s=self.p.snapshot()
        self.assertEqual(D(s['cash']),D('98.965')); self.assertEqual(D(s['reserved']),D(0))
        self.assertEqual(D(s['committed_and_reserved']),D('1.035'))
        self.assertEqual(s['burned_markets'],['m1'])

    def test_phantom_fill_record_id_cannot_free_cash(self):
        self.submitted(); t=terminal(self.r,('imaginary-fill',)); self.observe(t)
        with self.assertRaisesRegex(ReplayError,'COVERAGE_MISMATCH'):
            self.p.complete_fak(self.r.key,fill_record_ids=('imaginary-fill',),terminal_order_record_id=t.record_id,
                                now_ms=NOW+102,append=self.events.append)
        self.assertEqual(self.p.snapshot()['reserved'],'3')

    def test_omitted_partial_fill_rejected(self):
        self.submitted(); f=fill(self.r); self.observe(f); t=terminal(self.r,()); self.observe(t)
        with self.assertRaisesRegex(ReplayError,'COVERAGE_MISMATCH'):
            self.p.complete_fak(self.r.key,fill_record_ids=(),terminal_order_record_id=t.record_id,
                                now_ms=NOW+102,append=self.events.append)

    def test_known_nonfill_releases_but_dedup_is_permanent(self):
        self.submitted(); t=terminal(self.r); self.observe(t)
        self.p.complete_fak(self.r.key,fill_record_ids=(),terminal_order_record_id=t.record_id,
                            now_ms=NOW+102,append=self.events.append)
        self.assertEqual(self.p.snapshot()['cash'],'100')
        self.assertEqual(D(self.p.snapshot()['reserved']),D(0))
        with self.assertRaisesRegex(ReplayError,'ALREADY_ATTEMPTED'):self.reserve()

    def test_missing_terminal_keeps_reservation(self):
        self.submitted(); f=fill(self.r); self.observe(f)
        with self.assertRaisesRegex(ReplayError,'TERMINAL_EVIDENCE_REQUIRED'):
            self.p.complete_fak(self.r.key,fill_record_ids=(f.record_id,),terminal_order_record_id='missing',
                                now_ms=NOW+102,append=self.events.append)

    def test_cash_fee_double_count_rejected(self):
        self.submitted(); f=fill(self.r); md=dict(f.metadata)
        md['exact_paper_fill']=dict(md['exact_paper_fill'],shares_fee='.1',net_shares='1.9')
        f=replace(f,metadata=md); self.observe(f); t=terminal(self.r,(f.record_id,)); self.observe(t)
        with self.assertRaisesRegex(ReplayError,'ACCOUNTING_MISMATCH'):
            self.p.complete_fak(self.r.key,fill_record_ids=(f.record_id,),terminal_order_record_id=t.record_id,
                                now_ms=NOW+102,append=self.events.append)

    def test_settlement_payout_once_with_official_proof(self):
        self.opened(); f=final(self.r); self.observe(f)
        a=self.p.settle(self.r.key,final_record_id=f.record_id,now_ms=NOW+2001,append=self.events.append)
        b=self.p.settle(self.r.key,final_record_id=f.record_id,now_ms=NOW+2002,append=self.events.append)
        self.assertEqual(a,b); self.assertEqual(D(self.p.snapshot()['cash']),D('100.965'))
        self.assertEqual(self.p.snapshot()['states'][self.r.key],'FINAL')

    def test_settlement_loss_is_observed_not_invented(self):
        self.opened(); f=final(self.r,win=False); self.observe(f)
        self.p.settle(self.r.key,final_record_id=f.record_id,now_ms=NOW+2001,append=self.events.append)
        self.assertEqual(D(self.p.snapshot()['cash']),D('98.965'))

    def test_artificial_zero_final_without_proof_refused(self):
        self.opened(); f=final(self.r,win=False); md=dict(f.metadata); md.pop('resolution_proof')
        self.observe(replace(f,metadata=md))
        with self.assertRaisesRegex(ReplayError,'OFFICIAL_RESOLUTION_PROOF_REQUIRED'):
            self.p.settle(self.r.key,final_record_id=f.record_id,now_ms=NOW+2001,append=self.events.append)
        self.assertEqual(self.p.snapshot()['states'][self.r.key],'OPEN')

    def test_future_proof_or_changed_payout_refused(self):
        for mutation in ('future','payout'):
            with self.subTest(mutation=mutation):
                self.setUp(); self.opened(); f=final(self.r); md=json.loads(json.dumps(f.metadata))
                if mutation=='future':md['resolution_proof']['available_wall_ns']=(NOW+9999)*1_000_000
                else:md['exact_terminal']['payout']='99'
                self.observe(replace(f,metadata=md))
                with self.assertRaises(ReplayError):
                    self.p.settle(self.r.key,final_record_id=f.record_id,now_ms=NOW+2001,append=self.events.append)

    def test_canonical_duplicate_record_is_idempotent_conflict_is_not(self):
        e=self.reserve(); self.p.observe(e)
        with self.assertRaisesRegex(ReplayError,'RECORD_ID_REDEFINED'):
            self.p.observe(replace(e,recorded_ts_ms=NOW+1))
        self.assertTrue(self.p.snapshot()['poisoned'])

    def test_canonical_writer_full_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'execution.jsonl'
            p=projection(); req=self.r
            with CanonicalLedgerWriter(path,writer_id='SYNTHETIC_TEST_OWNER',model_sha=SHA) as writer:
                p.reserve(req,now_ms=NOW,receipt=receipt(req),append=writer.append,entry_gate_open=True)
                p.submit_fence(req.key,now_ms=NOW+1,append=writer.append,entry_gate_open=True)
                f=fill(req); t=terminal(req,(f.record_id,))
                for e in (f,t):writer.append(e); p.observe(e)
                p.complete_fak(req.key,fill_record_ids=(f.record_id,),terminal_order_record_id=t.record_id,
                               now_ms=NOW+102,append=writer.append)
                end=final(req); writer.append(end); p.observe(end)
                p.settle(req.key,final_record_id=end.record_id,now_ms=NOW+2001,append=writer.append)
            recovered=projection(sha='f'*40)
            records=list(iter_records(path))
            for event in records:recovered.observe(event)
            self.assertEqual(p.snapshot(),recovered.snapshot()); self.assertEqual(len(records),7)

    def test_concurrent_candidates_same_market_only_one_reservation(self):
        errors=[]
        def attempt(n):
            try:self.reserve(r=replace(self.r,signal_id='signal-'+str(n)))
            except ReplayError as e:errors.append(str(e))
        threads=[threading.Thread(target=attempt,args=(i,)) for i in range(8)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(len(self.events),1); self.assertEqual(len(errors),7)

    def test_late_fill_after_known_nonfill_is_not_silently_dropped(self):
        self.submitted(); t=terminal(self.r); self.observe(t)
        self.p.complete_fak(self.r.key,fill_record_ids=(),terminal_order_record_id=t.record_id,
                            now_ms=NOW+102,append=self.events.append)
        with self.assertRaisesRegex(ReplayError,'FILL_OUTSIDE_FENCED_ORDER'):
            self.p.observe(fill(self.r))
        self.assertTrue(self.p.snapshot()['poisoned'])

    def test_external_reservations_reduce_available_cash(self):
        external={'market_id':'old','asset':'BTC','horizon':'M15','strategy':'OTHER',
                  'parent_shock_id':'old-shock','currency':'USDC','cost':'4','state':'RESERVED'}
        p=projection(cash='5',external=[external])
        with self.assertRaisesRegex(ReplayError,'GLOBAL_CASH_LIMIT'):self.reserve(p=p)

    def test_foreign_monetary_event_requires_fresh_checkpoint(self):
        self.reserve()
        foreign=LedgerEvent('CAPITAL_RESERVE','OTHER',SHA,record_id='foreign',order_id='other',
                            recorded_ts_ms=NOW+1,metadata={})
        self.p.observe(foreign)
        self.assertTrue(self.p.snapshot()['external_checkpoint_stale'])
        with self.assertRaisesRegex(ReplayError,'ACCOUNT_RECONCILIATION'):
            self.reserve(r=request('m2','SOL'))
        with self.assertRaisesRegex(ReplayError,'ACCOUNT_RECONCILIATION'):
            self.p.submit_fence(self.r.key,now_ms=NOW+2,append=self.events.append,entry_gate_open=True)

    def test_received_canonical_metadata_cannot_mutate_projection(self):
        event=self.reserve()
        event.metadata['reservation_projection']['details']['receipt']['action']='NOTHING'
        recovered=self.reserve()
        self.assertEqual(recovered.metadata['reservation_projection']['details']['receipt']['action'],'TAKE')
        recovered.metadata['reservation_projection']['details']['receipt']['action']='CANCEL'
        self.assertEqual(self.reserve().metadata['reservation_projection']['details']['receipt']['action'],'TAKE')

    def test_fill_cannot_chase_above_original_limit(self):
        self.submitted(); f=fill(self.r); md=dict(f.metadata)
        md['exact_paper_fill']=dict(md['exact_paper_fill'],cash_debit='1.055')
        f=replace(f,fill_price=.51,metadata=md); self.observe(f); t=terminal(self.r,(f.record_id,)); self.observe(t)
        with self.assertRaisesRegex(ReplayError,'FILL_PRICE_OR_FENCE_TIME_MISMATCH'):
            self.p.complete_fak(self.r.key,fill_record_ids=(f.record_id,),terminal_order_record_id=t.record_id,
                                now_ms=NOW+102,append=self.events.append)

    def test_fak_terminal_state_must_match_exact_filled_quantity(self):
        for quantity, state in (('2','FAK_FILLED'),('5','FAK_PARTIAL_CANCELLED')):
            with self.subTest(quantity=quantity,state=state):
                self.setUp(); self.submitted(); f=fill(self.r,quantity=quantity)
                self.observe(f); t=terminal(self.r,(f.record_id,),state=state); self.observe(t)
                with self.assertRaisesRegex(ReplayError,'QUANTITY_STATE_MISMATCH'):
                    self.p.complete_fak(self.r.key,fill_record_ids=(f.record_id,),terminal_order_record_id=t.record_id,
                                        now_ms=NOW+102,append=self.events.append)
                self.assertEqual(self.p.snapshot()['reserved'],'3')

    def test_backdated_fak_completion_cannot_release_reserve(self):
        self.submitted(); f=fill(self.r); self.observe(f); t=terminal(self.r,(f.record_id,)); self.observe(t)
        count=len(self.events)
        with self.assertRaisesRegex(ReplayError,'TRANSITION_PRECEDES_ITS_EVIDENCE'):
            self.p.complete_fak(self.r.key,fill_record_ids=(f.record_id,),terminal_order_record_id=t.record_id,
                                now_ms=NOW+99,append=self.events.append)
        self.assertEqual(len(self.events),count)

    def test_backdated_settlement_cannot_release_cash(self):
        self.opened(); f=final(self.r); self.observe(f); count=len(self.events)
        with self.assertRaisesRegex(ReplayError,'TRANSITION_PRECEDES_ITS_EVIDENCE'):
            self.p.settle(self.r.key,final_record_id=f.record_id,now_ms=NOW+1999,append=self.events.append)
        self.assertEqual(len(self.events),count)
        self.assertEqual(self.p.snapshot()['states'][self.r.key],'OPEN')

    def test_release_cannot_precede_reservation_on_replay(self):
        self.reserve(); e=self.p.release_unsubmitted(self.r.key,now_ms=NOW+1,reason='test',append=self.events.append)
        fresh=projection(); fresh.observe(self.events[0])
        with self.assertRaisesRegex(ReplayError,'TRANSITION_PRECEDES_ITS_EVIDENCE'):
            fresh.observe(replace(e,recorded_ts_ms=NOW-1))

    def test_existing_coordinator_opt_in_actual_call_graph(self):
        raw=forward_envelope('coordinator-synthetic'); now_ns=NOW*1_000_000
        raw['decision_receive_timestamp_ns']=now_ns-10_000_000
        raw['source_event_timestamps_ns']=[now_ns-20_000_000]
        raw['expires_at_ns']=now_ns+1_000_000_000
        leg=raw['execution_plan']['legs'][0]
        req=ReservationRequest('cohort','f'*64,raw['market_id'],leg['token_id'],'s','parent','BTC','M5',
            raw['engine_id'],'USDC',D(20),D(str(leg['target_quantity'])),D(str(leg['limit_price'])),
            NOW+1000,raw['deterministic_replay_key'])
        decision=coordinate_reserved_paper([raw],now_ns=now_ns,reservation_projection=self.p,
            requests_by_replay_key={req.coordinator_replay_key:req},append_event=self.events.append,entry_gate_open=True)
        self.assertEqual(decision['action'],'TAKE',decision)
        self.assertTrue(decision['reservation_durable']); self.assertEqual(len(self.events),1)
        self.assertFalse(decision['new_risk_authorized'])
        denied=coordinate_reserved_paper([raw],now_ns=now_ns,reservation_projection=self.p,
            requests_by_replay_key={req.coordinator_replay_key:req},append_event=self.events.append,entry_gate_open=False)
        self.assertEqual(denied['action'],'NOTHING'); self.assertEqual(len(self.events),1)

if __name__=='__main__':unittest.main()
