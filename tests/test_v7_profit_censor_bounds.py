from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_profit_censor_bounds as bounds  # noqa: E402
from v7_profit_protocol import digest, validate  # noqa: E402


def protocol_v5() -> dict:
    value = json.loads((ROOT / "config/v7_profit_experiment_v5.json").read_text())
    validate(value)
    return value


def settlement(market: str, token: str, *, win: bool = False) -> dict:
    other = token + "-other"
    return {
        "market_id": market,
        "winning_token_id": token if win else other,
        "tokens": [token, other],
        "prices": [1.0, 0.0] if win else [0.0, 1.0],
        "settlement_closed": True,
    }


def selection(i: int, start: int) -> dict:
    return {
        "selection_key": f"s{i}",
        "market_id": f"m{i}",
        "token_id": f"t{i}",
        "origin_ns": start + i,
        "model_probability": 0.4,
        "pm_probability": 0.5,
        "fee_schedule": {"rate": 0.02, "exponent": 1.0},
    }


def observed_delay(sel: dict) -> dict:
    return {
        "selection_key": sel["selection_key"],
        "delay_ms": 1000,
        "state": "OBSERVED",
        "book_cut": {"best_ask": 0.6},
        "fee_per_share": 0.0048,
        "risk_allowance_per_share": 0.001,
    }


def arm(name: str, *, observed: bool = True) -> dict:
    if observed:
        return {
            "arm": name,
            "state": "OBSERVED",
            "operational_filled_shares": 0.0,
            "fills": [],
            "common_quote_quantity": 1.0,
            "research_request": {"quantity": 1.0},
        }
    return {
        "arm": name,
        "state": "BOOK_CONTINUITY_CENSORED",
        "operational_filled_shares": 0.0,
        "fills": [],
        "common_quote_quantity": 1.0,
        "research_request": {"quantity": 1.0},
    }


def comparison(i: int, *, censor_join10: bool = False) -> dict:
    return {
        "anchor_record_id": f"a{i}",
        "market_id": f"m{i}",
        "token_id": f"t{i}",
        "arms": [
            arm("JOIN_5S"),
            arm("JOIN_10S", observed=not censor_join10),
        ],
    }


def fixture(*, signal_censors: int = 1, maker_censors: int = 1):
    protocol = protocol_v5()
    start = 1_000_000_000
    end = start + 8 * 3_600_000_000_000
    manifest = {"protocol": protocol, "forward_start_ns": start, "confirmatory_end_ns": end}
    selections = {}
    delays = {}
    settlements = {}
    comparisons = []
    anchors = {}
    for i in range(20):
        row = selection(i, start)
        selections[row["selection_key"]] = row
        label = observed_delay(row)
        if i < signal_censors:
            label = {**label, "state": "BOOK_CONTINUITY_OR_WATERMARK_CENSORED", "book_cut": None,
                     "fee_per_share": None, "risk_allowance_per_share": 0.001}
        delays[(row["selection_key"], 1000)] = label
        settlements[row["market_id"]] = settlement(row["market_id"], row["token_id"], win=False)
        comparisons.append(comparison(i, censor_join10=i < maker_censors))
        anchors[f"a{i}"] = start + 1000 + i
    return selections, delays, comparisons, anchors, manifest, settlements


def test_named_v5_and_operational_default_are_identical() -> None:
    default = json.loads((ROOT / "config/v7_profit_experiment.json").read_text())
    named = json.loads((ROOT / "config/v7_profit_experiment_v5.json").read_text())
    assert digest(default) == digest(named)
    assert default == named


def test_v4_is_legacy_and_v5_is_explicit_opt_in() -> None:
    v4 = json.loads((ROOT / "config/v7_profit_experiment_v4.json").read_text())
    validate(v4)
    assert bounds.policy(v4) is None
    assert bounds.policy(protocol_v5())["mode"] == bounds.MODE


def test_binary_fee_maximum_is_exact() -> None:
    assert abs(bounds.max_binary_fee({"rate": 0.02, "exponent": 1.0}) - 0.005) < 1e-15
    assert abs(bounds.fee_at(0.6, {"rate": 0.02, "exponent": 1.0}) - 0.0048) < 1e-15


def test_terminal_censors_are_included_at_worst_case_support() -> None:
    selections, delays, comparisons, anchors, manifest, settlements = fixture()
    primary, audit = bounds.bounded_primary(selections, delays, comparisons, anchors, manifest, settlements)
    signal = audit["endpoint_audit"][bounds.SETTLEMENT_ENDPOINT]
    maker = audit["endpoint_audit"][bounds.MAKER_ENDPOINT]
    assert signal["eligible_units"] == 20
    assert signal["censored_units"] == 1
    assert signal["support_imputed_units"] == 1
    assert abs(signal["censor_fraction"] - 0.05) < 1e-15
    assert signal["cap_pass"] is True
    assert maker["eligible_units"] == 20
    assert maker["censored_units"] == 1
    assert maker["support_imputed_units"] == 1
    assert maker["cap_pass"] is True
    assert audit["all_endpoint_caps_and_support_pass"] is True
    expected_signal_lower = -1.0 - 2.0 * (0.005 + 0.001)
    assert any(abs(value - expected_signal_lower) < 1e-12 for value in primary[bounds.SETTLEMENT_ENDPOINT].values())
    assert any(abs(value - (-1.002)) < 1e-12 for value in primary[bounds.MAKER_ENDPOINT].values())


def test_malformed_observed_economics_fail_closed_not_as_censor() -> None:
    selections, delays, comparisons, anchors, manifest, settlements = fixture(signal_censors=0, maker_censors=0)
    delays[("s0", 1000)] = {**delays[("s0", 1000)], "fee_per_share": 0.0}
    _primary, audit = bounds.bounded_primary(selections, delays, comparisons, anchors, manifest, settlements)
    signal = audit["endpoint_audit"][bounds.SETTLEMENT_ENDPOINT]
    assert signal["censored_units"] == 0
    assert signal["support_unavailable_units"] == 1
    assert signal["support_complete"] is False
    assert audit["all_endpoint_caps_and_support_pass"] is False


def test_more_than_five_percent_censoring_fails_closed() -> None:
    data = fixture(signal_censors=2, maker_censors=2)
    _primary, audit = bounds.bounded_primary(*data)
    assert audit["endpoint_audit"][bounds.SETTLEMENT_ENDPOINT]["censor_fraction"] == 0.1
    assert audit["endpoint_audit"][bounds.SETTLEMENT_ENDPOINT]["cap_pass"] is False
    assert audit["endpoint_audit"][bounds.MAKER_ENDPOINT]["cap_pass"] is False
    assert audit["all_endpoint_caps_and_support_pass"] is False


def test_missing_terminal_record_is_not_reclassified_as_censor() -> None:
    selections, delays, comparisons, anchors, manifest, settlements = fixture()
    delays.pop(("s0", 1000))
    _primary, audit = bounds.bounded_primary(selections, delays, comparisons, anchors, manifest, settlements)
    signal = audit["endpoint_audit"][bounds.SETTLEMENT_ENDPOINT]
    assert signal["support_unavailable_units"] == 1
    assert audit["all_endpoint_caps_and_support_pass"] is False


def test_out_of_window_maker_anchor_never_enters_denominator() -> None:
    selections, delays, comparisons, anchors, manifest, settlements = fixture()
    anchors["outside"] = manifest["confirmatory_end_ns"] + 1
    comparisons.append({
        "anchor_record_id": "outside", "market_id": "outside-market", "token_id": "outside-token",
        "arms": [arm("JOIN_5S", observed=False), arm("JOIN_10S", observed=False)],
    })
    settlements["outside-market"] = settlement("outside-market", "outside-token")
    _primary, audit = bounds.bounded_primary(selections, delays, comparisons, anchors, manifest, settlements)
    assert audit["endpoint_audit"][bounds.MAKER_ENDPOINT]["eligible_units"] == 20


def test_policy_rejects_any_unregistered_cap_change() -> None:
    value = protocol_v5()
    for changed_cap in (0.049, 0.051, 1.5):
        changed = copy.deepcopy(value)
        changed["inference"]["confirmatory"]["censoring"]["endpoint_max_censor_fraction"][bounds.MAKER_ENDPOINT] = changed_cap
        try:
            bounds.policy(changed)
        except ValueError as exc:
            assert "cap_bounds" in str(exc)
        else:
            raise AssertionError("unregistered censor cap accepted")


if __name__ == "__main__":
    test_named_v5_and_operational_default_are_identical()
    test_v4_is_legacy_and_v5_is_explicit_opt_in()
    test_binary_fee_maximum_is_exact()
    test_terminal_censors_are_included_at_worst_case_support()
    test_malformed_observed_economics_fail_closed_not_as_censor()
    test_more_than_five_percent_censoring_fails_closed()
    test_missing_terminal_record_is_not_reclassified_as_censor()
    test_out_of_window_maker_anchor_never_enters_denominator()
    test_policy_rejects_any_unregistered_cap_change()
