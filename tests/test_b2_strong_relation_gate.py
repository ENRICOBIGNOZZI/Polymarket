from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "b2_strong_relation_gate",
    ROOT / "scripts" / "b2_strong_relation_gate.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
classify_scope = MODULE.classify_scope
gate_rows = MODULE.gate_rows


class B2StrongRelationGateTest(unittest.TestCase):
    def test_current_weak_semantic_bundle_is_rejected(self) -> None:
        scope = "semantic:0.1429:1|semantic:0.1000:1|semantic:0.6000:3|semantic:0.1000:1"
        ok, weak = classify_scope(scope, 0.20, 2, False)
        self.assertFalse(ok)
        self.assertEqual(len(weak), 3)
        self.assertIn("semantic:0.1000:1", weak)

    def test_same_event_and_strong_semantic_relations_pass(self) -> None:
        scope = "same_event:1.0000:0|semantic:0.3333:2|semantic:0.6000:3"
        ok, weak = classify_scope(scope, 0.20, 2, False)
        self.assertTrue(ok)
        self.assertEqual(weak, [])

    def test_same_category_cannot_authorize_execution_by_default(self) -> None:
        scope = "same_category:0.5000:0"
        ok, weak = classify_scope(scope, 0.20, 2, False)
        self.assertFalse(ok)
        self.assertEqual(weak, [scope])
        allowed, allowed_weak = classify_scope(scope, 0.20, 2, True)
        self.assertTrue(allowed)
        self.assertEqual(allowed_weak, [])

    def test_every_hedge_certificate_must_be_strong(self) -> None:
        rows = [
            {
                "market": "strong",
                "side": "YES",
                "coherence_scope": "same_event:1.0000:0|semantic:0.2500:2",
            },
            {
                "market": "weak",
                "side": "NO",
                "coherence_scope": "semantic:0.2500:2|semantic:0.1900:4",
            },
        ]
        kept, rejected = gate_rows(rows, 0.20, 2, False)
        self.assertEqual([row["market"] for row in kept], ["strong"])
        self.assertEqual([row["market"] for row in rejected], ["weak"])
        self.assertEqual(rejected[0]["weak_relation_certificates"], "semantic:0.1900:4")


if __name__ == "__main__":
    unittest.main()
