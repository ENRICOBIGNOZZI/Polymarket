"""Automatic immutable cohorts around the existing profit experiment machinery.

A new installed model creates a new prospective cohort, never rewrites or clears
an older one. Old pending signal/execution/markout windows continue to completion.
Actual Maker anchor model identity determines attribution, not current fair.
"""
from __future__ import annotations
from collections import Counter
import json
from pathlib import Path
import time
from v7_profit_experiments import LedgerTail, AUTH, atomic
from v7_profit_experiments_v6 import ProspectiveProfitExperiments
from v7_profit_protocol import freeze, digest
from v7_evidence_store import immutable, canonical


class ProfitCohorts:
    def __init__(self,run_root,output,protocol_path,book,code_sha,binary):
        self.run_root=run_root;self.output=output;self.protocol_path=protocol_path
        self.protocol=json.loads(protocol_path.read_text());self.book=book;self.sha=code_sha;self.binary=binary
        self.cohorts={};self.models={};self.last_status=0.;self.unidentified_anchors=0
        self.ledger=LedgerTail(run_root/'ledger/execution.jsonl')
        for manifest_path in sorted((output/'cohorts').glob('*/manifest.json')):
            manifest=json.loads(manifest_path.read_text())
            if manifest['code_sha']!=code_sha:raise ValueError('cohort code identity conflict')
            root=manifest_path.parent
            c=ProspectiveProfitExperiments(run_root,root,root/'protocol.json',book,code_sha,binary)
            c.status_path=root/'status.json'
            self.cohorts[root.name]=c
            ci=manifest.get('cohort_identity') or {}
            self.models[(manifest['frozen_model_hash'],ci.get('feature_schema_version','UNKNOWN'))]=root.name

    def cohort(self,model_hash,feature_schema='UNKNOWN'):
        if len(model_hash)!=64 or any(c not in '0123456789abcdef' for c in model_hash):raise ValueError('invalid cohort model identity')
        identity={'settlement_model_hash':model_hash,'feature_schema_version':feature_schema,
                  'base_protocol_sha256':digest(self.protocol),'code_sha':self.sha}
        key=digest(identity)
        if key not in self.cohorts:
            root=self.output/'cohorts'/key;root.mkdir(parents=True,exist_ok=True)
            immutable(root/'protocol.json',canonical(self.protocol))
            freeze(root/'manifest.json',self.protocol,self.sha,model_hash,time.time_ns(),cohort=identity)
            self.cohorts[key]=ProspectiveProfitExperiments(self.run_root,root,root/'protocol.json',self.book,self.sha,self.binary)
            self.cohorts[key].status_path=root/'status.json'
            self.models[(model_hash,feature_schema)]=key
        return self.cohorts[key]

    def tick(self,fair_status,origin,status):
        fair=fair_status.get('fair') or {};active=None
        model=str(fair.get('probability_model_hash') or '')
        if origin and fair.get('valid') is True and len(model)==64 and all(c in '0123456789abcdef' for c in model):
            schema=origin.get('feature_schema_version') or 'btc-m5-rich-external-causal-v2'
            active=self.cohort(model,schema)
        # Models appearing only in Maker anchors are still independently visible.
        # Their first observed order precedes the new cohort's future boundary;
        # it stays canonical evidence, never retroactively enters that experiment.
        for row in self.ledger.poll():
            m=row.get('metadata') or {}
            if row.get('event_type')!='ORDER_SUBMITTED' or m.get('component')!='professional_maker':continue
            model=((m.get('opportunity_envelope') or {}).get('settlement_model') or {}).get('model_hash')
            if not model:self.unidentified_anchors+=1;continue
            existing=[c for c in self.cohorts.values() if c.manifest['frozen_model_hash']==model]
            if not existing:self.cohort(model)
        latest_by_model={}
        for c in self.cohorts.values():
            model=c.manifest['frozen_model_hash'];previous=latest_by_model.get(model)
            if previous is None or c.manifest['created_ns']>previous.manifest['created_ns']:latest_by_model[model]=c
        if active:latest_by_model[active.manifest['frozen_model_hash']]=active
        for c in self.cohorts.values():
            c.accept_new_anchors=latest_by_model[c.manifest['frozen_model_hash']] is c
            c.tick(fair_status if c is active else {},origin if c is active else None,status)
        now=time.monotonic()
        if now-self.last_status>=1:
            self.last_status=now;counts=Counter()
            for c in self.cohorts.values():counts.update(c.counts)
            runtime={}
            try:runtime=json.loads((self.run_root/'control/runtime_status.json').read_text())
            except (OSError,ValueError):pass
            atomic(self.run_root/'profit_experiment_status.json',{'schema':'polymarket_v7_profit_cohort_status_v1',**AUTH,
              'timestamp_ms':time.time_ns()//1_000_000,'code_sha':self.sha,'run_id':runtime.get('run_id'),
              'state':'COLLECTING','counts':dict(counts),'cohort_count':len(self.cohorts),
              'unidentified_maker_anchors':self.unidentified_anchors,
              'cohorts':[{'path':str(c.output),'manifest_sha256':c.manifest['manifest_sha256'],
                'model_hash':c.manifest['frozen_model_hash'],'forward_start_ns':c.manifest['forward_start_ns'],
                'counts':dict(c.counts),'pending_signals':len(c.pending),'pending_maker_anchors':len(c.maker_pending),
                'pending_maker_candidates':len(getattr(c,'maker_candidates',{})),
                'last_error':c.last_error} for c in self.cohorts.values()],
              'historical_cohorts_preserved':True,'automatic_promotion':False})
