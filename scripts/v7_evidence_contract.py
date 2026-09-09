"""Stable, explicitly nullable identities for permanent economic observations."""
from __future__ import annotations
import hashlib
import json
import math
from v7_evidence_store import AUTH, canonical, digest

SCHEMA='polymarket_v7_economic_evidence_identity_v1'


def hybrid_identity(external_model_hash, market_weight):
    """Identify the declared probability recipe; unknown base stays unknown."""
    weight=float(market_weight)
    if not math.isfinite(weight) or not 0 <= weight <= 1:
        raise ValueError('invalid hybrid market weight')
    base=str(external_model_hash or '')
    known=len(base)==64 and all(c in '0123456789abcdef' for c in base)
    recipe={'schema':'polymarket_v7_pm_logit_blend_recipe_v1',
            'external_probability_model_hash':base if known else None,
            'market_prior_logit_weight':weight,
            'probability_clip_epsilon':1e-9,
            'formula':'sigmoid((1-weight)*logit(external)+weight*logit(pm))'}
    return {'probability_model_id':'hybrid_fair',
            'probability_model_family':'PM_EXTERNAL_LOGIT_BLEND',
            'probability_model_hash':digest(canonical(recipe)) if known else None,
            'probability_model_recipe':recipe}


def identity(row, source):
    m=row.get('metadata') or {};envelope=m.get('opportunity_envelope') or {}
    settlement=envelope.get('settlement_model') or {}
    execution=m.get('execution_alpha') or envelope.get('execution_alpha') or {}
    context=envelope.get('crypto_context') or (m.get('coordinator_receipt') or {}).get('crypto_context') or {}
    protocol=row.get('experiment_protocol_id') or (row.get('protocol') or {}).get('protocol_id')
    result={'schema':SCHEMA,**AUTH,'code_sha':row.get('code_sha') or row.get('model_sha'),
      'run_id':row.get('run_id') or envelope.get('run_id') or source.get('run_id'),
      'market_id':row.get('market_id'),'event_id':row.get('event_id'),'token_id':row.get('token_id'),
      'contract_family':row.get('contract_family') or m.get('contract_family') or context.get('contract_family'),
      'asset':row.get('asset') or m.get('asset') or context.get('asset'),
      'horizon':row.get('horizon') or m.get('horizon') or context.get('horizon'),
      'decision_id':row.get('decision_id') or m.get('decision_id'),
      'opportunity_id':row.get('opportunity_id') or envelope.get('opportunity_id'),
      'replay_key':row.get('replay_key') or m.get('opportunity_replay_key') or envelope.get('deterministic_replay_key'),
      'order_id':row.get('order_id'),'fill_id':row.get('fill_id'),'position_id':row.get('position_id'),
      'settlement_model':{'model_id':settlement.get('model_id') or m.get('decision_probability_model_id') or row.get('model_id') or m.get('probability_model_id'),
        'model_hash':settlement.get('model_hash') or m.get('decision_probability_model_hash') or row.get('model_hash') or m.get('probability_model_hash'),
        'model_family':settlement.get('model_family') or row.get('model_family')},
      'execution_model':{'model_id':execution.get('model_id') or m.get('execution_model_id'),
                         'model_hash':execution.get('model_hash') or m.get('execution_model_hash') or m.get('maker_execution_model_hash'),
                         'policy_hash':m.get('policy_hash'),'config_hash':m.get('config_hash')},
      'portfolio_risk_policy':{'policy_hash':envelope.get('policy_hash') or row.get('portfolio_policy_hash'),
                             'config_hash':envelope.get('config_hash') or row.get('portfolio_config_hash')},
      'feature_schema_version':row.get('feature_schema_version') or m.get('feature_schema_version'),
      'research_protocol':{'protocol_id':protocol,'manifest_hash':row.get('manifest_sha256') or row.get('experiment_manifest_hash')},
      'timestamps':{'exchange_ms':row.get('exchange_ts_ms'),'receive_ms':row.get('receive_ts_ms') or row.get('receive_wall_ms'),
        'decision_ms':row.get('decision_ts_ms') or m.get('decision_observed_ts_ms'),
        'receive_monotonic_ns':row.get('receive_monotonic_ns'),'observation_ns':row.get('origin_ns') or row.get('observed_wall_ns'),
        'recorded_ms':row.get('recorded_ts_ms'),'timestamp_semantics_version':'EXCHANGE_RECEIVE_DECISION_PUBLICATION_DISTINCT_V1'},
      'source':source,'source_record_id':row.get('record_id') or row.get('origin_id'),
      'source_record_sha256':digest(canonical(row)),
      'missing_semantics':'NULL_IS_UNKNOWN_OR_NOT_APPLICABLE; NEVER_INFERRED_FROM_OUTCOME'}
    return result


def compatibility(left,right,question):
    """Compatibility is question-specific, never an excuse to discard a source."""
    reasons=[]
    if question=='model_independent_raw':
        for key in ('source_schema','timestamp_semantics_version','contract_semantics_hash'):
            if not left.get(key) or left.get(key)!=right.get(key):reasons.append('UNKNOWN_OR_INCOMPATIBLE_'+key.upper())
    elif question=='model_comparison':
        for key in ('source_schema','timestamp_semantics_version','contract_semantics_hash','selection_semantics_hash','target_semantics'):
            if not left.get(key) or left.get(key)!=right.get(key):reasons.append('UNKNOWN_OR_INCOMPATIBLE_'+key.upper())
    else:reasons.append('UNDECLARED_COMPARISON_QUESTION')
    return {'compatible':not reasons,'question':question,'exclusion_reasons':reasons,'preserve_both_sources':True,
            'model_identity_equality_required':False,'paired_observation_overlap_required':question=='model_comparison'}
