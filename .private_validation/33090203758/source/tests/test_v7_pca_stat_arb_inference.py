#!/usr/bin/env python3
from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_pca_stat_arb_core as core
import v7_pca_stat_arb_inference as inference


@dataclass(frozen=True)
class M:
    market_id: str
    event_id: str
    question: str


def fixture(points: int = 120) -> core.RawPanel:
    times = tuple(index * 1800 for index in range(points))
    # Deliberately stationary nuisance factors with comfortably identified AR(1)
    # dynamics.  The previous very-slow oscillations generated a PCA component
    # with phi ~= 0.9996, correctly triggering the V7 near-unit-root fail-closed
    # common-factor forecast guard and making the downstream sigma test invalid.
    c1 = [math.sin(index * 0.31) for index in range(points)]
    c2 = [math.cos(index * 0.23) for index in range(points)]
    c3 = [0.6 * math.sin(index * 0.17) + 0.2 * math.cos(index * 0.43) for index in range(points)]
    residual = [0.25]
    for index in range(1, points):
        shock = 0.05 * math.sin(index * 0.41) + 0.025 * math.cos(index * 0.19)
        residual.append(0.78 * residual[-1] + shock)
    target = [0.75 * c1[i] - 0.35 * c2[i] + 0.25 * c3[i] + residual[i] for i in range(points)]
    return core.RawPanel(times, {
        "target": tuple(target),
        "c1": tuple(c1),
        "c2": tuple(c2),
        "c3": tuple(c3),
    })


def test_conditional_null_keeps_all_nuisance_controls_exactly_fixed():
    panel = fixture()
    model = core.fit_target(panel, "target", max_components=3, explained_variance_threshold=0.8)
    assert model is not None
    boot = inference.conditional_null_panel(panel, model, random.Random(7))
    assert boot is not None
    for control in model.controls:
        assert boot.values[control] == panel.values[control]
    assert boot.values["target"] != panel.values["target"]


def test_by_is_more_conservative_than_bh_under_arbitrary_dependence():
    pvalues = {"a": 0.03, "b": 0.06, "c": 0.09}
    assert core.bh_selected(pvalues, 0.10) == {"a", "b", "c"}
    assert inference.benjamini_yekutieli_selected(pvalues, 0.10) == set()
    assert inference.by_effective_q(3, 0.10) < 0.10


def test_predeclared_controls_are_metadata_only_and_input_order_invariant():
    markets = [
        M("a", "event", "Will BTC exceed 100k this year"),
        M("b", "event", "Will BTC exceed 120k this year"),
        M("c", "event", "Will BTC exceed 150k this year"),
        M("d", "other", "Will ETH exceed 10k this year"),
        M("e", "other", "Will rain exceed 10 inches"),
    ]
    first = inference.predeclare_target_controls(markets, "a", minimum_controls=2, maximum_controls=3)
    second = inference.predeclare_target_controls(list(reversed(markets)), "a", minimum_controls=2, maximum_controls=3)
    assert first is not None and second is not None
    assert first.controls == second.controls
    assert first.controls[:2] == ("b", "c")


def test_missing_predeclared_control_fails_closed_without_replacement():
    plan = inference.TargetControlPlan("target", ("c1", "c2", "missing"))
    panel = fixture()
    histories = {market_id: {t: x for t, x in zip(panel.times, values)} for market_id, values in panel.values.items()}
    assert inference.build_predeclared_target_panel(histories, plan, bucket_seconds=1800, min_points=72) is None


def test_total_single_leg_sigma_includes_common_factor_forecast_error():
    panel = fixture()
    model = core.fit_target(panel, "target", max_components=3, explained_variance_threshold=0.8)
    assert model is not None
    current = {market_id: values[-1] for market_id, values in panel.values.items()}
    residual_only = core.score_current(model, current, 4)
    total = inference.score_with_total_single_leg_risk(panel, model, current, 4)
    assert residual_only is not None and total is not None
    assert total.sigma_logit >= residual_only.sigma_logit
    assert total.sigma_logit > 0.0
    assert residual_only.common_factor_forecast_identified is True


def test_near_unit_root_common_factor_fails_closed():
    panel = fixture()
    model = core.fit_target(panel, "target", max_components=3, explained_variance_threshold=0.8)
    assert model is not None
    unstable = core.CurrentPcaTargetModel(
        **{
            **model.__dict__,
            "factor_phis": tuple(0.9995 if index == 0 else value for index, value in enumerate(model.factor_phis)),
        }
    )
    current = {market_id: values[-1] for market_id, values in panel.values.items()}
    assert core.score_current(unstable, current, 4) is None


def test_research_driver_preserves_successor_base_and_adds_current_contracts():
    base = (ROOT / "scripts" / "v7_pca_stat_arb_research_base.py").read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts" / "v7_pca_stat_arb_research.py").read_text(encoding="utf-8")
    assert "predeclare_target_controls" in base
    assert "conditional_target_bootstrap_pvalue" in base
    assert "benjamini_yekutieli_selected" in base
    assert "score_with_total_single_leg_risk" in base
    assert "resolve_fee_details" in base
    assert "uncertainty_penalty" in base
    assert "unestimable_pvalue\": 1.0" in base
    assert "validate_coherent_books" in wrapper
    assert "common_factor_forecast_identified" in wrapper
    assert "current_residual_z_gate" in wrapper
    assert "config/research_v7_market_data.json" in wrapper
    assert "v6_local_factor_intents.py" not in wrapper


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} V7 PCA inference successor tests")
