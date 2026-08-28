import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_market_open as kernel
import v7_market_open_pipeline as pipeline


def adapter():
    return pipeline.MarketSourceAdapter("polymarket-public", ("https://clob.example",),
                                        "market-stream-v1", True)


def stream(**overrides):
    values = {
        "source_id": "polymarket-public", "source_url": "https://clob.example/markets",
        "source_event_id": "create-1", "market_id": "m1", "event_id": "e1",
        "event_type": "CREATED", "published_ts_ms": 1_000, "received_ts_ms": 1_010,
        "payload_hash": "payload-sha",
    }
    values.update(overrides)
    return pipeline.MarketStreamEvent(**values)


def semantics(**overrides):
    values = {
        "market_id": "m1", "event_id": "e1",
        "rules_text": "YES iff official value >= 10",
        "settlement_source": "https://official.example/result", "comparator": ">=",
        "cutoff_ms": 10_000, "timezone": "UTC", "source_id": "polymarket-public",
        "source_url": "https://clob.example/markets/m1", "source_record_id": "create-1",
        "received_ts_ms": 1_010, "verified": True,
        "verification_method": "HUMAN_RULES_REVIEW",
    }
    values.update(overrides)
    return pipeline.StructuredSemantics(**values)


def lineaged_fair():
    estimate = kernel.FairEstimate(kernel.FairSource.BASE_RATE, .7, .05, "base-v1", 1_100, True)
    lineage = pipeline.FairLineage("official", "base-rate-1", "payload-sha", "mapping-sha",
                                   "dataset-sha", 1_100, True, "REPRODUCIBLE_MODEL")
    return pipeline.LineagedFair(estimate, lineage)


def test_detector_emits_exactly_one_causal_open_and_rejects_unknown_sources():
    detector = pipeline.NewMarketDetector([adapter()])
    opened = detector.observe(stream())
    assert opened and opened.open_ts_ms == 1_010
    assert detector.observe(stream(source_event_id="duplicate")) is None
    with pytest.raises(kernel.MarketOpenError, match="market_source_not_registered"):
        detector.observe(stream(source_id="unknown"))


def test_semantic_parser_never_uses_llm_as_authority():
    contract = pipeline.parse_verified_semantics(semantics(), decision_ts_ms=1_100,
                                                 adapter=adapter())
    assert contract.verified and contract.rules_hash
    with pytest.raises(kernel.MarketOpenError, match="llm_cannot_verify_semantics"):
        pipeline.parse_verified_semantics(
            semantics(verification_method="LLM"), decision_ts_ms=1_100, adapter=adapter(),
        )


def test_related_market_lookup_requires_exact_verified_relation():
    contract = pipeline.parse_verified_semantics(semantics(), decision_ts_ms=10_000_000,
                                                 adapter=adapter())
    candidate = pipeline.RelatedMarket("mature", "e1", "COMPLEMENT", .2, .03, 1_000,
                                       4_000_000, "mapping-sha", True)
    fair = pipeline.related_market_fair(contract, [candidate], decision_ts_ms=4_000_001)
    assert fair.estimate.probability == pytest.approx(.8)
    with pytest.raises(kernel.MarketOpenError, match="no_verified_related"):
        pipeline.related_market_fair(contract, [candidate.__class__(
            **{**candidate.__dict__, "verified": False})], decision_ts_ms=4_000_001)


def test_forward_open_tape_books_decisions_manifest_and_tamper_detection(tmp_path):
    detector = pipeline.NewMarketDetector([adapter()])
    opened = detector.observe(stream())
    contract = pipeline.parse_verified_semantics(semantics(), decision_ts_ms=1_200,
                                                 adapter=adapter())
    decision = pipeline.decide_verified_open(
        contract, [lineaged_fair()], decision_ts_ms=1_200, open_ts_ms=opened.open_ts_ms,
        pm_bid=.4, pm_ask=.5, executable_cost=.01, minimum_edge=.01,
    )
    assert decision.action == "TAKE" and decision.shadow_only

    path = tmp_path / "opens.jsonl"
    tape = pipeline.ForwardOpenTape(path)
    tape.append_open(opened)
    tape.append_book(pipeline.InitialBookSnapshot("m1", 1, 1_011, 1_020, .4, .5, 10, 11),
                     open_ts_ms=opened.open_ts_ms)
    tape.append_decision(decision, decision_ts_ms=1_200, code_sha="code",
                         config_sha="config", dataset_manifest_sha="dataset",
                         fair_lineages=[lineaged_fair().lineage])
    manifest = pipeline.build_open_dataset_manifest("opens-v1", tape, collector_sha="collector")
    assert manifest.point_in_time and manifest.markets == ("m1",)
    assert manifest.receive_timestamp_coverage
    pipeline.write_open_dataset_manifest(tmp_path / "manifest.json", manifest)
    with pytest.raises(kernel.MarketOpenError, match="already_exists"):
        pipeline.write_open_dataset_manifest(tmp_path / "manifest.json", manifest)

    rows = path.read_text().splitlines()
    damaged = json.loads(rows[1])
    damaged["payload"]["ask"] = .9
    rows[1] = json.dumps(damaged)
    path.write_text("\n".join(rows) + "\n")
    with pytest.raises(kernel.MarketOpenError, match="broken_open_tape_chain"):
        tape.read_verified()


def test_race_and_edge_decay_are_measured_in_full_paper():
    race = pipeline.OpenRaceObservation("m1", 1_000, 1_010, 1_012, 1_013, 1_020,
                                        .4, .6, 3, "paper-fill-v1")
    assert race.metrics()["detection_latency_ms"] == 10
    assert race.metrics()["first_spread"] == pytest.approx(.2)
    decay = pipeline.edge_decay_summary(1_000, {1_000: .1, 2_000: .04, 3_000: .004})
    assert decay["edge_half_life_seconds"] == 1.0
    assert decay["time_to_efficient_price_seconds"] == 2.0
    with pytest.raises(kernel.MarketOpenError, match="must_remain_full_paper"):
        pipeline.OpenRaceObservation("m", 1, 2, 3, 4, 5, .4, .5, 1, "v", False).validate()
