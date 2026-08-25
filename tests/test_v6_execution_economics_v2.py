#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for path in (ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class V6ExecutionEconomicsV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.common = load_script("v6_market_common_test_v2", "scripts/v6_market_common.py")
        cls.structural = load_script("v6_typed_structural_test_v2", "scripts/v6_typed_structural.py")
        cls.local = load_script("v6_local_factor_test_v2", "scripts/v6_local_factor_v2.py")

    def test_fee_schedule_is_verified_and_maker_taker_semantics_are_distinct(self):
        fee = self.common._fee_from_gamma({"feeSchedule":{"rate":0.1,"exponent":2.0,"takerOnly":True}})
        self.assertIsNotNone(fee); self.assertTrue(fee.verified); self.assertEqual(fee.source,"gamma:feeSchedule")
        self.assertEqual(self.common.fee_per_share(0.5,fee,taker=False),0.0)
        self.assertGreater(self.common.fee_per_share(0.5,fee,taker=True),0.0)

    def test_fee_disabled_is_verified_zero(self):
        fee=self.common._fee_from_gamma({"feesEnabled":False}); self.assertIsNotNone(fee); self.assertTrue(fee.verified); self.assertEqual(fee.rate,0.0)

    def test_fill_probability_responds_to_flow_and_queue(self):
        low=self.common.fill_probability_proxy(queue_ahead=1000,own_shares=20,compatible_flow_per_second=.1,horizon_seconds=90)
        high_flow=self.common.fill_probability_proxy(queue_ahead=1000,own_shares=20,compatible_flow_per_second=10,horizon_seconds=90)
        low_queue=self.common.fill_probability_proxy(queue_ahead=10,own_shares=20,compatible_flow_per_second=.1,horizon_seconds=90)
        self.assertLess(low,high_flow); self.assertLess(low,low_queue); self.assertGreaterEqual(low,0.0); self.assertLessEqual(high_flow,1.0)

    def test_typed_structural_text_family_keeps_dates(self):
        a=self.structural.threshold_signature("Will Bitcoin reach $82,500 by August 31, 2026?")
        b=self.structural.threshold_signature("Will Bitcoin reach $90,000 by August 31, 2026?")
        c=self.structural.threshold_signature("Will Bitcoin reach $90,000 by December 31, 2026?")
        self.assertIsNotNone(a); self.assertIsNotNone(b); self.assertIsNotNone(c); self.assertEqual(a.family,b.family); self.assertNotEqual(a.family,c.family); self.assertLess(a.threshold,b.threshold)

    def test_typed_structural_v2_requires_expiry_metadata_identity(self):
        text=(ROOT/"scripts/v6_typed_structural_v2.py").read_text(encoding="utf-8")
        self.assertIn("market.end_ts<=0",text); self.assertIn("signature.unit,market.end_ts",text); self.assertIn("same verified end_ts",text)

    def test_block_bootstrap_reversion_score_detects_stable_ar(self):
        innovations=[.04,-.025,.015,-.035,.02,.005,-.01]; residual=[.7]
        for i in range(1,160): residual.append(.55*residual[-1]+innovations[i%len(innovations)])
        phi,_,sd=self.local.ar_phi(residual); pvalue,score=self.local.mean_reversion_score_pvalue(residual,seed=7,reps=400)
        self.assertGreater(sd,0); self.assertGreater(phi,.02); self.assertLess(phi,.999); self.assertLess(score,0); self.assertLess(pvalue,.10)

    def test_candidate_loop_is_flow_fee_and_queue_aware(self):
        loop=(ROOT/"scripts/paper_v6_loop_v2.sh").read_text(encoding="utf-8")
        for required in ("v6_micro_maker.py","v6_micro_taker_v2.py","v6_hard_arb_paper_v2.py","v6_local_factor_v2.py","v6_queue_filter.py","v6_typed_structural_v2.py","--trade-tape","min-joint-fill-probability","v6_runtime_status_v2.py"):
            self.assertIn(required,loop)
        self.assertNotIn("polymarket_maker_paper --config",loop)


if __name__ == "__main__":
    unittest.main()
