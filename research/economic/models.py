"""Auditable offline baselines using the standard library; no execution authority."""
from __future__ import annotations
import hashlib
import json
from math import exp, isfinite, log, log1p, fsum, sqrt
from typing import Any


def _sigmoid(z:float)->float:
    return 1/(1+exp(-z)) if z>=0 else exp(z)/(1+exp(z))


def _dot(a,b):return fsum(x*y for x,y in zip(a,b))


def _solve(matrix:list[list[float]],rhs:list[float])->list[float]:
    """Partial-pivot elimination for the small ridge-positive Hessian."""
    n=len(rhs);a=[list(row)+[value] for row,value in zip(matrix,rhs)]
    for i in range(n):
        pivot=max(range(i,n),key=lambda k:abs(a[k][i]))
        a[i],a[pivot]=a[pivot],a[i]
        if abs(a[i][i])<1e-14:raise ValueError('singular regularized Hessian')
        divisor=a[i][i]
        for j in range(i,n+1):a[i][j]/=divisor
        for k in range(i+1,n):
            multiple=a[k][i]
            for j in range(i,n+1):a[k][j]-=multiple*a[i][j]
    out=[0.0]*n
    for i in range(n-1,-1,-1):out[i]=a[i][n]-fsum(a[i][j]*out[j] for j in range(i+1,n))
    return out


def _model_hash(model):return hashlib.sha256(json.dumps(model,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def fit_settlement_residual(rows:list[dict[str,Any]],*,feature_names:list[str],train_end_ns:int,
                            dataset_sha256:str,ridge:float=1.0,minimum_markets:int=100)->dict:
    """Penalized Bernoulli residual to PM prior; train-only scaling and labels.

    Rows require an outcome recorded strictly before the training cutoff and one
    preregistered decision per market. There is no test-set parameter search.
    The intercept is regularized as well; this is a baseline, not a deep model.
    """
    if not 0<len(feature_names)<=32 or len(set(feature_names))!=len(feature_names) or ridge<=0 or not isfinite(ridge):
        raise ValueError('features/ridge')
    if len(dataset_sha256)!=64 or any(x not in '0123456789abcdef' for x in dataset_sha256):
        raise ValueError('frozen dataset hash required')
    if minimum_markets<2 or len({r['market'] for r in rows})!=len(rows):raise ValueError('one observation per market')
    if len(rows)<minimum_markets:raise ValueError('insufficient unique training markets')
    for row in rows:
        if not 0<row['decision_ns']<=row['label_observed_ns']<train_end_ns:
            raise ValueError('training outcome unavailable at cutoff')
        if row.get('complete') is not True or row['outcome'] not in (0,1):raise ValueError('missing label')
        if not 0<row['pm_probability']<1:raise ValueError('invalid PM probability')
    raw=[[float(r['features'][k]) for k in feature_names] for r in rows]
    if not all(isfinite(v) for row in raw for v in row):raise ValueError('features must be observed and finite')
    n=len(raw);d=len(feature_names)
    mu=[fsum(row[j] for row in raw)/n for j in range(d)]
    scale=[max(1e-12,sqrt(fsum((row[j]-mu[j])**2 for row in raw)/n)) for j in range(d)]
    scale=[1.0 if v<=1e-12 else v for v in scale]
    X=[[1.0]+[(v-m)/s for v,m,s in zip(row,mu,scale)] for row in raw]
    y=[float(r['outcome']) for r in rows];offset=[log(r['pm_probability']/(1-r['pm_probability'])) for r in rows]
    beta=[0.0]*(d+1);converged=False
    def objective(b):
        z=[o+_dot(x,b) for o,x in zip(offset,X)]
        return fsum(max(v,0)+log1p(exp(-abs(v)))-target*v for v,target in zip(z,y))+.5*ridge*_dot(b,b)
    for _ in range(100):
        probabilities=[_sigmoid(o+_dot(x,beta)) for o,x in zip(offset,X)]
        weights=[max(p*(1-p),1e-8) for p in probabilities]
        gradient=[fsum(x[j]*(p-target) for x,p,target in zip(X,probabilities,y))+ridge*beta[j] for j in range(d+1)]
        hessian=[[fsum(x[j]*x[k]*weight for x,weight in zip(X,weights))+(ridge if j==k else 0.0) for k in range(d+1)] for j in range(d+1)]
        step=_solve(hessian,gradient);old=objective(beta);fraction=1.0
        for _ in range(20):
            updated=[b-fraction*s for b,s in zip(beta,step)]
            if objective(updated)<=old+1e-12:break
            fraction*=.5
        else:raise ValueError('optimizer failed line search')
        beta=updated
        if max(abs(fraction*s) for s in step)<1e-8:converged=True;break
    if not converged:raise ValueError('optimizer did not converge')
    model={'schema':'polymarket_settlement_residual_model_v1','feature_names':feature_names,
        'mean':mu,'scale':scale,'coefficients':beta,'train_end_ns':train_end_ns,
        'dataset_sha256':dataset_sha256,'ridge':ridge,'training_unique_markets':n,
        'paper_only':True,'execution_authority':False,'heldout_validated':False,'automatic_promotion':False}
    return {**model,'model_sha256':_model_hash(model)}


def predict(model:dict,rows:list[dict])->list[float]:
    if model.get('schema')!='polymarket_settlement_residual_model_v1':raise ValueError('schema')
    if _model_hash({k:v for k,v in model.items() if k!='model_sha256'})!=model.get('model_sha256'):
        raise ValueError('model hash mismatch')
    out=[]
    for row in rows:
        if row['decision_ns']<model['train_end_ns']:raise ValueError('prediction predates model availability')
        prior=float(row['pm_probability']);raw=[float(row['features'][k]) for k in model['feature_names']]
        if not 0<prior<1 or not all(isfinite(x) for x in raw):raise ValueError('invalid prediction inputs')
        x=[1.0]+[(v-m)/s for v,m,s in zip(raw,model['mean'],model['scale'])]
        out.append(_sigmoid(log(prior/(1-prior))+_dot(x,model['coefficients'])))
    return out


def settlement_edge(probability:float,price:float,fee_per_share:float,
                    uncertainty_buffer:float,other_cost_per_share:float=0)->float:
    values=(probability,price,fee_per_share,uncertainty_buffer,other_cost_per_share)
    if not all(isfinite(v) for v in values) or not 0<=probability<=1 or not 0<=price<=1 or min(values[2:])<0:
        raise ValueError('invalid edge inputs')
    return probability-price-fee_per_share-uncertainty_buffer-other_cost_per_share
