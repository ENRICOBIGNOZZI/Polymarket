"""Cold-plane capital configuration; no mutation, orders, or new allocator."""
from __future__ import annotations
from decimal import Decimal
import hashlib
import json
from typing import Any

KEYS=('sleeve_budget_microdollars','max_total_exposure_microdollars',
      'max_market_exposure_microdollars','max_single_order_microdollars')


def load_native_limits(policy:dict[str,Any], allocation:dict[str,Any])->dict[str,Any]:
    for value in (policy,allocation):
        if (value.get('paper_only') is not True or value.get('authenticated_execution') is not False
                or value.get('real_order_submission') is not False):raise ValueError('paper capital contract')
    if policy.get('schema')!='polymarket_v7_native_risk_policy_v1':raise ValueError('native policy schema')
    budgets=allocation.get('engine_budgets')
    if (allocation.get('schema')!='polymarket_v7_capital_allocation_v3'
            or allocation.get('capital_authority_owner')!='V7_CANONICAL_ALLOCATOR'
            or allocation.get('capital_authority_owner_count')!=1
            or not isinstance(budgets,dict) or set(budgets)!={'CRYPTO_SETTLEMENT_ENGINE'}):
        raise ValueError('canonical allocation contract')
    values=[Decimal(str(allocation.get('account_starting_capital'))),Decimal(str(allocation.get('reserve_budget'))),
            Decimal(str(budgets['CRYPTO_SETTLEMENT_ENGINE']))]
    if not all(x.is_finite() and x>=0 for x in values) or values[0]<=0 or abs(values[1]+values[2]-values[0])>Decimal('.000001'):
        raise ValueError('canonical allocation sum')
    limits={key:policy.get(key) for key in KEYS}
    if any(not isinstance(v,int) or isinstance(v,bool) or v<=0 for v in limits.values()):raise ValueError('positive integer risk limits required')
    fraction=Decimal(str(policy.get('target_order_fraction_of_context')))
    target_cap=policy.get('target_order_notional_cap_microdollars')
    if (not fraction.is_finite() or fraction<=0 or fraction>1
            or not isinstance(target_cap,int) or isinstance(target_cap,bool) or target_cap<=0):
        raise ValueError('capital based sizing contract invalid')
    if not (limits[KEYS[3]]<=limits[KEYS[2]]<=limits[KEYS[1]]<=limits[KEYS[0]]):raise ValueError('inconsistent nested risk caps')
    if target_cap>limits['max_single_order_microdollars']:
        raise ValueError('target order cap exceeds single order cap')
    allocated_micro=int(values[2]*1_000_000)
    if limits[KEYS[0]]>allocated_micro:raise ValueError('native suballocation exceeds canonical allocation')
    encoded=json.dumps(policy,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return {'schema':'polymarket_v7_native_risk_receipt_v1','limits':limits,
        'risk_policy_sha256':hashlib.sha256(encoded).hexdigest(),
        'canonical_engine_budget_microdollars':allocated_micro,
        'native_suballocation_microdollars':limits[KEYS[0]],
        'target_order_fraction_of_context':float(fraction),
        'target_order_notional_cap_microdollars':target_cap,
        'paper_sizing_contract':str(policy.get('paper_sizing_contract') or ''),
        'paper_only':True,'real_order_submission':False,'limits_increased':False}


def unsettled_exposure(rows:list[dict[str,Any]],expected_sha:str)->dict[str,Any]:
    """Conservative cost-basis plus fees on unresolved native markets.

    FINAL credit is allowed only after the same immutable fill reconciliation
    used by accounting. No market/position may disappear on rollover or restart.
    """
    from collections import defaultdict
    from decimal import ROUND_CEILING
    from v7_native_settlement_projection import allocate_final,native_final
    fills=defaultdict(list);closed=set();seen=set();inventory={};fees=defaultdict(Decimal)
    for row in rows:
        if row.get('event_type') not in {'FILL','FINAL'}:continue
        if row.get('model_sha')!=expected_sha or row.get('strategy')!='CRYPTO_SETTLEMENT_ENGINE':
            raise ValueError('unreconciled economic scope')
        market=str(row.get('market_id') or '')
        if not market or row.get('paper_only') is not True or row.get('authenticated_execution') is not False:
            raise ValueError('invalid economic identity')
        if row['event_type']=='FINAL':
            if market in closed or not native_final(row):raise ValueError('duplicate or non-native final')
            allocate_final(row,fills[market]);closed.add(market);continue
        if market in closed or not row.get('fill_id') or row['fill_id'] in seen:
            raise ValueError('duplicate or post-settlement fill')
        seen.add(row['fill_id']);fills[market].append(row)
        receipt=(row.get('metadata') or {}).get('native_settlement_receipt') or {}
        if not isinstance(receipt,dict) or receipt.get('owner')!='V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE':
            raise ValueError('unknown execution owner')
        qty,price,fee=(Decimal(str(row.get(k))) for k in ('filled_size','fill_price','fee'))
        if not all(v.is_finite() for v in (qty,price,fee)) or qty<=0 or not 0<=price<=1 or fee<0:
            raise ValueError('invalid fill economics')
        key=market,str(row.get('token_id') or '')
        if not key[1]:raise ValueError('missing token')
        held,basis=inventory.get(key,(Decimal(0),Decimal(0)))
        if row.get('side')=='BUY':held,basis=held+qty,basis+qty*price
        elif row.get('side')=='SELL' and held>=qty:
            basis=basis*(held-qty)/held;held-=qty
        else:raise ValueError('unbacked or invalid sale')
        inventory[key]=held,basis;fees[market]+=fee
    claims={}
    for market in sorted(set(fills)-closed):
        basis=sum((b for (m,_),(q,b) in inventory.items() if m==market),Decimal(0))
        claims[market]=int(((basis+fees[market])*1_000_000).to_integral_value(rounding=ROUND_CEILING))
    return {'unsettled_market_claims_microdollars':claims,'total_unsettled_microdollars':sum(claims.values()),
            'settled_markets':len(closed),'paper_only':True,'expected_model_sha':expected_sha}


def remaining_capital_lease(receipt:dict[str,Any],exposure:dict[str,Any])->dict[str,Any]:
    """Mechanical deduction from one canonical allowance, never a new allocator."""
    pending=exposure['total_unsettled_microdollars']
    if not isinstance(pending,int) or isinstance(pending,bool) or pending<0:raise ValueError('invalid pending exposure')
    limits=dict(receipt['limits'])
    remaining=min(limits['sleeve_budget_microdollars'],limits['max_total_exposure_microdollars'])-pending
    out={**receipt,**exposure,'base_limits':dict(limits),'remaining_microdollars':max(0,remaining),
         'lease_available':remaining>0,'lease_credit_policy':'FINAL_COMMITTED_OR_RETAIN_CLAIM'}
    if remaining<=0:out['limits']=None;return out
    limits['sleeve_budget_microdollars']=remaining;limits['max_total_exposure_microdollars']=remaining
    limits['max_market_exposure_microdollars']=min(limits['max_market_exposure_microdollars'],remaining)
    limits['max_single_order_microdollars']=min(limits['max_single_order_microdollars'],limits['max_market_exposure_microdollars'])
    out['limits']=limits
    return out
