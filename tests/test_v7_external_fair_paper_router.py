#!/usr/bin/env python3
from __future__ import annotations

import sys
import json
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_external_fair_paper_router as router  # noqa: E402
from v7_external_fair_paper_router import (  # noqa: E402
    Book, executable_sell_value, fee_per_share, robust_candidates,
)


def snapshot() -> dict:
    current = time.monotonic_ns()
    return {
        "code_sha": "a" * 40, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False,
        "contract": {"verified": True, "rules_hash_recognized": True},
        "settlement_reference": {"valid": True},
        "oracle": {"healthy": True, "continuity": "LIVE_CONTINUOUS"},
        "external": {"healthy": True},
        "fair": {"valid": True, "lower": 0.75, "upper": 0.80,
                 "calculated_monotonic_ns": current - 1, "valid_until_monotonic_ns": current + 1_000_000_000},
        "market": {"yes_token": "yes", "no_token": "no",
                   "fee_schedule": {"rate": 0.07, "exponent": 1, "takerOnly": True}},
    }


def book(token: str, ask: float) -> Book:
    return Book(token, ((ask - 0.01, 100.0),), ((ask, 100.0),), 0.01, 5.0,
                1_000, 1_001, f"book-{token}")


def main() -> None:
    assert abs(fee_per_share(0.5, {"rate": 0.07, "exponent": 1}) - 0.0175) < 1e-12
    depth_book = Book("yes", ((0.50, 2.0), (0.49, 3.0)), ((0.51, 10.0),), 0.01, 1.0, 1, 2, "depth")
    expected = 2.0 * (0.50 - 0.0175) + 3.0 * (0.49 - 0.07 * 0.49 * 0.51)
    assert abs(executable_sell_value(depth_book, 5.0, {"rate": 0.07, "exponent": 1}) - expected) < 1e-12
    assert executable_sell_value(depth_book, 6.0, {"rate": 0.07, "exponent": 1}) == 0.0
    receive_ms = 1_788_032_000_000
    raw_book = {
        "asset_id": "yes", "timestamp": str(receive_ms + 15), "hash": "clock-skew",
        "tick_size": "0.01", "min_order_size": "5",
        "bids": [{"price": "0.49", "size": "10"}],
        "asks": [{"price": "0.50", "size": "10"}],
    }
    skewed = router.parse_book(raw_book, receive_ms)
    assert skewed is not None and skewed.exchange_ts_ms == receive_ms
    raw_book["timestamp"] = str(receive_ms + router.MAX_CLOB_CLOCK_SKEW_MS + 1)
    assert router.parse_book(raw_book, receive_ms) is None
    raw_book["timestamp"] = str(receive_ms)
    raw_book["asks"] = []
    one_sided = router.parse_book(raw_book, receive_ms)
    assert one_sided is not None and one_sided.bids and not one_sided.asks
    assert robust_candidates(snapshot(), {"yes": one_sided}, {
        "minimum_robust_ev_per_share": 0.001, "base_execution_risk_per_share": 0.0005,
    }) == []
    raw_book["bids"] = []
    assert router.parse_book(raw_book, receive_ms) is None
    policy = {"minimum_robust_ev_per_share": 0.001, "base_execution_risk_per_share": 0.0005}
    rows = robust_candidates(snapshot(), {"yes": book("yes", 0.50), "no": book("no", 0.81)}, policy)
    assert len(rows) == 1 and rows[0]["outcome"] == "YES"
    assert rows[0]["robust_ev"] > 0.20
    invalid = snapshot(); invalid["fair"]["valid"] = False
    assert robust_candidates(invalid, {"yes": book("yes", 0.50)}, policy) == []
    unsafe = snapshot(); unsafe["real_order_submission"] = True
    assert robust_candidates(unsafe, {"yes": book("yes", 0.50)}, policy) == []
    source = (ROOT / "scripts" / "v7_external_fair_paper_router.py").read_text()
    assert 'event_type="ORDER_SUBMITTED"' in source
    assert 'event_type="FILL"' in source
    assert "api_key" not in source.lower()
    assert "signature" not in source.lower()

    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        external = run_root / "external_fair"
        external.mkdir(parents=True)
        live = snapshot()
        live["market"].update({"market_id": "m1", "event_id": "e1"})
        (external / "status.json").write_text(json.dumps(live))
        stamp = router.now_ms() - 1

        def public_request(url: str, payload=None, timeout=20):
            del url, timeout
            assert isinstance(payload, list)
            return [{
                "asset_id": item["token_id"], "timestamp": str(stamp),
                "hash": f"hash-{item['token_id']}", "tick_size": "0.01",
                "min_order_size": "5", "bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.50" if item["token_id"] == "yes" else "0.81", "size": "100"}],
            } for item in payload]

        paper = router.PaperRouter(
            run_root, "a" * 40, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        exploration_row = robust_candidates(
            live, {"yes": book("yes", 0.50), "no": book("no", 0.81)}, paper.policy,
        )[0]
        exploration_size = paper.order_size(exploration_row)
        assert paper.model_mature is False
        assert exploration_size >= exploration_row["book"].min_order_size
        assert exploration_size * (
            exploration_row["ask"] + exploration_row["fee_per_share"]
        ) <= 10.0 + 1e-9
        with mock.patch.object(router, "request_json", side_effect=public_request):
            paper.step()
        events = [json.loads(path.read_text()) for path in (run_root / "ledger" / "spool").glob("*.json")]
        event_types = {event["event_type"] for event in events}
        assert {"CANDIDATE", "ORDER_SUBMITTED", "ORDER_STATE", "FILL"} <= event_types
        status = json.loads((external / "paper_router_status.json").read_text())
        assert status["execution_authority"] == "PAPER_EXECUTION_OWNER"
        assert status["order_submission_enabled"] is True
        assert status["fills"] == 1
        assert status["model_mature"] is False
        assert status["sizing_regime"] == "IMMATURE_FIXED_EXPLORATION"
        assert status["market_capital_ceiling"] == 10.0
        assert status["book_requests"] == 2
        assert status["book_request_failures"] == 0
        assert status["book_parse_failures"] == 0
        assert status["last_decision"]["outcome"] == "FILLED"

        (run_root / "control").mkdir(exist_ok=True)
        (run_root / "control" / "CUTOVER_DRAIN").write_text("{}\n")
        paper.step()
        status = json.loads((external / "paper_router_status.json").read_text())
        assert status["state"] == "DRAINING"
        assert status["drain_requested"] is True
        assert status["drain_complete"] is False
        assert status["order_submission_enabled"] is False
        assert status["blocker"] == "CUTOVER_DRAIN"
        assert status["fills"] == 1
        (run_root / "control" / "CUTOVER_DRAIN").unlink()

        failing = router.PaperRouter(
            run_root, "b" * 40, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        failed_status = snapshot()
        failed_status["code_sha"] = "b" * 40
        failed_status["market"].update({"market_id": "m2", "event_id": "e2"})
        (external / "status.json").write_text(json.dumps(failed_status))
        with mock.patch.object(router, "request_json", side_effect=TimeoutError("bounded")):
            failing.step()
        status = json.loads((external / "paper_router_status.json").read_text())
        assert status["book_requests"] == 1
        assert status["book_request_failures"] == 1
        assert status["last_decision"]["outcome"] == "CLOB_BOOK_REQUEST_TIMEOUTERROR"
        assert status["rejection_reasons"]["CLOB_BOOK_REQUEST_TIMEOUTERROR"] == 1

    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        settling = router.PaperRouter(
            run_root, "c" * 40, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        opened_ms = router.now_ms() - 301_000
        settling.state["cash"] = 90.0
        settling.state["positions"] = {"position-up": {
            "position_id": "position-up", "order_id": "order-up", "fill_id": "fill-up",
            "market_id": "market-up-down", "event_id": "event-up-down", "token_id": "up-token",
            "outcome": "YES", "shares": 10.0, "entry_price": 0.4, "entry_cost": 4.0,
            "entry_fee": 0.2, "executable_value": 0.0, "opened_ms": opened_ms,
            "fee_schedule": {"rate": 0.07, "exponent": 1},
            "markouts": list(router.HORIZONS), "settled": False,
        }}
        resolution = {
            "closed": True, "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up-token", "down-token"]', "outcomePrices": '["1", "0"]',
        }
        with mock.patch.object(router, "request_json", return_value=resolution):
            settling.observe_positions()
        position = settling.state["positions"]["position-up"]
        assert position["settled"] is True and position["resolved_outcome"] == "Up"
        assert abs(settling.state["realized_pnl"] - 5.8) < 1e-12
        events = [json.loads(path.read_text()) for path in (run_root / "ledger" / "spool").glob("*.json")]
        final = next(event for event in events if event["event_type"] == "FINAL")
        assert final["final_pnl"] == 5.8
        assert final["realized_cashflow"] == 10.0
        assert final["unwind_loss"] == 0.0
        assert final["capital_cost"] == 0.0
        assert final["latency_cost"] == 0.0
        assert final["metadata"]["realized"] is True
        assert final["metadata"]["cost_vector_complete"] is True
        assert final["metadata"]["unwind_accounted"] is True
        assert final["metadata"]["winning_token_id"] == "up-token"


if __name__ == "__main__":
    main()
