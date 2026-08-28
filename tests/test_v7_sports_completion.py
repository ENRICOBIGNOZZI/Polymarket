import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_sports_latency as s


def source_spec(verified=True):
    return s.SportsSourceSpec(
        "official-league", "League", "wss://league.example/feed", s.Sport.SOCCER,
        "COMP", "v1", "schema-hash", "STRICT_MONOTONIC_PER_GAME",
        "NEW_SEQUENCE_REFERENCES_EVENT", 1000, verified, "FULL_STATE_AFTER_EVENT",
    )


def state(score_home=0):
    return {
        "pregame_logit": 0.0, "score_home": score_home, "score_away": 0,
        "remaining_seconds": 600, "red_cards_home": 0, "red_cards_away": 0,
    }


def event(seq, *, event_id=None, correction_of="", score_home=0, receive=1100):
    return s.SportsFeedEvent(
        event_id or f"e{seq}", "game", seq, "STATE", "official-league", True,
        receive - 5, receive, state(score_home), correction_of, "v1",
    )


def mapping():
    return s.ContractMapping(
        "market", "game", s.Sport.SOCCER, "MATCH_WINNER", "HOME", "League",
        "rules", True, 10_000, "UTC", "refund_if_abandoned",
        "home_wins_including_extra_time", "https://league.example/rules", "evidence", 1000,
    )


def test_verified_adapter_attests_schema_and_source():
    def decoder(payload, receive_ts_ms, spec):
        raw = json.loads(payload)
        return s.SportsFeedEvent(
            raw["id"], raw["game"], raw["seq"], "STATE", spec.source_id, True,
            raw["source_ts"], receive_ts_ms, raw["state"], schema_version=spec.schema_version,
        )

    adapter = s.VerifiedSportsStreamAdapter(source_spec(), decoder)
    raw = json.dumps({"id": "e1", "game": "game", "seq": 1, "source_ts": 1000, "state": state()}).encode()
    assert adapter.decode(raw, 1010).sequence == 1
    with pytest.raises(s.SportsError, match="unverified_official_source"):
        s.VerifiedSportsStreamAdapter(source_spec(False), decoder)


def test_sequence_duplicate_correction_and_resume_are_causal():
    guard = s.FeedGuard("game", 1000, source_spec=source_spec())
    first = event(1)
    guard.apply(first, 1100)
    guard.apply(first, 1101)
    assert guard.valid and guard.tape.records[-1].reason == "idempotent_duplicate"

    guard.disconnect()
    assert not guard.valid
    guard.begin_resume(resume_after_sequence=1)
    guard.apply(event(2, receive=1200), 1200)
    assert guard.valid and guard.connection_epoch == 1

    correction = event(3, correction_of="e1", score_home=1, receive=1250)
    guard.apply(correction, 1250)
    assert guard.valid and guard.correction_count == 1
    assert guard.tape.records[-1].reason == "correction"
    assert guard.tape.records[-1].previous_event_id == "e2"


def test_gap_bad_resume_unknown_correction_and_stale_state_fail_closed():
    guard = s.FeedGuard("game", 50, source_spec=source_spec())
    guard.apply(event(1), 1100)
    guard.apply(event(3, receive=1110), 1110)
    assert not guard.valid and guard.blocker == "feed_gap" and guard.gap_count == 1

    guard = s.FeedGuard("game", 50, source_spec=source_spec())
    guard.apply(event(1), 1100); guard.disconnect(); guard.begin_resume(resume_after_sequence=0)
    assert guard.connection_state is s.ConnectionState.QUARANTINED

    guard = s.FeedGuard("game", 50, source_spec=source_spec())
    guard.apply(event(1), 1100)
    guard.apply(event(2, correction_of="unknown", receive=1110), 1110)
    assert guard.blocker == "correction_target_unknown"

    guard = s.FeedGuard("game", 50, source_spec=source_spec())
    guard.apply(event(1), 1100)
    result = s.decide(
        mapping(), guard, now_ms=1200, pm_bid=.4, pm_ask=.5,
        uncertainty=.02, executable_cost=0, minimum_edge=0,
    )
    assert result.action == "NOTHING" and result.blocker == "stale_feed"


def test_contract_sport_selection_and_mapping_evidence_are_exact():
    invalid = s.ContractMapping(
        "market", "game", s.Sport.TENNIS, "MATCH_WINNER", "HOME", "League",
        "rules", True, 10_000, "UTC", "refund", "player_wins",
        "https://league.example/rules", "evidence", 1000,
    )
    with pytest.raises(s.SportsError, match="unsupported_selection"):
        invalid.validate()
    assert len(mapping().semantic_hash) == 64


def test_forward_shadow_records_event_lineage_race_and_no_execution(tmp_path):
    guard = s.FeedGuard("game", 1000, source_spec=source_spec())
    guard.apply(event(1), 1100)
    decision = s.decide(
        mapping(), guard, now_ms=1100, pm_bid=.2, pm_ask=.3,
        uncertainty=.01, executable_cost=0, minimum_edge=0,
    )
    race = s.SportsRaceObservation("game", "e1", 1095, 1100, 1102, 1110)
    output = tmp_path / "sports-shadow.jsonl"
    record_hash = s.SportsForwardShadow(output, run_id="run").record(
        mapping=mapping(), guard=guard, decision=decision, observed_at_ms=1110, race=race,
    )
    row = json.loads(output.read_text())
    assert row["record_hash"] == record_hash
    assert row["paper_only"] and not row["real_order_submission"]
    assert row["event_sequence"] == 1 and row["race"]["market_move_ts_ms"] == 1110
    assert race.receive_latency_ms == 5 and race.decision_latency_ms == 2 and race.lead_ms == 8


def test_chronological_calibration_enforces_independent_game_units():
    metrics = s.chronological_calibration([
        s.CalibrationPoint("g1", 10, .8, 1, 1),
        s.CalibrationPoint("g2", 20, .3, 0, 1),
        s.CalibrationPoint("future", 200, 1.0, 1, 100),
    ], cutoff_ms=100)
    assert metrics.observations == 2 and metrics.brier == pytest.approx(.065)
    with pytest.raises(s.SportsError, match="independent_games"):
        s.chronological_calibration([
            s.CalibrationPoint("g1", 10, .8, 1, 1),
            s.CalibrationPoint("g1", 20, .7, 1, 1),
        ], cutoff_ms=100)
