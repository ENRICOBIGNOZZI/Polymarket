#!/usr/bin/env python3
from __future__ import annotations
import json, sys, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_capital_allocator import ALLOCATOR_OWNER, ENGINES, allocate, component_observation_budgets, materialize

class CapitalAllocatorTests(unittest.TestCase):
    def config(self, crypto: float, reserve: float) -> dict:
        return {"paper_only":True,"starting_capital":10_000.0,"v7":{
            "authenticated_execution":False,"real_order_submission":False,
            "capital_authority_owner":ALLOCATOR_OWNER,
            "engine_capital_fractions":{"CRYPTO_SETTLEMENT_ENGINE":crypto},
            "component_observation_budget_fractions":{"professional_maker":0.2,"crypto_informed_taker":0.0},
            "reserve_fraction":reserve}}
    def test_engine_envelope_plus_reserve_equal_account(self):
        budgets=allocate(self.config(.4,.1)); self.assertAlmostEqual(sum(budgets.values()),10_000.0)
        self.assertEqual(budgets["CRYPTO_SETTLEMENT_ENGINE"],4_000.0); self.assertEqual(budgets["reserve"],6_000.0)
    def test_current_config_has_one_crypto_envelope_and_one_owner(self):
        cfg=json.loads((ROOT/'config/paper_v7.json').read_text()); budgets=allocate(cfg)
        self.assertEqual(set(budgets),{*ENGINES,'reserve'}); self.assertEqual(budgets['CRYPTO_SETTLEMENT_ENGINE'],4_000.0)
        self.assertAlmostEqual(budgets['reserve'],10_000.0); self.assertEqual(cfg['v7']['capital_authority_owner'],ALLOCATOR_OWNER)
    def test_component_observation_budget_is_not_capital(self):
        cfg=json.loads((ROOT/'config/paper_v7.json').read_text())
        self.assertEqual(component_observation_budgets(cfg),{'crypto_informed_taker':0.0,'professional_maker':2_000.0})
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'allocations'; manifest=materialize(ROOT/'config/paper_v7.json',root)
            self.assertEqual(manifest['engine_count'],1); self.assertEqual(manifest['engine_budget_sum'],4_000.0); self.assertEqual(manifest['reserve_budget'],10_000.0)
            maker=json.loads((root/'micro_maker.json').read_text()); self.assertEqual(maker['starting_capital'],0.0); self.assertEqual(maker['capital_scope']['observation_budget'],2_000.0)
            crypto=json.loads((root/'crypto_settlement_engine.json').read_text()); self.assertEqual(crypto['starting_capital'],4_000.0); self.assertEqual(crypto['capital_scope']['scope_class'],'ENGINE_ENVELOPE')
    def test_unknown_engine_partition_fails_closed(self):
        cfg=self.config(.4,.1); cfg['v7']['engine_capital_fractions']['THIRD_ENGINE']=.1
        with self.assertRaisesRegex(ValueError,'engine_capital_fraction_partition'): allocate(cfg)
    def test_overallocation_and_wrong_owner_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'capital_fractions_exceed_one'): allocate(self.config(.8,.4))
        cfg=self.config(.4,.1); cfg['v7']['capital_authority_owner']='SECOND_ALLOCATOR'
        with self.assertRaisesRegex(ValueError,'canonical_owner'): allocate(cfg)
if __name__=='__main__': unittest.main()
