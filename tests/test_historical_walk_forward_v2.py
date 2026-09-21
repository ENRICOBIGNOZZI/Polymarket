# HISTORICAL_WALK_FORWARD_V2 is an explicit causal-research CI gate.
import copy
import math

from research.walk_forward_v2.core import (
    HORIZONS_MS,
    Ridge,
    book_targets,
    build_dataset,
    folds,
    replay_one,
    valid_native,
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
