from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_execution_ledger as ledger
import v7_model_intent_router as router

SHA = "c" * 40


class ModelIntentRouterTest(unittest.TestCase):
    def test_local_factor_preserves_pair_and_blocks_unsafe_universe(self) -> None:
        report = {
            "timestamp": 100,
            "paper_only": True,
            "research_only": True,
            "live_intents_enabled": False,
            "submitted_orders": 0,
            "survivorship_safe": False,
            "current_residual_reconstructed_from_frozen_controls": True,
            "current_book_snapshot_contract": {"required": True, "guard_rejections": 0},
            "signals": [{
                "market_a": "a", "market_b": "b", "side_a": "YES", "side_b": "NO",
                "weight_a": 1.2, "weight_b": 0.8, "hold_seconds": 3600,
                "pvalue": 0.01, "current_residual_z_a": 2.0, "current_residual_z_b": -2.0,
            }],
        }
        intents = router.local_factor_intents(report, SHA)
        self.assertEqual(len(intents), 1)
        intent = intents[0]
        self.assertEqual(intent.family, "local_factor")
        self.assertEqual(len(intent.legs), 2)
        self.assertFalse(intent.executable)
        self.assertIn("point_in_time_universe_not_validated", intent.blockers)

    def test_local_factor_becomes_eligible_only_when_all_gates_pass(self) -> None:
        report = {
            "timestamp": 100,
            "paper_only": True, "research_only": True, "live_intents_enabled": False,
            "submitted_orders": 0, "survivorship_safe": True,
            "current_residual_reconstructed_from_frozen_controls": True,
            "current_book_snapshot_contract": {"required": True, "guard_rejections": 0},
            "signals": [{"market_a": "a", "market_b": "b", "side_a": "YES", "side_b": "NO", "weight_a": 1.0, "weight_b": 1.0, "hold_seconds": 1800}],
        }
        intent = router.local_factor_intents(report, SHA)[0]
        self.assertTrue(intent.executable)
        self.assertEqual(intent.blockers, ())

    def test_pca_horizons_remain_separate_and_single_leg(self) -> None:
        report = {
            "timestamp": 100, "paper_only": True, "research_only": True,
            "live_intents_enabled": False, "submitted_orders": 0,
            "single_leg_only": True, "hedge_legs_allowed": False,
            "total_single_leg_forecast_risk": True, "survivorship_safe": True,
            "horizons": [
                {"horizon_minutes": 30, "shadow_candidates": [{"market_id": "a", "event_id": "e1", "side": "YES", "horizon_seconds": 1800, "entry_price": 0.4, "net_edge": 0.01, "economic_score": 1.0}]},
                {"horizon_minutes": 120, "shadow_candidates": [{"market_id": "b", "event_id": "e2", "side": "NO", "horizon_seconds": 7200, "entry_price": 0.6, "net_edge": 0.02, "economic_score": 2.0}]},
            ],
        }
        intents = router.pca_intents(report, SHA)
        self.assertEqual([intent.horizon_seconds for intent in intents], [1800, 7200])
        self.assertTrue(all(len(intent.legs) == 1 for intent in intents))
        self.assertTrue(all(intent.executable for intent in intents))
        self.assertNotEqual(intents[0].candidate_id, intents[1].candidate_id)

    def test_pca_survivorship_unsafe_is_candidate_only(self) -> None:
        report = {
            "timestamp": 100, "paper_only": True, "research_only": True,
            "live_intents_enabled": False, "submitted_orders": 0,
            "single_leg_only": True, "hedge_legs_allowed": False,
            "total_single_leg_forecast_risk": True, "survivorship_safe": False,
            "horizons": [{"horizon_minutes": 60, "shadow_candidates": [{"market_id": "a", "event_id": "e", "side": "YES", "horizon_seconds": 3600, "net_edge": 0.01, "economic_score": 1.0}]}],
        }
        intent = router.pca_intents(report, SHA)[0]
        self.assertFalse(intent.executable)
        self.assertIn("point_in_time_universe_not_validated", intent.blockers)

    def test_ranking_can_never_be_absolute_single_leg(self) -> None:
        report = {
            "timestamp": 100, "paper_only": True, "research_only": True,
            "live_intents_enabled": False, "submitted_orders": 0,
            "relative_pair_only": True, "absolute_single_leg_mapping_disabled": True,
            "pool_evidence_across_horizons": False, "frozen_holdout_fit_validated": True,
            "point_in_time_universe_validated": True, "survivorship_safe": True,
        }
        pairs = [{
            "top_market_id": "top", "bottom_market_id": "bottom",
            "top_event_id": "et", "bottom_event_id": "eb", "horizon_seconds": 7200,
            "top_side": "YES", "bottom_side": "NO",
            "top_shares_per_pair_dollar": 1.1, "bottom_shares_per_pair_dollar": 0.9,
            "completed_pair_net_edge": 0.01, "economic_score": 0.5,
        }]
        intent = router.ranking_intents(report, pairs, SHA)[0]
        self.assertTrue(intent.executable)
        self.assertEqual(intent.semantics, "relative_top_bottom_pair")
        self.assertEqual(tuple(leg["side"] for leg in intent.legs), ("YES", "NO"))
        bad = dict(pairs[0]); bad["bottom_side"] = "YES"
        self.assertEqual(router.ranking_intents(report, [bad], SHA), [])

    def test_ranking_current_report_fails_closed_without_point_in_time_universe(self) -> None:
        report = {
            "timestamp": 100, "paper_only": True, "research_only": True,
            "live_intents_enabled": False, "submitted_orders": 0,
            "relative_pair_only": True, "absolute_single_leg_mapping_disabled": True,
            "pool_evidence_across_horizons": False, "frozen_holdout_fit_validated": True,
            "point_in_time_universe_validated": False, "survivorship_safe": False,
        }
        pairs = [{"top_market_id": "top", "bottom_market_id": "bottom", "horizon_seconds": 21600, "top_side": "YES", "bottom_side": "NO", "top_shares_per_pair_dollar": 1.0, "bottom_shares_per_pair_dollar": 1.0, "completed_pair_net_edge": 0.01, "economic_score": 0.5}]
        intent = router.ranking_intents(report, pairs, SHA)[0]
        self.assertFalse(intent.executable)
        self.assertIn("point_in_time_universe_not_validated", intent.blockers)

    def test_candidate_events_do_not_manufacture_orders_or_fills(self) -> None:
        intent = router.ModelIntent(
            candidate_id="id", model_sha=SHA, family="pca", horizon_seconds=3600,
            decision_ts_ms=100_000, semantics="single_leg_residual_stat_arb",
            legs=({"role": "single", "market_id": "m", "event_id": "e", "side": "YES", "weight": 1.0},),
            predicted_edge=0.01, economic_score=1.0, executable=False,
            blockers=("point_in_time_universe_not_validated",), provenance={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            with ledger.CanonicalLedgerWriter(path, writer_id="test", model_sha=SHA) as writer:
                self.assertEqual(router.write_candidate_events(writer, [intent]), 1)
            events = list(ledger.iter_events(path, expected_model_sha=SHA))
        self.assertEqual([event.event_type for event in events], ["CANDIDATE"])
        self.assertEqual(events[0].intended_action, "RESEARCH_CANDIDATE")
        self.assertFalse(events[0].metadata["execution_eligible"])


if __name__ == "__main__":
    unittest.main()
