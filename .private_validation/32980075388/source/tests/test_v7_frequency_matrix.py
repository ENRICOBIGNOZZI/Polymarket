#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_frequency_registration_is_explicit_and_nonpooled():
    matrix = json.loads((ROOT / "config/v7_frequency_matrix.json").read_text(encoding="utf-8"))
    assert matrix["forecast_horizons_minutes"]["pca_stat_arb"] == [30, 60, 120, 360]
    assert matrix["forecast_horizons_minutes"]["cross_sectional_rank"] == [30, 60, 120, 360]
    assert matrix["local_factor_fidelity_minutes"] == [30, 60]
    rules = matrix["evidence_rules"]
    assert rules["separate_state_by_frequency"] is True
    assert rules["separate_pnl_by_frequency"] is True
    assert rules["no_post_hoc_frequency_selection_on_same_holdout"] is True


def test_ranking_new_30m_60m_are_forward_challengers_not_retrofit():
    cfg = json.loads((ROOT / "config/research_v7_cross_sectional_rank.json").read_text(encoding="utf-8"))
    registration = cfg["frequency_registration"]
    assert registration["legacy_discovery_selected_horizons_minutes"] == [120, 360]
    assert registration["new_prospective_challenger_horizons_minutes"] == [30, 60]
    assert registration["pool_evidence_across_horizons"] is False
    assert registration["select_new_horizon_on_pre_registration_history"] is False


def test_local_factor_30m_and_60m_are_separate_configs():
    thirty = json.loads((ROOT / "config/research_v7_local_factor.json").read_text(encoding="utf-8"))
    sixty = json.loads((ROOT / "config/research_v7_local_factor_60m.json").read_text(encoding="utf-8"))
    assert thirty["history"]["fidelity_minutes"] == 30
    assert sixty["history"]["fidelity_minutes"] == 60
    assert sixty["promotion_gate"]["do_not_pool_with_30m_for_inference"] is True


if __name__ == "__main__":
    test_frequency_registration_is_explicit_and_nonpooled()
    test_ranking_new_30m_60m_are_forward_challengers_not_retrofit()
    test_local_factor_30m_and_60m_are_separate_configs()
    print("ok 3 v7 frequency tests")
