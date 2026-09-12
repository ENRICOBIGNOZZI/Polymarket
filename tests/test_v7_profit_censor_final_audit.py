from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_profit_experiments import AUTH  # noqa: E402
from v7_profit_protocol import digest, fixed_window_digest, freeze  # noqa: E402
from v7_profit_report import final_window_audit  # noqa: E402

V5_PROTOCOL_PATH = ROOT / "config/v7_profit_experiment_20260911.json"


def arm(name: str, censored: bool = False) -> dict:
    return {
        "arm": name,
        "state": "BOOK_CONTINUITY_CENSORED" if censored else "OBSERVED",
        "operational_filled_shares": 0.0,
        "fills": [],
        "common_quote_quantity": 1.0,
        "research_request": {"quantity": 1.0},
    }


def make_window(protocol_path: Path, signal_censors: int, maker_censors: int):
    protocol = json.loads(protocol_path.read_text())
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    manifest = freeze(root / "manifest.json", protocol, "a" * 40, "b" * 64, 300_000_000_001)
    start = manifest["forward_start_ns"] + 1_000_000_000
    end = manifest["confirmatory_end_ns"]
    observations = []
    labels = {}
    for i in range(20):
        market, token, other = f"m{i}", f"t{i}", f"o{i}"
        selected = {
            "kind": "SIGNAL_SELECTION", "selection_key": f"s{i}", "market_id": market,
            "token_id": token, "origin_ns": start + i * 1_000_000,
            "model_probability": 0.4, "pm_probability": 0.5,
            "fee_schedule": {"rate": 0.02, "exponent": 1.0},
        }
        observations.append(selected)
        for delay in protocol["signal"]["delays_ms"]:
            censored = delay == 1000 and i < signal_censors
            observations.append({
                "kind": "DELAY_LABEL", "selection_key": f"s{i}", "market_id": market,
                "token_id": token, "delay_ms": delay,
                "state": "BOOK_CONTINUITY_OR_WATERMARK_CENSORED" if censored else "OBSERVED",
                "book_cut": None if censored else {"best_ask": 0.6},
                "fee_per_share": None if censored else 0.0048,
                "risk_allowance_per_share": 0.001,
            })
        anchor_id = f"a{i}"
        observations.append({
            "kind": "MAKER_ANCHOR", "market_id": market, "token_id": token,
            "origin_ms": (start + 100_000_000 + i * 1_000_000) // 1_000_000,
            "order": {"record_id": anchor_id},
        })
        observations.append({
            "kind": "MAKER_COMPARISON", "anchor_record_id": anchor_id,
            "market_id": market, "token_id": token,
            "arms": [arm("JOIN_5S"), arm("JOIN_10S", censored=i < maker_censors)],
        })
        raw = {"id": market, "closed": True, "clobTokenIds": [token, other], "outcomePrices": [0.0, 1.0]}
        labels[market] = {
            "market_id": market, "winning_token_id": other, "tokens": [token, other],
            "prices": [0.0, 1.0], "settlement_closed": True,
            "observed_ms": end // 1_000_000 + 1000,
            "source_sha256": digest(raw), "raw_response": raw,
        }
    closure = {
        "schema": "polymarket_v7_confirmatory_window_closure_v1", **AUTH,
        "manifest_sha256": manifest["manifest_sha256"], "closed_at_ns": end + 1,
        "forward_end_ns": end, "window_observations_sha256": fixed_window_digest(observations, manifest),
        "pending_window_signals": 0, "pending_window_maker_anchors": 0,
        "scope": "OBSERVED_SELECTED_POPULATION; NOT_PROOF_OF_UNOBSERVED_OPPORTUNITY_COVERAGE",
    }
    closure["closure_sha256"] = digest(closure)
    return temp, observations, manifest, labels, closure, end + 2


def test_v5_one_in_twenty_terminal_censors_pass_with_worst_case_support() -> None:
    temp, observations, manifest, labels, closure, now = make_window(V5_PROTOCOL_PATH, 1, 1)
    try:
        audit = final_window_audit(observations, manifest, labels, now, closure)
        assert audit["terminal_records_and_verified_settlements_complete"] is True
        assert audit["complete_primary_causal_coverage"] is True
        bounded = audit["confirmatory_censoring"]
        assert bounded["endpoint_audit"]["selected_settlement_surplus_cost2_delay1000"]["censor_fraction"] == 0.05
        assert bounded["endpoint_audit"]["maker_join10_minus_join5_settlement_net_cost2"]["censor_fraction"] == 0.05
        assert bounded["all_endpoint_caps_and_support_pass"] is True
    finally:
        temp.cleanup()


def test_v5_two_in_twenty_terminal_censors_fail_cap() -> None:
    temp, observations, manifest, labels, closure, now = make_window(V5_PROTOCOL_PATH, 2, 2)
    try:
        audit = final_window_audit(observations, manifest, labels, now, closure)
        assert audit["terminal_records_and_verified_settlements_complete"] is True
        assert audit["complete_primary_causal_coverage"] is False
        assert audit["confirmatory_censoring"]["all_endpoint_caps_and_support_pass"] is False
    finally:
        temp.cleanup()


def test_prior_protocol_keeps_original_zero_censor_tolerance() -> None:
    temp, observations, manifest, labels, closure, now = make_window(
        ROOT / "config/v7_profit_experiment_20260910.json", 1, 1
    )
    try:
        audit = final_window_audit(observations, manifest, labels, now, closure)
        assert audit["terminal_records_and_verified_settlements_complete"] is True
        assert audit["signal_primary_censored_origins"] == 1
        assert audit["maker_primary_censored_anchors"] == 1
        assert audit["complete_primary_causal_coverage"] is False
        assert "confirmatory_censoring" not in audit
    finally:
        temp.cleanup()


def test_missing_terminal_record_stays_fail_closed_in_v5() -> None:
    temp, observations, manifest, labels, _closure, now = make_window(V5_PROTOCOL_PATH, 0, 0)
    try:
        observations = [r for r in observations if not (
            r["kind"] == "DELAY_LABEL" and r["selection_key"] == "s0" and r["delay_ms"] == 1000
        )]
        closure = {
            "schema": "polymarket_v7_confirmatory_window_closure_v1", **AUTH,
            "manifest_sha256": manifest["manifest_sha256"], "closed_at_ns": manifest["confirmatory_end_ns"] + 1,
            "forward_end_ns": manifest["confirmatory_end_ns"],
            "window_observations_sha256": fixed_window_digest(observations, manifest),
            "pending_window_signals": 0, "pending_window_maker_anchors": 0,
        }
        closure["closure_sha256"] = digest(closure)
        audit = final_window_audit(observations, manifest, labels, now, closure)
        assert len(audit["missing_delay_labels"]) == 1
        assert audit["terminal_records_and_verified_settlements_complete"] is False
        assert audit["complete_primary_causal_coverage"] is False
    finally:
        temp.cleanup()


if __name__ == "__main__":
    test_v5_one_in_twenty_terminal_censors_pass_with_worst_case_support()
    test_v5_two_in_twenty_terminal_censors_fail_cap()
    test_prior_protocol_keeps_original_zero_censor_tolerance()
    test_missing_terminal_record_stays_fail_closed_in_v5()
