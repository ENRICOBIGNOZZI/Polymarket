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
classify_row = MODULE.classify_row
gate_rows = MODULE.gate_rows


class B2StrongRelationGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = {
            "fr": {
                "slug": "will-bernard-cazeneuve-win-the-2027-french-presidential-election",
                "question": "Will Bernard Cazeneuve win the 2027 French presidential election?",
            },
            "us": {
                "slug": "will-person-ae-win-the-2028-us-presidential-election",
                "question": "Will Raphael Warnock win the 2028 US Presidential Election?",
            },
            "dal": {
                "slug": "will-dallas-mavericks-win-the-2027-nba-finals",
                "question": "Will Dallas Mavericks win the 2027 NBA Finals?",
            },
            "atl": {
                "slug": "will-atlanta-hawks-win-the-2027-nba-finals",
                "question": "Will Atlanta Hawks win the 2027 NBA Finals?",
            },
        }

    def classify(self, row: dict[str, str]):
        return classify_row(row, self.metadata, 0.20, 2, 2, False)

    def test_generic_presidential_words_do_not_link_different_elections(self) -> None:
        row = {
            "market": "fr",
            "legs": "fr:YES:1|us:YES:0.16",
            "coherence_scope": "semantic:0.2500:2",
        }
        ok, weak, notes = self.classify(row)
        self.assertFalse(ok)
        self.assertEqual(weak, ["semantic:0.2500:2"])
        self.assertIn("us:context=<none>", notes)

    def test_same_competition_and_year_is_meaningful_context(self) -> None:
        row = {
            "market": "dal",
            "legs": "dal:YES:1|atl:YES:0.20",
            "coherence_scope": "semantic:0.3333:2",
        }
        ok, weak, notes = self.classify(row)
        self.assertTrue(ok)
        self.assertEqual(weak, [])
        self.assertIn("atl:context=2027,nba", notes)

    def test_current_weak_semantic_bundle_is_rejected_before_context(self) -> None:
        row = {
            "market": "dal",
            "legs": "dal:YES:1|atl:YES:0.2",
            "coherence_scope": "semantic:0.1000:1",
        }
        ok, weak, _ = self.classify(row)
        self.assertFalse(ok)
        self.assertEqual(weak, ["semantic:0.1000:1"])

    def test_same_event_passes_without_semantic_context(self) -> None:
        row = {
            "market": "fr",
            "legs": "fr:YES:1|us:YES:0.2",
            "coherence_scope": "same_event:1.0000:0",
        }
        ok, weak, notes = self.classify(row)
        self.assertTrue(ok)
        self.assertEqual(weak, [])
        self.assertEqual(notes, ["us:same_event"])

    def test_same_category_cannot_authorize_execution_by_default(self) -> None:
        row = {
            "market": "dal",
            "legs": "dal:YES:1|atl:YES:0.2",
            "coherence_scope": "same_category:0.5000:0",
        }
        ok, weak, _ = self.classify(row)
        self.assertFalse(ok)
        self.assertEqual(weak, ["same_category:0.5000:0"])

    def test_every_hedge_certificate_must_match_a_hedge(self) -> None:
        row = {
            "market": "dal",
            "legs": "dal:YES:1|atl:YES:0.2",
            "coherence_scope": "semantic:0.3333:2|semantic:0.3333:2",
        }
        ok, weak, _ = self.classify(row)
        self.assertFalse(ok)
        self.assertEqual(weak, ["certificate_hedge_count_mismatch"])

    def test_gate_rows_records_context(self) -> None:
        rows = [
            {
                "market": "dal",
                "side": "YES",
                "legs": "dal:YES:1|atl:YES:0.2",
                "coherence_scope": "semantic:0.3333:2",
            },
            {
                "market": "fr",
                "side": "YES",
                "legs": "fr:YES:1|us:YES:0.2",
                "coherence_scope": "semantic:0.2500:2",
            },
        ]
        kept, rejected = gate_rows(rows, self.metadata, 0.20, 2, 2, False)
        self.assertEqual([row["market"] for row in kept], ["dal"])
        self.assertEqual([row["market"] for row in rejected], ["fr"])
        self.assertIn("2027,nba", kept[0]["strong_relation_context"])
        self.assertEqual(rejected[0]["weak_relation_certificates"], "semantic:0.2500:2")


if __name__ == "__main__":
    unittest.main()
