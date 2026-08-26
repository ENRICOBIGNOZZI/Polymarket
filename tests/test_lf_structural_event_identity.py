#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_structural_event_identity_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_structural_event_identity_audit", MODULE_PATH)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


class StructuralEventIdentityAuditTest(unittest.TestCase):
    def test_legacy_python_hash_changes_across_fixed_hash_seeds(self) -> None:
        family = "will bitcoin be <direction> <threshold> by <year>|august 2026"
        ids = [AUDIT.legacy_event_id_for_seed(family, "UP", seed) for seed in (1, 2, 3)]
        self.assertEqual(len(set(ids)), 3)

    def test_stable_digest_is_restart_invariant_and_normalized(self) -> None:
        a = AUDIT.stable_event_id("  bitcoin   threshold family ", "up")
        b = AUDIT.stable_event_id("bitcoin threshold family", "UP")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("STRUCT:"))

    def test_current_scanner_uses_restart_variant_builtin_hash(self) -> None:
        source = (ROOT / "scripts" / "v6_relation_intents.py").read_text(encoding="utf-8")
        self.assertIn('abs(hash((family, direction)))', source)

    def test_restart_variant_identity_can_fragment_event_risk_buckets(self) -> None:
        family = "will bitcoin be <direction> <threshold> by <year>|august 2026"
        first = AUDIT.legacy_event_id_for_seed(family, "UP", 1)
        second = AUDIT.legacy_event_id_for_seed(family, "UP", 2)
        self.assertNotEqual(first, second)
        committed = {first: 1_000.0, second: 1_000.0}
        event_cap = 1_500.0
        self.assertTrue(all(value <= event_cap for value in committed.values()))
        self.assertGreater(sum(committed.values()), event_cap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
