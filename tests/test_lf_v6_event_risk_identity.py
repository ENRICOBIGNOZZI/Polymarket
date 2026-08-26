from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_v6_event_risk_identity_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v6_event_risk_identity_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class V6EventRiskIdentityAuditTest(unittest.TestCase):
    def test_shared_intent_event_fragmented_into_condition_buckets(self) -> None:
        intents = [
            {"bundle_id": "B", "event_id": "842328", "market_id": "1"},
            {"bundle_id": "B", "event_id": "842328", "market_id": "2"},
            {"bundle_id": "B", "event_id": "842328", "market_id": "3"},
        ]
        legs = [
            {"bundle_id": "B", "event_id": "cond-a", "target_shares": "61.2245", "filled_shares": "0", "limit_price": "0.74", "entry_cash": "0", "order_state": "RESTING", "exited": "0"},
            {"bundle_id": "B", "event_id": "cond-b", "target_shares": "61.2245", "filled_shares": "0", "limit_price": "0.15", "entry_cash": "0", "order_state": "RESTING", "exited": "0"},
            {"bundle_id": "B", "event_id": "cond-c", "target_shares": "61.2245", "filled_shares": "0", "limit_price": "0.09", "entry_cash": "0", "order_state": "RESTING", "exited": "0"},
        ]
        out = MODULE.audit(intents, legs)
        self.assertEqual(out["mismatch_bundles"], 1)
        mismatch = out["mismatches"][0]
        self.assertEqual(mismatch["intent_event_id"], "842328")
        self.assertEqual(mismatch["persisted_leg_event_ids"], ["cond-a", "cond-b", "cond-c"])
        self.assertAlmostEqual(mismatch["commitment_usd"], 60.00001, places=5)
        self.assertAlmostEqual(mismatch["largest_persisted_bucket_usd"], 45.30613, places=5)
        self.assertGreater(mismatch["fragmentation_ratio"], 1.32)
        self.assertAlmostEqual(out["intent_event_commitment_usd"]["842328"], 60.00001, places=5)

    def test_fragmentation_can_bypass_event_cap_without_any_bucket_breaching(self) -> None:
        self.assertTrue(MODULE.fragmented_cap_allows([60.0, 60.0, 60.0], 100.0))
        self.assertFalse(MODULE.fragmented_cap_allows([120.0, 20.0], 100.0))
        self.assertFalse(MODULE.fragmented_cap_allows([30.0, 30.0], 100.0))

    def test_current_source_uses_market_event_identity_for_multileg_event_cap(self) -> None:
        source = (ROOT / "src" / "multileg_paper.cpp").read_text(encoding="utf-8")
        self.assertIn("event_per_unit[ms[i].event_id]+=leg_per_unit", source)
        self.assertIn("l.event_id=ms[i].event_id", source)
        self.assertIn("if(l.event_id!=event_id||l.exited) continue", source)
        self.assertIn("b.event_id=h.event_id", source)


if __name__ == "__main__":
    unittest.main()
