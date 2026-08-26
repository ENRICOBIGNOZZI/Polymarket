#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_frequency_registration_follows_operator_authority_and_is_nonpooled():
    directives = load_json("config/operator_directives.json")
    matrix = load_json("config/v7_frequency_matrix.json")
    authorized = directives["frequency_research"]["forecast_minutes"]
    assert authorized == [30, 60, 120, 360]
    assert matrix["forecast_horizons_minutes"]["pca_stat_arb"] == authorized
    assert matrix["forecast_horizons_minutes"]["cross_sectional_rank"] == authorized
    assert matrix["local_factor_fidelity_minutes"] == [30, 60]
    rules = matrix["evidence_rules"]
    assert rules["separate_state_by_frequency"] is True
    assert rules["separate_pnl_by_frequency"] is True
    assert rules["no_pooling_across_horizons_for_pvalues"] is True
    assert rules["no_post_hoc_frequency_selection_on_same_holdout"] is True


def test_ranking_registration_is_authority_bound_and_prospective():
    directives = load_json("config/operator_directives.json")
    cfg = load_json("config/research_v7_cross_sectional_rank.json")
    registration = cfg["frequency_registration"]
    authorized = directives["frequency_research"]["forecast_minutes"]
    legacy = registration["legacy_discovery_selected_horizons_minutes"]
    prospective = registration["new_prospective_challenger_horizons_minutes"]
    assert cfg["horizons_minutes"] == authorized
    assert legacy == [120, 360]
    assert prospective == [h for h in authorized if h not in legacy] == [30, 60]
    assert registration["pool_evidence_across_horizons"] is False
    assert registration["select_new_horizon_on_pre_registration_history"] is False


def test_local_factor_30m_and_60m_are_separate_configs():
    thirty = load_json("config/research_v7_local_factor.json")
    sixty = load_json("config/research_v7_local_factor_60m.json")
    assert thirty["history"]["fidelity_minutes"] == 30
    assert sixty["history"]["fidelity_minutes"] == 60
    assert sixty["promotion_gate"]["do_not_pool_with_30m_for_inference"] is True


if __name__ == "__main__":
    test_frequency_registration_follows_operator_authority_and_is_nonpooled()
    test_ranking_registration_is_authority_bound_and_prospective()
    test_local_factor_30m_and_60m_are_separate_configs()
    print("ok 3 v7 frequency tests")
