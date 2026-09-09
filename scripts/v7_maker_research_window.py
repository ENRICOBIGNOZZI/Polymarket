"""Freeze each valid execution window before evaluating later fill markouts.

Lives inside the existing profit collector. No queue reconstruction, execution
owner, economic authority, parameter optimization or retrospective relabeling.
"""
from __future__ import annotations
import copy
import time
from v7_profit_experiments import replay_anchor, preserve_source, finite

WAITING='WAITING_FOR_EVIDENCE'


def continuity(anchor,book,status,target_ms,now_ms,target_monotonic_ns=None):
    if (anchor['observer_session_id']!=book.session or anchor['connection_epoch']!=book.epoch
            or anchor['book_gap_counter']!=book.gaps):return 'TRANSPORT_GAP_OR_SESSION_CHANGE'
    if (status.get('paper_only') is not True or status.get('authenticated_execution') is not False
            or status.get('real_order_submission') is not False or status.get('model_sha')!=book.model_sha):
        return 'INVALID_STATUS_IDENTITY'
    arrival=anchor['order']['metadata'].get('arrival_receive_monotonic_ns')
    if not arrival or arrival<=0:return 'MISSING_NATIVE_ARRIVAL_CLOCK'
    target_monotonic_ns=target_monotonic_ns or arrival+int((target_ms-anchor['origin_ms'])*1e6)
    if (status.get('observer_session_id')!=book.session or status.get('connection_epoch')!=book.epoch
            or status.get('state')!='running' or not 0<=now_ms-status.get('timestamp_ms',0)<=2000
            or status.get('book_events_written',0)>book.sequence
            or min(book.watermark_ms,status.get('book_watermark_receive_wall_ms',0))<target_ms
            or min(getattr(book,'watermark_monotonic_ns',0),status.get('book_watermark_receive_monotonic_ns',0))<target_monotonic_ns):return WAITING
    if status.get('evidence_complete') is not True:return 'TRANSPORT_EVIDENCE_INCOMPLETE'
    return 'OBSERVED'


class MakerWindow:
    def __init__(self,anchor,restored=None):
        self.anchor=anchor;self.executions={};self.markouts={};self.sources=[];self.waits=0
        for row in restored or []:
            if row.get('anchor_record_id')!=anchor['order']['record_id']:continue
            self.apply(row)

    def apply(self,row):
        if row['kind']=='MAKER_EXECUTION_WINDOW':
            self.executions.update({r['arm']:r for r in row['arms']})
            self.sources.append(row['source_evidence'])
        elif row['kind']=='MAKER_MARKOUT_LABEL':
            self.markouts[row['markout_key']]=row

    def advance(self,owner,status,now_ns):
        anchor=self.anchor;book=owner.book;protocol=owner.protocol;config=protocol['maker'];now=now_ns/1e6
        start=anchor['origin_ms'];grace=config.get('evidence_wait_ms',10000)
        for life in sorted({a['lifetime_ms'] for a in config['arms']}):
            arms=[a for a in config['arms'] if a['lifetime_ms']==life and a['id'] not in self.executions]
            if not arms:continue
            horizon=life+100;target=start+horizon
            if now<target:continue
            target_mono=anchor['order']['metadata'].get('arrival_receive_monotonic_ns',0)+horizon*1_000_000
            state=continuity(anchor,book,status,target,now,target_mono)
            if state==WAITING and now<target+grace:
                self.waits+=1;continue
            selected=copy.deepcopy(protocol);selected['maker']['arms']=arms
            result=replay_anchor(anchor,book,status,selected,owner.binary,evaluation_ms=horizon,
                                 include_markouts=False,require_all_features=False)
            if state!='OBSERVED':
                for arm in result['arms']:
                    arm.update(state='PUBLICATION_TIMEOUT_CENSORED' if state==WAITING else state,
                               operational_filled_shares=None,fills=[])
            # A fresh valid arrival book is mandatory for every arm. Flow
            # history is additionally mandatory only for the flow-selected arm;
            # unobserved flow is never zero-filled into that selection rule.
            source=preserve_source(owner.output,result['source'])
            row=owner.emit('MAKER_EXECUTION_WINDOW',market_id=anchor['market_id'],token_id=anchor['token_id'],
                anchor_record_id=anchor['order']['record_id'],execution_horizon_ms=horizon,
                arms=result['arms'],source_evidence=source,waiting_polls=self.waits,
                evidence_state=state,freeze_semantics='EXECUTION_RESULT_IMMUTABLE_BEFORE_LATER_MARKOUT')
            self.apply(row)
        for aid,arm in self.executions.items():
            for index,fill in enumerate(arm['fills']):
                for horizon in config['markout_horizons_ms']:
                    key=f'{aid}|{index}|{horizon}'
                    if key in self.markouts:continue
                    target_mono=fill['receive_monotonic_ns']+horizon*1_000_000
                    target_ms=start+(target_mono-anchor['order']['metadata']['arrival_receive_monotonic_ns'])/1e6
                    if now<target_ms:continue
                    state=continuity(anchor,book,status,target_ms,now,target_mono)
                    if state==WAITING and now<target_ms+grace:continue
                    cut=None
                    if state=='OBSERVED':
                        cut=next((r for r in reversed(book.history.get((anchor['market_id'],anchor['token_id']),[]))
                                  if r.get('receive_monotonic_ns',0)<=target_mono),None)
                        if (not cut or not cut.get('valid') or not cut.get('lineage_continuous')
                                or not 0<cut['best_bid']<cut['best_ask']<1):state='BOOK_AT_MARKOUT_CENSORED';cut=None
                    value=None
                    if state=='OBSERVED' and cut:
                        value={'mid_minus_fill':(cut['best_bid']+cut['best_ask'])/2-fill['price'],
                            'best_bid_minus_fill':cut['best_bid']-fill['price'],'bid_depth_l1':cut['bid_depth_l1'],
                            'liquidation_depth_sufficient':cut['bid_depth_l1']>=fill['quantity'],'source_cut':cut}
                    row=owner.emit('MAKER_MARKOUT_LABEL',market_id=anchor['market_id'],token_id=anchor['token_id'],
                        anchor_record_id=anchor['order']['record_id'],markout_key=key,arm=aid,fill_index=index,
                        horizon_ms=horizon,target_receive_monotonic_ns=target_mono,
                        state='PUBLICATION_TIMEOUT_CENSORED' if state==WAITING else state,markout=value,
                        proof={'status':status,'consumed_sequence':book.sequence,'gap_counter':book.gaps})
                    self.apply(row)
        complete=len(self.executions)==len(config['arms']) and all(
            f"{aid}|{i}|{h}" in self.markouts for aid,a in self.executions.items()
            for i,_ in enumerate(a['fills']) for h in config['markout_horizons_ms'])
        if not complete:return None
        arms=copy.deepcopy(list(self.executions.values()))
        for arm in arms:
            for i,fill in enumerate(arm['fills']):
                fill['markouts']={str(h):self.markouts[f"{arm['arm']}|{i}|{h}"]['markout'] for h in config['markout_horizons_ms']}
                fill['markout_states']={str(h):self.markouts[f"{arm['arm']}|{i}|{h}"]['state'] for h in config['markout_horizons_ms']}
        return {'arms':arms,'execution_sources':self.sources,'waiting_polls':self.waits,
                'comparison_semantics':'PAIRED_RESEARCH_COUNTERFACTUAL_NOT_ADDITIONAL_CANONICAL_FILLS',
                'validity_semantics':'FROZEN_EXECUTION_AND_INDEPENDENT_MARKOUT_WINDOWS',
                'book_scope':'L1_PLUS_AGGREGATE_FEATURES_NO_FULL_DEPTH_REPLAY'}
