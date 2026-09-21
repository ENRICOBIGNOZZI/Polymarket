# HISTORICAL_WALK_FORWARD_V2 is an explicit causal-research CI gate.
import copy
import json
import math
import sys

from research.walk_forward_v2.core import (
    HORIZONS_MS,
    Ridge,
    quantity_for_notional_microdollars,
    asset_markout_predictors,
    asset_selection_diagnostics,
    live_parity_policy_diagnostics,
    common_signal_support_diagnostics,
    book_targets,
    build_dataset,
    folds,
    fit_full_repricing,
    economic_evaluation,
    executable_markout_target,
    replay_one,
    replay_policy,
    replay_policy_summary,
    summarize,
    settlement_predictors,
    valid_native,
    native_pair_l1,
    native_repricing_point,
    attach_native_repricing,
    attach_native_repricing_stream,
)
from research.walk_forward_v2.promotion import (
    assess_candidate,
    expanding_history_gate,
    main as promotion_main,
)


def record(market="m", decision_ns=1_789_921_800_000_000_001, epoch=7):
    return {
        "decision_id": market + str(decision_ns), "market_id": market, "token_id": "yes-" + market,
        "asset": "BTC", "horizon": "M5", "decision_ns": decision_ns,
        "information_end_ns": decision_ns + 2_000_000_000, "trigger_ns": decision_ns - 1,
        "signal_age_ns": 1, "tte_ns": 110_000_000_000, "direction": 1, "reason": "Accepted",
        "accepted": True, "signal_valid": True, "confirmed": True, "book_valid": True,
        "pretrigger": True, "bid": .49, "ask": .50, "quantity": 10., "tick": .01,
        "minimum": 1., "fee_rate": .01, "fee_exponent": 1., "epoch": epoch,
        "features": {"external.binance_return_100ms_bp": 1.0}, "label": 1,
        "label_information_ns": decision_ns + 10_000_000_000, "label_provenance": "ARCHIVED_CAUSAL_RECEIVE_TIME",
    }


def book(row, time_ns, epoch=7, ask=.51):
    return {
        "market_id": row["market_id"], "token_id": row["token_id"], "time_ns": time_ns,
        "session": "s", "epoch": epoch, "sequence": time_ns, "bid": ask - .01,
        "ask": ask, "quantity": 4., "tick": .01, "features": {},
    }


def test_target_is_first_actual_book_after_integer_ns_boundary():
    row = record()
    decision = row["decision_ns"]
    before = book(row, decision + 24_999_999, ask=.40)
    first = book(row, decision + 25_000_000, ask=.51)
    later = book(row, decision + 30_000_000, ask=.70)
    book_targets([row], [before, later, first])
    target = row["targets"]["25"]
    assert target["state"] == "OBSERVED"
    assert target["observed_time_ns"] == first["time_ns"]
    assert math.isclose(target["ask_change"], .01, abs_tol=1e-12)


def test_target_never_forward_fills_or_crosses_epoch():
    row = record()
    book_targets([row], [book(row, row["decision_ns"] + 25_000_000, epoch=8)])
    assert row["targets"]["25"]["state"] == "UNAVAILABLE_EPOCH_CHANGED"
    row = record()
    book_targets([row], [book(row, row["decision_ns"] + 25_000_000 + 50_000_001)])
    assert row["targets"]["25"]["state"] == "UNAVAILABLE_TOLERANCE"


def test_expanding_folds_keep_whole_markets_and_training_labels_before_cutoff():
    rows = []
    for index in range(8):
        item = record(market="m" + str(index), decision_ns=1_789_921_800_000_000_000 + index * 10_000_000_000)
        item["label_information_ns"] = item["decision_ns"] + 1
        rows.append(item)
    found, receipt = folds(rows, desired_folds=2, embargo_ns=1)
    assert receipt["state"] == "READY"
    for fold in found:
        assert not set(fold["train_markets"]) & set(fold["test_markets"])
        assert all(row["label_information_ns"] < fold["cutoff_ns"] for row in fold["train_settlement"])
        assert all(row["information_end_ns"] < fold["cutoff_ns"] for row in fold["train_repricing"])


def test_preprocessor_is_fit_only_on_training_rows():
    train = [record("a"), record("b")]
    train[0]["features"]["x"] = 0.
    train[1]["features"]["x"] = 2.
    model = Ridge(["x"], ridge=1.).fit(train, lambda row: row["label"])
    center = model.center["x"]
    test = record("test")
    test["features"]["x"] = 9_999_999.
    model.predict(test)
    assert model.center["x"] == center == 1.


def test_missing_arrival_is_censored_not_zero_pnl():
    row = record()
    row["timeline"] = []
    outcome = replay_one(row, .9, .1, latency_ms=50)
    assert outcome["status"].startswith("UNAVAILABLE")
    assert outcome["pnl"] is None
    assert outcome["filled"] == 0.


def test_replay_uses_visible_depth_and_is_deterministic():
    row = record()
    row["timeline"] = [book(row, row["decision_ns"] + 100_000_000, ask=.50)]
    first = replay_one(copy.deepcopy(row), .9, .1, latency_ms=100)
    second = replay_one(copy.deepcopy(row), .9, .1, latency_ms=100)
    assert first == second
    assert first["status"] == "PARTIAL_FILL"
    assert first["filled"] == 4.



def test_runtime_native_schema_does_not_require_duplicated_authority_flags():
    row = {
        "schema": "polymarket_v7_native_observation_v1",
        "paper_only": True,
        "execution_authority": False,
    }
    assert valid_native(row)
    row["authenticated_execution"] = False
    row["real_order_submission"] = False
    assert valid_native(row)
    row["real_order_submission"] = True
    assert not valid_native(row)


def test_build_dataset_discovers_real_run_root_layout(tmp_path):
    (tmp_path / "research" / "hft_permanent" / "compact").mkdir(parents=True)
    (tmp_path / "research" / "hft_permanent" / "compact_closed").mkdir()
    (tmp_path / "research" / "hft_permanent" / "windows").mkdir()
    (tmp_path / "research" / "public_settlements").mkdir()
    data = build_dataset(tmp_path)
    assert data["hft_root"] == str(tmp_path / "research" / "hft_permanent")
    assert str(tmp_path / "research" / "public_settlements") in data["settlement_roots"]
    assert data["input_state"] == "NO_ADMISSIBLE_NATIVE_DECISIONS"



def test_combined_policy_uses_settlement_value_but_requires_positive_repricing():
    row = record()
    row["timeline"] = [book(row, row["decision_ns"] + 100_000_000, ask=.50)]
    settlement_only = replay_one(copy.deepcopy(row), .90, None, latency_ms=100)
    assert settlement_only["filled"] > 0
    combined_negative = replay_one(copy.deepcopy(row), .90, -.01, latency_ms=100,
                                   valuation_mode="SETTLEMENT_WITH_REPRICING_CONFIRMATION")
    assert combined_negative["filled"] == 0
    assert not combined_negative["funnel"]["predicted_repricing_positive"]
    combined_positive = replay_one(copy.deepcopy(row), .90, .01, latency_ms=100,
                                   valuation_mode="SETTLEMENT_WITH_REPRICING_CONFIRMATION")
    assert combined_positive["filled"] > 0

# End HISTORICAL_WALK_FORWARD_V2 regression gate.


def test_ridge_predict_many_matches_scalar_predictions():
    train = [record("a"), record("b"), record("c")]
    for index, row in enumerate(train):
        row["features"]["x"] = float(index)
        row["label"] = index % 2
    model = Ridge(["x"], ridge=1.0).fit(train, lambda row: row["label"])
    scalar = [model.predict(row) for row in train]
    vector = model.predict_many(train)
    assert len(vector) == len(scalar)
    assert all(math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
               for a, b in zip(scalar, vector))


def test_book_targets_cache_sorted_timeline_times_for_replay():
    row = record()
    decision = row["decision_ns"]
    books = [
        book(row, decision + 100_000_000, ask=.51),
        book(row, decision + 25_000_000, ask=.50),
        book(row, decision + 250_000_000, ask=.52),
    ]
    book_targets([row], books)
    assert row["timeline_times"] == sorted(row["timeline_times"])
    assert row["timeline_times"] == [entry["time_ns"] for entry in row["timeline"]]


def test_replay_policy_enforces_one_entry_per_market_and_reserves_capital():
    first = record("same", decision_ns=1_789_921_800_000_000_001)
    second = record("same", decision_ns=first["decision_ns"] + 1_000_000_000)
    for row in (first, second):
        row["timeline"] = [book(row, row["decision_ns"] + 100_000_000, ask=.50)]
        row["timeline_times"] = [row["timeline"][0]["time_ns"]]
    evaluations = []
    for index, row in enumerate((first, second)):
        evaluations.append({
            "decision_ns": row["decision_ns"], "decision_id": row["decision_id"],
            "row": row, "settlement_predictions": {"pm": .90},
            "repricing_predictions": {"250": .01},
        })
    outcomes = replay_policy(
        evaluations, lambda event: (event["settlement_predictions"]["pm"],
                                    event["repricing_predictions"]["250"]),
        latency_ms=100, valuation_mode="SETTLEMENT_WITH_REPRICING_CONFIRMATION")
    assert outcomes[0]["funnel"]["simulated_order"] is True
    assert outcomes[1]["status"] == "FILTERED_MARKET_ALREADY_TRADED"
    assert outcomes[1]["funnel"]["simulated_order"] is False


def test_replay_policy_stops_new_orders_after_capital_ceiling():
    evaluations = []
    base = 1_789_921_800_000_000_001
    for index in range(3):
        row = record("m" + str(index), decision_ns=base + index * 1_000_000_000)
        row["timeline"] = [book(row, row["decision_ns"] + 100_000_000, ask=.50)]
        row["timeline_times"] = [row["timeline"][0]["time_ns"]]
        evaluations.append({
            "decision_ns": row["decision_ns"], "decision_id": row["decision_id"],
            "row": row, "settlement_predictions": {"pm": .90},
            "repricing_predictions": {"250": .01},
        })
    outcomes = replay_policy(
        evaluations, lambda event: (event["settlement_predictions"]["pm"],
                                    event["repricing_predictions"]["250"]),
        latency_ms=100, valuation_mode="SETTLEMENT_WITH_REPRICING_CONFIRMATION",
        capital_budget=7.5)
    assert sum(row["funnel"]["simulated_order"] for row in outcomes) == 2
    assert outcomes[2]["funnel"]["capital_admitted"] is False


def test_pm_baseline_remains_available_before_any_settlement_labels():
    test = [record("u0"), record("u1")]
    for row in test:
        row["label"] = None
        row["label_information_ns"] = None
    predictions, meta = settlement_predictors([], test)
    assert meta["state"] == "PM_BASELINE_ONLY_INSUFFICIENT_SETTLEMENT_TRAINING"
    assert predictions["pm"] == [(row["bid"] + row["ask"]) / 2 for row in test]
    assert predictions["logistic_offset"] == [None, None]
    assert predictions["boosted_offset"] == [None, None]



def test_native_kind6_repricing_label_populates_oos_target_and_arrival():
    origin = record("native")
    origin.update({
        "server_id": "s1", "run_id": "r1", "capture_id": "c1",
        "signal_version": 17, "repricing_origin_signal_version": 17,
        "decision_monotonic_ns": 1_000_000_000,
    })
    label = {
        "schema": "polymarket_v7_native_observation_v1",
        "paper_only": True,
        "execution_authority": False,
        "kind": 6,
        "server_id": "s1", "run_id": "r1", "capture_id": "c1",
        "market_id": "native", "token_id": "yes-native",
        "repricing_origin_signal_version": 17,
        "repricing_horizon_ms": 100,
        "decision_monotonic_ns": 1_000_000_000,
        "decision_wall_ns": origin["decision_ns"],
        "observed_monotonic_ns": 1_100_000_001,
        "close_monotonic_ns": 200_000_000_000,
        "close_wall_ns": origin["decision_ns"] + 199_000_000_000,
        "connection_epoch": 7,
        "repricing_pair_valid": True,
        "book_valid": True,
        "bid_e4": 5000, "ask_e4": 5100, "tick_e4": 100,
        "ask_quantity": 4_000_000,
        "yes_bid_e4": 5000, "yes_ask_e4": 5100,
        "no_bid_e4": 4900, "no_ask_e4": 5000,
        "yes_bid_quantity": 3_000_000, "yes_ask_quantity": 4_000_000,
        "no_bid_quantity": 5_000_000, "no_ask_quantity": 6_000_000,
        "yes_bid_e4": 5000, "yes_ask_e4": 5100,
        "no_bid_e4": 4900, "no_ask_e4": 5000,
        "yes_bid_quantity": 3_000_000, "yes_ask_quantity": 4_000_000,
        "no_bid_quantity": 5_000_000, "no_ask_quantity": 6_000_000,
    }
    parsed = native_repricing_point(label)
    assert parsed is not None
    key, horizon, point = parsed
    proof = attach_native_repricing([origin], {(key, horizon): point})
    assert proof["matched_target_rows"] == 1
    assert origin["targets"]["100"]["state"] == "OBSERVED"
    assert math.isclose(origin["targets"]["100"]["mid_change"], .010, abs_tol=1e-12)
    arrival = origin["arrivals"]["100"]
    assert math.isclose(arrival["ask"], .51, abs_tol=1e-12)
    assert arrival["quantity"] == 4.0
    assert arrival["pair"]["state"] == "BILATERAL_EXECUTABLE_READY"
    assert arrival["pair"]["yes"]["ask_quantity"] == 4.0
    assert arrival["pair"]["no"]["ask_quantity"] == 6.0
    assert origin["targets"]["100"]["pair"] == arrival["pair"]
    assert arrival["pair"]["NO"]["ask_quantity"] == 6.0
    assert origin["targets"]["100"]["pair"]["YES"]["bid_quantity"] == 3.0


def test_native_kind6_wrong_identity_never_joins_origin():
    origin = record("native2")
    origin.update({
        "server_id": "s1", "run_id": "r1", "capture_id": "c1",
        "signal_version": 3, "repricing_origin_signal_version": 3,
        "decision_monotonic_ns": 1_000_000_000,
    })
    label = {
        "schema": "polymarket_v7_native_observation_v1",
        "paper_only": True, "execution_authority": False, "kind": 6,
        "server_id": "s1", "run_id": "r1", "capture_id": "other",
        "market_id": "native2", "token_id": "yes-native2",
        "repricing_origin_signal_version": 3, "repricing_horizon_ms": 250,
        "decision_monotonic_ns": 1_000_000_000,
        "decision_wall_ns": origin["decision_ns"],
        "observed_monotonic_ns": 1_250_000_001,
        "close_monotonic_ns": 200_000_000_000,
        "close_wall_ns": origin["decision_ns"] + 199_000_000_000,
        "connection_epoch": 7, "repricing_pair_valid": True, "book_valid": True,
        "bid_e4": 5100, "ask_e4": 5200, "tick_e4": 100, "ask_quantity": 5_000_000,
    }
    key, horizon, point = native_repricing_point(label)
    proof = attach_native_repricing([origin], {(key, horizon): point})
    assert proof["matched_target_rows"] == 0
    assert origin["targets"]["250"]["state"] == "UNAVAILABLE_NO_NATIVE_REPRICING_LABEL"



def test_full_window_repricing_fit_uses_all_observed_history_without_promotion():
    rows = []
    base = 1_789_921_800_000_000_001
    for index in range(10):
        row = record("full" + str(index), decision_ns=base + index * 1_000_000_000)
        row["features"]["x"] = float(index)
        for horizon in HORIZONS_MS:
            row.setdefault("targets", {})[str(horizon)] = {
                "state": "OBSERVED",
                "source": "NATIVE_REPRICING_KIND6_ASOF_HORIZON",
                "observed_time_ns": row["decision_ns"] + horizon * 1_000_000,
                "mid_change": .001 * (index + 1),
                "arrival_bid": .51 + .001 * index,
            }
        rows.append(row)
    artifact = fit_full_repricing(rows)
    assert artifact["automatic_promotion"] is False
    for horizon in HORIZONS_MS:
        model = artifact["models"][str(horizon)]
        markout = artifact["executable_markout_models"][str(horizon)]
        assert model["state"] == "READY"
        assert markout["state"] == "READY"
        assert model["rows"] == markout["rows"] == 10
        assert model["unique_markets"] == markout["unique_markets"] == 10
        assert model["label_sources"] == {"NATIVE_REPRICING_KIND6_ASOF_HORIZON": 10}
        assert markout["target"] == "future_executable_bid_minus_decision_ask_minus_entry_and_exit_taker_fees"
        assert model["training_start_ns"] == rows[0]["decision_ns"]
        assert model["training_end_ns"] == rows[-1]["decision_ns"]



def test_native_kind6_streaming_attaches_without_materializing_label_corpus(tmp_path):
    origin = record("stream")
    origin.update({
        "server_id": "s1", "run_id": "r1", "capture_id": "c1",
        "signal_version": 5, "repricing_origin_signal_version": 5,
        "decision_monotonic_ns": 2_000_000_000,
    })
    label = {
        "schema": "polymarket_v7_native_observation_v1",
        "paper_only": True, "execution_authority": False, "kind": 6,
        "server_id": "s1", "run_id": "r1", "capture_id": "c1",
        "market_id": "stream", "token_id": "yes-stream",
        "repricing_origin_signal_version": 5, "repricing_horizon_ms": 250,
        "decision_monotonic_ns": 2_000_000_000,
        "decision_wall_ns": origin["decision_ns"],
        "observed_monotonic_ns": 2_250_000_001,
        "close_monotonic_ns": 200_000_000_000,
        "close_wall_ns": origin["decision_ns"] + 198_000_000_000,
        "connection_epoch": 7, "repricing_pair_valid": True, "book_valid": True,
        "bid_e4": 5200, "ask_e4": 5300, "tick_e4": 100,
        "ask_quantity": 6_000_000,
    }
    path = tmp_path / "native.jsonl"
    import json
    path.write_text(json.dumps(label) + "\n")
    proof = attach_native_repricing_stream([origin], [path])
    assert proof["native_label_rows"] == 1
    assert proof["matched_target_rows"] == 1
    assert proof["short_horizon_observed_pairs"] == 1
    assert origin["targets"]["250"]["state"] == "OBSERVED"
    assert origin["arrivals"]["250"]["quantity"] == 6.0



def test_executable_markout_target_uses_future_bid_current_ask_and_fee():
    row = record("econ")
    row["fee_rate"] = 0.0
    row["targets"] = {
        "500": {
            "state": "OBSERVED",
            "arrival_bid": .53,
            "observed_time_ns": row["decision_ns"] + 500_000_000,
        }
    }
    assert math.isclose(executable_markout_target(row, "500"), .03, abs_tol=1e-12)


def test_executable_markout_replay_marks_matching_horizon():
    row = record("markout")
    row["fee_rate"] = 0.0
    row["arrivals"] = {
        "100": {"time_ns": row["decision_ns"] + 100_000_000,
                "bid": .49, "ask": .50, "quantity": 5.0, "epoch": 7}
    }
    row["targets"] = {
        "500": {"state": "OBSERVED", "arrival_bid": .55},
        "250": {"state": "OBSERVED", "arrival_bid": .45},
    }
    outcome = replay_one(
        row, .03, None, latency_ms=100,
        valuation_mode="EXECUTABLE_MARKOUT", markout_horizon_ms=500,
        edge_threshold=.005, execution_reserve=.005)
    assert outcome["filled"] == 5.0
    assert outcome["markout"] > 0
    assert math.isclose(outcome["markout_before_fee"], .25, abs_tol=1e-12)


def test_horizon_latency_matrix_never_scores_exit_before_arrival():
    rows = []
    base = 1_789_921_800_000_000_001
    for index in range(12):
        row = record("hl" + str(index), decision_ns=base + index * 10_000_000_000)
        row["label"] = None
        row["label_information_ns"] = None
        row["fee_rate"] = 0.0
        row["features"]["x"] = float(index)
        row["arrivals"] = {}
        row["targets"] = {}
        for horizon in HORIZONS_MS:
            key = str(horizon)
            row["targets"][key] = {
                "state": "OBSERVED",
                "arrival_bid": .55,
                "mid_change": .03,
                "observed_time_ns": row["decision_ns"] + horizon * 1_000_000,
            }
            if horizon in (25, 50, 100, 250, 500):
                row["arrivals"][key] = {
                    "time_ns": row["decision_ns"] + horizon * 1_000_000,
                    "bid": .49, "ask": .50, "quantity": 5.0, "epoch": 7,
                }
        rows.append(row)
    evaluations = []
    for row in rows:
        evaluations.append({
            "fold": 1, "cutoff_ns": base, "decision_id": row["decision_id"],
            "market_id": row["market_id"], "asset": row["asset"],
            "horizon": row["horizon"], "decision_ns": row["decision_ns"],
            "row": row,
            "settlement_predictions": {"pm": .495, "logistic_offset": None, "boosted_offset": None},
            "repricing_predictions": {str(h): .03 for h in HORIZONS_MS},
            "markout_predictions": {str(h): .05 for h in HORIZONS_MS},
        })
    economics = economic_evaluation(evaluations)
    assert economics["horizon_latency"]["25"]["25"]["state"] == "LATENCY_NOT_BEFORE_MARKOUT_HORIZON"
    assert economics["horizon_latency"]["25"]["10"]["state"] == "READY"
    assert economics["horizon_latency"]["500"]["100"]["state"] == "READY"



def test_native_label_information_time_not_nominal_horizon_controls_training_cut():
    origin = record("late-label")
    origin.update({
        "server_id": "s1", "run_id": "r1", "capture_id": "c1",
        "signal_version": 9, "repricing_origin_signal_version": 9,
        "decision_monotonic_ns": 1_000_000_000,
    })
    # 100ms economic target, but producer cannot emit it until 3.5s after decision.
    label = {
        "schema": "polymarket_v7_native_observation_v1",
        "paper_only": True, "execution_authority": False, "kind": 6,
        "server_id": "s1", "run_id": "r1", "capture_id": "c1",
        "market_id": "late-label", "token_id": "yes-late-label",
        "repricing_origin_signal_version": 9, "repricing_horizon_ms": 100,
        "decision_monotonic_ns": 1_000_000_000,
        "decision_wall_ns": origin["decision_ns"],
        "observed_monotonic_ns": 4_500_000_000,
        "close_monotonic_ns": 200_000_000_000,
        "close_wall_ns": origin["decision_ns"] + 199_000_000_000,
        "connection_epoch": 7, "repricing_pair_valid": True, "book_valid": True,
        "bid_e4": 5000, "ask_e4": 5100, "tick_e4": 100,
        "ask_quantity": 4_000_000,
    }
    key, horizon, point = native_repricing_point(label)
    assert point["target_time_ns"] == origin["decision_ns"] + 100_000_000
    assert point["information_ns"] == origin["decision_ns"] + 3_500_000_000
    attach_native_repricing([origin], {(key, horizon): point})
    assert origin["information_end_ns"] == origin["decision_ns"] + 3_500_000_000



def test_executable_markout_charges_both_entry_and_exit_fee():
    row = record("roundtrip-fee")
    row["fee_rate"] = .01
    row["fee_exponent"] = 1.0
    row["targets"] = {
        "500": {
            "state": "OBSERVED",
            "arrival_bid": .55,
            "observed_time_ns": row["decision_ns"] + 500_000_000,
        }
    }
    gross = .55 - .50
    from research.walk_forward_v2.core import fee_per_share
    expected = gross - fee_per_share(row, .50) - fee_per_share(row, .55)
    assert math.isclose(executable_markout_target(row, "500"), expected, abs_tol=1e-12)

    row["arrivals"] = {
        "100": {"time_ns": row["decision_ns"] + 100_000_000,
                "bid": .49, "ask": .50, "quantity": 5.0, "epoch": 7}
    }
    outcome = replay_one(
        row, .04, None, latency_ms=100, valuation_mode="EXECUTABLE_MARKOUT",
        markout_horizon_ms=500, edge_threshold=0.0, execution_reserve=0.0)
    assert outcome["filled"] == 5.0
    assert outcome["markout_exit_fee"] > 0
    assert math.isclose(
        outcome["markout"],
        outcome["markout_before_fee"] - outcome["markout_entry_fee"] - outcome["markout_exit_fee"],
        abs_tol=1e-12)



def test_streaming_replay_summary_matches_materialized_outcomes_exactly():
    evaluations = []
    base = 1_789_921_800_000_000_001
    for index in range(12):
        row = record("speed" + str(index), decision_ns=base + index * 1_000_000_000)
        row["fee_rate"] = .01
        row["targets"] = {
            "500": {
                "state": "OBSERVED",
                "arrival_bid": .56 if index % 2 == 0 else .52,
                "observed_time_ns": row["decision_ns"] + 500_000_000,
            }
        }
        row["arrivals"] = {
            "100": {
                "time_ns": row["decision_ns"] + 100_000_000,
                "bid": .49, "ask": .50, "quantity": 5.0, "epoch": 7,
            }
        }
        evaluations.append({
            "decision_ns": row["decision_ns"],
            "decision_id": row["decision_id"],
            "row": row,
            "settlement_predictions": {"pm": .495},
            "repricing_predictions": {"500": .03},
            "markout_predictions": {"500": .03},
        })
    selector = lambda event: (event["markout_predictions"]["500"], None)
    ordered = sorted(evaluations, key=lambda value: (value["decision_ns"], value["decision_id"]))
    outcomes = replay_policy(
        ordered, selector, latency_ms=100, valuation_mode="EXECUTABLE_MARKOUT",
        markout_horizon_ms=500, assume_sorted=True)
    materialized = summarize(outcomes)
    streamed = replay_policy_summary(
        ordered, selector, latency_ms=100, valuation_mode="EXECUTABLE_MARKOUT",
        markout_horizon_ms=500, assume_sorted=True)
    assert streamed == materialized



def test_economic_evaluation_retains_only_fill_level_equity_events():
    rows = []
    base = 1_789_921_800_000_000_001
    for index in range(12):
        row = record("eq" + str(index), decision_ns=base + index * 1_000_000_000)
        row["label"] = None
        row["label_information_ns"] = None
        row["fee_rate"] = 0.0
        row["targets"] = {}
        row["arrivals"] = {}
        for horizon in HORIZONS_MS:
            key = str(horizon)
            row["targets"][key] = {
                "state": "OBSERVED",
                "arrival_bid": .56,
                "mid_change": .03,
                "observed_time_ns": row["decision_ns"] + horizon * 1_000_000,
            }
            if horizon in (25, 50, 100, 250, 500):
                row["arrivals"][key] = {
                    "time_ns": row["decision_ns"] + horizon * 1_000_000,
                    "bid": .49, "ask": .50, "quantity": 5.0, "epoch": 7,
                }
        rows.append(row)

    evaluations = []
    for row in rows:
        evaluations.append({
            "fold": 1, "cutoff_ns": base, "decision_id": row["decision_id"],
            "market_id": row["market_id"], "asset": row["asset"],
            "horizon": row["horizon"], "decision_ns": row["decision_ns"],
            "row": row,
            "settlement_predictions": {"pm": .495, "logistic_offset": None, "boosted_offset": None},
            "repricing_predictions": {str(h): .03 for h in HORIZONS_MS},
            "markout_predictions": {str(h): .05 for h in HORIZONS_MS},
        })

    economics = economic_evaluation(evaluations)
    for name in ("markout_500ms", "markout_1000ms", "markout_2000ms"):
        events = economics["models"][name]["equity_events"]
        assert events
        assert all(event["filled"] > 0 for event in events)
        assert all(event["markout"] is not None for event in events)
        assert [event["decision_ns"] for event in events] == sorted(
            event["decision_ns"] for event in events
        )
        assert len(events) <= economics["models"][name]["metrics"]["fills"]



def test_per_asset_markout_models_use_same_hyperparameters_but_separate_coefficients():
    train = []
    base = 1_789_921_800_000_000_001
    for asset, future_bid in (("BTC", .56), ("ETH", .44)):
        for index in range(10):
            row = record(f"{asset.lower()}{index}", decision_ns=base + len(train) * 1_000_000_000)
            row["asset"] = asset
            row["fee_rate"] = 0.0
            row["features"]["x"] = float(index)
            row["targets"] = {}
            for horizon in HORIZONS_MS:
                row["targets"][str(horizon)] = {
                    "state": "OBSERVED",
                    "arrival_bid": future_bid,
                    "mid_change": future_bid - .495,
                    "observed_time_ns": row["decision_ns"] + horizon * 1_000_000,
                }
            train.append(row)

    test = []
    for asset in ("BTC", "ETH"):
        row = record(f"test-{asset.lower()}", decision_ns=base + 100_000_000_000 + len(test))
        row["asset"] = asset
        row["fee_rate"] = 0.0
        row["features"]["x"] = 5.0
        test.append(row)

    predictions, meta = asset_markout_predictors(train, test)
    btc = predictions["1000"][0]
    eth = predictions["1000"][1]

    assert btc is not None and eth is not None
    assert btc > .03
    assert eth < -.03
    assert meta["1000"]["BTC"]["ridge"] == 8.0
    assert meta["1000"]["ETH"]["ridge"] == 8.0
    assert meta["1000"]["BTC"]["feature_names"] == meta["1000"]["ETH"]["feature_names"]
    assert meta["1000"]["BTC"]["target"] == meta["1000"]["ETH"]["target"]



def test_live_parity_diagnostics_expose_tte_entry_cap_and_full_depth_mismatches():
    row = record("parity")
    row["asset"] = "ETH"
    row["tte_ns"] = 100_000_000_000
    row["ask"] = .78
    row["bid"] = .77
    row["quantity"] = 3.0
    row["minimum"] = 1.0
    event = {
        "decision_ns": row["decision_ns"],
        "decision_id": row["decision_id"],
        "row": row,
        "asset_markout_predictions": {"500": .02, "1000": .02, "2000": .02},
    }
    diag = live_parity_policy_diagnostics([event])
    cell = diag["horizons"]["500"]["ETH"]
    assert cell["research_tte"] == 1
    assert cell["live_tte"] == 0
    assert cell["research_entry_cap"] == 0
    assert cell["live_entry_cap"] == 1
    assert cell["research_depth_gate"] == 1
    assert cell["live_full_depth_gate"] == 0


def test_asset_diagnostics_surface_fee_schedule_and_size_capacity():
    events = []
    for asset, rate, depth in (("BTC", .01, 12.0), ("ETH", .03, 4.0)):
        row = record("diag-" + asset.lower())
        row["asset"] = asset
        row["fee_rate"] = rate
        row["fee_exponent"] = 1.0
        row["quantity"] = depth
        row["minimum"] = 1.0
        row["targets"] = {
            "1000": {
                "state": "OBSERVED",
                "arrival_bid": .55,
                "observed_time_ns": row["decision_ns"] + 1_000_000_000,
            }
        }
        events.append({
            "decision_ns": row["decision_ns"],
            "decision_id": row["decision_id"],
            "row": row,
            "markout_predictions": {"1000": .02},
            "asset_markout_predictions": {"1000": .02},
        })
    diag = asset_selection_diagnostics(events, horizons=(1000,))
    btc = diag["horizons"]["1000"]["BTC"]
    eth = diag["horizons"]["1000"]["ETH"]
    assert btc["mean_entry_fee"] < eth["mean_entry_fee"]
    assert btc["fee_schedule_counts"] != eth["fee_schedule_counts"]
    assert btc["size_capacity"]["5.0"]["both"] == 1
    assert eth["size_capacity"]["5.0"]["both"] == 0
    assert diag["required_prediction"] == .01



def test_asset_diagnostics_expose_configured_paper_delay_components():
    row = record("delay")
    row["asset"] = "SOL"
    row["paper_venue_delay_ns"] = 250_000_000
    row["paper_assumed_transport_delay_ns"] = 250_000_000
    row["targets"] = {
        "1000": {
            "state": "OBSERVED",
            "arrival_bid": .55,
            "observed_time_ns": row["decision_ns"] + 1_000_000_000,
        }
    }
    event = {
        "decision_ns": row["decision_ns"],
        "decision_id": row["decision_id"],
        "row": row,
        "markout_predictions": {"1000": .02},
        "asset_markout_predictions": {"1000": .02},
    }
    diag = asset_selection_diagnostics([event], horizons=(1000,))
    cell = diag["horizons"]["1000"]["SOL"]
    assert cell["paper_venue_delay_ms_quantiles"]["0.5"] == 250.0
    assert cell["paper_assumed_transport_delay_ms_quantiles"]["0.5"] == 250.0
    assert cell["paper_configured_total_delay_ms_quantiles"]["0.5"] == 500.0
    assert cell["paper_configured_total_delay_ms_counts"] == {"500": 1}



def test_exact_live_policy_replay_geometry_differs_from_research_geometry():
    row = record("live-geometry")
    row["fee_rate"] = 0.0
    row["tte_ns"] = 110_000_000_000
    row["ask"] = .78
    row["bid"] = .77
    row["quantity"] = 5.0
    row["minimum"] = 5.0
    row["arrivals"] = {
        "25": {
            "time_ns": row["decision_ns"] + 25_000_000,
            "bid": .77, "ask": .78, "quantity": 5.0, "epoch": 7,
        }
    }
    row["targets"] = {
        "500": {
            "state": "OBSERVED",
            "arrival_bid": .82,
            "observed_time_ns": row["decision_ns"] + 500_000_000,
        }
    }
    research = replay_one(
        row, .03, None, latency_ms=25,
        valuation_mode="EXECUTABLE_MARKOUT", markout_horizon_ms=500,
        edge_threshold=.005, execution_reserve=.005,
        entry_cap=.75, shares=5.0,
    )
    live = replay_one(
        row, .03, None, latency_ms=25,
        valuation_mode="EXECUTABLE_MARKOUT", markout_horizon_ms=500,
        edge_threshold=.005, execution_reserve=.005,
        entry_cap=.80, shares=5.0,
        minimum_tte_ns=105_000_000_000,
        maximum_tte_ns=120_000_000_000,
        require_full_visible_depth=True,
    )
    assert research["funnel"]["price_cap"] is False
    assert live["funnel"]["price_cap"] is True
    assert live["funnel"]["tte_valid"] is True
    assert live["funnel"]["sufficient_depth"] is True
    assert live["funnel"]["simulated_order"] is True

    too_early = copy.deepcopy(row)
    too_early["tte_ns"] = 100_000_000_000
    live_early = replay_one(
        too_early, .03, None, latency_ms=25,
        valuation_mode="EXECUTABLE_MARKOUT", markout_horizon_ms=500,
        edge_threshold=.005, execution_reserve=.005,
        entry_cap=.80, shares=5.0,
        minimum_tte_ns=105_000_000_000,
        maximum_tte_ns=120_000_000_000,
        require_full_visible_depth=True,
    )
    assert live_early["funnel"]["tte_valid"] is False
    assert live_early["funnel"]["simulated_order"] is False



def test_common_signal_support_uses_same_shock_floor_and_age_cap_for_all_assets():
    events = []
    base = 1_789_921_800_000_000_001
    specs = [
        ("BTC", 1.5, 5.0, .02, .03),
        ("ETH", 1.5, 5.0, .02, .03),
        ("DOGE", 1.0, 5.0, .02, .03),
        ("XRP", 1.5, 60.0, .02, .03),
    ]
    for index, (asset, shock, age_ms, pooled, specific) in enumerate(specs):
        row = record("common-" + asset.lower(), decision_ns=base + index)
        row["asset"] = asset
        row["signal_age_ns"] = int(age_ms * 1_000_000)
        row["features"]["binance_return_100ms_bp"] = shock
        row["targets"] = {
            "1000": {
                "state": "OBSERVED",
                "arrival_bid": .55,
                "observed_time_ns": row["decision_ns"] + 1_000_000_000,
            }
        }
        events.append({
            "decision_ns": row["decision_ns"],
            "decision_id": row["decision_id"],
            "row": row,
            "markout_predictions": {"1000": pooled},
            "asset_markout_predictions": {"1000": specific},
        })

    diag = common_signal_support_diagnostics(
        events, horizons=(1000,), common_shock_floor_bp=1.13,
        age_caps_ms=(10, 100), required_prediction=.01)
    fast = diag["horizons"]["1000"]["10"]
    slow = diag["horizons"]["1000"]["100"]

    assert fast["BTC"]["rows"] == 1
    assert fast["ETH"]["rows"] == 1
    assert fast["DOGE"]["rows"] == 0
    assert fast["XRP"]["rows"] == 0
    assert slow["XRP"]["rows"] == 1
    assert fast["BTC"]["pooled_passes_required_prediction"] == 1
    assert fast["ETH"]["asset_passes_required_prediction"] == 1
    assert diag["common_shock_floor_bp"] == 1.13


def test_promotion_candidate_requires_support_positive_ci_and_latency_robustness():
    economics = {
        "live_policy_promotion_candidates": {
            "pooled_1000ms": {
                "family": "POOLED",
                "horizon_ms": 1000,
                "reference_latency_ms": 50,
                "metrics": {
                    "marked_fills": 60, "fills": 70, "fill_rate": .5,
                    "markout_pnl": 2.0, "markout_per_fill": .04,
                },
                "uncertainty": {
                    "markout_per_fill_interval": [.005, .08],
                    "markout": {"markets": 30},
                },
                "candidate_contract": {},
            }
        },
        "live_parity_horizon_latency": {
            "1000": {
                "25": {"state": "READY", "markout_pnl": 2.2, "marked_fills": 61},
                "50": {"state": "READY", "markout_pnl": 2.0, "marked_fills": 60},
            }
        },
    }
    result = assess_candidate(economics, "pooled_1000ms")
    assert result["qualified"] is True
    assert result["score"] == .005

    economics["live_policy_promotion_candidates"]["pooled_1000ms"]["uncertainty"]["markout_per_fill_interval"][0] = -.001
    failed = assess_candidate(economics, "pooled_1000ms")
    assert failed["qualified"] is False
    assert "MARKET_BLOCK_LOWER_BOUND_NEGATIVE" in failed["failures"]


def test_expanding_history_gate_rejects_shrinking_history():
    manifest = {"minimum_wall_ns": 1_789_921_800_000_000_000}
    artifact = {
        "training_window": {
            "mode": "EXPANDING_ALL_CAUSAL_HISTORY",
            "decision_rows": 100,
            "maximum_decision_ns": 200,
        }
    }
    previous = {
        "configured_minimum_wall_ns": 1_789_921_800_000_000_000,
        "training_window": {"decision_rows": 101, "maximum_decision_ns": 201},
    }
    ok, failures, _ = expanding_history_gate(manifest, artifact, previous)
    assert ok is False
    assert "TRAINING_ROWS_DECREASED" in failures
    assert "TRAINING_END_MOVED_BACKWARD" in failures


def test_nightly_promotion_writes_only_paper_research_registry(tmp_path, monkeypatch):
    report = tmp_path / "report"
    report.mkdir()
    safety = {
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
    }
    economics = {
        **safety,
        "live_policy_promotion_candidates": {
            "pooled_1000ms": {
                "family": "POOLED", "horizon_ms": 1000,
                "reference_latency_ms": 50,
                "metrics": {
                    "marked_fills": 60, "fills": 70, "fill_rate": .5,
                    "markout_pnl": 2.0, "markout_per_fill": .04,
                },
                "uncertainty": {
                    "markout_per_fill_interval": [.005, .08],
                    "markout": {"markets": 30},
                },
                "candidate_contract": {"ridge": 8.0},
            }
        },
        "live_parity_horizon_latency": {
            "1000": {
                "25": {"state": "READY", "markout_pnl": 2.2, "marked_fills": 61},
                "50": {"state": "READY", "markout_pnl": 2.0, "marked_fills": 60},
            }
        },
    }
    manifest = {
        **safety,
        "minimum_wall_ns": 1_789_921_800_000_000_000,
        "data_sha256": "d" * 64,
    }
    results = {**safety, "start_sha": "a" * 40}
    model = {
        "state": "READY",
        "target": "future_executable_bid_minus_decision_ask_minus_entry_and_exit_taker_fees",
    }
    artifact = {
        **safety,
        "automatic_promotion": False,
        "training_window": {
            "mode": "EXPANDING_ALL_CAUSAL_HISTORY",
            "minimum_wall_ns": 1_789_921_800_000_000_001,
            "maximum_decision_ns": 1_789_930_000_000_000_000,
            "decision_rows": 1000,
        },
        "executable_markout_models": {"1000": model},
        "asset_executable_markout_models": {},
    }
    for name, value in (
        ("economic_metrics.json", economics),
        ("data_manifest.json", manifest),
        ("results.json", results),
        ("full_window_repricing_models.json", artifact),
    ):
        (report / name).write_text(json.dumps(value), encoding="utf-8")

    registry = tmp_path / "champion.json"
    receipt = report / "promotion_receipt.json"
    monkeypatch.setattr(sys, "argv", [
        "promotion", "--report-dir", str(report),
        "--registry", str(registry), "--receipt", str(receipt),
    ])
    assert promotion_main() == 0
    champion = json.loads(registry.read_text())
    assert champion["candidate_id"] == "pooled_1000ms"
    assert champion["promotion_scope"] == "PAPER_RESEARCH_CHAMPION_ONLY"
    assert champion["hot_path_mutation"] is False
    assert champion["trader_restart_required"] is False
    assert champion["real_order_submission"] is False
    assert json.loads(receipt.read_text())["action"] == "PROMOTE"



def test_existing_champion_is_refit_when_it_remains_best(tmp_path, monkeypatch):
    report = tmp_path / "report"
    report.mkdir()
    safety = {
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
    }
    economics = {
        **safety,
        "live_policy_promotion_candidates": {
            "pooled_1000ms": {
                "family": "POOLED", "horizon_ms": 1000,
                "reference_latency_ms": 50,
                "metrics": {
                    "marked_fills": 80, "fills": 90, "fill_rate": .5,
                    "markout_pnl": 3.0, "markout_per_fill": .05,
                },
                "uncertainty": {
                    "markout_per_fill_interval": [.01, .09],
                    "markout": {"markets": 40},
                },
                "candidate_contract": {"ridge": 8.0},
            }
        },
        "live_parity_horizon_latency": {
            "1000": {
                "25": {"state": "READY", "markout_pnl": 3.2, "marked_fills": 81},
                "50": {"state": "READY", "markout_pnl": 3.0, "marked_fills": 80},
            }
        },
    }
    manifest = {
        **safety,
        "minimum_wall_ns": 1_789_921_800_000_000_000,
        "data_sha256": "e" * 64,
    }
    results = {**safety, "start_sha": "b" * 40}
    model = {
        "state": "READY",
        "target": "future_executable_bid_minus_decision_ask_minus_entry_and_exit_taker_fees",
        "training_end_ns": 300,
    }
    artifact = {
        **safety,
        "automatic_promotion": False,
        "training_window": {
            "mode": "EXPANDING_ALL_CAUSAL_HISTORY",
            "minimum_wall_ns": 1_789_921_800_000_000_001,
            "maximum_decision_ns": 300,
            "decision_rows": 1200,
        },
        "executable_markout_models": {"1000": model},
        "asset_executable_markout_models": {},
    }
    for name, value in (
        ("economic_metrics.json", economics),
        ("data_manifest.json", manifest),
        ("results.json", results),
        ("full_window_repricing_models.json", artifact),
    ):
        (report / name).write_text(json.dumps(value), encoding="utf-8")

    registry = tmp_path / "champion.json"
    registry.write_text(json.dumps({
        "schema": "polymarket_v7_executable_markout_research_champion_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "candidate_id": "pooled_1000ms",
        "configured_minimum_wall_ns": 1_789_921_800_000_000_000,
        "training_window": {"decision_rows": 1000, "maximum_decision_ns": 200},
    }), encoding="utf-8")
    receipt = report / "promotion_receipt.json"
    monkeypatch.setattr(sys, "argv", [
        "promotion", "--report-dir", str(report),
        "--registry", str(registry), "--receipt", str(receipt),
    ])
    assert promotion_main() == 0
    value = json.loads(receipt.read_text())
    assert value["action"] == "REFIT_CHAMPION"
    assert value["registry_updated"] is True
    champion = json.loads(registry.read_text())
    assert champion["candidate_id"] == "pooled_1000ms"
    assert champion["training_window"]["decision_rows"] == 1200
    assert champion["training_window"]["maximum_decision_ns"] == 300
    assert champion["selection_action"] == "REFIT_CHAMPION"



def test_live_zero_chase_refuses_one_tick_worse_arrival():
    row = record("zero-chase")
    row["fee_rate"] = 0.0
    row["timeline"] = [book(row, row["decision_ns"] + 50_000_000, ask=.51)]
    row["timeline_times"] = [row["timeline"][0]["time_ns"]]
    row["targets"] = {
        "500": {"state": "OBSERVED", "arrival_bid": .55}
    }
    live = replay_one(
        copy.deepcopy(row), .03, None,
        latency_ms=50, valuation_mode="EXECUTABLE_MARKOUT",
        markout_horizon_ms=500, edge_threshold=.005, execution_reserve=.005,
        entry_cap=.80, shares=5.0, minimum_tte_ns=105_000_000_000,
        maximum_tte_ns=120_000_000_000, require_full_visible_depth=True,
        limit_chase_ticks=0)
    research_chase = replay_one(
        copy.deepcopy(row), .03, None,
        latency_ms=50, valuation_mode="EXECUTABLE_MARKOUT",
        markout_horizon_ms=500, edge_threshold=.005, execution_reserve=.005,
        entry_cap=.80, shares=5.0, minimum_tte_ns=105_000_000_000,
        maximum_tte_ns=120_000_000_000, require_full_visible_depth=True,
        limit_chase_ticks=2)
    assert live["status"] == "NO_FILL_LIMIT_NOT_TOUCHED"
    assert research_chase["filled"] == 4.0


def test_capital_notional_quantity_matches_native_integer_formula():
    q = quantity_for_notional_microdollars(83_333_333, .50)
    assert math.isclose(q, 166.666666, abs_tol=1e-12)

    row = record("capital-size")
    row["fee_rate"] = 0.0
    row["quantity"] = 200.0
    row["minimum"] = 5.0
    row["timeline"] = [book(row, row["decision_ns"] + 50_000_000, ask=.50)]
    row["timeline"][0]["quantity"] = 200.0
    row["timeline_times"] = [row["timeline"][0]["time_ns"]]
    row["targets"] = {
        "500": {"state": "OBSERVED", "arrival_bid": .55}
    }
    outcome = replay_one(
        row, .03, None,
        latency_ms=50, valuation_mode="EXECUTABLE_MARKOUT",
        markout_horizon_ms=500, edge_threshold=.005, execution_reserve=.005,
        entry_cap=.80, minimum_tte_ns=105_000_000_000,
        maximum_tte_ns=120_000_000_000, require_full_visible_depth=True,
        limit_chase_ticks=0, target_notional_microdollars=83_333_333)
    assert math.isclose(outcome["requested"], 166.666666, abs_tol=1e-12)
    assert math.isclose(outcome["filled"], 166.666666, abs_tol=1e-12)
    assert outcome["reserved_microdollars"] <= 83_333_333

    thin = copy.deepcopy(row)
    thin["quantity"] = 100.0
    rejected = replay_one(
        thin, .03, None,
        latency_ms=50, valuation_mode="EXECUTABLE_MARKOUT",
        markout_horizon_ms=500, edge_threshold=.005, execution_reserve=.005,
        entry_cap=.80, minimum_tte_ns=105_000_000_000,
        maximum_tte_ns=120_000_000_000, require_full_visible_depth=True,
        limit_chase_ticks=0, target_notional_microdollars=83_333_333)
    assert rejected["status"] == "FILTERED"
    assert rejected["funnel"]["sufficient_depth"] is False



def test_native_pair_l1_requires_complete_bilateral_prices_and_quantities():
    raw = {
        "repricing_pair_valid": True,
        "yes_bid_e4": 4900, "yes_ask_e4": 5000,
        "no_bid_e4": 5000, "no_ask_e4": 5100,
        "yes_bid_quantity": 1_000_000, "yes_ask_quantity": 2_000_000,
        "no_bid_quantity": 3_000_000, "no_ask_quantity": 4_000_000,
    }
    pair = native_pair_l1(raw)
    assert pair["YES"]["ask"] == .50
    assert pair["NO"]["ask_quantity"] == 4.0

    incomplete = dict(raw)
    incomplete.pop("no_ask_quantity")
    assert native_pair_l1(incomplete) is None

    invalid = dict(raw)
    invalid["no_ask_quantity"] = -1
    assert native_pair_l1(invalid) is None
