#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from v7_learned_execution_walkforward import analyze_walk_forward, validate_joint
from v7_learned_execution_model import JointExample

spec = importlib.util.spec_from_file_location("model_tests", ROOT / "tests" / "test_v7_learned_execution_model.py")
helpers = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(helpers)

SHA = helpers.SHA
sub, fill, timeout, mark = helpers.sub, helpers.fill, helpers.timeout, helpers.mark


def test_fill_walk_forward_has_positive_fold_stability_and_day_bootstrap():
    hour = 3_600_000
    events = []
    for i in range(240):
        q = float(i % 100)
        oid, ts = f"wf-{i}", 1_000 + i * hour
        events += [sub(oid, ts, q), fill(oid, ts + 20) if q < 40 else timeout(oid, ts + 20)]
    report = analyze_walk_forward(
        events,
        SHA,
        bandwidth=0.5,
        folds=4,
        min_order_train=80,
        min_order_test=20,
        min_markout_train=999,
        min_markout_test=999,
        min_joint_train=999,
        min_joint_test=999,
        bootstrap_samples=200,
    )
    wf = report["strategy_validation"]["GRAPH_RV"]["fill"]
    assert wf["scored_folds"] == 4
    assert wf["positive_brier_fold_fraction"] == 1.0
    assert wf["worst_brier_improvement"] > 0.0
    boot = wf["brier_day_block_bootstrap"]
    assert boot["state"] == "BOOTSTRAPPED" and boot["days"] >= 2
    assert boot["ci_lower"] > 0.0 and boot["p_nonpositive"] == 0.0
    assert report["validation_authority"] == "BLOCKED_WALK_FORWARD"
    assert report["single_terminal_holdout_role"] == "DIAGNOSTIC_ONLY"


def test_markout_walk_forward_uses_mature_labels_and_day_blocks():
    hour = 3_600_000
    events = []
    for i in range(240):
        imbalance = -0.9 + 1.8 * (i % 80) / 79.0
        oid, ts, fill_ts = f"mwf-{i}", 1_000 + i * hour, 1_020 + i * hour
        events += [
            sub(oid, ts, 10.0, imb=imbalance),
            fill(oid, fill_ts),
            mark(oid, fill_ts, 0.02 * imbalance - 0.003),
        ]
    report = analyze_walk_forward(
        events,
        SHA,
        bandwidth=0.5,
        folds=4,
        min_order_train=80,
        min_order_test=20,
        min_markout_train=60,
        min_markout_test=15,
        min_joint_train=999,
        min_joint_test=999,
        bootstrap_samples=200,
    )
    wf = report["strategy_validation"]["GRAPH_RV"]["markouts"]["60s"]
    assert wf["scored_folds"] == 4 and wf["positive_mse_fold_fraction"] == 1.0
    boot = wf["mse_day_block_bootstrap"]
    assert boot["state"] == "BOOTSTRAPPED" and boot["ci_lower"] > 0.0


def test_joint_walk_forward_beats_product_marginal_benchmark_by_fold():
    hour = 3_600_000
    rows = []
    for i in range(160):
        x = float(i % 20) / 20.0
        state = "COMPLETE|COMPLETE" if x < 0.5 else "NO_FILL|NO_FILL"
        rows.append(
            JointExample(
                f"jwf-{i}",
                "GRAPH_RV",
                ("1", "2"),
                2,
                1_000 + i * 6 * hour,
                1_100 + i * 6 * hour,
                (x, x, x, x),
                state,
            )
        )
    wf = validate_joint(
        rows,
        bandwidth=0.4,
        folds=4,
        min_train=60,
        min_test=15,
        embargo_ms=0,
        bootstrap_samples=200,
        seed_key="joint-test",
    )
    assert wf["scored_folds"] == 4
    assert wf["positive_vs_marginal_fold_fraction"] == 1.0
    boot = wf["vs_product_marginals_day_block_bootstrap"]
    assert boot["state"] == "BOOTSTRAPPED" and boot["ci_lower"] > 0.0


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for test in tests:
        test()
    print(f"ok {len(tests)} v7 learned execution walk-forward tests")
