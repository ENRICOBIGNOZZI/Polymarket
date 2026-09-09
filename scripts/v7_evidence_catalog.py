#!/usr/bin/env python3
"""Versioned V7 source catalog and exhaustive file inventory, with no authority.

Catalog entries distinguish raw sources from projections. Unclassified files are
retained as UNKNOWN, never silently called reproducible. Publication time is not
exchange time. Snapshot identities are recorded as source metadata, not backfilled
into historical observations.
"""
from __future__ import annotations
import argparse
from collections import Counter
import fnmatch
from functools import lru_cache
import gzip
import json
import os
from pathlib import Path
import time
from v7_evidence_store import AUTH, canonical, digest, immutable

IDENTITIES=('code_sha','run_id','market_id','event_id','token_id','contract_family','asset','horizon',
 'decision_id','opportunity_id','replay_key','order_id','fill_id','position_id','model_id','model_hash',
 'model_family','feature_schema_version','policy_hash','config_hash','experiment_protocol_id',
 'experiment_manifest_hash','exchange_timestamp','receive_timestamp','decision_timestamp','source_snapshot_id')

# These mappings describe where a value can exist, not a claim that every old
# record contains it. Raw bytes remain available if a later decoder is needed.
def family(name,patterns,producer,consumers,schema,topics,clocks,kind='CAUSAL_SOURCE',append=True):
    return dict(source_family=name,patterns=patterns,producer=producer,consumers=consumers,schema=schema,
      topics=topics,timestamp_semantics=clocks,source_kind=kind,reproducible=False,
      append_only=append,retention='PERMANENT_NO_UNIQUE_SOURCE_DELETION',
      compression='VERIFIED_GZIP_AND_CONTENT_ADDRESSED_OBJECTS',
      restart_behavior='NEW_SESSION_OR_APPEND; ALL_PREVIOUS_REVISIONS_REMAIN',
      cutover_behavior='IMMUTABLE_REVISION_STORE_OUTSIDE_RUN_ROOT',
      model_dependence='MODEL_INDEPENDENT_RAW' if kind=='CAUSAL_SOURCE' else 'EXPLICIT_IDENTITY_STRATA',
      identity_fields={k:None for k in IDENTITIES})


@lru_cache(maxsize=1)
def definitions():
    receive={'exchange':'source exchange timestamp when provided; UNKNOWN otherwise',
             'receive':'local wall and monotonic clock recorded at ingestion',
             'decision':'NOT_APPLICABLE_TO_RAW_SOURCE','publication':'never substituted for causal receive time'}
    event={'exchange':'exchange_ts_ms if provided','receive':'receive_ts_ms',
           'decision':'decision_ts_ms or same-decision metadata; UNKNOWN when absent',
           'publication':'recorded_ts_ms is ledger publication, not decision time'}
    items=[
      family('canonical_ledger',['ledger/execution.jsonl','archive/canonical-ledger/execution-*.jsonl.gz'],
        'polymarket_v7_fast_structural_runtime canonical ledger writer', ['portfolio','OMS','attribution','permanent datasets'],
        'canonical V7 execution record + economic journal',
        ['order lifecycle','fills','cancellations','final settlement','realized PnL','opportunity envelopes','fees','queue estimates'],event,'CANONICAL_ECONOMIC_SOURCE'),
      family('pm_causal_book',['micro_maker/book_observations/*.jsonl*'],
        'polymarket_v7_maker_fillability_observer',['BookTimeline','profit experiments','raw datasets'],
        'polymarket_v7_causal_book_observation_v1',
        ['Polymarket market books','PM L1','public trades','public aggressive flow','microprice','OFI','trade imbalance','realized volatility','short returns','feature freshness'],receive),
      family('pm_l2_binary',['research/crypto_book/**/*.bin*','research/crypto_book/**/*.json','external_cancel/workspaces/*/normalized_events/*.bin*','external_cancel/books/*/normalized_events/*.bin*','external_cancel/books/*/normalized_events/*.json'],
        'polymarket_v7_maker_fillability_observer',['native book replay','raw datasets'],
        'TapeSessionHeader schema 3 + CryptoBookTapePayload schema 2; immutable token-handle manifest',
        ['PM L2 where recorded','Polymarket public trades','causal book lineage'],receive),
      family('pm_public_trades',['trade_tape.csv*','micro_maker/fillability_ws.jsonl*'],
        'trade recorder / polymarket_v7_maker_fillability_observer',['MakerPaperMarketEngine','flow analysis'],
        'CSV trade recorder / polymarket_v7_maker_fillability_trade_v1',
        ['Polymarket public trades','public aggressive flow','flow reach'],receive),
      family('external_raw',['external_fair/raw/*.bin*'],
        'polymarket_v7_external_venue_runtime raw frame sink',['external tape decoders','future ML'],
        'TapeSessionHeader v3 + RawTapeDiskRecordHeader + original payload',
        ['Binance spot','Binance futures/perpetual','Coinbase','Bybit spot/perpetual','Deribit futures/perpetual','funding','basis','open interest','liquidations'],receive),
      family('external_normalized',['external_fair/normalized_events/*.bin*','external_fair/tapes/*.bin*'],
        'polymarket_v7_external_venue_runtime',['external feature kernels','replay'],
        'TapeSessionHeader v3 + TapeRecord; payload ABI version required',
        ['microprice','OFI','trade imbalance','realized volatility','short returns','cross-venue dispersion','external market events'],receive,'DERIVED_WITH_RAW_SOURCE'),
      family('oracle_rtds',['external_fair/rtds_events.jsonl*','external_fair/rtds_rejected_frames.jsonl*'],
        'v7_rtds_external_fair_monitor.py',['settlement fair','reference verification','latency diagnostics'],
        'RTDS enriched observations / polymarket_v7_rtds_rejected_frame_v1',
        ['Chainlink/oracle observations','Binance reference observations','reference price','observed latency','rejected frames'],receive),
      family('derivative_rest',['external_fair/binance_usdm_rest.jsonl*','external_fair/deribit_rest.jsonl*','external_fair/coinbase_l2_rest.jsonl*'],
        'existing Binance/Deribit/Coinbase REST observers',['rich contextual_features','future feature reconstruction'],
        'versioned REST observation envelopes with request/receive clocks and raw responses',
        ['Deribit options surface','funding','basis','open interest','OI velocity','Coinbase L2','implied volatility'],receive),
      family('fair_predictions',['external_fair/counterfactuals.jsonl*'],
        'v7_external_fair_paper_router.py',['maturity','offline benchmark','signal dataset'],
        'versioned FORECAST, OPPORTUNITY_SET, VIRTUAL_FILL, VIRTUAL_FINAL records',
        ['PM probabilities','reference price','model predictions','uncertainty','TTE','settlement labels','counterfactual execution experiments','raw causal feature cuts'],event,'RESEARCH_OBSERVATION'),
      family('lead_lag',['external_fair/pm_lead_lag.jsonl*'],
        'v7_external_lead_lag_collector.py',['lead-lag research','PM response dataset'],
        'polymarket_v7_external_pm_lead_lag_origin_v1 + observation_v1; immutable origin hash references; legacy full rows retained',
        ['signal-delay experiments','PM response','model-independent raw causal cuts','fixed external features','market movement'],receive,'RESEARCH_OBSERVATION'),
      family('profit_experiments',['profit_experiments/**/observations.jsonl*','profit_experiments/**/manifest.json',
        'profit_experiments/**/sources/*.json.gz','profit_experiments/**/settlements.json',
        'profit_experiments/**/confirmatory_window_closure.json','profit_experiments/**/confirmatory_final.json',
        'profit_experiments/**/final_analysis_sources/*.py.gz'],
        'existing profit collector / report loop',['profit report','cross-cohort permanent analysis'],
        'profit observation/manifest/source/settlement schemas, explicitly protocol stratified',
        ['signal-delay experiments','counterfactual execution experiments','settlement labels','model predictions','uncertainty','Maker paired arms'],receive,'RESEARCH_OBSERVATION'),
      family('coordinator_decisions',['coordinator/decisions.jsonl*','global_portfolio/decisions.jsonl*','control/decisions.jsonl*','opportunities/decisions.jsonl*','global_portfolio_coordinator.events.jsonl*'],
        'v7_global_portfolio_coordinator.py',['opportunity funnel','authorization attribution'],
        'polymarket_v7_global_opportunity_decision_v1',
        ['coordinator decisions','portfolio selection','opportunity envelopes','authorization attempts'],event,'CANONICAL_DECISION_SOURCE'),
      family('authorization',['opportunities/authorization_publications.jsonl*','micro_maker/authorization_attempts.jsonl*','micro_maker/authorized_make/*.json','micro_maker/authorized_cancel/*.json','opportunities/**/*.json','spool/**/*.json'],
        'coordinator / authorization receipt consumers',['single ledger writer','opportunity funnel'],
        'typed V7 opportunity/receipt/canonical spool envelopes',
        ['authorization attempts','rejected authorizations and rejection reasons','opportunity envelopes'],event,'CANONICAL_DECISION_SOURCE'),
      family('maker_learning',['micro_maker/research_evidence.jsonl*','micro_maker/evidence.jsonl*','micro_maker/execution_model.json','micro_maker/book_features/*.json','micro_maker/reward_selection.events.jsonl*'],
        'Maker observer / v7_maker_durable_learning.py',['placement model','execution dataset'],
        'versioned placement features and current-run model; underlying raw observations retained',
        ['fillability data','queue estimates','placement action','fill probability','feature freshness'],receive,'DERIVED_MODEL_OR_FEATURE',False),
      family('markouts',['research/evidence/maker_markout/*.json','micro_maker/markout*.jsonl*'],
        'polymarket_v7_maker_markout_observer',['attribution','adverse selection analysis'],
        'exact fill-id joined MARKOUT research evidence',
        ['markouts','fill-conditioned execution prices'],receive,'RESEARCH_OBSERVATION'),
      family('latency',['micro_maker/latency.csv*','latency/*.json*','latency/*.csv*'],
        'native observed latency producers',['latency gates','decision-chain diagnostics'],
        'versioned latency evidence; configured constants separately ineligible',
        ['observed latency','decision-to-arrival chain'],receive),
      family('point_in_time_universe',['universe_snapshots/*.json.gz','universe/*.json','universe/*.jsonl*','structural_relations/*.csv'],
        'universe archiver / adaptive universe / structural scanner',['market selection','historical opportunity universe'],
        'polymarket_v7_point_in_time_universe_v2 and versioned selection/relations schemas',
        ['market selection','market/event/token identity','liquidity','spread'],receive,'POINT_IN_TIME_SOURCE',False),
      family('policies_models',['models/**/*','control/fee_reward_registry.json','control/external_source_registry.json','control/*snapshot*.json','control/allocations/*.json','control/crypto*json'],
        'checked-in models and policy/registry projections',['fair/portfolio/risk','source provenance'],
        'explicit model/feature/policy/config/registry schema and hashes',
        ['fee schedules','model identity','policy identity','config identity','risk state'],event,'POINT_IN_TIME_SOURCE',False),
      family('structural_candidates',['fast_structural/*.csv*'],
        'polymarket_v7_fast_structural_runtime',['structural research','latency diagnostics'],
        'versioned structural opportunity/latency/error CSV', ['structural opportunities','fees','latency','market selection'],event,'CANONICAL_DECISION_SOURCE'),
      family('quarantined_legacy',['external_cancel/quarantine/**/*'],
        'legacy research collectors',['compatibility audit only'], 'legacy explicitly quarantined schemas',
        ['incompatible historical research evidence'],receive,'QUARANTINED_PRESERVED'),
      family('source_manifests',['*manifest*.jsonl','**/*manifest*.jsonl'],
        'verified compression/evidence archiver',['recovery/provenance'], 'versioned immutable source compression records',
        ['checksums','source provenance'],event,'SOURCE_PROVENANCE'),
      family('snapshots',['*.json','**/*.json'],
        'resolved from process manifest where available',['resolved from process manifest where available'],
        'read actual schema in inventory; UNKNOWN if absent',
        ['runtime health','status publication','data quality'],
        {'exchange':'UNKNOWN_UNLESS_RECORDED','receive':'UNKNOWN_UNLESS_RECORDED','decision':'UNKNOWN_UNLESS_RECORDED','publication':'source snapshot time only'},'SNAPSHOT_UNPROVEN_REPRODUCIBILITY',False),
      family('diagnostics',['*.log*','**/*.log*'],
        'process named by launcher log',['incident review'], 'unstructured diagnostic log',
        ['health diagnostics'],{'exchange':'UNKNOWN','receive':'UNKNOWN','decision':'UNKNOWN','publication':'log time if recorded'},'DIAGNOSTIC_UNPROVEN_REPRODUCIBILITY'),
      family('unclassified',['*','**/*'], 'UNKNOWN',['UNKNOWN'],'UNKNOWN', ['UNCLASSIFIED_PRESERVED'],
        {'exchange':'UNKNOWN','receive':'UNKNOWN','decision':'UNKNOWN','publication':'filesystem mtime is not a causal timestamp'},'UNKNOWN_PRESERVE')]
    for d in items:
        fields=d['identity_fields'];name=d['source_family']
        if name in {'canonical_ledger','markouts','maker_learning'}:
            fields.update({k:k for k in ['market_id','event_id','token_id','order_id','fill_id','position_id','opportunity_id']})
            fields.update(code_sha='model_sha',run_id='metadata.opportunity_envelope.run_id',
                replay_key='metadata.opportunity_replay_key OR metadata.opportunity_envelope.deterministic_replay_key; Taker: exact embedded coordinator_receipt join',
                model_id='metadata.opportunity_envelope.settlement_model.model_id OR metadata.decision_probability_model_id',
                model_hash='metadata.opportunity_envelope.settlement_model.model_hash OR metadata.decision_probability_model_hash',
                policy_hash='metadata.opportunity_envelope.policy_hash (portfolio); metadata.policy_hash (execution)',
                config_hash='metadata.opportunity_envelope.config_hash (portfolio); metadata.config_hash (execution)',
                exchange_timestamp='exchange_ts_ms',receive_timestamp='receive_ts_ms',decision_timestamp='decision_ts_ms',
                source_snapshot_id='book_snapshot_id; metadata.placement_features_snapshot_id',
                feature_schema_version='metadata.placement_features_schema',model_family='metadata.model_family')
            for k in ('asset','horizon','contract_family'):fields[k]='metadata.opportunity_envelope.crypto_context.'+k
        elif name=='pm_causal_book':
            fields.update(code_sha='model_sha',market_id='market_id',token_id='token_id',
                source_snapshot_id='observer_session_id + connection_epoch + observer_sequence',
                feature_schema_version='feature_semantics',exchange_timestamp='exchange_event_ns',
                receive_timestamp='receive_monotonic_ns + receive_wall_ms')
        elif name in {'fair_predictions','lead_lag','profit_experiments'}:
            fields.update(code_sha='model_sha OR code_sha',market_id='market_id',event_id='event_id when recorded',
                token_id='token_id OR yes_token/no_token',model_id='probability_model_id OR model_id',
                model_hash='probability_model_hash OR model_hash; immutable manifest.frozen_model_hash',
                feature_schema_version='feature_schema_version OR evidence_semantics_version; legacy absence remains unknown',
                policy_hash='policy_sha256 when recorded',experiment_protocol_id='experiment_protocol_id OR manifest.protocol.protocol_id',
                experiment_manifest_hash='manifest_sha256',source_snapshot_id='origin_id OR forecast_id OR selection_key; feature_sha256/rich_feature_sha256 and exact book cuts',
                exchange_timestamp='origin_book.exchange_event_ns OR origin/label exchange timestamps when recorded',
                receive_timestamp='origin_ns OR origin_observed_wall_ns OR observed_ms; target cut receive clocks separately',
                decision_timestamp='decision_ts_ms when recorded')
        elif name in {'coordinator_decisions','authorization'}:
            fields.update(code_sha='model_sha OR opportunity_inputs[].model_sha',run_id='run_id when present',
                market_id='market_id OR opportunity_inputs[].market_id',token_id='token_id OR opportunity_inputs[].token_id',
                replay_key='replay_key OR deterministic_replay_key OR opportunity_inputs[].replay_key',
                source_snapshot_id='source_snapshot_identity OR opportunity_inputs[].source_snapshot_identity',
                policy_hash='policy_hash',config_hash='config_hash',decision_timestamp='decision_timestamp_ns OR decision_receive_timestamp_ns',
                model_id='settlement_model.model_id',model_hash='settlement_model.model_hash')
            for k in ('asset','horizon','contract_family'):fields[k]='crypto_context.'+k
        elif name in {'pm_l2_binary','external_raw','external_normalized'}:
            fields.update(code_sha='versioned session header / sidecar code identity',
                source_snapshot_id='session manifest + segment + record byte offset; symbol/token handle requires matching immutable mapping',
                exchange_timestamp='versioned binary payload exchange-event field when available',
                receive_timestamp='versioned binary receive monotonic/wall fields; decoder ABI must match schema')
        d['identity_mapping_semantics']='Candidate source fields, not a claim of historical completeness. Null: unavailable, unparsed or not applicable; source bytes preserved.'
        d['identity_distinctions']={'execution_model':'metadata/opportunity execution model; never replaced by fair model',
          'settlement_probability_model':'settlement_model or probability_model_id/hash',
          'portfolio_risk_policy':'policy_hash/config_hash on decision envelope',
          'research_protocol':'immutable experiment manifest, not code SHA'}
    return items


def classify(relative):
    for d in definitions():
        if any(fnmatch.fnmatch(relative,p) for p in d['patterns']):return d
    raise AssertionError('unclassified fallback absent')


def sampled_schema(path):
    if not any(s in path.name for s in ('.json','.csv')):return None
    try:
        opener=gzip.open if path.suffix=='.gz' else open
        with opener(path,'rb') as f: prefix=f.read(65536)
        line=prefix.split(b'\n',1)[0]
        try:value=json.loads(line if '.jsonl' in path.name else prefix)
        except (ValueError,UnicodeDecodeError):return None
        return value.get('schema') if isinstance(value,dict) else None
    except (OSError,EOFError):return None


def inventory(roots, output, process_manifest=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    summary=Counter();families=Counter();errors=[];entries=[]
    processes=(process_manifest or {}).get('processes',[])
    excluded_roots=[(Path(root)/'permanent_evidence/store').resolve() for name,root in roots.items() if name=='durable']
    excluded_roots.append(output.resolve())
    for name,root in roots.items():
        root=Path(root).resolve()
        if not root.exists():errors.append({'root':name,'state':'MISSING_ROOT'});continue
        for folder,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not (Path(folder)/d).is_symlink()
                and not any((Path(folder)/d).resolve().is_relative_to(x) for x in excluded_roots)]
            for filename in sorted(files):
                p=Path(folder)/filename;rel=str(p.relative_to(root))
                # Archives retain the original within-run path for semantics.
                logical=rel.split('/',1)[1] if name=='archives' and '/' in rel else rel
                d=classify(logical)
                try:stat=p.lstat()
                except OSError as exc:errors.append({'path':str(p),'state':str(exc)});continue
                entry={'root':name,'path':str(p),'relative_path':rel,'logical_source_path':logical,
                  'source_family':d['source_family'],'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                  'allocated_bytes':getattr(stat,'st_blocks',0)*512,'device':stat.st_dev,'inode':stat.st_ino,
                  'is_symlink':p.is_symlink(),'observed_schema':None if p.is_symlink() else sampled_schema(p),
                  'producer':d['producer'],'consumers':d['consumers'],'source_kind':d['source_kind'],
                  'historical_reproducibility_proven':False,'retention':d['retention']}
                matches=[x['id'] for x in processes if logical in x.get('outputs',[])]
                if matches:entry['producer_process_ids']=matches
                consumers=[x['id'] for x in processes if logical in x.get('inputs',[])]
                if consumers:entry['consumer_process_ids']=consumers
                entries.append(entry);summary[name+'_files']+=1;summary[name+'_bytes']+=stat.st_size;families[d['source_family']]+=1
    payload=b''.join(canonical(e)+b'\n' for e in entries);stamp=time.time_ns();sha=digest(payload)
    name='inventory-'+str(stamp)+'-'+sha[:12]+'.jsonl.gz';immutable(output/name,gzip.compress(payload,mtime=0))
    manifest={'schema':'polymarket_v7_permanent_data_catalog_v1',**AUTH,'captured_ns':stamp,'roots':{k:str(v) for k,v in roots.items()},
      'files':len(entries),'summary':dict(summary),'source_families':dict(families),'errors':errors,
      'inventory_file':name,'inventory_decompressed_sha256':sha,'definitions_sha256':digest(canonical(definitions())),
      'definitions':definitions(),'scope':'ALL_FILES_UNDER_EXPLICIT_ROOTS; aliases recorded, symlinks not followed; object-store internals excluded',
      'identity_contract':'NULL means not mapped/applicable; inspect source record. No fabricated identity or causal timestamp.'}
    immutable(output/('catalog-'+str(stamp)+'.json'),canonical(manifest))
    return manifest


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--root',action='append',required=True,help='name=path')
    ap.add_argument('--output',type=Path,required=True);ap.add_argument('--process-manifest',type=Path)
    args=ap.parse_args();roots=dict(s.split('=',1) for s in args.root)
    d=inventory(roots,args.output,json.loads(args.process_manifest.read_text()) if args.process_manifest else None)
    print(json.dumps({k:d[k] for k in ['files','summary','source_families','errors','inventory_file']}))

if __name__=='__main__':main()
