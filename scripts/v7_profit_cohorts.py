"""Automatic immutable cohorts around the prospective profit experiments.

Residual v6 signal evidence is admitted only for the explicitly frozen
PM-as-offset model family. Three non-overlapping 8h replications are frozen at
the first eligible model observation and reuse the exact same model hash.
Historical cohorts are never rewritten or silently pooled.
"""
from __future__ import annotations
from collections import Counter
import json
from pathlib import Path
import time
from v7_profit_experiments import LedgerTail, AUTH, atomic
from v7_prospective_profit_experiments import ProspectiveProfitExperiments
from v7_profit_protocol import freeze, digest, FIVE_MIN_NS, V6_PROTOCOL_ID
from v7_evidence_store import immutable, canonical


def valid_hash(value, length=64):
    return isinstance(value,str) and len(value)==length and all(c in '0123456789abcdef' for c in value)


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
            self.models.setdefault((manifest['frozen_model_hash'],ci.get('feature_schema_version','UNKNOWN')),[]).append(root.name)

    def cohort(self,model_hash,feature_schema='UNKNOWN',*,replication_index=None,replication_base_start_ns=None,forward_start_ns=None):
        if not valid_hash(model_hash):raise ValueError('invalid cohort model identity')
        identity={'settlement_model_hash':model_hash,'feature_schema_version':feature_schema,
                  'base_protocol_sha256':digest(self.protocol),'code_sha':self.sha}
        if replication_index is not None:
            confirm=(self.protocol.get('inference') or {}).get('confirmatory') or {}
            count=int(confirm.get('replication_count') or 0)
            if not 0<=int(replication_index)<count:raise ValueError('invalid residual replication index')
            if not isinstance(replication_base_start_ns,int) or replication_base_start_ns<=0:
                raise ValueError('invalid residual replication base')
            identity.update({'residual_replication_index':int(replication_index),
                             'residual_replication_count':count,
                             'replication_base_start_ns':replication_base_start_ns})
        key=digest(identity)
        if key not in self.cohorts:
            root=self.output/'cohorts'/key;root.mkdir(parents=True,exist_ok=True)
            immutable(root/'protocol.json',canonical(self.protocol))
            freeze(root/'manifest.json',self.protocol,self.sha,model_hash,time.time_ns(),
                   cohort=identity,forward_start_ns=forward_start_ns)
            self.cohorts[key]=ProspectiveProfitExperiments(self.run_root,root,root/'protocol.json',self.book,self.sha,self.binary)
            self.cohorts[key].status_path=root/'status.json'
            self.models.setdefault((model_hash,feature_schema),[]).append(key)
        return self.cohorts[key]

    def _replication_duration_ns(self):
        confirm=(self.protocol.get('inference') or {}).get('confirmatory') or {}
        return int(confirm.get('duration_hours') or 0)*3_600_000_000_000

    def _residual_cohorts(self,model_hash,feature_schema):
        out=[]
        for c in self.cohorts.values():
            ci=c.manifest.get('cohort_identity') or {}
            if (c.manifest.get('frozen_model_hash')==model_hash
                    and ci.get('feature_schema_version')==feature_schema
                    and isinstance(ci.get('residual_replication_index'),int)):
                out.append(c)
        return sorted(out,key=lambda c:(c.manifest.get('cohort_identity') or {})['residual_replication_index'])

    def ensure_residual_replications(self,model_hash,feature_schema,observed_ns):
        confirm=(self.protocol.get('inference') or {}).get('confirmatory') or {}
        count=int(confirm.get('replication_count') or 0)
        duration=self._replication_duration_ns()
        if count!=3 or duration!=8*3_600_000_000_000:
            raise ValueError('residual replication protocol identity changed')
        existing=self._residual_cohorts(model_hash,feature_schema)
        if existing:
            bases={(c.manifest.get('cohort_identity') or {}).get('replication_base_start_ns') for c in existing}
            if len(bases)!=1:raise ValueError('residual replication base identity conflict')
            base=bases.pop()
        else:
            base=((int(observed_ns)//FIVE_MIN_NS)+1)*FIVE_MIN_NS
        result=[]
        for index in range(count):
            start=base+index*duration
            result.append(self.cohort(model_hash,feature_schema,
                replication_index=index,replication_base_start_ns=base,forward_start_ns=start))
        hashes={c.manifest['frozen_model_hash'] for c in result}
        starts=[c.manifest['forward_start_ns'] for c in result]
        ends=[c.manifest['confirmatory_end_ns'] for c in result]
        if len(hashes)!=1 or starts!=[base+i*duration for i in range(count)] or ends!=[s+duration for s in starts]:
            raise ValueError('residual replication schedule conflict')
        return result

    def _signal_active(self,fair_status,origin):
        fair=fair_status.get('fair') or {}
        if not origin or fair.get('valid') is not True:return None
        model=str(fair.get('probability_model_hash') or '')
        if not valid_hash(model):return None
        schema=origin.get('feature_schema_version') or 'btc-m5-rich-external-causal-v2'
        required=str((self.protocol.get('signal') or {}).get('required_probability_model_id_prefix') or '')
        model_id=str(fair.get('probability_model_id') or '')
        if self.protocol.get('protocol_id')==V6_PROTOCOL_ID:
            if not required or not model_id.startswith(required):return None
            candidates=self.ensure_residual_replications(model,schema,int(origin['origin_observed_wall_ns']))
        else:
            candidates=[self.cohort(model,schema)]
        origin_ns=int(origin['origin_observed_wall_ns'])
        matches=[c for c in candidates if c.manifest['forward_start_ns']<=origin_ns<c.manifest.get('confirmatory_end_ns',2**63-1)]
        if len(matches)>1:raise ValueError('overlapping residual replication windows')
        return matches[0] if matches else None

    def tick(self,fair_status,origin,status):
        active=self._signal_active(fair_status,origin)
        # Maker models are independently visible, but v6 never invents a
        # non-residual signal cohort from ledger history. Once the residual
        # fair model is observed, its three fixed cohorts already exist.
        for row in self.ledger.poll():
            m=row.get('metadata') or {}
            if row.get('event_type')!='ORDER_SUBMITTED' or m.get('component')!='professional_maker':continue
            model=((m.get('opportunity_envelope') or {}).get('settlement_model') or {}).get('model_hash')
            if not model:self.unidentified_anchors+=1;continue
            existing=[c for c in self.cohorts.values() if c.manifest['frozen_model_hash']==model]
            if not existing and self.protocol.get('protocol_id')!=V6_PROTOCOL_ID:self.cohort(model)
        now_ns=time.time_ns()
        for c in self.cohorts.values():
            start=c.manifest['forward_start_ns'];end=c.manifest.get('confirmatory_end_ns')
            c.accept_new_anchors=now_ns>=start and (end is None or now_ns<end)
            c.tick(fair_status if c is active else {},origin if c is active else None,status)
        now=time.monotonic()
        if now-self.last_status>=1:
            self.last_status=now;counts=Counter()
            for c in self.cohorts.values():counts.update(c.counts)
            runtime={}
            try:runtime=json.loads((self.run_root/'control/runtime_status.json').read_text())
            except (OSError,ValueError):pass
            atomic(self.run_root/'profit_experiment_status.json',{'schema':'polymarket_v7_profit_cohort_status_v1',**AUTH,
              'timestamp_ms':now_ns//1_000_000,'code_sha':self.sha,'run_id':runtime.get('run_id'),
              'state':'COLLECTING','counts':dict(counts),'cohort_count':len(self.cohorts),
              'unidentified_maker_anchors':self.unidentified_anchors,
              'active_signal_cohort':str(active.output) if active else None,
              'cohorts':[{'path':str(c.output),'manifest_sha256':c.manifest['manifest_sha256'],
                'model_hash':c.manifest['frozen_model_hash'],'forward_start_ns':c.manifest['forward_start_ns'],
                'confirmatory_end_ns':c.manifest.get('confirmatory_end_ns'),
                'replication_index':(c.manifest.get('cohort_identity') or {}).get('residual_replication_index'),
                'counts':dict(c.counts),'pending_signals':len(c.pending),'pending_maker_anchors':len(c.maker_pending),
                'pending_maker_candidates':len(getattr(c,'maker_candidates',{})),
                'last_error':c.last_error} for c in self.cohorts.values()],
              'historical_cohorts_preserved':True,'automatic_promotion':False})
