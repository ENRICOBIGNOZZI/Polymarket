from research.walk_forward_v3.gate_frontier import (
    GATE_POLICIES,
    gate_policy,
    select_gate_from_validation,
)


class DummyModel:
    maximum_effective_action_age_ms = None
    minimum_regime_action_targets = 0
    minimum_side_regime_action_targets = 0
    support_policy_mode = "DIAGNOSTIC"


def entry(policy_id, lower, observed):
    return {
        "policy_id": policy_id,
        "robust_total_pnl_lower_bound": lower,
        "risk": {
            "total_observed_net_pnl": observed,
            "selected_trades": 3,
        },
    }


def test_gate_policy_context_restores_model_state():
    model = DummyModel()
    spec = next(
        item for item in GATE_POLICIES
        if item["policy_id"] == "ALL_GATES_BALANCED"
    )
    with gate_policy(model, spec):
        assert model.maximum_effective_action_age_ms == 250.0
        assert model.minimum_regime_action_targets == 8
        assert model.minimum_side_regime_action_targets == 2
        assert model.support_policy_mode == "ROBUST_WORST_CASE"
    assert model.maximum_effective_action_age_ms is None
    assert model.minimum_regime_action_targets == 0
    assert model.minimum_side_regime_action_targets == 0
    assert model.support_policy_mode == "DIAGNOSTIC"


def test_validation_gate_selection_uses_robust_lower_bound_not_observed_pnl():
    result = select_gate_from_validation([
        entry("HIGH_OBS_BAD_LOWER", -1.0, 10.0),
        entry("POSITIVE_LOWER", .3, .4),
        entry("BETTER_LOWER", .5, .1),
    ])
    assert result["state"] == "VALIDATION_GATE_POLICY_SELECTED"
    assert result["policy_id"] == "BETTER_LOWER"
    assert result["selected_validation_lower_bound"] == .5


def test_no_trade_baseline_wins_when_all_validation_lower_bounds_nonpositive():
    result = select_gate_from_validation([
        entry("A", -1.0, 4.0),
        entry("B", 0.0, 8.0),
    ])
    assert result["state"] == "NO_TRADE_DOMINATES_VALIDATION_LOWER_BOUND"
    assert result["policy_id"] is None
    assert result["selected_validation_lower_bound"] == 0.0


def test_missing_robust_bounds_fail_closed():
    result = select_gate_from_validation([
        entry("A", None, 1.0),
        entry("B", None, 2.0),
    ])
    assert result["state"] == "NO_VALIDATION_POLICY_WITH_CAUSAL_LOWER_BOUND"
    assert result["policy_id"] is None


def test_policy_grid_contains_separate_single_lever_and_combined_candidates():
    names = {item["policy_id"] for item in GATE_POLICIES}
    assert {
        "OPEN_DIAGNOSTIC",
        "AGE100",
        "AGE250",
        "ROBUST_SUPPORT",
        "REGIME8",
        "REGIME32",
        "SIDE2",
        "SIDE5",
        "ALL_GATES_BALANCED",
    }.issubset(names)
