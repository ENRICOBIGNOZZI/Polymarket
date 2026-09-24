"""Native reducer must not turn updates, gaps or censored episodes into trades."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_v7_exact_arb_native_runtime_bundle import MODEL, bundle, fixture
from v7_exact_arb_native_evidence import Evidence, SAFETY, SCHEMA, canonical


@pytest.fixture
def data(tmp_path):
    _, graph = fixture()
    wire = bundle(graph)
    body = json.loads(wire["payload"])
    (tmp_path/(wire["sha256"]+".json")).write_text(wire["payload"])
    relation = body["relations"][0]
    row = {**SAFETY, "schema": SCHEMA, "model_sha": MODEL,
           "actionable": False, "economic_execution_verified": False,
           "evidence_scope": "CAUSAL_FRAME_END_PRE_ALLOCATION_EVALUATION",
           "observer_session_id": "session-a", "observation_sequence": 1,
           "decision_start_ns": 100, "decision_end_ns": 101, "receive_monotonic_ns": 100,
           "native_bundle_sha256": wire["sha256"], "graph_generation": body["graph_generation"],
           "selection_receipt_sha256": "b"*64,
           "relation_handle": 0, "proof_handle": int(relation["proof_hash"][:16], 16),
           "connection_epoch": 1, "continuity_serial": 2, "evaluation_accepted": True,
           "evaluation_valid_until_monotonic_ns": 1000, "source_valid_until_monotonic_ns": 10000,
           "near_arbitrage": {"valid": True, "books_ready": True, "lineage_ready": True,
               "fees_ready": True, "freshness_ready": True, "leg_skew_ready": True,
               "depth_complete": True, "raw_distance_nano": -20000000,
               "after_fee_distance_nano": -10000000, "after_reserve_distance_nano": -9500000,
               "actionable_tick_nano": 10000000, "guarantee_nano": 1000000000}}
    return tmp_path, row, relation["relation_family"]+":BUY"


def change(row, seq, positive=True, **kwargs):
    result = deepcopy(row)
    result.update(observation_sequence=seq, decision_start_ns=seq*100,
                  decision_end_ns=seq*100+1, receive_monotonic_ns=seq*100,
                  evaluation_accepted=positive)
    if not positive:
        for stage in ("raw", "after_fee", "after_reserve"):
            result["near_arbitrage"][stage+"_distance_nano"] = 20000000
    result.update(kwargs)
    return result


def reduce(data, rows):
    directory, _, _ = data
    with_memory = Evidence(":memory:", directory, MODEL)
    try:
        for row in rows:
            with_memory.ingest(row)
        report = with_memory.finish()
        episodes = [json.loads(r[0]) for r in with_memory.db.execute("SELECT payload FROM episodes ORDER BY id")]
        return report, episodes
    finally:
        with_memory.close()


def test_positive_updates_are_one_left_censored_segment_not_trades(data):
    _, row, family = data
    report, episodes = reduce(data, [change(row, i) for i in range(1, 6)])
    funnel = report["families"][family]["evaluation_funnel"]
    assert funnel["accepted_evaluations"] == 5
    assert funnel["raw_positive_segments"] == 1
    assert funnel["raw_observed_starts"] == 0
    assert len(episodes) == 4
    assert all(e["left_censored"] and e["right_censored"] for e in episodes)
    assert all(e["complete_lifetime_ns"] is None for e in episodes)
    assert all(v is None for v in report["economic_evidence"].values())


def test_observed_start_end_and_exact_descriptive_quantiles(data):
    _, row, family = data
    report, episodes = reduce(data, [change(row, 1, False), change(row, 2), change(row, 3), change(row, 4, False)])
    assert all(e["complete_lifetime_ns"] == 200 for e in episodes)
    assert all(not e["left_censored"] and not e["right_censored"] for e in episodes)
    metrics = report["families"][family]
    assert metrics["evaluation_funnel"]["raw_observed_starts"] == 1
    assert metrics["lifetimes"]["raw"]["complete_episodes"] == 1
    near = metrics["near_arbitrage"]["raw"]
    assert near["quantiles"]["pusd"]["0"] == -.02
    assert near["quantiles"]["ticks"]["0.99"] == 2
    assert near["quantiles"]["bps"]["0"] == -200
    assert near["fraction_within_ticks"]["1"] == 0
    assert near["fraction_within_ticks"]["2"] == 1


@pytest.mark.parametrize("mutation", [
    {"observation_sequence": 4}, {"connection_epoch": 2}, {"continuity_serial": 3},
    {"selection_receipt_sha256": "c"*64}, {"observer_session_id": "session-b", "observation_sequence": 1},
])
def test_gaps_and_resets_censor_without_manufacturing_new_arrivals(data, mutation):
    _, row, family = data
    rows = [change(row, 1, False), change(row, 2), change(row, 3, **mutation)]
    report, episodes = reduce(data, rows)
    assert report["families"][family]["evaluation_funnel"]["raw_observed_starts"] == 1
    assert len(episodes) == 8
    assert all(e["right_censored"] for e in episodes)
    assert sum(e["left_censored"] for e in episodes) == 4


def test_expired_book_cannot_bridge_long_quiet_interval(data):
    _, row, family = data
    rows = [change(row, 1, False), change(row, 2, evaluation_valid_until_monotonic_ns=250), change(row, 3)]
    report, episodes = reduce(data, rows)
    assert len(episodes) == 8
    assert sum(e["end_reason"] == "BOOK_OR_SOURCE_EXPIRED" for e in episodes) == 4
    assert report["families"][family]["lifetimes"]["raw"]["complete_episodes"] == 0


def test_invalid_observation_is_missing_not_zero_or_negative(data):
    _, row, family = data
    invalid = change(row, 2, False)
    invalid["near_arbitrage"].update(valid=False, freshness_ready=False, raw_distance_nano=None,
                                    after_fee_distance_nano=None, after_reserve_distance_nano=None)
    report, episodes = reduce(data, [row, invalid, change(row, 3)])
    assert report["families"][family]["near_arbitrage"]["raw"]["observations"] == 2
    assert report["families"][family]["evaluation_funnel"]["raw_observed_starts"] == 0
    assert sum(e["end_reason"] == "INVALID_RELATION_OBSERVATION" for e in episodes) == 4


def test_rotated_overlap_and_replay_do_not_double_count(data):
    _, row, _ = data
    first, episodes = reduce(data, [row, change(row, 2), row, change(row, 3)])
    second, again = reduce(data, [row, change(row, 2), change(row, 3)])
    assert episodes == again
    assert first["normalized_input_sha256"] == second["normalized_input_sha256"]
    assert first["families"] == second["families"]
    assert first["engineering_evidence"]["duplicate_rows_removed"] == 1


def test_incomplete_sizing_censors_acceptance_not_top_of_book(data):
    _, row, family = data
    unknown = change(row, 3, evaluation_accepted=False, rejection_reason="SIZING_INCOMPLETE",
                     sizing_search_exhausted=True)
    report, episodes = reduce(data, [change(row, 1, False), change(row, 2), unknown,
                                    change(row, 4), change(row, 5, False)])
    stages = sorted((e for e in episodes if e["stage"] == "pre_allocation"), key=lambda e: e["first_positive_ns"])
    assert len(stages) == 2
    assert stages[0]["end_reason"] == "SIZING_SEARCH_INCOMPLETE"
    assert stages[0]["right_censored"]
    assert stages[1]["left_censored"]
    metrics = report["families"][family]
    assert metrics["evaluation_funnel"]["sizing_unknown_evaluations"] == 1
    assert metrics["evaluation_funnel"]["pre_allocation_observed_starts"] == 1
    assert metrics["lifetimes"]["pre_allocation"]["complete_episodes"] == 0
    assert metrics["lifetimes"]["raw"]["complete_episodes"] == 1


@pytest.mark.parametrize("mutation", [
    {"global_size_optimum_proven": True, "sizing_search_exhausted": True},
    {"global_size_optimum_proven": 1}, {"sizing_quantities_evaluated": 4097},
    {"sizing_proof_scope": "VERIFIED_VENUE_EXECUTION"},
])
def test_invalid_sizing_certificate(data, mutation):
    _, row, _ = data
    certified = change(row, 1, sizing_model="PER_L2_LEVEL_5DP_EXACT_ORDER_LATTICE_V1",
        global_size_optimum_proven=True, sizing_search_exhausted=False, sizing_quantities_evaluated=10,
        sizing_proof_scope="RECORDED_MODEL_ONLY_NOT_VERIFIED_VENUE_EXECUTION")
    certified.update(mutation)
    with pytest.raises(ValueError, match="invalid_sizing_certificate"):
        reduce(data, [certified])


@pytest.mark.parametrize("mutation,reason", [
    ({"schema": "polymarket_v7_native_exact_arb_observation_v1"}, "observation_identity"),
    ({"paper_only": False}, "paper_boundary"),
    ({"model_sha": "wrong"}, "observation_identity"),
    ({"receive_monotonic_ns": 200}, "observation_clock"),
    ({"decision_end_ns": 99}, "observation_clock"),
    ({"observation_sequence": True}, "invalid_integer"),
    ({"proof_handle": 0}, "proof_identity"),
    ({"relation_handle": 500}, "relation_handle"),
    ({"evaluation_valid_until_monotonic_ns": 99}, "evaluation_deadline"),
    ({"evaluation_valid_until_monotonic_ns": 20000}, "evaluation_deadline"),
])
def test_malformed_evidence_fails_closed(data, mutation, reason):
    _, row, _ = data
    with pytest.raises(ValueError, match=reason):
        reduce(data, [{**row, **mutation}])


def test_conflicting_duplicate_and_reordering_fail_closed(data):
    _, row, _ = data
    with pytest.raises(ValueError, match="conflicting_duplicate"):
        reduce(data, [row, change(row, 1, False)])
    with pytest.raises(ValueError, match="noncausal_input_order"):
        reduce(data, [change(row, 2), row])
    with pytest.raises(ValueError, match="noncausal_input_order"):
        reduce(data, [row, change(row, 2, decision_start_ns=50, receive_monotonic_ns=50)])


def test_tampered_bundle_cannot_supply_family_identity(data):
    directory, row, _ = data
    path = directory/(row["native_bundle_sha256"]+".json")
    path.write_text(path.read_text()+" ")
    with pytest.raises(ValueError, match="bundle_digest"):
        reduce(data, [row])


def test_missing_diagnostics_have_no_numeric_zero_quantiles(data):
    _, row, family = data
    invalid = change(row, 1, False)
    invalid["near_arbitrage"].update(valid=False, books_ready=False)
    report, _ = reduce(data, [invalid])
    assert report["families"][family]["near_arbitrage"]["raw"]["quantiles"]["pusd"]["0"] is None


def test_cli_immutable_replay_report_and_truncated_tail(data):
    directory, row, _ = data
    tape = directory/"sealed.jsonl"
    tape.write_text(canonical(row)+"\n")
    command = [sys.executable, str(Path(__file__).resolve().parents[1]/"scripts/v7_exact_arb_native_evidence.py"),
               "--segments", str(tape), "--bundles", str(directory), "--model-sha", MODEL,
               "--output", str(directory/"reports")]
    first = subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
    second = subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
    assert first == second
    assert Path(first).is_file()
    assert (Path(first).parent/"episodes.jsonl").is_file()
    tape.write_text(canonical(row))
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode != 0 and "unsealed_row" in failed.stderr
