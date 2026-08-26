#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from v7_learned_execution_model import (
    ExecutionModelError,
    JointExample,
    Kernel,
    analyze,
    build_joint,
    build_orders,
    predict_distribution,
    product_marginal_probability,
    split,
)

SHA = "a" * 40


def event(kind: str, oid: str = "", **extra):
    row = dict(
        event_type=kind,
        strategy="GRAPH_RV",
        model_sha=SHA,
        paper_only=True,
        authenticated_execution=False,
        order_id=oid or None,
        candidate_id=None,
        opportunity_id=None,
        bundle_id=None,
        fill_id=None,
        leg_id=None,
        token_id=None,
        side=None,
        book_snapshot_id=None,
        recorded_ts_ms=10_000,
        receive_ts_ms=None,
        decision_ts_ms=None,
        exchange_ts_ms=None,
        bid=None,
        ask=None,
        bid_depth=None,
        ask_depth=None,
        queue_ahead=None,
        intended_size=None,
        intended_action=None,
        predicted_alpha=None,
        expected_ev=None,
        timeout_ms=None,
        filled_size=None,
        fill_price=None,
        fee=None,
        fee_source=None,
        executable_liquidation_value=None,
        complete=None,
        order_state=None,
        markouts={},
    )
    row.update(extra)
    return SimpleNamespace(**row)


def submit(oid: str, ts: int, queue: float | None, *, bundle=None, candidate=None, leg=None, imbalance=0.0):
    total = 200.0
    bd = total * (1.0 + imbalance) / 2.0
    return event(
        "ORDER_SUBMITTED",
        oid,
        bundle_id=bundle,
        candidate_id=candidate,
        leg_id=leg,
        book_snapshot_id=f"book-{oid}",
        recorded_ts_ms=ts + 3,
        receive_ts_ms=ts,
        decision_ts_ms=ts + 2,
        exchange_ts_ms=ts - 1,
        bid=0.49,
        ask=0.51,
        bid_depth=bd,
        ask_depth=total - bd,
        queue_ahead=queue,
        intended_size=10.0,
        intended_action="JOIN_MAKER",
        predicted_alpha=0.01,
        expected_ev=0.002,
        timeout_ms=60_000,
    )


def timeout(oid: str, ts: int):
    return event("ORDER_STATE", oid, recorded_ts_ms=ts, order_state="TIMEOUT")


def fill(oid: str, ts: int, qty=10.0):
    return event(
        "FILL",
        oid,
        fill_id=f"fill-{oid}",
        recorded_ts_ms=ts,
        exchange_ts_ms=ts - 2,
        receive_ts_ms=ts - 1,
        token_id=f"token-{oid}",
        side="BUY",
        fill_price=0.50,
        filled_size=qty,
        fee=0.001,
        fee_source="test_authoritative",
        complete=qty >= 10.0,
    )


def markout(oid: str, ts: int, value: float, horizon: str = "60s", *, fill_id: str | None = None):
    return event(
        "MARKOUT",
        oid,
        fill_id=fill_id or f"fill-{oid}",
        recorded_ts_ms=ts,
        exchange_ts_ms=ts - 2,
        receive_ts_ms=ts - 1,
        book_snapshot_id=f"mark-book-{oid}-{horizon}",
        executable_liquidation_value=0.49,
        markouts={horizon: value},
    )


def test_fail_closed_safety_and_sha():
    bad = submit("x", 1_000, 5.0)
    bad.model_sha = "b" * 40
    try:
        build_orders([bad], SHA)
    except ExecutionModelError as exc:
        assert "mixed_sha" in str(exc)
    else:
        raise AssertionError("mixed SHA accepted")

    auth = submit("y", 1_000, 5.0)
    auth.authenticated_execution = True
    try:
        build_orders([auth], SHA)
    except ExecutionModelError as exc:
        assert "authenticated_execution_forbidden" in str(exc)
    else:
        raise AssertionError("authenticated evidence accepted")

    missing = submit("z", 1_000, 5.0)
    del missing.paper_only
    try:
        build_orders([missing], SHA)
    except ExecutionModelError as exc:
        assert "not_paper_only" in str(exc)
    else:
        raise AssertionError("missing PAPER provenance accepted")


def test_missing_book_or_queue_is_excluded_not_zero_imputed():
    bad = submit("bad", 1_000, None)
    good = submit("good", 2_000, 5.0)
    rows = [bad, timeout("bad", 1_100), good, timeout("good", 2_100)]
    orders, stats = build_orders(rows, SHA)
    assert [row.order_id for row in orders] == ["good"]
    assert stats["excluded_missing_or_invalid_features"] == 1


def test_unresolved_order_does_not_become_negative_label():
    rows = [submit("closed", 1_000, 10.0), timeout("closed", 2_000), submit("open", 3_000, 10.0)]
    orders, stats = build_orders(rows, SHA)
    assert [row.order_id for row in orders] == ["closed"]
    assert orders[0].fill == 0 and stats["unresolved_orders"] == 1


def test_split_requires_label_maturity_and_embargo():
    rows = []
    for i in range(20):
        label_ts = i * 1_000 + (10_000 if i in {12, 13, 14} else 100)
        rows.append(SimpleNamespace(ts_ms=i * 1_000, label_ts_ms=label_ts, order_id=str(i)))
    train, test = split(rows, 8, 4, 0.25, 2_000)
    assert test[0].ts_ms == 15_000
    assert all(row.label_ts_ms <= 13_000 for row in train)
    assert {row.order_id for row in train}.isdisjoint({"12", "13", "14"})


def test_kernel_fill_model_learns_queue_effect_oos():
    events = []
    for i in range(240):
        queue = float(i % 100)
        oid, ts = f"o{i}", 1_000 + i * 100
        events.append(submit(oid, ts, queue))
        events.append(fill(oid, ts + 20) if queue < 40 else timeout(oid, ts + 20))
    report = analyze(
        events,
        SHA,
        min_order_train=120,
        min_order_test=40,
        min_markout_train=999,
        min_markout_test=999,
        min_joint_train=999,
        min_joint_test=999,
        bandwidth=0.5,
    )
    assert report["fill_model"]["state"] == "OOS_SCORED"
    assert report["fill_model"]["oos_brier"] < report["fill_model"]["baseline_brier"] * 0.35


def test_markout_model_reads_append_only_fill_linked_events():
    events = []
    for i in range(320):
        imbalance = -0.9 + 1.8 * (i % 80) / 79.0
        oid, ts = f"m{i}", 1_000 + i * 1_000
        events.append(submit(oid, ts, 10.0 if i % 4 != 3 else 90.0, imbalance=imbalance))
        if i % 4 != 3:
            events.append(fill(oid, ts + 20))
            events.append(markout(oid, ts + 80, 0.02 * imbalance - 0.003))
        else:
            events.append(timeout(oid, ts + 20))
    report = analyze(
        events,
        SHA,
        min_order_train=160,
        min_order_test=60,
        min_markout_train=80,
        min_markout_test=20,
        min_joint_train=999,
        min_joint_test=999,
        bandwidth=0.5,
    )
    mark = report["markout_models"]["60s"]
    assert mark["state"] == "OOS_SCORED"
    assert mark["oos_rmse"] < mark["baseline_rmse"]
    assert report["causal_contract"]["markout_source"] == "append_only_MARKOUT_by_fill_id"


def test_orphan_or_noncausal_markout_fails_closed():
    rows = [submit("x", 1_000, 5.0), fill("x", 1_020), markout("x", 1_080, 0.1, fill_id="missing")]
    try:
        build_orders(rows, SHA)
    except ExecutionModelError as exc:
        assert "orphan_fill_id" in str(exc)
    else:
        raise AssertionError("orphan markout accepted")

    rows = [submit("y", 2_000, 5.0), fill("y", 2_020), markout("y", 2_010, 0.1)]
    try:
        build_orders(rows, SHA)
    except ExecutionModelError as exc:
        assert "noncausal_clock" in str(exc)
    else:
        raise AssertionError("noncausal markout accepted")


def test_joint_distribution_is_direct_not_marginal_product():
    rows = [
        JointExample(
            str(i),
            2,
            i,
            i,
            (0.0,) * 23,
            "COMPLETE|COMPLETE" if i % 2 == 0 else "NO_FILL|NO_FILL",
        )
        for i in range(200)
    ]
    kernel = Kernel.fit([row.x for row in rows], 1.0)
    labels = [row.state for row in rows]
    dist = predict_distribution(kernel, rows[0].x, labels)
    direct = -0.5 * (math.log(dist["COMPLETE|COMPLETE"]) + math.log(dist["NO_FILL|NO_FILL"]))
    marginal = -0.5 * (
        math.log(product_marginal_probability(labels, "COMPLETE|COMPLETE"))
        + math.log(product_marginal_probability(labels, "NO_FILL|NO_FILL"))
    )
    assert direct + 0.5 < marginal


def test_joint_examples_use_bundle_id_keep_partial_and_require_leg_ids():
    rows = [
        submit("a", 1_000, 5.0, bundle="bundle-c", candidate="same", leg="1"),
        fill("a", 1_100, 10.0),
        submit("b", 1_001, 5.0, bundle="bundle-c", candidate="same", leg="2"),
        fill("b", 1_101, 4.0),
        timeout("b", 1_200),
        submit("x", 2_000, 5.0, candidate="same", leg="1"),
        timeout("x", 2_100),
        submit("y", 2_001, 5.0, candidate="same", leg="2"),
        timeout("y", 2_101),
        submit("u", 3_000, 5.0, bundle="bundle-bad"),
        timeout("u", 3_100),
        submit("v", 3_001, 5.0, bundle="bundle-bad"),
        timeout("v", 3_101),
    ]
    orders, _ = build_orders(rows, SHA)
    joint, stats = build_joint(orders)
    assert len(joint) == 1 and joint[0].state == "COMPLETE|PARTIAL"
    assert joint[0].group_id == "bundle-c"
    assert stats["skipped_missing_bundle_id"] == 2
    assert stats["skipped_missing_or_duplicate_leg_id"] == 1


def test_report_is_read_only_never_promotes_and_declares_missingness_contract():
    events = []
    for i in range(120):
        queue = float(i % 60)
        oid, ts = f"r{i}", 1_000 + i * 100
        events += [submit(oid, ts, queue), fill(oid, ts + 20) if queue < 30 else timeout(oid, ts + 20)]
    report = analyze(
        events,
        SHA,
        min_order_train=60,
        min_order_test=20,
        min_markout_train=999,
        min_markout_test=999,
        min_joint_train=999,
        min_joint_test=999,
    )
    assert report["read_only"] is True and report["promotion_allowed"] is False
    assert report["decision"] == "MORE_EVIDENCE_REQUIRED"
    assert report["causal_contract"]["product_of_marginals_role"] == "benchmark_only"
    assert report["causal_contract"]["missing_executable_book_inputs"] == "exclude_not_zero_impute"
    assert report["causal_contract"]["joint_grouping"] == "bundle_id_with_unique_leg_id"


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for test in tests:
        test()
    print(f"ok {len(tests)} v7 learned execution tests")
