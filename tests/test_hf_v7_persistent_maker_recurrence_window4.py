#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "research" / "hf_v7_persistent_maker_recurrence_window4_20260827.json"


class PersistentMakerRecurrenceWindow4Tests(unittest.TestCase):
    def test_third_recurrence_is_chronological_but_not_execution_credit(self) -> None:
        report = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        cutoff = int(report["preregistered_source"]["prospective_not_before_ms"])
        prior_final = int(report["prior_prospective_recurrence"]["final_observed_ts_ms"])
        recurrence = report["prospective_recurrence"]
        later = report["later_window"]
        first = int(recurrence["first_later_observed_ts_ms"])
        final = int(recurrence["final_later_observed_ts_ms"])

        self.assertGreater(first, cutoff)
        self.assertGreater(first, prior_final)
        self.assertGreater(final, first)
        self.assertEqual(int(recurrence["delay_after_preregistered_cutoff_ms"]), first - cutoff)
        self.assertEqual(int(recurrence["gap_after_prior_recurrence_ms"]), first - prior_final)
        self.assertEqual(int(recurrence["observed_later_span_ms"]), final - first)
        self.assertEqual(recurrence["candidate_id"], "maker-binary:1321564")
        self.assertTrue(recurrence["third_independent_post_registration_recurrence"])

        self.assertTrue(recurrence["all_rows_above_authorized_0_5bp_floor"])
        self.assertTrue(recurrence["passes_original_all_rows_extra_10bp_edge_frontier"])
        self.assertGreater(
            float(recurrence["minimum_edge_after_extra_10bp_stress"]),
            float(report["authority"]["min_net_edge"]),
        )
        self.assertFalse(recurrence["passes_original_persistence_window"])
        self.assertLess(
            int(recurrence["state_change_observations"]),
            int(report["preregistered_source"]["original_persistence_min_observations"]),
        )
        self.assertLess(
            int(recurrence["observed_later_span_ms"]),
            int(report["preregistered_source"]["original_persistence_min_span_ms"]),
        )

        self.assertEqual(later["research_head_sha"], "ad25ec36046fec8e490e8fa8aacca45b4d9766be")
        self.assertEqual(later["checked_out_revision"], "da9c1a862b101c79bd7239b8c7c5f8c207ddbd48")
        self.assertNotEqual(later["checked_out_revision"], later["research_head_sha"])
        self.assertEqual(later["checked_out_ref"], "refs/pull/708/merge")
        self.assertFalse(later["authority_valid_execution_window"])
        self.assertEqual(int(recurrence["first_row_exchange_ts_ms"]), 0)

        legacy_policy = later["observed_legacy_candidate_policy"]
        self.assertNotEqual(float(legacy_policy["min_net_edge"]), float(report["authority"]["min_net_edge"]))
        self.assertNotEqual(float(legacy_policy["external_uncertainty_penalty"]), float(report["authority"]["uncertainty_penalty"]))

        self.assertFalse(report["paper_fill_claim"])
        self.assertFalse(report["realized_pnl_claim"])
        self.assertFalse(recurrence["paper_fill"])
        self.assertFalse(recurrence["paired_completion"])
        self.assertIsNone(recurrence["realized_fill_conditioned_pnl_usd"])
        self.assertFalse(report["promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
