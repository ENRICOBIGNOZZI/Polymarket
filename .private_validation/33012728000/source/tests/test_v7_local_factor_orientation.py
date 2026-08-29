#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "v7_local_factor_core_orientation_test"
SPEC = importlib.util.spec_from_file_location(NAME, ROOT / "scripts" / "v7_local_factor_core.py")
assert SPEC is not None and SPEC.loader is not None
lf = importlib.util.module_from_spec(SPEC)
sys.modules[NAME] = lf
SPEC.loader.exec_module(lf)


def close(a: float, b: float, tol: float = 1e-9) -> bool:
    return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= tol


def test_temporal_pc1_is_invariant_to_individual_control_sign_flip() -> None:
    c1 = tuple(float(i - 20) for i in range(40))
    c2 = tuple(0.7 * x + (0.3 if i % 3 else -0.2) for i, x in enumerate(c1))
    c3 = tuple(-0.4 * x + (0.1 if i % 2 else -0.1) for i, x in enumerate(c1))
    a = lf.orientation_invariant_pc1({"c1": c1, "c2": c2, "c3": c3})
    b = lf.orientation_invariant_pc1({"c1": c1, "c2": tuple(-x for x in c2), "c3": c3})
    assert a is not None and b is not None
    assert len(a) == len(b)
    assert max(abs(x - y) for x, y in zip(a, b)) < 1e-9


def test_oppositely_coded_duplicate_controls_do_not_cancel_factor() -> None:
    times = tuple(range(60))
    common = [0.0]
    for i in range(1, len(times)):
        common.append(common[-1] + (0.08 if i % 5 else -0.05))
    c1 = common
    c2 = [-x for x in common]
    a = [x + 0.03 * math.sin(i) for i, x in enumerate(common)]
    b = [1.2 * x + 0.04 * math.cos(i) for i, x in enumerate(common)]
    panel = lf.standardize_levels({"a": a, "b": b, "c1": c1, "c2": c2}, times)
    assert panel is not None
    old = [0.5 * (panel.values["c1"][i] + panel.values["c2"][i]) for i in range(len(times))]
    assert max(abs(x) for x in old) < 1e-10
    fit = lf.fit_pair(panel, "a", "b")
    assert fit is not None
    assert abs(fit.loading_a) > 1e-6
    assert abs(fit.loading_b) > 1e-6


def test_pair_fit_is_invariant_to_control_yes_no_recoding() -> None:
    times = tuple(range(80))
    f = [0.0]
    for i in range(1, len(times)):
        f.append(f[-1] + 0.12 * math.sin(i / 5.0) + 0.03)
    c1 = [x + 0.02 * math.sin(i) for i, x in enumerate(f)]
    c2 = [0.9 * x + 0.03 * math.cos(i / 2.0) for i, x in enumerate(f)]
    a = [1.1 * x + 0.05 * math.sin(i / 3.0) for i, x in enumerate(f)]
    b = [0.8 * x + 0.04 * math.cos(i / 4.0) for i, x in enumerate(f)]
    panel1 = lf.standardize_levels({"a": a, "b": b, "c1": c1, "c2": c2}, times)
    panel2 = lf.standardize_levels({"a": a, "b": b, "c1": c1, "c2": [-x for x in c2]}, times)
    assert panel1 is not None and panel2 is not None
    fit1 = lf.fit_pair(panel1, "a", "b")
    fit2 = lf.fit_pair(panel2, "a", "b")
    assert fit1 is not None and fit2 is not None
    assert close(fit1.loading_a, fit2.loading_a)
    assert close(fit1.loading_b, fit2.loading_b)
    assert close(fit1.residual_z_a, fit2.residual_z_a)
    assert close(fit1.residual_z_b, fit2.residual_z_b)
    assert close(fit1.pair_stat, fit2.pair_stat)


def test_completed_history_view_excludes_incomplete_current_and_future_buckets() -> None:
    now = 10_000
    bucket = 60
    current_start = (now // bucket) * bucket
    histories = {
        "a": {
            current_start - bucket: 1.0,
            current_start: 2.0,
            current_start + bucket: 3.0,
        }
    }
    completed = lf.completed_history_view(histories, now=now, bucket_seconds=bucket)
    assert completed == {"a": {current_start - bucket: 1.0}}


def test_recent_completed_regular_panel_is_fresh() -> None:
    now = 10_000
    bucket = 60
    current_start = (now // bucket) * bucket
    times = tuple(current_start - 10 * bucket + i * bucket for i in range(10))
    panel = lf.standardize_levels(
        {
            "a": [float(i) for i in range(10)],
            "b": [1.2 * i + (0.1 if i % 2 else -0.1) for i in range(10)],
            "c1": [0.8 * i + (0.2 if i % 3 else -0.2) for i in range(10)],
            "c2": [1.1 * i + (0.15 if i % 4 else -0.15) for i in range(10)],
        },
        times,
    )
    assert panel is not None
    result = lf.assess_panel_freshness(panel, now=now, bucket_seconds=bucket, maximum_age_buckets=2.0)
    assert result.fresh
    assert result.reason == "fresh_completed_regular_history"
    assert result.latest_completed_bucket_end_ts == current_start


def test_regular_but_stale_panel_fails_closed() -> None:
    now = 10_000
    bucket = 60
    current_start = (now // bucket) * bucket
    times = tuple(current_start - 16 * bucket + i * bucket for i in range(10))
    panel = lf.standardize_levels(
        {
            "a": [float(i) for i in range(10)],
            "b": [1.2 * i + (0.1 if i % 2 else -0.1) for i in range(10)],
            "c1": [0.8 * i + (0.2 if i % 3 else -0.2) for i in range(10)],
            "c2": [1.1 * i + (0.15 if i % 4 else -0.15) for i in range(10)],
        },
        times,
    )
    assert panel is not None
    result = lf.assess_panel_freshness(panel, now=now, bucket_seconds=bucket, maximum_age_buckets=2.0)
    assert not result.fresh
    assert result.reason == "stale_history_state"
    assert result.state_age_seconds is not None and result.state_age_seconds > result.maximum_state_age_seconds


def test_current_incomplete_bucket_panel_fails_closed() -> None:
    now = 10_000
    bucket = 60
    current_start = (now // bucket) * bucket
    times = tuple(current_start - 9 * bucket + i * bucket for i in range(10))
    panel = lf.standardize_levels(
        {
            "a": [float(i) for i in range(10)],
            "b": [1.2 * i + (0.1 if i % 2 else -0.1) for i in range(10)],
            "c1": [0.8 * i + (0.2 if i % 3 else -0.2) for i in range(10)],
            "c2": [1.1 * i + (0.15 if i % 4 else -0.15) for i in range(10)],
        },
        times,
    )
    assert panel is not None
    result = lf.assess_panel_freshness(panel, now=now, bucket_seconds=bucket, maximum_age_buckets=2.0)
    assert not result.fresh
    assert result.reason == "incomplete_or_future_bucket"


def test_research_driver_rechecks_freshness_before_current_book_signal() -> None:
    source = (ROOT / "scripts" / "v7_local_factor_research.py").read_text(encoding="utf-8")
    assert "completed_history_view" in source
    assert "pre_inference" in source
    assert "pre_signal_current_book" in source
    assert "signal_now = int(time.time())" in source
    assert source.index("pre_signal_current_book") < source.index("yes_a = books.get")
    for config_name in ("research_v7_local_factor.json", "research_v7_local_factor_60m.json"):
        cfg = json.loads((ROOT / "config" / config_name).read_text(encoding="utf-8"))
        history = cfg["history"]
        assert history["completed_buckets_only"] is True
        assert history["fail_closed_on_stale_state"] is True
        assert history["maximum_history_state_age_buckets"] == 2.0
        assert cfg["promotion_gate"]["require_fresh_completed_history_state"] is True


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} local-factor orientation/freshness tests")
