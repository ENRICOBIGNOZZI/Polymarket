import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from v7_function_test_support import function_test_loader, raises

import v7_cross_platform_collector as cross
import v7_osint_mapping_collector as osint_mapping
import v7_sports_collector as sports
from v7_osint_engine import RawEvent, SourceTier
from v7_osint_pipeline import CausalEventTape


class KalshiClient:
    connection_epoch = 1
    reconnect_count = 0

    def __init__(self, malformed=False):
        self.malformed = malformed

    def get(self, path):
        timing = {"request_ms": 2.0, "ttfb_ms": 1.0, "connection_epoch": 1,
                  "body_sha256": "b" * 64}
        if path.startswith("/markets?"):
            return {"markets": [{"ticker": "KXTEST", "title": "Test"}]}, timing
        if self.malformed:
            return {"wrong": {}}, timing
        return {"orderbook_fp": {"yes_dollars": [["0.40", "10"]],
                                  "no_dollars": [["0.50", "8"]]}}, timing


def test_kalshi_public_polling_collects_real_books_but_not_fake_equivalence(tmp_path):
    status = cross.collect_once(
        repository_root=ROOT, config_path=ROOT / "config" / "v7_external_inputs.json",
        mappings_path=ROOT / "config" / "v7_external_mappings.json",
        tape_path=tmp_path / "books.jsonl", state_path=tmp_path / "state.json",
        status_path=tmp_path / "status.json", client=KalshiClient(), now_ms=1_800_000_000_000,
    )
    assert status["feed_status"] == "OPERATIONAL" and status["synchronized_books"] == 1
    assert status["verified_mappings"] == 0
    assert status["blocker"] == "BLOCKED_NO_VERIFIED_EQUIVALENCE"
    assert status["polling_latency_not_event_latency"] is True
    rows = [json.loads(line) for line in (tmp_path / "books.jsonl").read_text().splitlines()]
    assert any(row["kind"] == "ORDERBOOK_SNAPSHOT" for row in rows)
    assert all(row["real_order_submission"] is False for row in [status])


def test_kalshi_metadata_discovery_pages_to_exhaustion_and_polls_books(tmp_path):
    class PagedClient(KalshiClient):
        def get(self, path):
            timing = {"request_ms": 2.0, "ttfb_ms": 1.0, "connection_epoch": 1,
                      "body_sha256": "b" * 64}
            if path.startswith("/markets?"):
                if "cursor=next" in path:
                    return {"markets": [{"ticker": "KX2", "title": "Second"}], "cursor": ""}, timing
                return {"markets": [{"ticker": "KX1", "title": "First"}], "cursor": "next"}, timing
            return {"orderbook_fp": {"yes_dollars": [["0.40", "10"]],
                                      "no_dollars": [["0.50", "8"]]}}, timing
    status = cross.collect_once(
        repository_root=ROOT, config_path=ROOT / "config" / "v7_external_inputs.json",
        mappings_path=ROOT / "config" / "v7_external_mappings.json",
        tape_path=tmp_path / "books.jsonl", state_path=tmp_path / "state.json",
        status_path=tmp_path / "status.json", client=PagedClient(), now_ms=1_800_000_000_000,
    )
    assert status["discovery_exhaustive"] is True
    assert status["discovered_markets"] == status["synchronized_books"] == 2
    assert status["metadata_changes"] == 2


def test_kalshi_malformed_book_degrades_locally(tmp_path):
    status = cross.collect_once(
        repository_root=ROOT, config_path=ROOT / "config" / "v7_external_inputs.json",
        mappings_path=ROOT / "config" / "v7_external_mappings.json",
        tape_path=tmp_path / "books", state_path=tmp_path / "state",
        status_path=tmp_path / "status", client=KalshiClient(malformed=True),
        now_ms=1_800_000_000_000,
    )
    assert status["feed_status"] == "DEGRADED"
    assert status["parse_failure_count"] == 1 and not status["feed_operational"]


def test_kalshi_429_or_500_fails_only_cross_component(tmp_path):
    class FailedClient(KalshiClient):
        def get(self, _path):
            raise cross.CrossCollectorError("venue_http_status:429")
    status = cross.collect_once(
        repository_root=ROOT, config_path=ROOT / "config" / "v7_external_inputs.json",
        mappings_path=ROOT / "config" / "v7_external_mappings.json",
        tape_path=tmp_path / "books", state_path=tmp_path / "state",
        status_path=tmp_path / "status", client=FailedClient(), now_ms=1_800_000_000_000,
    )
    assert status["feed_status"] == "DOWN" and "venue_http_status:429" in status["blocker"]
    assert status["execution_authority"] is False


def test_sportradar_chunk_parser_and_normalizer_preserve_source_clock():
    objects = list(sports.iter_concatenated_json([b'{"heartbeat":{"interval":5}}', b'{"x":1}']))
    assert len(objects) == 2
    payload = {
        "payload": {
            "sport_event": {"id": "sr:game:1"},
            "sport_event_status": {"status": "live", "match_status": "1st_half",
                                   "home_score": 1, "away_score": 0},
            "timeline": [{"id": "event-1", "type": "score_change",
                          "time": "2026-08-28T20:00:00Z", "match_clock": "12:00"}],
        }
    }
    rows = sports.normalize_sportradar_payload(payload, receive_ts_ms=1_787_947_201_000,
                                               next_sequence={})
    assert rows[0].source_ts_ms == 1_787_947_200_000
    assert rows[0].receive_ts_ms == 1_787_947_201_000 and rows[0].sequence == 1
    with raises(sports.SportsCollectorError, "truncated"):
        list(sports.iter_concatenated_json([b'{"payload":']))


def test_sports_provider_interface_is_swappable_and_recovery_is_explicit():
    provider = sports._load_configuration(ROOT / "config" / "v7_external_inputs.json")
    state = {"sequences": {"game": 4}, "connection_epoch": 0}
    adapter = sports.SportradarSportsFeedAdapter(provider, state)
    with raises(sports.SportsCollectorError, "credentials_required"):
        adapter.connect(secret="")
    adapter.connect(secret="", stream=(b"{}",))
    assert "sport_event_id=sr:game:1" in adapter.subscribe(("sr:game:1",))
    assert adapter.sequence_state() == {"game": 4}
    assert adapter.reconnect() == "REST_TIMELINE_RECOVERY_REQUIRED_BEFORE_RESUBSCRIBE"
    assert state["reconnect_count"] == state["gap_count"] == 1


def test_sports_missing_secret_is_explicit_and_nonfatal(tmp_path):
    previous = os.environ.pop("PM_V7_SPORTRADAR_API_KEY", None)
    try:
        status = sports.run_session(
            repository_root=ROOT, config_path=ROOT / "config" / "v7_external_inputs.json",
            mappings_path=ROOT / "config" / "v7_external_mappings.json",
            tape_path=tmp_path / "events", state_path=tmp_path / "state",
            status_path=tmp_path / "status", now_ms=1_800_000_000_000,
        )
    finally:
        if previous is not None:
            os.environ["PM_V7_SPORTRADAR_API_KEY"] = previous
    assert status["feed_status"] == "CREDENTIALS_REQUIRED"
    assert status["missing_secret"] == "PM_V7_SPORTRADAR_API_KEY"
    assert status["implementation_complete"] and not status["feed_operational"]
    assert status["real_order_submission"] is False


def test_osint_candidates_never_become_verified_by_lexical_score(tmp_path):
    raw = RawEvent(
        "event-1", "POLICY_ANNOUNCEMENT", "Federal Reserve", "fed", SourceTier.PRIMARY,
        "source-1", "root-1", 1_799_999_000_000, 1_799_999_100_000, "a" * 64,
        content=json.dumps({"title": "Federal Reserve rate decision", "summary": "interest rate"}),
    )
    tape = tmp_path / "raw.jsonl"
    CausalEventTape(tape).append_event(raw, source_registry_sha="b" * 64)

    def fetcher(_endpoint):
        return ([{"id": "market-1", "question": "Will the Federal Reserve change the interest rate?",
                  "description": "Federal Reserve decision"}],
                {"ttfb_ms": 1, "request_ms": 2})

    status = osint_mapping.collect_once(
        repository_root=ROOT, config_path=ROOT / "config" / "v7_external_inputs.json",
        mappings_path=ROOT / "config" / "v7_external_mappings.json",
        raw_tape_path=tape, candidate_tape_path=tmp_path / "candidates.jsonl",
        state_path=tmp_path / "state.json", status_path=tmp_path / "status.json",
        now_ms=1_800_000_000_000, market_fetcher=fetcher,
    )
    assert status["candidate_mappings"] == 1 and status["verified_mappings"] == 0
    candidate = json.loads((tmp_path / "candidates.jsonl").read_text())
    assert candidate["candidate_only"] and candidate["verification_authority"] is False
    assert status["forward_collection_active"] is False


def test_osint_gamma_discovery_pages_until_short_page():
    class Response:
        headers = {}
        def __init__(self, rows): self.rows = rows
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _limit): return json.dumps(self.rows).encode()
    seen = []
    def urlopen(request, timeout):
        del timeout
        seen.append(request.full_url)
        offset = int(request.full_url.split("offset=")[1].split("&")[0])
        return Response([{"id": str(offset)}, {"id": str(offset + 1)}] if offset == 0 else [{"id": str(offset)}])
    original = osint_mapping.urllib.request.urlopen
    osint_mapping.urllib.request.urlopen = urlopen
    try:
        rows, timing = osint_mapping.fetch_markets(
            "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=2"
        )
    finally:
        osint_mapping.urllib.request.urlopen = original
    assert [row["id"] for row in rows] == ["0", "1", "2"]
    assert timing["pages"] == 2 and timing["discovery_exhaustive"] is True
    assert len(seen) == 2


def test_osint_market_discovery_failure_is_explicit_and_non_executing(tmp_path):
    def failed(_endpoint):
        raise osint_mapping.OsintMappingCollectorError("http_status:500")
    status = osint_mapping.collect_once(
        repository_root=ROOT, config_path=ROOT / "config" / "v7_external_inputs.json",
        mappings_path=ROOT / "config" / "v7_external_mappings.json",
        raw_tape_path=tmp_path / "raw", candidate_tape_path=tmp_path / "candidates",
        state_path=tmp_path / "state", status_path=tmp_path / "status",
        now_ms=1_800_000_000_000, market_fetcher=failed,
    )
    assert status["market_discovery_operational"] is False
    assert "http_status:500" in status["blocker"]
    assert status["execution_authority"] is False and status["real_order_submission"] is False


load_tests = function_test_loader(globals())
