from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_cross_sectional_rank_frozen as frozen

CONFIG = ROOT / "config" / "research_v7_cross_sectional_rank_15m.json"


class Ranking15mRegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_15m_is_isolated_and_preregistered(self) -> None:
        cfg = self.cfg
        self.assertTrue(cfg["paper_only"])
        self.assertTrue(cfg["research_only"])
        self.assertFalse(cfg["live_intents_enabled"])
        self.assertEqual(cfg["horizons_minutes"], [15])
        self.assertEqual(cfg["history"]["fidelity_minutes"], 15)
        registration = cfg["frequency_registration"]
        self.assertEqual(registration["new_prospective_challenger_horizons_minutes"], [15])
        self.assertFalse(registration["pool_evidence_across_horizons"])
        self.assertFalse(registration["select_new_horizon_on_pre_registration_history"])
        self.assertTrue(registration["frozen_holdout_only"])
        discovery = cfg["discovery"]
        self.assertGreater(discovery["forward_holdout_start_ts"], discovery["discovery_cutoff_ts"])
        self.assertEqual(discovery["forward_holdout_start_ts"], 1787788800)

    def test_15m_feature_semantics_match_15m_grid(self) -> None:
        semantics = self.cfg["model"]["feature_horizon_semantics_minutes"]
        self.assertEqual(semantics["mom_1"], 15)
        self.assertEqual(semantics["mom_2"], 30)
        self.assertEqual(semantics["mom_4"], 60)
        self.assertEqual(semantics["mom_12"], 180)
        self.assertEqual(semantics["vol_12"], 180)

    def test_frozen_cutoff_is_one_15m_embargo_bucket(self) -> None:
        cfg = self.cfg
        cutoff = frozen.frozen_training_label_cutoff_ts(
            cfg["discovery"]["forward_holdout_start_ts"],
            bucket_seconds=15 * 60,
            embargo_steps=cfg["history"]["purge_embargo_buckets"],
        )
        self.assertEqual(cutoff, 1787787900)

    def test_execution_contract_is_v7_only_and_relative(self) -> None:
        serialized = json.dumps(self.cfg, sort_keys=True).lower()
        self.assertNotIn("shared_v6_v7", serialized)
        pair = self.cfg["relative_pair_contract"]
        self.assertTrue(pair["relative_forecast_only"])
        self.assertFalse(pair["absolute_single_leg_direction_allowed"])
        self.assertFalse(pair["terminal_probability_interpretation"])
        execution = self.cfg["execution_shadow"]
        self.assertFalse(execution["allow_product_of_marginals"])
        self.assertFalse(execution["allow_minimum_marginal_proxy"])
        self.assertEqual(
            execution["joint_fill_probability_source"],
            "canonical_v7_execution_ledger_empirical_joint_states",
        )

    def test_scheduler_contract_requires_server_and_15m_pit(self) -> None:
        scheduler = self.cfg["scheduler_contract"]
        self.assertTrue(scheduler["continuous_server_evaluation_required"])
        self.assertTrue(scheduler["github_actions_may_validate_but_is_not_exchange_runtime"])
        self.assertEqual(scheduler["point_in_time_universe_cadence_minutes_required"], 15)
        self.assertTrue(scheduler["canonical_scheduler_registry_update_required_before_integration"])


if __name__ == "__main__":
    unittest.main()
