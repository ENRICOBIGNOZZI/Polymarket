#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "v7_local_factor_orientation"
SPEC = importlib.util.spec_from_file_location(NAME, ROOT / "scripts" / "v7_local_factor_orientation.py")
assert SPEC is not None and SPEC.loader is not None
orientation = importlib.util.module_from_spec(SPEC)
sys.modules[NAME] = orientation
SPEC.loader.exec_module(orientation)

CORE_NAME = "v7_local_factor_core"
CORE_SPEC = importlib.util.spec_from_file_location(CORE_NAME, ROOT / "scripts" / "v7_local_factor_core.py")
assert CORE_SPEC is not None and CORE_SPEC.loader is not None
lf = importlib.util.module_from_spec(CORE_SPEC)
sys.modules[CORE_NAME] = lf
CORE_SPEC.loader.exec_module(lf)


def test_orientation_invariant_pc1_survives_control_sign_flip() -> None:
    n = 100
    controls = {
        "c1": [math.sin(i / 10.0) for i in range(n)],
        "c2": [0.8 * math.sin(i / 10.0) + 0.2 * math.cos(i / 7.0) for i in range(n)],
        "c3": [0.6 * math.sin(i / 10.0) - 0.1 * math.cos(i / 11.0) for i in range(n)],
    }
    first = orientation.orientation_invariant_pc1(controls)
    flipped = dict(controls)
    flipped["c2"] = [-value for value in controls["c2"]]
    second = orientation.orientation_invariant_pc1(flipped)
    assert first is not None and second is not None
    correlation = abs(sum(a * b for a, b in zip(first, second))) / math.sqrt(
        sum(a * a for a in first) * sum(b * b for b in second)
    )
    assert correlation > 0.99


def test_fit_pair_uses_orientation_invariant_factor_and_excludes_targets() -> None:
    times = tuple(range(40))
    controls = {
        "c1": [0.2 * i + math.sin(i / 5.0) for i in times],
        "c2": [0.15 * i + math.sin(i / 5.0 + 0.2) for i in times],
        "c3": [0.12 * i + math.sin(i / 5.0 - 0.3) for i in times],
    }
    levels = {
        **controls,
        "a": [0.5 * controls["c1"][i] + 0.1 * math.sin(i / 2.0) for i in times],
        "b": [-0.4 * controls["c1"][i] + 0.1 * math.cos(i / 3.0) for i in times],
    }
    panel = lf.standardize_levels(levels, times)
    assert panel is not None
    fit = lf.fit_pair(panel, "a", "b")
    assert fit is not None
    assert fit.controls == ("c1", "c2", "c3")
    assert len(fit.control_factor_loadings) == 3


def test_completed_history_view_excludes_current_bucket() -> None:
    now = 10_000
    bucket = 60
    current_start = (now // bucket) * bucket
    histories = {
        "a": {
            current_start - 2 * bucket: 0.4,
            current_start - bucket: 0.5,
            current_start: 0.6,
        }
    }
    completed = lf.completed_history_view(histories, now=now, bucket_seconds=bucket)
    assert current_start not in completed["a"]
    assert current_start - bucket in completed["a"]


def test_fresh_completed_regular_panel_passes() -> None:
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


def test_stale_completed_panel_fails_closed() -> None:
    now = 10_000
    bucket = 60
    current_start = (now // bucket) * bucket
    times = tuple(current_start - 20 * bucket + i * bucket for i in range(10))
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


def test_research_driver_preserves_history_freshness_and_adds_current_book_guard() -> None:
    base = (ROOT / "scripts" / "v7_local_factor_research_base.py").read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts" / "v7_local_factor_research.py").read_text(encoding="utf-8")
    assert "completed_history_view" in base
    assert "pre_inference" in base
    assert "pre_signal_current_book" in base
    assert "signal_now = int(time.time())" in base
    assert base.index("pre_signal_current_book") < base.index("yes_a = books.get")
    assert "validate_coherent_books" in wrapper
    assert "required_markets = (fit.market_a, fit.market_b, *fit.controls)" in wrapper
    assert "current_residual_reconstructed_from_frozen_controls" in wrapper
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
