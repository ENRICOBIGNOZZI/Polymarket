from __future__ import annotations
import json, unittest
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[1]; CONFIG=ROOT/'config/paper_v7.json'; DIRECTIVES=ROOT/'config/operator_directives.json'

def number(value:Any)->float|None:
    if isinstance(value,bool) or not isinstance(value,(int,float)): return None
    return float(value)
def authorization()->dict[str,Any]: return dict(json.loads(DIRECTIVES.read_text())['paper_v7_authorization'])

def validate(config:dict[str,Any])->list[str]:
    auth=authorization(); errors=[]
    def exact(label,value,expected):
        current=number(value)
        if current is None or abs(current-expected)>1e-12: errors.append(f"{label} required={expected:g}, got {value!r}")
    def ceiling(label,value,limit):
        current=number(value)
        if current is None: errors.append(f"{label} missing or non-numeric")
        elif current>limit+1e-12: errors.append(f"{label} allowed<={limit:g}, got {current:g}")
    def floor(label,value,limit):
        current=number(value)
        if current is None: errors.append(f"{label} missing or non-numeric")
        elif current<limit-1e-12: errors.append(f"{label} required>={limit:g}, got {current:g}")
    if config.get('paper_only') is not True: errors.append('paper_only must be true')
    if config.get('fixed_dollar_trade_cap_enabled') is not False: errors.append('fixed_dollar_trade_cap_enabled must be false for V7 PAPER')
    ceiling('market_limit',config.get('market_limit'),float(auth['market_limit']))
    floor('min_liquidity',config.get('min_liquidity'),float(auth['min_liquidity']))
    floor('min_net_edge',config.get('min_net_edge'),float(auth['min_net_edge']))
    floor('uncertainty_penalty',config.get('uncertainty_penalty'),float(auth['uncertainty_penalty']))
    ceiling('fractional_kelly',config.get('fractional_kelly'),float(auth['fractional_kelly_ceiling']))
    ceiling('max_drawdown',config.get('max_drawdown'),float(auth['max_drawdown']))
    for key in ('max_trade_fraction','max_market_fraction','max_event_fraction','max_gross_fraction'): exact(key,config.get(key),float(auth[key]))
    guard=number(config.get('max_trade_usd')); allowed=float(auth['max_trade_usd_disabled_cap_value'])
    if guard is None or not 0<guard<=allowed: errors.append(f'max_trade_usd must be a positive finite defense-in-depth guard <= {allowed:g}')
    multi=config.get('multi_strategy') if isinstance(config.get('multi_strategy'),dict) else {}
    if multi.get('paper_only') is not True: errors.append('multi_strategy.paper_only must be true')
    ceiling('multi_strategy.global_max_drawdown',multi.get('global_max_drawdown'),float(auth['max_drawdown']))
    exact('multi_strategy.global_max_gross_fraction',multi.get('global_max_gross_fraction'),float(auth['max_gross_fraction']))
    v7=config.get('v7') if isinstance(config.get('v7'),dict) else {}
    if v7.get('paper_only') is not True: errors.append('v7.paper_only must be true')
    if v7.get('authenticated_execution') is not False: errors.append('v7.authenticated_execution must be false')
    if v7.get('real_order_submission') is not False: errors.append('v7.real_order_submission must be false')
    if v7.get('authoritative_fee_required') is not True: errors.append('v7.authoritative_fee_required must be true')
    if v7.get('shared_execution_ledger_required') is not True: errors.append('v7.shared_execution_ledger_required must be true')
    floor('v7.intent_min_edge',v7.get('intent_min_edge'),float(auth['min_net_edge']))
    if v7.get('capital_authority_owner')!='V7_CANONICAL_ALLOCATOR': errors.append('v7.capital_authority_owner must be V7_CANONICAL_ALLOCATOR')
    ef=v7.get('engine_capital_fractions') if isinstance(v7.get('engine_capital_fractions'),dict) else {}
    if set(ef)!={'CRYPTO_SETTLEMENT_ENGINE'}: errors.append('v7.engine_capital_fractions must contain exactly CRYPTO_SETTLEMENT_ENGINE')
    obs=v7.get('component_observation_budget_fractions') if isinstance(v7.get('component_observation_budget_fractions'),dict) else {}
    if set(obs)!={'professional_maker','crypto_informed_taker'}: errors.append('v7.component_observation_budget_fractions must be crypto-only')
    values=[]
    for label,value in [(f'engine:{k}',x) for k,x in ef.items()]+[(f'observation:{k}',x) for k,x in obs.items()]+[('reserve_fraction',v7.get('reserve_fraction'))]:
        n=number(value)
        if n is None or n<0: errors.append(f'v7.{label} missing, non-numeric or negative')
        if label.startswith('engine:') or label=='reserve_fraction':
            if n is not None and n>=0: values.append(n)
    if sum(values)>1+1e-9: errors.append(f'v7 engine capital plus reserve exceeds 100%: total={sum(values):g}')
    if any(k.endswith('_capital_fraction') for k in v7): errors.append('component strategy capital fractions are forbidden')
    return errors

def authorized_config()->dict[str,Any]:
    return {'paper_only':True,'market_limit':1000,'min_liquidity':2.0,'min_net_edge':0.00005,'uncertainty_penalty':0.0,'fractional_kelly':0.25,'fixed_dollar_trade_cap_enabled':False,'max_trade_usd':300.0,'max_trade_fraction':1.0,'max_market_fraction':1.0,'max_event_fraction':1.0,'max_gross_fraction':1.0,'max_drawdown':0.15,'multi_strategy':{'paper_only':True,'global_max_drawdown':0.15,'global_max_gross_fraction':1.0},'v7':{'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'capital_authority_owner':'V7_CANONICAL_ALLOCATOR','engine_capital_fractions':{'CRYPTO_SETTLEMENT_ENGINE':0.40},'component_observation_budget_fractions':{'professional_maker':0.20,'crypto_informed_taker':0.0},'reserve_fraction':0.20,'intent_min_edge':0.00005,'authoritative_fee_required':True,'shared_execution_ledger_required':True}}

class V7AuthorizedPaperEnvelopeContractTest(unittest.TestCase):
    def test_operator_directive_is_source_of_truth(self):
        auth=authorization(); self.assertFalse(auth['fixed_dollar_trade_cap_enabled']); self.assertEqual(float(auth['max_trade_usd_disabled_cap_value']),300.0)
        for key in ('max_trade_fraction','max_market_fraction','max_event_fraction','max_gross_fraction'): self.assertEqual(float(auth[key]),1.0)
        self.assertFalse(auth['authenticated_execution']); self.assertFalse(auth['real_order_submission'])
    def test_authorized_crypto_envelope_is_valid(self): self.assertEqual(validate(authorized_config()),[])
    def test_obsolete_bounded_policy_is_rejected(self):
        cfg=authorized_config(); cfg.update({'fixed_dollar_trade_cap_enabled':True,'max_trade_usd':125.0,'max_market_fraction':0.05,'max_event_fraction':0.15,'max_gross_fraction':0.70}); cfg['multi_strategy']['global_max_gross_fraction']=0.70
        joined='\n'.join(validate(cfg)); self.assertIn('fixed_dollar_trade_cap_enabled must be false',joined); self.assertIn('max_market_fraction required=1',joined); self.assertIn('max_event_fraction required=1',joined); self.assertIn('max_gross_fraction required=1',joined)
    def test_economic_and_execution_safety_still_bind(self):
        cfg=authorized_config(); cfg['max_drawdown']=.151; cfg['fractional_kelly']=.251; cfg['min_net_edge']=0.; cfg['v7']['authenticated_execution']=True; cfg['v7']['real_order_submission']=True; cfg['v7']['authoritative_fee_required']=False; cfg['v7']['shared_execution_ledger_required']=False
        joined='\n'.join(validate(cfg));
        for token in ('max_drawdown allowed<=0.15','fractional_kelly allowed<=0.25','min_net_edge required>=5e-05','v7.authenticated_execution must be false','v7.real_order_submission must be false','v7.authoritative_fee_required must be true','v7.shared_execution_ledger_required must be true'): self.assertIn(token,joined)
    def test_noncrypto_capital_partition_is_rejected(self):
        cfg=authorized_config(); cfg['v7']['engine_capital_fractions']['OLD_ENGINE']=.1; self.assertIn('exactly CRYPTO_SETTLEMENT_ENGINE','\n'.join(validate(cfg)))
    def test_current_config_respects_authorization(self):
        cfg=json.loads(CONFIG.read_text()); errors=validate(cfg); self.assertEqual(errors,[],'\n'.join(errors))
if __name__=='__main__': unittest.main()
