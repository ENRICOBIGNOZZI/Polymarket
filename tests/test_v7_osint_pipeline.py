import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v7_function_test_support import function_test_loader, raises

import v7_osint_engine as kernel
import v7_osint_pipeline as pipeline


def registry():
    return pipeline.SourceRegistry([
        pipeline.AuthoritativeSource(
            "court", "Example Supreme Court", kernel.SourceTier.PRIMARY,
            ("https://court.example.gov",), ("COURT_RULING",), "court-json-v1",
        )
    ])


def document(**overrides):
    values = {
        "source_id": "court", "source_url": "https://court.example.gov/releases/1",
        "source_event_id": "docket-1", "root_lineage_id": "case-1",
        "event_family": "COURT_RULING", "entity": "case", "published_ts_ms": 1_000,
        "received_ts_ms": 1_010, "payload": b"official ruling",
    }
    values.update(overrides)
    return pipeline.SourceDocument(**values)


def link():
    return kernel.EventMarketLink("COURT_RULING", "court", 1, "ruling controls condition",
                                  1, True, "HUMAN_RULES_REVIEW")


def row(index, outcome, *, event_received, label_received):
    event = pipeline.ingest_document(registry(), document(
        source_event_id=f"docket-{index}", root_lineage_id=f"case-{index}",
        published_ts_ms=event_received - 10, received_ts_ms=event_received,
        payload=f"official ruling {index}".encode(),
    ))
    label = pipeline.ingest_resolved_label(registry(), event, pipeline.ResolvedLabelDocument(
        f"market-{index}", "court", outcome, event.published_ts_ms + 1_000, label_received,
        "court", "https://court.example.gov/resolutions", f"resolution-{index}",
        f"official resolution {index}".encode(),
    ))
    return pipeline.EventFamilyRow(event, label, link())


def test_authoritative_adapter_is_deny_by_default_and_hash_verified():
    event = pipeline.ingest_document(registry(), document())
    assert event.source_tier == kernel.SourceTier.PRIMARY
    assert event.payload_hash
    with raises(kernel.OsintError, "source_origin_not_allowed"):
        pipeline.ingest_document(registry(), document(source_url="https://copy.example/release"))
    with raises(kernel.OsintError, "source_payload_hash_mismatch"):
        pipeline.ingest_document(registry(), document(advertised_payload_sha256="0" * 64))


def test_corroboration_is_causal_and_counts_independent_lineage_once():
    event = pipeline.ingest_document(registry(), document())
    independent = replace(event, event_id="other", source_id="regulator",
                          root_lineage_id="independent", payload_hash="other-hash")
    copied = replace(event, event_id="copy", source_id="media")
    future = replace(independent, event_id="future", source_id="authority-2",
                     root_lineage_id="future-root", received_ts_ms=2_000)
    result = pipeline.corroborate(event, [copied, independent, future], decision_ts_ms=1_500)
    assert result.independent_events == (independent,)
    assert result.independent_source_count == 1
    assert {reason for _, reason in result.rejected} == {
        "nonindependent_corroboration", "future_event_used",
    }


def test_causal_event_tape_detects_history_tampering(tmp_path):
    event = pipeline.ingest_document(registry(), document())
    path = tmp_path / "event.jsonl"
    tape = pipeline.CausalEventTape(path)
    first = tape.append_event(event, source_registry_sha=registry().registry_sha)
    assert tape.read_verified()[0].record_hash == first.record_hash
    wire = json.loads(path.read_text().strip())
    wire["payload"]["entity"] = "tampered"
    path.write_text(json.dumps(wire) + "\n")
    with raises(kernel.OsintError, "event_tape_hash_mismatch"):
        tape.read_verified()


def test_dataset_fit_is_point_in_time_chronological_and_forward_shadow(tmp_path):
    training = [row(i, i % 4 != 0, event_received=1_000 + i, label_received=2_000 + i)
                for i in range(24)]
    oos = [row(100 + i, i % 3 != 0, event_received=4_000 + i,
               label_received=5_000 + i) for i in range(12)]
    rows = training + oos
    manifest = pipeline.build_dataset_manifest(
        "court-v1", rows, source_files={"events.jsonl": b"immutable raw bytes"},
        collector_sha="collector-sha",
    )
    fitted = pipeline.fit_chronological_oos(
        rows, manifest, training_end_ms=3_000, event_family="COURT_RULING",
        source_tier=kernel.SourceTier.PRIMARY, market_family="court",
        time_to_resolution_bucket="LE_1H", minimum_training_rows=20, minimum_oos_rows=10,
    )
    assert fitted.model.frozen and fitted.model.oos_validated
    assert fitted.training_rows == 24 and fitted.oos_rows == 12
    assert fitted.training_dataset_sha == manifest.dataset_sha
    artifact = tmp_path / "court-v1.json"
    pipeline.write_dataset_artifact(artifact, manifest, rows)
    with raises(kernel.OsintError, "already_exists"):
        pipeline.write_dataset_artifact(artifact, manifest, rows)

    future_event = oos[0].event
    decision = kernel.update_probability(
        market_id=oos[0].label.market_id, prior=.5, event=future_event, link=link(),
        model=fitted.model, decision_ts_ms=4_100, pm_bid=.4, pm_ask=.5,
    )
    tape = pipeline.CausalEventTape(tmp_path / "shadow.jsonl")
    recorded = pipeline.record_forward_shadow(
        tape, decision=decision, fitted=fitted, decision_ts_ms=4_100,
        code_sha="code-sha", config_sha="config-sha",
    )
    label = replace(oos[0].label, resolved_ts_ms=4_500, received_ts_ms=5_000)
    evaluated = pipeline.evaluate_forward_shadow(tape, label,
                                                  decision_record_hash=recorded.record_hash)
    assert evaluated.kind == "FORWARD_SHADOW_LABEL"
    assert tape.read_verified()[-1].payload["decision_record_hash"] == recorded.record_hash


def test_llm_can_extract_but_cannot_verify_labels():
    event = pipeline.ingest_document(registry(), document(extracted_by_llm=True))
    label = pipeline.ResolvedLabel("l", event.event_id, "m", "court", True, 2_000, 2_001,
                                      "court", "r", "hash", True, "LLM", registry().registry_sha)
    with raises(kernel.OsintError, "llm_cannot_verify_label"):
        label.validate(event)


def test_forward_reaction_tape_requires_verified_mapping_and_causal_horizon(tmp_path):
    point = pipeline.ForwardReactionPoint(
        "event", "mapping", "market", 1_000, 250, 1_250,
        .40, .45, 10, 12, .42, .60, .15, .12, .01, .02, None, 7, "a" * 40,
    )
    tape = pipeline.ForwardReactionTape(tmp_path / "reaction.jsonl")
    with raises(kernel.OsintError, "verified_mapping"):
        tape.append(point, mapping_verified=False)
    record_hash = tape.append(point, mapping_verified=True)
    row = json.loads((tmp_path / "reaction.jsonl").read_text())
    assert row["record_hash"] == record_hash and row["horizon_ms"] == 250
    with raises(kernel.OsintError, "duplicate"):
        tape.append(point, mapping_verified=True)
    with raises(kernel.OsintError, "noncausal"):
        pipeline.ForwardReactionTape(tmp_path / "bad").append(
            replace(point, observed_ts_ms=1_249), mapping_verified=True,
        )


load_tests = function_test_loader(globals())

if __name__ == "__main__":
    unittest.main()
