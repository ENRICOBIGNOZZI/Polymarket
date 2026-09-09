#!/usr/bin/env python3
"""Four reproducible research datasets from immutable permanent source revisions.

This builder consumes explicit revisions, not the current installed model. Source
records and incompatible generations remain in the permanent byte store. A
materialization is a content-addressed view, never replacement canonical cash.
"""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import time
from v7_evidence_store import EvidenceStore,AUTH,canonical,digest,immutable
from v7_evidence_contract import identity
from v7_external_rich_train import build_rows
from v7_external_rich_model import FEATURE_NAMES,FEATURE_SCHEMA
from v7_profit_attribution import analyze,dec,metadata
from v7_causal_book import TARGET as BOOK_TARGET
from v7_external_lead_lag_collector import ORIGIN_SCHEMA,hydrate_label

SCHEMA='polymarket_v7_permanent_research_dataset_v1'
BUILDER_FILES=['v7_permanent_datasets.py','v7_evidence_contract.py','v7_external_rich_train.py','v7_external_rich_model.py','v7_profit_attribution.py','v7_maker_durable_learning.py','v7_external_lead_lag_collector.py','v7_compressed_journal.py']
BUILDER_BYTES={name:(Path(__file__).parent/name).read_bytes() for name in BUILDER_FILES}
BUILDER_HASHES={name:digest(raw) for name,raw in BUILDER_BYTES.items()}
DATASETS=('settlement_prediction','pm_response','maker_execution','economic_decision')


def plain(value):
    # Decimal cash fields stay exact decimal strings, never binary-float labels.
    return json.loads(json.dumps(value,default=lambda x:str(x) if isinstance(x,Decimal) else x,allow_nan=False))


def source_records(store,revisions):
    """A closed verified prefix is reproducible even after all source paths vanish."""
    unique={};refs=defaultdict(list);excluded=Counter()
    for revision in sorted(set(revisions)):
        source=store.revision(revision);family=(source.get('contract') or {}).get('source_family')
        if family not in {'canonical_ledger','fair_predictions','lead_lag','profit_experiments','markouts'}:
            excluded['SOURCE_FAMILY_NOT_USED_BY_THIS_DATASET_VIEW']+=1;continue
        path=source['relative_path']
        if '.jsonl' not in path and family!='markouts':
            excluded['NON_RECORD_SIDECAR_PRESERVED']+=1;continue
        if family=='markouts' and '.jsonl' not in path:
            raw=b''.join(store.bytes(revision));values=[json.loads(raw)]
        else:values=store.json_rows(revision)
        for row in values:
            if not isinstance(row,dict):raise ValueError('non-object evidence record')
            if family=='fair_predictions' and row.get('event_type') not in {'FORECAST','FORECAST_FINAL'}:
                excluded['NON_FORECAST_RECORD_PRESERVED_IN_SOURCE']+=1;continue
            if family=='profit_experiments':
                excluded['EXPERIMENT_RECORD_HANDLED_BY_PROTOCOL_REPORT']+=1;continue
            if (row.get('paper_only') is not True or row.get('authenticated_execution') is not False
                    or row.get('real_order_submission') is True):
                excluded['INCOMPATIBLE_AUTHORITY_PRESERVED']+=1;continue
            record_hash=digest(canonical(row))
            # Same raw record may exist in live, durable, checkpoints and imports.
            # Conflicting versions remain separate hashes and are detected at the
            # semantic join below instead of silently picking latest.
            unique[record_hash]=row
            ref={'source_revision':revision,'source_id':source['source_id'],'source_record_sha256':record_hash,
                 'source_family':family,'source_partition':source['partition']}
            if ref not in refs[record_hash]:refs[record_hash].append(ref)
    return unique,refs,dict(excluded)


def build(unique,refs,cutoff_ms):
    output={name:[] for name in DATASETS};excluded=Counter();forecasts=[];ledger=[];origins={};record_refs={}
    for sha,row in unique.items():
        source=refs[sha][0];family=source['source_family'];event=row.get('event_type')
        if family=='fair_predictions' and event in {'FORECAST','FORECAST_FINAL'}:
            forecasts.append(row)
            if event=='FORECAST':origins[row['forecast_id']]=row
        if family in {'canonical_ledger','markouts'}:
            key=(row.get('model_sha'),row.get('record_id'))
            if not all(key):excluded['LEGACY_LEDGER_IDENTITY_MISSING']+=1;continue
            if key in record_refs and record_refs[key]!=sha:raise ValueError('conflicting canonical ledger record')
            record_refs[key]=sha;ledger.append(row)
        if family=='lead_lag':
            if row.get('schema')==ORIGIN_SCHEMA:continue
            label_refs=list(refs[sha])
            origin_hash=row.get('origin_record_sha256')
            if origin_hash:
                if origin_hash not in unique:
                    excluded['PM_RESPONSE_MISSING_PERSISTED_ORIGIN']+=1;continue
                row=hydrate_label(row,unique[origin_hash])
                label_refs+=refs[origin_hash]
            origin_ns=row.get('origin_observed_wall_ns');label_ms=row.get('label_pm_receive_ts_ms')
            causal=row.get('target_semantics')==BOOK_TARGET
            realized=row.get('realized_horizon_ms');delta=row.get('delta_probability')
            if not isinstance(origin_ns,(int,float)) or origin_ns<=0:
                excluded['PM_RESPONSE_MISSING_OR_NONCAUSAL_CLOCK']+=1;continue
            target=row.get('label_target_ts_ms');available=row.get('label_available_after_receive_ms')
            eligible=causal and row.get('nominal_horizon_eligible') is True and row.get('label_state')=='CAUSAL_BOOK_OBSERVED'
            if eligible:
                cuts=row.get('label_book_cuts') or []
                eligible=(isinstance(target,(int,float)) and isinstance(available,(int,float))
                    and origin_ns/1e6<=target<=available and len(cuts)==2
                    and set(c.get('token_id') for c in cuts)=={row.get('yes_token'),row.get('no_token')}
                    and row.get('yes_token') and row.get('no_token') and row['yes_token']!=row['no_token']
                    and all(c.get('valid') is True and c.get('lineage_continuous') is True
                        and c.get('market_id')==row.get('market_id')
                        and c.get('observer_session_id')==row.get('observer_session_id') and row.get('observer_session_id')
                        and c.get('connection_epoch')==row.get('connection_epoch') and row.get('connection_epoch') is not None
                        and c.get('receive_wall_ms',float('inf'))<=target for c in cuts))
                if not eligible:excluded['PM_RESPONSE_CAUSAL_PROOF_INCOMPLETE']+=1
            elif not causal and (not isinstance(label_ms,(int,float)) or label_ms*1e6<origin_ns):
                excluded['PM_RESPONSE_LEGACY_CLOCK_INVALID']+=1;continue
            output['pm_response'].append({'identity':identity(row,source),'market_id':row.get('market_id'),
                'origin_id':row.get('origin_id'),'origin_observed_wall_ns':origin_ns,
                'horizon_requested_ms':row.get('horizon_ms'),'realized_horizon_ms':realized,
                'target_semantics':'RECEIVE_TIME_CAUSAL_BOOK_CUT' if causal else 'FIRST_OBSERVED_LATER_SNAPSHOT_LEGACY',
                'fixed_horizon_training_eligible':eligible,'label_state':row.get('label_state','LEGACY_OBSERVED_HORIZON'),
                'label_available_after_receive_ms':available,'target_timestamp_ms':target,
                'raw_model_features':row.get('rich_model_features'),
                'raw_causal_inputs':row.get('rich_feature_cut'),
                'feature_schema_version':row.get('feature_schema_version'),
                'causal_observation_schema':row.get('causal_observation_schema'),
                'feature_schema_hash':digest(canonical({'names':sorted((row.get('rich_model_features') or {}).keys()),'source_schema':row.get('schema')})),
                'delta_probability':delta,'delta_logit':row.get('delta_logit'),'label_receive_ms':label_ms,
                'label_pm_yes':row.get('label_pm_yes'),'origin_pm_yes':row.get('origin_pm_yes'),
                'original_observation':row,'sources':label_refs})
    clean,reasons=build_rows(forecasts,cutoff_ms);excluded.update(reasons)
    by_record={r.get('record_id'):r for r in forecasts}
    for row in clean:
        origin=origins[row['forecast_id']];final=by_record[row['final_record_id']]
        source_hash=digest(canonical(origin));final_hash=digest(canonical(final))
        source=refs[source_hash][0]
        output['settlement_prediction'].append({**row,'identity':identity(origin,source),
            'feature_schema_version':FEATURE_SCHEMA,'feature_schema_hash':digest(canonical({'version':FEATURE_SCHEMA,'names':FEATURE_NAMES})),
            'raw_causal_inputs':origin.get('rich_feature_cut') or {k:origin.get(k) for k in ['external_features','external_context','oracle_value','reference_value','observed_tte_seconds','observed_ms']},
            'stored_predictions':{'pm':origin.get('market_yes'),'execution_model':origin.get('model_yes'),
                'structural':origin.get('external_only_yes'),'hybrid':origin.get('hybrid_yes'),'rich_research':origin.get('research_model_yes')},
            'prediction_identities':{
                **{k:origin.get(k) for k in ['execution_probability_model_id','execution_probability_model_hash',
                    'research_model_model_id','research_model_model_hash','external_only_model_id',
                    'external_only_model_hash','hybrid_model_id','hybrid_model_hash','hybrid_model_recipe']},
                'research_model_id':origin.get('research_model_model_id') or origin.get('research_model_id'),
                'research_model_hash':origin.get('research_model_model_hash') or origin.get('research_model_hash')},
            'sources':refs[source_hash]+refs[final_hash]})
    # Dedupe by exact code/record identity before cash attribution. Research
    # counterfactual rows cannot enter this collection of canonical records.
    ledger_unique={}
    for row in ledger:
        if metadata(row).get('counterfactual') is True or metadata(row).get('excluded_from_portfolio_equity') is True:
            if row.get('event_type')!='MARKOUT':excluded['COUNTERFACTUAL_NOT_CANONICAL_CASH']+=1;continue
        ledger_unique[(row['model_sha'],row['record_id'])]=row
    ledger=list(ledger_unique.values());by_order=defaultdict(list)
    for row in ledger:
        if row.get('order_id'):by_order[(row['model_sha'],row['order_id'])].append(row)
    for (code,oid),events in by_order.items():
        order=next((r for r in events if r.get('event_type')=='ORDER_SUBMITTED' and metadata(r).get('component')=='professional_maker'),None)
        if order is None:continue
        terminal=sorted([r for r in events if r.get('event_type')=='ORDER_STATE'],key=lambda r:r.get('recorded_ts_ms') or 0)
        fills=[r for r in events if r.get('event_type')=='FILL' and dec(r.get('filled_size')) is not None and dec(r['filled_size'])>0]
        labels=metadata(terminal[-1]) if terminal else {}
        # Fill-level cumulative flow is also valid measured evidence; absence
        # remains null, never order_examples' numerical fitting defaults.
        measured=terminal+fills
        def observed_max(key):
            values=[dec(metadata(r).get(key)) for r in measured];values=[x for x in values if x is not None]
            return max(values) if values else None
        flow=observed_max('opposite_flow_prints_seen');reach=observed_max('price_reach_prints_seen')
        source_hash=digest(canonical(order));source=refs[source_hash][0];m=metadata(order)
        quantity=sum((dec(r['filled_size']) for r in fills),Decimal(0))
        row={'identity':identity(order,source),'market_id':order.get('market_id'),'order_id':oid,
            'origin_ms':order.get('decision_ts_ms'),'action':m.get('placement_action'),'economic_action':order.get('intended_action'),
            'features':m.get('placement_features'),'feature_schema_version':m.get('placement_features_schema'),
            'feature_schema_hash':digest(canonical({'version':m.get('placement_features_schema'),'names':sorted((m.get('placement_features') or {}).keys())})),
            'quote_price':order.get('limit_price'),'queue_ahead':order.get('queue_ahead'),'intended_quantity':order.get('intended_size'),
            'opposite_flow_prints':flow,'price_reach_prints':reach,'queue_exhausted':True if quantity>0 else None,
            'positive_operational_quantity':quantity,'terminal_observed':bool(terminal),'terminal_outcome':labels.get('execution_outcome'),
            'fills':fills,'markouts':[r for r in events if r.get('event_type')=='MARKOUT'],
            'finals':[r for r in events if r.get('event_type')=='FINAL'],
            'sources':[ref for r in events for ref in refs[digest(canonical(r))]]}
        output['maker_execution'].append(plain(row))
    attribution=analyze(ledger)
    for row in attribution['positions']:
        order_ids={f['order_id'] for f in row.get('fill_details',[]) if f.get('order_id')}
        related=[r for r in ledger if r['model_sha']==row['code_sha'] and
            (r.get('position_id')==row['position_id'] or r.get('order_id') in order_ids)]
        output['economic_decision'].append(plain({**row,'cash_scope':'CANONICAL_PAPER_LEDGER_ONLY',
            'sources':[ref for r in related for ref in refs[digest(canonical(r))]],
            'feature_schema_hash':digest(canonical({'schema':'same_decision_position_attribution_v2','builder_hashes':BUILDER_HASHES}))}))
    for name in DATASETS:output[name].sort(key=lambda r:canonical(r))
    return output,dict(excluded),{k:plain(attribution[k]) for k in ['canonical_final_positions','canonical_final_pnl','reconciled_positions','unattributed_ledger_pnl']}


def materialize(store_root,revisions,output_root,cutoff_ms):
    with EvidenceStore(store_root) as store:unique,refs,source_exclusions=source_records(store,revisions)
    data,exclusions,reconciliation=build(unique,refs,cutoff_ms)
    outputs={};root=Path(output_root)
    for name,raw in BUILDER_BYTES.items():
        immutable(root/'implementation_sources'/(BUILDER_HASHES[name]+'.py.gz'),gzip.compress(raw,mtime=0))
    for name,values in data.items():
        parts=[]
        for start in range(0,len(values),1000):
            payload=b''.join(canonical(row)+b'\n' for row in values[start:start+1000]);sha=digest(payload)
            path=Path('chunks')/name/(sha+'.jsonl.gz');compressed=gzip.compress(payload,mtime=0)
            immutable(root/path,compressed)
            if digest(gzip.decompress((root/path).read_bytes()))!=sha:raise ValueError('dataset checksum mismatch')
            parts.append({'path':str(path),'sha256':sha,'rows':len(values[start:start+1000]),'raw_bytes':len(payload),'compressed_bytes':len(compressed)})
        outputs[name]={'rows':len(values),'contracts':len({r.get('market_id') for r in values if r.get('market_id')}),'parts':parts}
    manifest={'schema':SCHEMA,**AUTH,'builder_hashes':BUILDER_HASHES,'cutoff_ms':cutoff_ms,'source_revisions':sorted(set(revisions)),
        'source_records':len(unique),'source_exclusions':source_exclusions,'record_exclusions':exclusions,
        'datasets':outputs,'canonical_reconciliation':reconciliation,'feature_preprocessing':'NO_FITTED_SCALING_IN_DATASET; NULLS_PRESERVED',
        'source_reconstruction':'REBUILD_FROM_EXPLICIT_IMMUTABLE_SOURCE_REVISIONS_AND_EXACT_BUILDER_HASHES'}
    manifest_hash=digest(canonical(manifest));immutable(root/'manifests'/(manifest_hash+'.json'),canonical(manifest))
    return {'manifest_hash':manifest_hash,**manifest}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--store',type=Path,required=True)
    ap.add_argument('--revision-list',type=Path,required=True,help='JSON array of frozen source revision hashes')
    ap.add_argument('--output',type=Path,required=True);ap.add_argument('--cutoff-ms',type=int,required=True)
    args=ap.parse_args();result=materialize(args.store,json.loads(args.revision_list.read_text()),args.output,args.cutoff_ms)
    print(json.dumps({k:result[k] for k in ['manifest_hash','source_records','datasets','record_exclusions','canonical_reconciliation']}))

if __name__=='__main__':main()
