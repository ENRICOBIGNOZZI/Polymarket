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
    Book, entry_tte_allowed, executable_sell_value, fee_per_share,
    hybrid_probability, live_market_yes, model_market_disagreement_allowed,
    robust_candidates,
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
        "fair": {"valid": True, "yes": 0.77, "pm_mid": 0.72,
                 "gamma_discovery_mid_diagnostic": 0.485,
                 "lower": 0.75, "upper": 0.80, "tte_seconds": 45.0,
                 "calculated_monotonic_ns": current - 1, "valid_until_monotonic_ns": current + 1_000_000_000},
        "market": {"yes_token": "yes", "no_token": "no",
                   "fee_schedule": {"rate": 0.07, "exponent": 1, "takerOnly": True}},
    }


def book(token: str, ask: float, bid: float | None = None) -> Book:
    return Book(token, ((ask - 0.01 if bid is None else bid, 100.0),), ((ask, 100.0),), 0.01, 5.0,
                1_000, 1_001, f"book-{token}")


def main() -> None:
    assert abs(fee_per_share(0.5, {"rate": 0.07, "exponent": 1}) - 0.0175) < 1e-12
    assert abs(router.expected_calibration_error(
        [0.1, 0.2, 0.8, 0.9], [0.0, 1.0, 0.0, 1.0], bins=2,
    ) - 0.35) < 1e-12
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
    policy = {
        "minimum_entry_tte_seconds": 5.0, "maximum_entry_tte_seconds": 60.0,
        "maximum_model_market_disagreement": 0.20,
        "minimum_robust_ev_per_share": 0.001, "base_execution_risk_per_share": 0.0005,
    }
    live_books = {"yes": book("yes", 0.58, 0.57), "no": book("no", 0.43, 0.42)}
    assert abs(live_market_yes(live_books, snapshot()["market"]) - 0.575) < 1e-12
    assert 0.575 < hybrid_probability(0.77, 0.575, 0.35) < 0.77
    rows = robust_candidates(snapshot(), live_books, policy)
    assert len(rows) == 1 and rows[0]["outcome"] == "YES"
    assert rows[0]["robust_ev"] > 0.14
    assert rows[0]["tte_seconds"] == 45.0
    extreme = snapshot(); extreme["fair"]["pm_mid"] = 0.01
    assert not model_market_disagreement_allowed(extreme["fair"], policy)
    # The static Gamma discovery midpoint is diagnostic only. Live coherent
    # books remain authoritative for the executable disagreement gate.
    assert robust_candidates(extreme, live_books, policy)
    extreme_books = {"yes": book("yes", 0.02, 0.01), "no": book("no", 0.99, 0.98)}
    assert robust_candidates(extreme, extreme_books, policy) == []
    # Regression for the observed failure: Gamma remained near 0.485 while
    # the executable CLOB had moved to roughly 0.865 YES. A 0.595 model must
    # be rejected against the live market, irrespective of the discovery mid.
    stale_gamma = snapshot()
    stale_gamma["fair"].update({"yes": 0.595, "pm_mid": 0.485, "lower": 0.534, "upper": 0.654})
    moved_books = {"yes": book("yes", 0.87, 0.86), "no": book("no", 0.14, 0.13)}
    assert abs(live_market_yes(moved_books, stale_gamma["market"]) - 0.865) < 1e-12
    assert robust_candidates(stale_gamma, moved_books, policy) == []
    incoherent = {"yes": book("yes", 0.50), "no": book("no", 0.81)}
    assert live_market_yes(incoherent, snapshot()["market"]) is None
    assert robust_candidates(snapshot(), incoherent, policy) == []
    early = snapshot(); early["fair"]["tte_seconds"] = 60.001
    assert not entry_tte_allowed(early["fair"], policy)
    assert robust_candidates(early, {"yes": book("yes", 0.50)}, policy) == []
    boundary = snapshot(); boundary["fair"]["tte_seconds"] = 60.0
    assert entry_tte_allowed(boundary["fair"], policy)
    assert robust_candidates(boundary, live_books, policy)
    expired = snapshot(); expired["fair"]["tte_seconds"] = 4.999
    assert not entry_tte_allowed(expired["fair"], policy)
    assert robust_candidates(expired, {"yes": book("yes", 0.50)}, policy) == []
    missing_tte = snapshot(); missing_tte["fair"].pop("tte_seconds")
    assert robust_candidates(missing_tte, {"yes": book("yes", 0.50)}, policy) == []
    invalid = snapshot(); invalid["fair"]["valid"] = False
    assert robust_candidates(invalid, {"yes": book("yes", 0.50)}, policy) == []
    unsafe = snapshot(); unsafe["real_order_submission"] = True
    assert robust_candidates(unsafe, {"yes": book("yes", 0.50)}, policy) == []
    source = (ROOT / "scripts" / "v7_external_fair_paper_router.py").read_text()
    assert 'event_type="ORDER_SUBMITTED"' in source
    assert 'event_type="FILL"' in source
    assert '"economic_authority": "SHADOW_COUNTERFACTUAL"' in source
    assert '"VIRTUAL_FILL"' in source
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
                "min_order_size": "5",
                "bids": [{"price": "0.57" if item["token_id"] == "yes" else "0.42", "size": "100"}],
                "asks": [{"price": "0.58" if item["token_id"] == "yes" else "0.43", "size": "100"}],
            } for item in payload]

        paper = router.PaperRouter(
            run_root, "a" * 40, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        exploration_row = robust_candidates(
            live, live_books, paper.policy,
        )[0]
        exploration_size = paper.order_size(exploration_row)
        assert paper.model_mature is False
        assert exploration_size >= exploration_row["book"].min_order_size
        assert exploration_size * (
            exploration_row["ask"] + exploration_row["fee_per_share"]
        ) <= 10.0 + 1e-9
        with mock.patch.object(router, "request_json", side_effect=public_request):
            paper.step()
        canonical = [
            json.loads(path.read_text())
            for path in (run_root / "ledger" / "spool").glob("*.json")
        ]
        assert {event["event_type"] for event in canonical} == {
            "CANDIDATE", "ORDER_SUBMITTED", "FILL",
        }
        assert all(event["metadata"]["counterfactual"] is True for event in canonical)
        assert all(event["metadata"]["excluded_from_portfolio_equity"] is True for event in canonical)
        events = [json.loads(line) for line in (external / "counterfactuals.jsonl").read_text().splitlines()]
        event_types = {event["event_type"] for event in events}
        assert {"FORECAST", "CANDIDATE", "VIRTUAL_FILL"} <= event_types
        assert all(event["evidence_semantics_version"] == router.EVIDENCE_SEMANTICS_VERSION
                   for event in events)
        durable_tape = run_root / "paper_v7_durable" / "external_fair" / "counterfactuals.jsonl"
        assert durable_tape.exists()
        status = json.loads((external / "paper_router_status.json").read_text())
        assert status["execution_authority"] == "SHADOW_ZERO_AUTHORITY"
        assert status["order_submission_enabled"] is False
        assert status["counterfactual_collection_enabled"] is True
        assert status["fills"] == 0
        assert status["counterfactual_fills"] == 1
        assert status["counterfactual_forecasts"] == 1
        assert status["counterfactual_pending_forecasts"] == 1
        assert status["model_mature"] is False
        assert status["sizing_regime"] == "IMMATURE_SHADOW_FIXED_NOTIONAL"
        assert status["market_capital_ceiling"] == 10.0
        assert status["entry_tte_window_seconds"] == {"minimum": 5.0, "maximum": 300.0}
        assert status["book_requests"] == 2
        assert status["book_request_failures"] == 0
        assert status["book_parse_failures"] == 0
        assert abs(status["live_market"]["yes"] - 0.575) < 1e-12
        assert status["live_market"]["valid"] is True
        assert status["last_decision"]["outcome"] == "VIRTUAL_FILL"
        fill = next(event for event in events if event["event_type"] == "VIRTUAL_FILL")
        assert fill["metadata"]["tte_seconds"] == 45.0
        assert fill["metadata"]["arrival_tte_seconds"] == 45.0
        assert fill["metadata"]["robust_probability"] == 0.75
        assert fill["metadata"]["pm_mid"] == 0.575
        assert fill["metadata"]["gamma_discovery_mid_diagnostic"] == 0.485
        assert fill["metadata"]["robust_ev_per_share"] > 0.14
        assert fill["metadata"]["arrival_robust_ev_per_share"] > 0.14

        extreme_live = snapshot()
        extreme_live["market"].update({"market_id": "m-disagreement", "event_id": "e-disagreement"})
        (external / "status.json").write_text(json.dumps(extreme_live))

        def public_request_extreme(url: str, payload=None, timeout=20):
            del url, timeout
            assert isinstance(payload, list)
            return [{
                "asset_id": item["token_id"], "timestamp": str(stamp),
                "hash": f"extreme-{item['token_id']}", "tick_size": "0.01",
                "min_order_size": "5",
                "bids": [{"price": "0.01" if item["token_id"] == "yes" else "0.98", "size": "100"}],
                "asks": [{"price": "0.02" if item["token_id"] == "yes" else "0.99", "size": "100"}],
            } for item in payload]

        with mock.patch.object(router, "request_json", side_effect=public_request_extreme):
            paper.step()
        status = json.loads((external / "paper_router_status.json").read_text())
        assert status["fills"] == 0
        assert status["counterfactual_fills"] == 1
        assert status["last_decision"]["outcome"] == "MODEL_MARKET_DISAGREEMENT_LIMIT"
        assert status["rejection_reasons"]["MODEL_MARKET_DISAGREEMENT_LIMIT"] == 1

        (run_root / "control").mkdir(exist_ok=True)
        (run_root / "control" / "CUTOVER_DRAIN").write_text("{}\n")
        paper.step()
        status = json.loads((external / "paper_router_status.json").read_text())
        assert status["state"] == "DRAINING"
        assert status["drain_requested"] is True
        assert status["drain_complete"] is True
        assert status["order_submission_enabled"] is False
        assert status["blocker"] == "CUTOVER_DRAIN"
        assert status["fills"] == 0
        (run_root / "control" / "CUTOVER_DRAIN").unlink()

        failing = router.PaperRouter(
            run_root, "b" * 40, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        # A cutover changes exact execution SHA but must not erase a compatible
        # unresolved SHADOW position or its future settlement label.
        assert failing.state["counterfactual_fills"] == 1
        assert len(failing.state["positions"]) == 1
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
        settling.state["positions"] = {"position-up": {
            "position_id": "position-up", "counterfactual_id": "shadow-up", "fill_id": "fill-up",
            "order_id": "order-up",
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
        assert abs(settling.state["counterfactual_realized_pnl"] - 5.8) < 1e-12
        assert settling.state["realized_pnl"] == 0.0
        events = [json.loads(line) for line in (run_root / "external_fair" / "counterfactuals.jsonl").read_text().splitlines()]
        final = next(event for event in events if event["event_type"] == "VIRTUAL_FINAL")
        assert final["counterfactual_pnl"] == 5.8
        assert final["virtual_cashflow"] == 10.0
        assert final["metadata"]["counterfactual"] is True
        assert final["metadata"]["winning_token_id"] == "up-token"

    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        collector = router.PaperRouter(
            run_root, "d" * 40, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        observation = snapshot()
        observation["code_sha"] = "d" * 40
        observation["fair"].update({"yes": 0.60, "pm_mid": 0.20})
        observation["fair_models"] = {
            "hybrid_fair": {"yes": 0.70},
            "external_only_fair": observation["fair"],
        }
        observation["market"].update({"market_id": "forecast-market", "event_id": "forecast-event"})
        books = {"yes": book("yes", 0.81, 0.79), "no": book("no", 0.21, 0.19)}
        assert collector.record_forecast(observation, books)
        assert not collector.record_forecast(observation, books)
        pending = next(iter(collector.state["pending_forecasts"].values()))
        assert abs(pending["market_yes"] - 0.80) < 1e-12
        assert 0.60 < pending["hybrid_yes"] < 0.80
        assert pending["market_mid_source"] == "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH"
        # Simulate an exact-SHA cutover before settlement. The ephemeral state
        # is intentionally unavailable; pending identity must come from the
        # compact durable tape.
        resumed = router.PaperRouter(
            run_root, "e" * 40, ROOT / "config" / "v7_external_fair.json",
            "https://clob.invalid", "https://gamma.invalid",
        )
        assert resumed.state["forecasts"] == 1
        assert len(resumed.state["pending_forecasts"]) == 1
        resumed_pending = next(iter(resumed.state["pending_forecasts"].values()))
        assert resumed_pending["yes_token"] == "yes"
        assert resumed_pending["no_token"] == "no"
        resumed_pending["resolution_due_ms"] = router.now_ms() - 10_000
        resolution = {
            "closed": True, "clobTokenIds": '["yes", "no"]',
            "outcomePrices": '["1", "0"]',
        }
        with mock.patch.object(router, "request_json", return_value=resolution):
            resumed.observe_forecasts()
        assert resumed.state["forecasts"] == 1
        assert resumed.state["resolved_forecasts"] == 1
        assert resumed.state["pending_forecasts"] == {}
        maturity = resumed.maturity_diagnostics()
        assert maturity["independent_settlement_markets"] == 1
        assert maturity["forecast_rows"] == 1
        events = [
            json.loads(line) for line in
            (run_root / "external_fair" / "counterfactuals.jsonl").read_text().splitlines()
        ]
        final = next(event for event in events if event["event_type"] == "FORECAST_FINAL")
        assert final["actual_yes"] == 1.0
        assert final["model_brier"] > final["market_brier"]
        assert final["external_only_brier"] > final["hybrid_brier"]


if __name__ == "__main__":
    main()
