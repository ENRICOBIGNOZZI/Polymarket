import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from v7_function_test_support import function_test_loader
import v7_osint_collector as c
import v7_osint_engine as o


def test_checked_in_registry_is_authoritative_but_statistics_are_not_claimed_empirical():
    registry = o.CollectorSourceCatalog.load(ROOT / "config" / "v7_osint_sources.json")
    assert len(registry.enabled()) >= 3
    assert all(x.authority_tier <= o.SourceTier.VERIFIED_PROVIDER for x in registry.enabled())
    assert all(x.statistics_status == "PRIOR_UNVALIDATED" for x in registry.enabled())
    assert all(x.endpoint.startswith("https://") for x in registry.enabled())


def test_collector_preserves_receive_time_dedup_and_corrections(tmp_path):
    source = o.CollectorSourceCatalog.load(ROOT / "config" / "v7_osint_sources.json").get(
        "us_federal_register_documents"
    )
    registry = o.CollectorSourceCatalog((source,))
    payloads = [
        {"results": [{"document_number": "2026-1", "publication_date": "2026-08-28",
                      "title": "Rule", "abstract": "first", "html_url": "https://example/1"}]},
        {"results": [{"document_number": "2026-1", "publication_date": "2026-08-28",
                      "title": "Rule", "abstract": "corrected", "html_url": "https://example/1"}]},
    ]

    def fetcher(_source, conditional, _timeout):
        index = 0 if not conditional.get("seen") else 1
        return c.HttpResponse(200, {"ETag": f'"{index}"'}, json.dumps(payloads[index]).encode())

    tape, state = tmp_path / "tape.jsonl", tmp_path / "state.json"
    first_receive = c.timestamp_ms("2026-08-28T12:00:00Z")
    first = c.collect_once(registry, tape_path=tape, state_path=state, fetcher=fetcher,
                           now_ms=first_receive)
    second = c.collect_once(registry, tape_path=tape, state_path=state, fetcher=fetcher,
                            now_ms=first_receive + 1_000)
    rows = [json.loads(x)["payload"] for x in tape.read_text().splitlines()]
    assert first["new_events"] == second["new_events"] == 1
    assert len(rows) == 2 and rows[0]["root_lineage_id"] == rows[1]["root_lineage_id"]
    assert rows[1]["correction_of"] == rows[0]["event_id"]
    assert rows[0]["received_ts_ms"] < rows[1]["received_ts_ms"]
    assert all(x["source_tier"] == int(source.authority_tier) for x in rows)
    assert all(len(x["source_registry_sha"]) == 64 for x in rows)


def test_atom_parser_and_304_are_supported(tmp_path):
    xml = b"""<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>x</id>
      <published>2026-08-28T10:00:00Z</published><title>Filing</title>
      <summary>Body</summary><link href='https://sec.gov/x'/></entry></feed>"""
    rows = c.parse_xml_feed(xml, atom=True)
    assert rows[0]["source_event_id"] == "x" and rows[0]["url"] == "https://sec.gov/x"
    source = o.CollectorSourceCatalog.load(ROOT / "config" / "v7_osint_sources.json").get(
        "sec_edgar_current_filings"
    )
    result = c.collect_once(
        o.CollectorSourceCatalog((source,)), tape_path=tmp_path / "tape", state_path=tmp_path / "state",
        fetcher=lambda *_: c.HttpResponse(304, {}, b""), now_ms=1_788_000_000_000,
    )
    assert result["new_events"] == 0 and result["healthy_sources"] == 1


def test_tape_recovers_dedup_state_after_append_before_state_crash(tmp_path):
    source = o.CollectorSourceCatalog.load(ROOT / "config" / "v7_osint_sources.json").get(
        "us_federal_register_documents"
    )
    receive = c.timestamp_ms("2026-08-28T12:00:00Z")
    item = {"source_event_id": "doc", "published_ts_ms": receive - 1000,
            "title": "same", "summary": "same", "url": "https://example/doc"}
    event = c.materialize_events(source, [item], received_ts_ms=receive,
                                 source_state={}, connection_epoch=1)[0]
    tape, state = tmp_path / "tape", tmp_path / "state"
    authority = c.SourceRegistry((c.AuthoritativeSource(
        source.source_id, source.entity, source.authority_tier,
        (c._origin(source.endpoint),), source.event_types, source.adapter_version,
    ),))
    c.append_tape(tape, [event], source_registry_sha=authority.registry_sha)

    def fetcher(_source, _conditional, _timeout):
        body = {"results": [{"document_number": "doc", "publication_date": "2026-08-28",
                             "title": "same", "abstract": "same", "html_url": "https://example/doc"}]}
        return c.HttpResponse(200, {}, json.dumps(body).encode())

    result = c.collect_once(o.CollectorSourceCatalog((source,)), tape_path=tape, state_path=state,
                            fetcher=fetcher, now_ms=receive)
    assert result["new_events"] == 0
    assert len(tape.read_text().splitlines()) == 1
    recovered = json.loads(state.read_text())["sources"][source.source_id]["seen"]["doc"]
    assert recovered["event_id"] == event.event_id


def test_likelihood_fit_is_chronological_independent_and_oos_gated():
    train = []
    test = []
    for index in range(24):
        outcome = index % 2
        direction = 1 if outcome else -1
        train.append(o.LikelihoodObservation("COURT_RULING", f"train-{index}", 1000 + index,
                                             direction, outcome, .5))
        test.append(o.LikelihoodObservation("COURT_RULING", f"test-{index}", 3000 + index,
                                            direction, outcome, .5))
    model, report = o.fit_likelihood_model(
        "COURT_RULING", train, trained_until_ms=2000, oos_rows=test, minimum_oos_events=20
    )
    assert model.frozen and model.oos_validated and model.log_likelihood_ratio > 0
    assert report is not None and report.independent_events == 24
    assert report.brier < report.baseline_brier


def test_normalization_rejects_source_family_mismatch():
    registry = o.CollectorSourceCatalog.load(ROOT / "config" / "v7_osint_sources.json")
    source = registry.get("federal_reserve_press_all")
    event = o.RawEvent("e", "COURT_RULING", "fed", source.source_id, source.authority_tier,
                       "x", "root", 1000, 1010, "hash")
    try:
        o.normalize_event(event, source, decision_ts_ms=1100)
    except o.OsintError as exc:
        assert str(exc) == "source_not_authorized_for_event_family"
    else:
        raise AssertionError("mismatched event family must fail closed")


def test_runtime_wires_only_the_research_collector_not_an_execution_bridge():
    runtime = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text()
    assert "scripts/v7_osint_collector.py" in runtime
    assert '--tape "$RUN_ROOT/osint/raw_events.jsonl"' in runtime
    assert "Research-only official-source OSINT tape" in runtime
    assert "PM_V7_OSINT_EXECUTION" not in runtime


load_tests = function_test_loader(globals())

if __name__ == "__main__":
    unittest.main()
