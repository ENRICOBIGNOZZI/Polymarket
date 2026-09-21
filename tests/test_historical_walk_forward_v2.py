# HISTORICAL_WALK_FORWARD_V2 is an explicit causal-research CI gate.
import copy
import math

from research.walk_forward_v2.core import (
    HORIZONS_MS,
    Ridge,
    book_targets,
    build_dataset,
    folds,
    fit_full_repricing,
    economic_evaluation,
    executable_markout_target,
    replay_one,
    replay_policy,
    settlement_predictors,
    valid_native,
    native_repricing_point,
    attach_native_repricing,
    attach_native_repricing_stream,
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
