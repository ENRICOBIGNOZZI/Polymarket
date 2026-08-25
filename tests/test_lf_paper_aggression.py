#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER_SPEC = importlib.util.spec_from_file_location(
    "render_lf_semantic_challenger", ROOT / "scripts" / "render_lf_semantic_challenger.py"
)
assert RENDER_SPEC and RENDER_SPEC.loader
renderer = importlib.util.module_from_spec(RENDER_SPEC)
sys.modules[RENDER_SPEC.name] = renderer
RENDER_SPEC.loader.exec_module(renderer)


def tokens(text: str) -> set[str]:
    out: set[str] = set()
    cur = ""
    for char in text.lower():
        if char.isalnum():
            cur += char
        else:
            if cur and (len(cur) >= 2 or cur.isdigit()):
                out.add(cur)
            cur = ""
    if cur and (len(cur) >= 2 or cur.isdigit()):
        out.add(cur)
    return out


DIRECTIONS = {"above", "below", "over", "under", "more", "less", "least", "most", "before", "after", "between", "by"}
STOPWORDS = {
    "will", "would", "could", "should", "does", "did", "the", "this", "that", "what", "when", "which",
    "who", "whom", "whose", "have", "has", "had", "happen", "happens", "happened", "market", "markets",
    "price", "prices", "reach", "reaches", "reached", "become", "becomes", "became", "before", "after",
    "during", "between", "into", "from", "with", "without", "over", "under", "above", "below", "more",
    "less", "than", "then", "year", "years", "month", "months", "day", "days", "week", "weeks", "yes",
    "not", "never", "no", "and", "for", "are", "was", "were", "you", "your", "its", "our", "their",
}


def relation_compatible(a: str, b: str, same_event: bool = False, minimum_similarity: float = 0.55) -> bool:
    ta, tb = tokens(a), tokens(b)
    inter = len(ta & tb)
    union = len(ta | tb)
    lexical = inter / union if union else 0.0
    if lexical < minimum_similarity:
        return False
    negative = lambda x: bool(x & {"no", "not", "never", "without"})
    if negative(ta) != negative(tb):
        return False
    numeric = lambda x: {t for t in x if any(ch.isdigit() for ch in t)}
    if numeric(ta) != numeric(tb):
        return False
    if (ta & DIRECTIONS) != (tb & DIRECTIONS):
        return False
    salient = lambda x: {t for t in x if t not in STOPWORDS and t not in DIRECTIONS and not any(ch.isdigit() for ch in t)}
    sa, sb = salient(ta), salient(tb)
    shared = len(sa & sb)
    entity_union = len(sa | sb)
    entity_sim = shared / entity_union if entity_union else 0.0
    return shared >= 2 and entity_sim >= (0.65 if same_event else 0.80)


class LFPaperAggressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads((ROOT / "config" / "paper_v5.json").read_text(encoding="utf-8"))
        cls.loop = (ROOT / "scripts" / "paper_v5_loop.sh").read_text(encoding="utf-8")
        cls.engine = (ROOT / "src" / "engine.cpp").read_text(encoding="utf-8")
        cls.rendered = renderer.render(cls.engine)
        cls.strategies = {
            item["name"]: item for item in cls.config["multi_strategy"]["strategies"]
        }

    def test_hard_risk_boundaries_are_unchanged(self) -> None:
        self.assertEqual(self.config["max_drawdown"], 0.15)
        self.assertEqual(self.config["max_market_fraction"], 0.025)
        self.assertEqual(self.config["max_event_fraction"], 0.08)
        self.assertEqual(self.config["max_gross_fraction"], 0.45)
        self.assertEqual(self.config["multi_strategy"]["global_max_drawdown"], 0.15)
        self.assertEqual(self.config["multi_strategy"]["global_max_gross_fraction"], 0.45)
        for strategy in self.strategies.values():
            self.assertEqual(strategy["overrides"]["max_drawdown"], 0.15)
        self.assertIn('--max-leg-risk-usd 12', self.loop)
        self.assertIn('--completion-threshold 0.75', self.loop)

    def test_lf_search_is_materially_broader_and_edge_gate_stays_positive(self) -> None:
        self.assertEqual(self.config["market_limit"], 1000)
        self.assertEqual(self.config["min_liquidity"], 10.0)
        self.assertEqual(self.config["pca_min_history"], 16)
        self.assertEqual(self.config["pca_universe"], 600)
        self.assertEqual(self.strategies["pca"]["overrides"]["pca_min_history"], 16)
        self.assertEqual(self.strategies["pca"]["overrides"]["pca_universe"], 600)
        self.assertGreater(self.config["min_net_edge"], 0.0)
        self.assertLessEqual(self.config["min_net_edge"], 0.00005)
        self.assertIn('STAT_INTERVAL_SECONDS="${V5_STAT_INTERVAL_SECONDS:-60}"', self.loop)
        self.assertIn('--history-universe 500', self.loop)
        self.assertIn('--universe 500', self.loop)
        self.assertIn('--min-z 0.55 --min-t-reversion 0.60', self.loop)
        # Do not manufacture B2 opportunities by weakening the already-problematic
        # relation or unit-root/reversion evidence gates.
        self.assertIn('--min-jaccard 0.20 --min-shared-tokens 2', self.loop)
        self.assertIn('--min-t-reversion 0.60', self.loop)

    def test_semantic_shrink_is_ten_times_weaker_and_relation_guard_is_compiled(self) -> None:
        semantic = self.strategies["semantic"]["overrides"]
        self.assertEqual(semantic["semantic_min_similarity"], 0.55)
        self.assertLessEqual(semantic["semantic_shrink"], 0.05)
        self.assertIn('negative(target_tokens) != negative(peer_tokens)', self.rendered)
        self.assertIn('target_numeric != subset(peer_tokens, {}, true)', self.rendered)
        self.assertIn('required_entity_sim = same_event ? 0.65 : 0.80', self.rendered)
        self.assertNotIn('if (cur.size() >= 3) out.push_back(cur);', self.rendered)

    def test_semantic_counterexamples_abstain_but_near_equivalent_entities_pass(self) -> None:
        self.assertFalse(relation_compatible(
            "Will no Fed rate cuts happen in 2026?",
            "Will 1 Fed rate cut happen in 2026?",
            same_event=True,
        ))
        self.assertFalse(relation_compatible(
            "Will Bitcoin be above 100000 by December 31 2026?",
            "Will Bitcoin be above 150000 by December 31 2026?",
            same_event=True,
        ))
        self.assertFalse(relation_compatible(
            "Will Alice win the 2028 mayor election?",
            "Will Bob win the 2028 mayor election?",
            same_event=True,
        ))
        self.assertTrue(relation_compatible(
            "Will Donald Trump win Florida in 2028?",
            "Will Donald Trump win Florida during 2028?",
            same_event=True,
        ))

    def test_external_intelligence_is_runtime_wired_but_probability_only(self) -> None:
        self.assertEqual(self.config["external_signals_file"], "runs/paper_v5_live/external_signals.csv")
        self.assertIn('refresh_external_feed()', self.loop)
        self.assertIn('telemetry/latest-external-signals.jsonl', self.loop)
        self.assertIn('materialize_external_paper_signals.py', self.loop)
        self.assertIn('--min-mapping-score "$EXTERNAL_MIN_MAPPING_SCORE"', self.loop)
        materializer = (ROOT / "scripts" / "materialize_external_paper_signals.py").read_text(encoding="utf-8")
        self.assertIn('q_external', materializer)
        self.assertNotIn('feature_value") *', materializer)


if __name__ == "__main__":
    unittest.main()
