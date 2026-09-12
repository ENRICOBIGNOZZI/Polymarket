"""Immutable prospective research identity; no trading permission."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

SCHEMA='polymarket_v7_profit_experiment_manifest_v1'
V5_PROTOCOL_ID='permanent-profit-causes-20260911-v5'
V6_PROTOCOL_ID='permanent-profit-causes-20260912-v6'
V6_RESIDUAL_PREFIX='btc-m5-rich-logit-residual-'
WORST_CASE_CENSOR_MODE='WORST_CASE_LOWER_SUPPORT_IMPUTATION'
CENSOR_ENDPOINTS={'selected_settlement_surplus_cost2_delay1000','maker_join10_minus_join5_settlement_net_cost2'}
PROTOCOL_BOOTSTRAP_SEED={V5_PROTOCOL_ID:20260908,V6_PROTOCOL_ID:20260912}
FIVE_MIN_NS=300_000_000_000


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _validate_confirmatory_censoring(protocol_id,censor):
    if protocol_id not in (V5_PROTOCOL_ID,V6_PROTOCOL_ID):
        if censor is not None:raise ValueError('profit_protocol:censoring_requires_registered_protocol')
        return
    caps=censor.get('endpoint_max_censor_fraction') if isinstance(censor,dict) else None
    if (not isinstance(censor,dict) or censor.get('mode')!=WORST_CASE_CENSOR_MODE
            or censor.get('prospective_only') is not True or censor.get('no_censor_dropping') is not True
            or censor.get('terminal_records_required') is not True or censor.get('verified_settlements_required') is not True
            or not isinstance(caps,dict) or set(caps)!=CENSOR_ENDPOINTS
            or any(isinstance(v,bool) or not isinstance(v,(int,float)) or float(v)!=0.05 for v in caps.values())
            or censor.get('missing_terminal_record_policy')!='FAIL_CLOSED_NOT_A_CENSOR'
            or censor.get('missing_settlement_policy')!='FAIL_CLOSED_NOT_A_CENSOR'):
        raise ValueError('profit_protocol:confirmatory_censoring_identity')


def _validate_v6_maker(maker):
    if (maker.get('anchor')!='FIRST_PROSPECTIVELY_ELIGIBLE_CANONICAL_MAKER_ORDER_PER_CONTRACT'
            or maker.get('anchor_eligibility_semantics')!='ARRIVAL_TIME_ONLY_NO_FUTURE_FILL_MARKOUT_OR_SETTLEMENT'
            or maker.get('anchor_skip_audit_required') is not True
            or maker.get('anchor_requires_valid_arrival_book') is not True
            or maker.get('anchor_requires_continuous_lineage') is not True
            or maker.get('anchor_requires_fresh_features') is not True):
        raise ValueError('profit_protocol:v6_maker_anchor_identity')
    wait=maker.get('anchor_eligibility_wait_ms')
    age=maker.get('maximum_feature_age_ms')
    if (isinstance(wait,bool) or not isinstance(wait,int) or not 100<=wait<=5000
            or isinstance(age,bool) or not isinstance(age,int) or not 1<=age<=1000):
        raise ValueError('profit_protocol:v6_maker_anchor_timing')


def _validate_v6_signal_and_replication(signal,confirm):
    if (signal.get('required_probability_model_id_prefix')!=V6_RESIDUAL_PREFIX
            or signal.get('required_model_semantics')!='POLYMARKET_LOGIT_OFFSET_PLUS_EXTERNAL_RESIDUAL'):
        raise ValueError('profit_protocol:v6_residual_model_identity')
    if (confirm.get('replication_count')!=3
            or confirm.get('replication_schedule')!='THREE_CONTIGUOUS_NONOVERLAPPING_WINDOWS_SAME_FROZEN_MODEL'
            or confirm.get('same_frozen_model_across_replications') is not True
            or confirm.get('retraining_between_replications') is not False
            or confirm.get('replication_results_pooled_for_primary_claim') is not False):
        raise ValueError('profit_protocol:v6_replication_identity')


def validate(protocol):
    if (protocol.get('schema')!='polymarket_v7_profit_experiment_protocol_v1'
            or protocol.get('paper_only') is not True or protocol.get('authenticated_execution') is not False
            or protocol.get('real_order_submission') is not False
            or protocol.get('execution_authority')!='ZERO_AUTHORITY_RESEARCH_ONLY'):
        raise ValueError('profit_protocol:authority')
    protocol_id=protocol.get('protocol_id')
    signal=protocol['signal'];maker=protocol['maker'];inference=protocol['inference']
    for edges in (signal['margin_edges'],signal['tte_edges_seconds']):
        if len(edges)<2 or any(isinstance(v,bool) or not isinstance(v,(int,float)) for v in edges) or sorted(set(edges))!=edges:
            raise ValueError('profit_protocol:bins')
    if signal['delays_ms'] != [0,100,250,500,1000] or signal['cost_stress_multipliers'] != [1.,1.5,2.]:
        raise ValueError('profit_protocol:measurement_grid')
    if maker['comparison']!='PAIRED_NATIVE_ENGINE_RESEARCH_REPLAY_NO_ADDITIONAL_ROUTER':
        raise ValueError('profit_protocol:execution_owner')
    if maker.get('required_replay_evidence')!='CONTINUOUS_TRANSPORT_VALID_PRINTS_AND_VALID_ARRIVAL_BOOK' or maker.get('markout_evidence')!='VALID_BOOK_AT_EACH_MARKOUT_OTHERWISE_CENSORED':
        raise ValueError('profit_protocol:replay_observation_requirements')
    if maker['post_only_required'] is not True or maker['preserve_anchor_probe_loss_cap'] is not True:
        raise ValueError('profit_protocol:execution_boundaries')
    if [(x.get('id'),x.get('placement'),x.get('lifetime_ms')) for x in maker['arms']] != [('JOIN_5S','JOIN',5000),('FLOW_JOIN_5S','JOIN',5000),('JOIN_10S','JOIN',10000),('IMPROVE1_5S','IMPROVE1',5000)]:
        raise ValueError('profit_protocol:maker_arms')
    expected_seed=PROTOCOL_BOOTSTRAP_SEED.get(protocol_id,20260908)
    if inference.get('bootstrap_draws')!=20000 or inference.get('bootstrap_seed')!=expected_seed or inference.get('minimum_contracts_for_interval')!=12:
        raise ValueError('profit_protocol:inference_identity')
    if inference['automatic_promotion'] is not False or inference['automatic_sizing_change'] is not False:
        raise ValueError('profit_protocol:no_promotion')
    if protocol_id==V6_PROTOCOL_ID:_validate_v6_maker(maker)
    if 'confirmatory' in inference:
        confirm=inference['confirmatory']
        duration_hours=confirm.get('duration_hours');legacy_days=confirm.get('calendar_days')
        duration_valid=(duration_hours==8 and legacy_days is None) or (legacy_days==7 and duration_hours is None)
        if (confirm.get('primary_endpoints')!=['model_brier_improvement_over_pm','selected_settlement_surplus_cost2_delay1000','maker_join10_minus_join5_settlement_net_cost2']
            or confirm.get('family_size')!=3 or not duration_valid
            or confirm.get('look_policy')!='ONE_FIXED_END_OF_WINDOW_ANALYSIS_NO_EARLY_PROMOTION'
            or inference.get('temporal_block_contracts')!=[3,6,12]
            or inference.get('minimum_temporal_blocks')!=8 or inference.get('minimum_tail_draws')!=50):
            raise ValueError('profit_protocol:confirmatory_identity')
        if protocol_id==V6_PROTOCOL_ID:_validate_v6_signal_and_replication(signal,confirm)
        _validate_confirmatory_censoring(protocol_id,confirm.get('censoring'))
    digest(protocol)  # Reject nonfinite JSON.


def freeze(path: Path, protocol: dict, code_sha: str, model_hash: str, now_ns: int, *, cohort: dict | None = None, forward_start_ns: int | None = None):
    validate(protocol)
    for value,length in ((code_sha,40),(model_hash,64)):
        if len(value)!=length or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('profit_protocol:exact_identity')
    if forward_start_ns is not None:
        if (isinstance(forward_start_ns,bool) or not isinstance(forward_start_ns,int)
                or forward_start_ns<=0 or forward_start_ns%FIVE_MIN_NS!=0):
            raise ValueError('profit_protocol:forward_start_boundary')
    identity={'protocol_sha256':digest(protocol),'code_sha':code_sha,'frozen_model_hash':model_hash}
    if cohort is not None:identity['cohort_identity']=cohort
    if path.exists():
        existing=json.loads(path.read_text())
        if existing.get('schema')!=SCHEMA or any(existing.get(k)!=v for k,v in identity.items()):
            raise ValueError('profit_protocol:immutable_identity_changed')
        if forward_start_ns is not None and existing.get('forward_start_ns')!=forward_start_ns:
            raise ValueError('profit_protocol:immutable_forward_start_changed')
        if digest({k:v for k,v in existing.items() if k!='manifest_sha256'})!=existing.get('manifest_sha256'):
            raise ValueError('profit_protocol:manifest_hash_mismatch')
        return existing
    start=forward_start_ns if forward_start_ns is not None else ((now_ns//FIVE_MIN_NS)+1)*FIVE_MIN_NS
    value={'schema':SCHEMA,**identity,'protocol':protocol,'created_ns':now_ns,
        'forward_start_ns':start,
        'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
        'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY'}
    confirm=protocol['inference'].get('confirmatory')
    if confirm:
        if confirm.get('duration_hours') is not None:
            duration_ns=int(confirm['duration_hours'])*3_600_000_000_000
        else:
            duration_ns=int(confirm['calendar_days'])*86_400_000_000_000
        value['confirmatory_end_ns']=value['forward_start_ns']+duration_ns
    value['manifest_sha256']=digest(value)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+f'.tmp.{os.getpid()}')
    with temporary.open('x') as stream:
        stream.write(json.dumps(value,sort_keys=True,indent=2)+'\n');stream.flush();os.fsync(stream.fileno())
    try:os.link(temporary,path)  # Publish atomically without replacing a prior identity.
    finally:temporary.unlink()
    return value


def bin_index(value,edges):
    for index,(low,high) in enumerate(zip(edges,edges[1:])):
        if low<=value<high:return index
    return None


def fixed_window_digest(observations,manifest):
    """Identity of every selected origin and its terminal window evidence."""
    start,end=manifest['forward_start_ns'],manifest['confirmatory_end_ns']
    selections={r['selection_key'] for r in observations if r['kind']=='SIGNAL_SELECTION' and start<=r['origin_ns']<end}
    anchors={r['order']['record_id'] for r in observations if r['kind']=='MAKER_ANCHOR' and start<=r['origin_ms']*1_000_000<end}
    def inside(row):
        kind=row['kind']
        if kind in ('SIGNAL_SELECTION','DELAY_LABEL'):return row['selection_key'] in selections
        if kind=='MAKER_ANCHOR':return row['order']['record_id'] in anchors
        return kind.startswith('MAKER_') and row.get('anchor_record_id') in anchors
    return digest(sorted(digest(r) for r in observations if inside(r)))
