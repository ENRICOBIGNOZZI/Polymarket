from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "research_b2_relation_quality.py"


def load_module():
    spec = importlib.util.spec_from_file_location("research_b2_relation_quality_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load research_b2_relation_quality.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class B2RelationQualityResearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_same_event_is_strong_even_without_text_overlap(self):
        relation = self.module.parse_relation("same_event:1.0000:0")
        self.assertTrue(self.module.relation_is_strong(relation))

    def test_one_token_semantic_overlap_is_not_execution_grade(self):
        relation = self.module.parse_relation("semantic:0.1000:1")
        self.assertFalse(self.module.relation_is_strong(relation))

    def test_category_alone_is_prior_not_execution_authority(self):
        relation = self.module.parse_relation("same_category:0.5000:1")
        self.assertFalse(self.module.relation_is_strong(relation))

    def test_current_high_edge_candidate_fails_strong_relation_policy(self):
        candidate = {
            "market": "2774057",
            "slug": "strait-of-hormuz-traffic-returns-to-normal-by-september-30-20260702154339440",
            "raw_expected_edge": "0.00302568",
            "maker_entry_net_edge": "0.00222302",
            "taker_net_edge": "-0.0132019",
            "coherence_scope": "semantic:0.7143:5|semantic:0.1000:1|semantic:0.0833:1|semantic:0.7143:5|semantic:0.7143:5|semantic:0.1111:1",
        }
        audit = self.module.audit_candidate(candidate)
        self.assertFalse(audit.relation_quality_pass)
        self.assertEqual(len(audit.weak_relations), 3)
        self.assertAlmostEqual(audit.completion_hurdle, 0.8558812623, places=8)

    def test_current_messi_candidate_survives_relation_policy_but_has_high_completion_hurdle(self):
        candidate = {
            "market": "608565",
            "slug": "will-lionel-messi-win-the-2026-ballon-dor",
            "raw_expected_edge": "0.00181837",
            "maker_entry_net_edge": "0.000480047",
            "taker_net_edge": "-0.00268639",
            "coherence_scope": "semantic:0.2857:2|semantic:0.3333:2|semantic:0.3333:2",
        }
        audit = self.module.audit_candidate(candidate)
        self.assertTrue(audit.relation_quality_pass)
        self.assertEqual(audit.weak_relations, ())
        self.assertAlmostEqual(audit.completion_hurdle, 0.8483952152, places=8)

    def test_current_two_maker_positive_rows_reduce_to_one_under_strong_relations(self):
        payload = {
            "candidates": {
                "b2": [
                    {
                        "market": "2774057",
                        "slug": "hormuz",
                        "raw_expected_edge": "0.00302568",
                        "maker_entry_net_edge": "0.00222302",
                        "taker_net_edge": "-0.0132019",
                        "coherence_scope": "semantic:0.7143:5|semantic:0.1000:1|semantic:0.0833:1|semantic:0.7143:5|semantic:0.7143:5|semantic:0.1111:1",
                    },
                    {
                        "market": "608565",
                        "slug": "messi",
                        "raw_expected_edge": "0.00181837",
                        "maker_entry_net_edge": "0.000480047",
                        "taker_net_edge": "-0.00268639",
                        "coherence_scope": "semantic:0.2857:2|semantic:0.3333:2|semantic:0.3333:2",
                    },
                ]
            }
        }
        report = self.module.evaluate_snapshot(payload)
        self.assertEqual(report["maker_positive_count"], 2)
        self.assertEqual(report["strong_relation_maker_positive_count"], 1)
        self.assertEqual(report["strong_relation_maker_positive"][0]["market"], "608565")


if __name__ == "__main__":
    unittest.main()
