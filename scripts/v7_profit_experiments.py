"""Prospective observations in the existing collector; no execution authority.

The protocol is frozen before a future contract boundary. Replay uses only the
canonical native queue engine and the receive-sequenced public observer tape.
"""
from __future__ import annotations
from collections import Counter
import json
import gzip
import hashlib
import math
import os
from pathlib import Path
import subprocess
import time
from v7_profit_protocol import freeze, digest, bin_index, fixed_window_digest

AUTH={'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
      'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY','excluded_from_portfolio_equity':True}


def finite(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)


def append(path, row):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n')
        stream.flush()


def atomic(path,row):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+f'.tmp.{os.getpid()}')
    temp.write_text(json.dumps(row,sort_keys=True,allow_nan=False)+'\n');os.replace(temp,path)


def preserve_source(root, source):
    """Immutable compressed evidence; reports read references, not entire tapes."""
    payload=json.dumps(source,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    sha=hashlib.sha256(payload).hexdigest();relative=Path('sources')/(sha+'.json.gz');path=root/relative
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        temp=path.with_name(path.name+f'.tmp.{os.getpid()}')
        with temp.open('xb') as stream:
            stream.write(gzip.compress(payload,mtime=0));stream.flush();os.fsync(stream.fileno())
        try:os.link(temp,path)
        finally:temp.unlink()
    if path.is_symlink():raise ValueError('unsafe research source path')
    compressed=path.read_bytes()
    if hashlib.sha256(gzip.decompress(compressed)).hexdigest()!=sha:raise ValueError('research source hash mismatch')
    return {'source_sha256':sha,'source_path':str(relative),'source_compressed_sha256':hashlib.sha256(compressed).hexdigest(),
            'source_uncompressed_bytes':len(payload),'source_compressed_bytes':len(compressed),'source_book_rows':len(source['path'])}


def rows(path):
    if path.exists():
        with path.open('rb') as stream:
            for line in stream:
                if line.endswith(b'\n') and line.strip():yield json.loads(line)


class LedgerTail:
    def __init__(self,path):self.path=path;self.offset=0;self.inode=None
    def poll(self):
        if not self.path.exists():return []
        with self.path.open('rb') as stream:
            stat=os.fstat(stream.fileno());identity=(stat.st_dev,stat.st_ino)
            if identity!=self.inode or stat.st_size<self.offset:self.offset=0;self.inode=identity
            stream.seek(self.offset);out=[]
            while True:
                raw=stream.readline()
                if not raw or not raw.endswith(b'\n'):break
                self.offset=stream.tell()
                if raw.strip():out.append(json.loads(raw))
            return out


def fee_at(price,schedule):
    rate,exponent=schedule.get('rate'),schedule.get('exponent')
    if not finite(rate) or not finite(exponent) or not 0<=rate<=1 or not 0<exponent<=10:return None
    return rate*(price*(1-price))**exponent


class ProfitExperiments:
    def __init__(self,run_root,output,protocol_path,book,code_sha,binary):
        self.run_root,self.output,self.book,self.sha,self.binary=run_root,output,book,code_sha,binary
        self.protocol=json.loads(protocol_path.read_text());self.manifest=None
        self.pending={};self.maker_pending={};self.maker_windows={};self.selected=set();self.anchors=set();self.done=set()
        restored_windows=[]
        self.counts=Counter();self.last_scan=0.;self.last_error=None
        self.ledger=LedgerTail(run_root/'ledger/execution.jsonl')
        self.manifest_path=output/'manifest.json'
        self.status_path=run_root/'profit_experiment_status.json'
        self.accept_new_anchors=True
        if self.manifest_path.exists():
            old=json.loads(self.manifest_path.read_text())
            self.manifest=freeze(self.manifest_path,self.protocol,code_sha,old['frozen_model_hash'],time.time_ns(),cohort=old.get('cohort_identity'))
        for row in rows(output/'observations.jsonl'):
            if row.get('code_sha')!=code_sha:raise ValueError('profit output identity conflict')
            if row['kind']=='SIGNAL_SELECTION':self.selected.add(row['selection_key']);self.pending[row['selection_key']]=row
            elif row['kind']=='DELAY_LABEL':self.done.add((row['selection_key'],row['delay_ms']))
            elif row['kind']=='MAKER_ANCHOR':self.anchors.add(row['market_id']);self.maker_pending[row['market_id']]=row
            elif row['kind']=='MAKER_COMPARISON':self.maker_pending.pop(row['market_id'],None)
            elif row['kind'] in ('MAKER_EXECUTION_WINDOW','MAKER_MARKOUT_LABEL'):restored_windows.append(row)
            self.counts[row['kind']]+=1
        if self.protocol['maker'].get('validity_semantics')=='SEPARATE_EXECUTION_AND_MARKOUT_WINDOWS':
            from v7_maker_research_window import MakerWindow
            self.maker_windows={market:MakerWindow(anchor,restored_windows) for market,anchor in self.maker_pending.items()}

    def emit(self,kind,**data):
        row={'schema':'polymarket_v7_profit_observation_v1',**AUTH,'code_sha':self.sha,
             'experiment_protocol_id':self.protocol['protocol_id'],
             'manifest_sha256':self.manifest['manifest_sha256'],'recorded_ns':time.time_ns(),'kind':kind,**data}
        append(self.output/'observations.jsonl',row);self.counts[kind]+=1
        return row

    def tick(self,fair_status,origin,status):
        now=time.time_ns();fair=fair_status.get('fair') or {};market=fair_status.get('market') or {}
        if origin and not self.manifest:
            self.manifest=freeze(self.manifest_path,self.protocol,self.sha,str(fair.get('probability_model_hash') or ''),now)
        if not self.manifest:return
        if origin and origin['origin_observed_wall_ns']>=self.manifest['forward_start_ns']:
            if fair.get('probability_model_hash')==self.manifest['frozen_model_hash']:
                self.select_signal(origin,fair,market,status)
            else:self.last_error='FROZEN_MODEL_CHANGED_OBSERVATION_REJECTED'
        self.label_signals(status,now)
        if time.monotonic()-self.last_scan>=.25:
            self.last_scan=time.monotonic()
            self.collect_anchors(now)
            self.finish_makers(status,now)
            self.seal_confirmatory_window(now)
            atomic(self.status_path,{'schema':'polymarket_v7_profit_experiment_status_v1',
                **AUTH,'code_sha':self.sha,'timestamp_ms':now//1000000,'manifest':self.manifest,
                'counts':dict(self.counts),'pending_signals':len(self.pending),'pending_maker_anchors':len(self.maker_pending),
                'last_error':self.last_error,'state':'COLLECTING' if now>=self.manifest['forward_start_ns'] else 'AWAITING_PREREGISTERED_BOUNDARY'})

    def seal_confirmatory_window(self,now):
        end=self.manifest.get('confirmatory_end_ns');path=self.output/'confirmatory_window_closure.json'
        if end is None or now<end or path.exists():return
        if any(r['origin_ns']<end for r in self.pending.values()):return
        if any(r['origin_ms']*1_000_000<end for r in self.maker_pending.values()):return
        source=self.output/'observations.jsonl'
        if not source.exists():return
        # All prior observations must be durable before the producer seal appears.
        with source.open('rb') as stream:os.fsync(stream.fileno())
        observations=list(rows(source))
        value={'schema':'polymarket_v7_confirmatory_window_closure_v1',**AUTH,
            'manifest_sha256':self.manifest['manifest_sha256'],'closed_at_ns':now,
            'forward_end_ns':end,'window_observations_sha256':fixed_window_digest(observations,self.manifest),
            'pending_window_signals':0,'pending_window_maker_anchors':0,
            'scope':'OBSERVED_SELECTED_POPULATION; NOT_PROOF_OF_UNOBSERVED_OPPORTUNITY_COVERAGE'}
        value['closure_sha256']=digest(value)
        from v7_evidence_store import immutable,canonical
        immutable(path,canonical(value))

    def select_signal(self,origin,fair,market,status):
        ms=origin['origin_observed_wall_ns']/1e6
        p,tte=fair.get('yes'),fair.get('tte_seconds');config=self.protocol['signal']
        if not finite(p) or not 0<=p<=1 or not finite(tte):return
        ti=bin_index(tte,config['tte_edges_seconds'])
        if ti is None:return
        evidence=self.book.label(origin['market_id'],origin['yes_token'],origin['no_token'],ms,ms,status)
        if not evidence:return
        schedule=market.get('fee_schedule') or {}
        if market.get('fees_enabled_explicit') is not True:return
        for index,outcome in enumerate(('YES','NO')):
            cut=evidence['origin_book_cuts'][index];prob=p if index==0 else 1-p
            fee=fee_at(cut['best_ask'],schedule)
            if fee is None:continue
            margin=prob-cut['best_ask']-fee-config['execution_risk_per_share'];mi=bin_index(margin,config['margin_edges'])
            if mi is None or margin<config['minimum_point_margin_after_costs']:continue
            key=f"{origin['market_id']}|{outcome}|{mi}|{ti}"
            if key in self.selected:continue
            row=self.emit('SIGNAL_SELECTION',selection_key=key,market_id=origin['market_id'],token_id=cut['token_id'],outcome=outcome,
                origin_ns=origin['origin_observed_wall_ns'],yes_token=origin['yes_token'],no_token=origin['no_token'],
                model_probability=prob,pm_probability=evidence['origin_pm_yes'] if index==0 else 1-evidence['origin_pm_yes'],
                probability_bounds=[fair.get('lower'),fair.get('upper')] if index==0 else [1-fair.get('upper',1),1-fair.get('lower',0)],probability_interval_validated=fair.get('probability_interval_validated'),
                feature_sha256=origin['rich_feature_sha256'],model_hash=fair['probability_model_hash'],
                model_id=fair.get('probability_model_id'),model_family=fair.get('family'),event_id=market.get('event_id'),
                feature_schema_version=origin.get('feature_schema_version'),
                raw_feature_cut=origin.get('rich_feature_cut'),raw_model_features=origin.get('rich_model_features'),
                margin_bin=mi,tte_bin=ti,tte_seconds=tte,point_net_margin=margin,fee_schedule=schedule,
                origin_book=cut,book_scope='L1_PLUS_AGGREGATE_FEATURES_NO_FULL_DEPTH_REPLAY')
            self.selected.add(key);self.pending[key]=row

    def label_signals(self,status,now):
        config=self.protocol['signal']
        for key,row in list(self.pending.items()):
            for delay in config['delays_ms']:
                if (key,delay) in self.done:continue
                target=row['origin_ns']/1e6+delay
                if now/1e6<target:continue
                evidence=self.book.label(row['market_id'],row['yes_token'],row['no_token'],row['origin_ns']/1e6,target,status)
                if not evidence and now/1e6<target+config['label_grace_ms']:continue
                data={'state':'BOOK_CONTINUITY_OR_WATERMARK_CENSORED','book_cut':None,'point_net_margin':None,'cost_stress':None}
                if evidence:
                    cut=evidence['label_book_cuts'][0 if row['outcome']=='YES' else 1]
                    fee=fee_at(cut['best_ask'],row['fee_schedule']);depth=cut.get('ask_depth_l1')
                    state='OBSERVED' if finite(depth) and depth>=config['standardized_quote_quantity'] and fee is not None else 'DEPTH_OR_FEE_CENSORED'
                    cost=None if fee is None else fee+config['execution_risk_per_share']
                    data={'state':state,'book_cut':cut,'fee_per_share':fee,'risk_allowance_per_share':config['execution_risk_per_share'],
                        'point_net_margin':row['model_probability']-cut['best_ask']-cost if state=='OBSERVED' else None,
                        'cost_stress':{str(x):row['model_probability']-cut['best_ask']-x*cost for x in config['cost_stress_multipliers']} if state=='OBSERVED' else None}
                self.emit('DELAY_LABEL',selection_key=key,market_id=row['market_id'],token_id=row['token_id'],delay_ms=delay,
                    target_receive_ms=target,fixed_signal_probability=row['model_probability'],**data)
                self.done.add((key,delay))
            if all((key,d) in self.done for d in config['delays_ms']):self.pending.pop(key)

    def collect_anchors(self,now):
        for order in self.ledger.poll():
            if not self.accept_new_anchors:continue
            m=order.get('metadata') or {};market=str(order.get('market_id') or '')
            if (order.get('event_type')!='ORDER_SUBMITTED' or m.get('component')!='professional_maker' or market in self.anchors
                or order.get('model_sha')!=self.sha or order.get('paper_only') is not True or order.get('authenticated_execution') is not False
                or m.get('counterfactual') is True or m.get('excluded_from_portfolio_equity') is True
                or (order.get('receive_ts_ms') or 0)*1000000<self.manifest['forward_start_ns']):continue
            if self.protocol['maker'].get('validity_semantics')=='SEPARATE_EXECUTION_AND_MARKOUT_WINDOWS':
                actual_model=((m.get('opportunity_envelope') or {}).get('settlement_model') or {}).get('model_hash')
                if actual_model!=self.manifest['frozen_model_hash']:continue
            self.anchors.add(market)
            row=self.emit('MAKER_ANCHOR',market_id=market,token_id=order['token_id'],origin_ms=order['receive_ts_ms'],
                order=order,book_gap_counter=self.book.gaps,observer_session_id=self.book.session,connection_epoch=self.book.epoch)
            self.maker_pending[market]=row

    def finish_makers(self,status,now):
        if self.protocol['maker'].get('validity_semantics')=='SEPARATE_EXECUTION_AND_MARKOUT_WINDOWS':
            from v7_maker_research_window import MakerWindow
            for market,anchor in list(self.maker_pending.items()):
                window=self.maker_windows.setdefault(market,MakerWindow(anchor))
                result=window.advance(self,status,now)
                if result is not None:
                    self.emit('MAKER_COMPARISON',market_id=market,token_id=anchor['token_id'],
                        anchor_record_id=anchor['order']['record_id'],**result)
                    self.maker_pending.pop(market);self.maker_windows.pop(market)
            return
        for market,anchor in list(self.maker_pending.items()):
            # Wait through longer life + cancel latency + longest markout.
            if now/1e6<anchor['origin_ms']+42000:continue
            result=replay_anchor(anchor,self.book,status,self.protocol,self.binary)
            self.emit('MAKER_COMPARISON',market_id=market,token_id=anchor['token_id'],anchor_record_id=anchor['order']['record_id'],
                      **preserve_source(self.output,result['source']),**{k:v for k,v in result.items() if k!='source'})
            self.maker_pending.pop(market)


def replay_anchor(anchor,book,status,protocol,binary, *, evaluation_ms=42000, include_markouts=True, require_all_features=True):
    order=anchor['order'];m=order['metadata'];start=anchor['origin_ms'];market=anchor['market_id'];token=anchor['token_id']
    history=list(book.history.get((market,token),[]));arrival=m.get('arrival_receive_monotonic_ns') or 0
    origin=next((r for r in reversed(history) if r.get('receive_monotonic_ns',0)<=arrival and r['receive_wall_ms']<=start),None)
    reason=None
    if (not origin or anchor['book_gap_counter']!=book.gaps or anchor['observer_session_id']!=book.session
        or anchor['connection_epoch']!=book.epoch or not history or history[0]['receive_wall_ms']>start
        or book.watermark_ms<start+evaluation_ms or status.get('evidence_complete') is not True
        or getattr(book,'watermark_monotonic_ns',0)<arrival+evaluation_ms*1000000
        or status.get('book_watermark_receive_monotonic_ns',0)<arrival+evaluation_ms*1000000
        or status.get('model_sha')!=book.model_sha or status.get('observer_session_id')!=book.session
        or status.get('connection_epoch')!=book.epoch or status.get('state')!='running'
        or status.get('paper_only') is not True or status.get('authenticated_execution') is not False or status.get('real_order_submission') is not False
        or status.get('book_events_written',0)>book.sequence or status.get('book_watermark_receive_wall_ms',0)<start+evaluation_ms
        or not 0<=time.time_ns()/1e6-status.get('timestamp_ms',0)<=2000):reason='BOOK_CONTINUITY_CENSORED'
    if origin and (start-origin['receive_wall_ms']>protocol['maker']['maximum_feature_age_ms'] or (require_all_features and origin.get('features_valid') is not True)):reason='STALE_OR_INCOMPLETE_FEATURES'
    if not m.get('arrival_receive_monotonic_ns') or not m.get('arrival_exchange_event_ns'):reason='MISSING_NATIVE_ARRIVAL_CLOCK'
    path=[r for r in history if arrival<=r.get('receive_monotonic_ns',0)<=arrival+evaluation_ms*1000000]
    if origin and (origin.get('valid') is not True or origin.get('lineage_continuous') is not True
                   or origin.get('receive_monotonic_ns',0)<=0):reason='INVALID_ARRIVAL_BOOK'
    if origin and any(r.get('tick_size')!=origin['tick_size'] for r in path):reason='TICK_REGIME_CHANGED'
    invalid_books=sum(r.get('valid') is not True or r.get('lineage_continuous') is not True for r in path)
    output=[];source={'anchor':anchor,'origin_book':origin,'path':path,
        'evaluation_ms':evaluation_ms,'proof':{'status':status,'consumed_sequence':book.sequence,
        'consumed_watermark_ms':book.watermark_ms,'consumed_watermark_monotonic_ns':getattr(book,'watermark_monotonic_ns',0),
        'gap_counter':book.gaps,'session':book.session,'epoch':book.epoch}}
    for arm in protocol['maker']['arms']:
        arm_reason=reason
        if (not require_all_features and arm.get('minimum_opposite_prints_per_second') is not None
                and origin and origin.get('features_valid') is not True):arm_reason='ORIGIN_FLOW_FEATURES_UNAVAILABLE'
        execution_path=[r for r in path if r.get('receive_monotonic_ns',0)<=arrival+(arm['lifetime_ms']+100)*1000000]
        if any('public_trade' not in r for r in execution_path):arm_reason='MISSING_TRADE_PAYLOAD_CENSORED'
        if any(r.get('public_trade') and (r.get('valid') is not True or r.get('lineage_continuous') is not True) for r in execution_path):
            arm_reason='TRADE_LINEAGE_CENSORED'
        # The native resting-order engine consumes public prints, not book
        # deltas. An invalid intermediate book without a lost/invalid print is
        # therefore not a missing queue input. Transport gaps remain censored.
        row={'arm':arm['id'],'state':arm_reason or 'OBSERVED','operational_filled_shares':None,'fills':[],'counterfactual':True,
             'intermediate_invalid_book_rows':invalid_books,
             'replay_input_basis':'CONTINUOUS_TRANSPORT_VALID_PRINTS_AND_VALID_ARRIVAL_BOOK'}
        if not arm_reason:
            qty=math.floor(order['intended_size']*1e6)/1e6;tick=origin['tick_size'];price=origin['best_bid']+(tick if arm['placement']=='IMPROVE1' else 0)
            cap=(m.get('opportunity_envelope') or {}).get('exploration',{}).get('probe_loss_cap')
            if not finite(cap):cap=order['intended_size']*order['limit_price']
            qty=min(qty,math.floor(cap/(origin['best_bid']+tick)*1e6)/1e6)
            row['common_quote_quantity']=qty
            flow=origin['placement_features'].get('aggressive_sell_prints_per_second')
            if price>=origin['best_ask']-1e-9:row['state']='POST_ONLY_INELIGIBLE'
            elif qty*price>cap+1e-6:row['state']='ANCHOR_LOSS_CAP_INELIGIBLE'
            elif arm.get('minimum_opposite_prints_per_second') is not None and (not finite(flow) or flow<arm['minimum_opposite_prints_per_second']):
                row.update(state='FLOW_FILTER_ABSTAIN',operational_filled_shares=0.,fills=[])
            else:
                start_ns=m['arrival_receive_monotonic_ns']-1000000
                trades=[{**r['public_trade'],'receive_monotonic_ns':r['receive_monotonic_ns'],'observer_sequence':r['observer_sequence']}
                        for r in path if r.get('public_trade') and start_ns<=r['receive_monotonic_ns']<=start_ns+(arm['lifetime_ms']+101)*1000000]
                request={'start_ns':start_ns,'exchange_ns':m['arrival_exchange_event_ns'],'tick':tick,'price':price,'quantity':qty,
                    'best_ask':origin['best_ask'],'queue_ahead':origin['bid_depth_l1'] if arm['placement']=='JOIN' else 0.,'lifetime_ms':arm['lifetime_ms'],'trades':trades}
                try:
                    completed=subprocess.run([str(binary)],input=json.dumps(request),text=True,capture_output=True,timeout=5,check=True)
                    native=json.loads(completed.stdout);row.update(native);row.update(arm=arm['id'],state='OBSERVED',research_request=request)
                    for fill in row['fills']:
                        fill['markouts']={}
                        for h in protocol['maker']['markout_horizons_ms'] if include_markouts else []:
                            cut=next((r for r in reversed(history) if r.get('receive_monotonic_ns',0)<=fill['receive_monotonic_ns']+h*1000000),None)
                            if cut and (cut.get('valid') is not True or cut.get('lineage_continuous') is not True
                                        or not 0<cut['best_bid']<cut['best_ask']<1):cut=None
                            fill['markouts'][str(h)]={'mid_minus_fill':(cut['best_bid']+cut['best_ask'])/2-price,
                                'best_bid_minus_fill':cut['best_bid']-price,'bid_depth_l1':cut['bid_depth_l1'],
                                'liquidation_depth_sufficient':cut['bid_depth_l1']>=fill['quantity'],'source_cut':cut} if cut else None
                except (OSError,subprocess.SubprocessError,ValueError,KeyError) as exc:row.update(state='NATIVE_REPLAY_CENSORED',error=str(exc),operational_filled_shares=None,fills=[])
        output.append(row)
    return {'arms':output,'source':source,'book_scope':'L1_PLUS_AGGREGATE_FEATURES_NO_FULL_DEPTH_REPLAY',
        'comparison_semantics':'PAIRED_RESEARCH_COUNTERFACTUAL_NOT_ADDITIONAL_CANONICAL_FILLS'}
