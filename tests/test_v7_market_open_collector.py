import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from v7_function_test_support import function_test_loader
import v7_market_open_collector as c


def market(market_id, *, quote=True, liquidity=10, volume=0):
    return {
        "id": market_id, "conditionId": f"condition-{market_id}", "question": f"Question {market_id}",
        "description": "Rules", "resolutionSource": "official", "createdAt": "2026-08-28T10:00:00Z",
        "active": True, "acceptingOrders": True,
        "bestBid": .4 if quote else None, "bestAsk": .6 if quote else None,
        "liquidityNum": liquidity, "volumeNum": volume,
        "clobTokenIds": json.dumps([f"yes-{market_id}", f"no-{market_id}"]),
        "events": [{"id": f"event-{market_id}"}],
    }


def test_initial_snapshot_is_baseline_not_fake_market_creation(tmp_path):
    tape, state = tmp_path / "tape", tmp_path / "state"
    fetcher = lambda *_: [market("old")]
    result = c.collect_once(gamma_url="https://gamma", limit=10, tape_path=tape,
                            state_path=state, fetcher=fetcher, now_ms=1_788_000_000_000)
    assert result["bootstrap"] and result["new_markets"] == 0
    rows = [json.loads(x)["payload"] for x in tape.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["milestone"] == c.BASELINE_MILESTONE


def test_only_post_bootstrap_market_emits_each_causal_milestone_once(tmp_path):
    tape, state = tmp_path / "tape", tmp_path / "state"
    snapshots = [[market("old")], [market("old"), market("new", volume=5)],
                 [market("old"), market("new", volume=5)]]
    index = {"value": 0}

    def fetcher(*_):
        value = snapshots[index["value"]]
        index["value"] += 1
        return value

    for offset in range(3):
        c.collect_once(gamma_url="https://gamma", limit=10, tape_path=tape,
                       state_path=state, fetcher=fetcher, now_ms=1_788_000_000_000 + offset)
    rows = [json.loads(x)["payload"] for x in tape.read_text().splitlines()]
    open_rows = [x for x in rows if x["milestone"] != c.BASELINE_MILESTONE]
    assert {x["market_id"] for x in open_rows} == {"new"}
    assert {x["milestone"] for x in open_rows} == set(c.MILESTONES)
    assert len({x["event_id"] for x in open_rows}) == len(c.MILESTONES)
    assert all(x["semantic_verification"] == "UNVERIFIED" for x in rows)
    assert all(x["authority"] == "RESEARCH" and not x["real_order_submission"] for x in rows)
    assert all(len(x["previous_hash"]) == 64 and len(x["record_hash"]) == 64
               for x in map(json.loads, tape.read_text().splitlines()))


def test_malformed_book_is_rejected_without_stopping_other_markets(tmp_path):
    bad = market("bad"); bad["bestBid"], bad["bestAsk"] = .9, .1
    result = c.collect_once(
        gamma_url="https://gamma", limit=10, tape_path=tmp_path / "tape",
        state_path=tmp_path / "state", fetcher=lambda *_: [bad, market("good")],
        now_ms=1_788_000_000_000,
    )
    assert result["rejected_observations"] == 1 and result["observed_markets"] == 1


def test_tape_recovers_baseline_and_milestones_after_state_loss(tmp_path):
    tape, state = tmp_path / "tape", tmp_path / "state"
    c.collect_once(gamma_url="https://gamma", limit=10, tape_path=tape, state_path=state,
                   fetcher=lambda *_: [market("old")], now_ms=1_788_000_000_000)
    state.unlink()
    result = c.collect_once(gamma_url="https://gamma", limit=10, tape_path=tape, state_path=state,
                            fetcher=lambda *_: [market("old")], now_ms=1_788_000_001_000)
    assert result["bootstrap"] is False and result["new_markets"] == 0
    assert len(tape.read_text().splitlines()) == 1


load_tests = function_test_loader(globals())

if __name__ == "__main__":
    unittest.main()
