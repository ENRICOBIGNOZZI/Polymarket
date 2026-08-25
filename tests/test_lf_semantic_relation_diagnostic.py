#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_semantic_relation_diagnostic.py"
SPEC = importlib.util.spec_from_file_location("lf_semantic_relation_diagnostic", MODULE_PATH)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class LFSemanticRelationDiagnosticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = (ROOT / "src" / "engine.cpp").read_text(encoding="utf-8")
        cls.config = json.loads((ROOT / "config" / "paper_v5.json").read_text(encoding="utf-8"))
        cls.result = MOD.run_diagnostic(cls.engine, cls.config)

    def test_current_semantic_contract_is_lexical_without_relation_guards(self) -> None:
        contract = self.result["current_contract"]
        self.assertTrue(contract["semantic_block_found"])
        self.assertTrue(contract["lexical_jaccard_used"])
        self.assertTrue(contract["direct_peer_probability_average"])
        self.assertTrue(contract["shrink_to_peer_probability"])
        self.assertFalse(contract["explicit_polarity_guard"])
        self.assertFalse(contract["explicit_threshold_guard"])
        self.assertFalse(contract["explicit_expiry_guard"])

    def test_opposite_polarity_questions_pass_similarity_gate(self) -> None:
        case = self.result["counterexamples"]["opposite_polarity"]
        self.assertGreater(case["lexical_similarity"], self.config["semantic_min_similarity"])
        self.assertTrue(case["passes_similarity_gate"])
        self.assertEqual(case["accepted_peer_counts"], [1, 1])
        self.assertAlmostEqual(case["semantic_probabilities"][0], 0.50, places=12)
        self.assertAlmostEqual(case["semantic_probabilities"][1], 0.50, places=12)
        self.assertAlmostEqual(case["absolute_probability_shifts"][0], 0.30, places=12)
        self.assertAlmostEqual(case["absolute_probability_shifts"][1], 0.30, places=12)

    def test_ordered_threshold_curve_is_collapsed(self) -> None:
        case = self.result["counterexamples"]["ordered_thresholds"]
        self.assertGreater(case["lexical_similarity"], self.config["semantic_min_similarity"])
        self.assertTrue(case["passes_similarity_gate"])
        self.assertAlmostEqual(case["market_monotonic_gap"], 0.50, places=12)
        self.assertAlmostEqual(case["semantic_probabilities"][0], 0.50, places=12)
        self.assertAlmostEqual(case["semantic_probabilities"][1], 0.50, places=12)
        self.assertAlmostEqual(case["semantic_monotonic_gap"], 0.0, places=12)
        self.assertAlmostEqual(case["absolute_probability_shifts"][0], 0.25, places=12)
        self.assertAlmostEqual(case["absolute_probability_shifts"][1], 0.25, places=12)

    def test_diagnostic_marks_current_contract_material(self) -> None:
        self.assertTrue(self.result["material_structural_defect"])
        self.assertEqual(self.result["evidence_state"], "MORE_EVIDENCE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
