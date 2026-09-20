from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_native_executable_dataset as m


HORIZONS = (100, 250, 500, 750, 1000, 1250, 1500, 1750, 2000)
DECISION_NS = 1_000_000_000
TICK = 100


def base_row(kind: int, *, horizon_ms=None, token="yes", direction=1):
    observed = DECISION_NS + ((horizon_ms or 0) * 1_000_000)
    row = {
        "schema": "polymarket_v7_native_observation_v1",
        "paper_only": True,
        "execution_authority": False,
        "code_sha": "a" * 40,
        "run_id": "run-1",
        "server_id": "server-1",
        "market_id": "market-1",
        "capture_id": "capture-1",
        "capture_semantics_version": 2,
        "capture_mode": "DECISION_WINDOWS",
        "connection_epoch": 1,
        "asset": "BTC",
        "horizon": "M5",
        "kind": kind,
        "reason": 1,
        "repricing_origin_signal_version": 9,
        "repricing_horizon_ms": horizon_ms,
        "decision_monotonic_ns": DECISION_NS,
        "observed_monotonic_ns": observed + 1,
        "close_monotonic_ns": 20_000_000_000,
        "decision_wall_ns": 2_000_000_000,
        "token_id": token,
        "direction": direction,
        "tick_e4": TICK,
        "paper_terms_sha256": "b" * 64,
        "fee_rate": 0.07,
        "fee_exponent": 1.0,
        "fee_source": "TEST_EXPLICIT",
        "paper_venue_delay_ns": 0,
        "paper_assumed_transport_delay_ns": 250_000_000,
        "taker_maximum_entry_price_e4": 9000,
        "minimum_order_microunits": 1_000_000,
        "proposed_quantity": 5_000_000,
        "proposed_price_tick": 50,
        "binance_return_100ms_bp": 1.0,
        "confirmation_return_100ms_bp": 0.2,
        "confirmation_venue": "COINBASE",
        "signal_age_ns": 5_000_000,
        "tte_ns": 100_000_000_000,
        "external_features": {"input_receive_ns": DECISION_NS - 1, "return_250ms": 0.0001},
        "slow_context": None,
        "repricing_pair_valid": True,
        "yes_bid_e4": 4900,
        "yes_ask_e4": 5000,
        "no_bid_e4": 5000,
        "no_ask_e4": 5100,
        "bids": [[4900, 5_000_000]],
        "asks": [[5000, 5_000_000]],
    }
    return row


def capture_rows(*, token="yes", direction=1, entry_depth=None, exit_depth=None):
    origin = base_row(2, token=token, direction=direction)
    data = [origin]
    for h in HORIZONS:
        row = base_row(6, horizon_ms=h, token=token, direction=direction)
        if h == 250 and entry_depth is not None:
            row["asks"] = entry_depth
        if h == 500:
            row["bids"] = [[5200, 10_000_000]]
        if h == 750:
            row["bids"] = exit_depth if exit_depth is not None else [[5200, 10_000_000]]
        if h == 1000:
            row["bids"] = [[5300, 10_000_000]]
        if h == 1250:
            row["bids"] = [[5300, 10_000_000]]
        if h == 1500:
            row["bids"] = [[5400, 10_000_000]]
        if h == 1750:
            row["bids"] = [[5400, 10_000_000]]
        if h == 2000:
            row["bids"] = [[5500, 10_000_000]]
        data.append(row)
    for i, row in enumerate(data, 1):
        row["sequence"] = i
    return data


def write_capture(path: Path, data):
    text = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in data)
    path.write_text(text)
    first = data[0]
    closed = {
        "schema": "polymarket_v7_native_capture_closed_v1",
        "closed": True,
        "healthy": True,
        "bytes": len(text.encode()),
        "last_sequence": len(data),
        "watermark_monotonic_ns": max(r["observed_monotonic_ns"] for r in data),
    }
    for key in ("run_id", "server_id", "market_id", "capture_id", "code_sha"):
        closed[key] = first[key]
    Path(str(path) + ".closed.json").write_text(json.dumps(closed))


def rows_for(result, model):
    return [row for row in result if row["execution_model"] == model]


def test_fee_can_reverse_positive_gross_edge(tmp_path):
    p = tmp_path / "x.jsonl"
    write_capture(p, capture_rows())
    out, summary = m.build([p], require_closed=True)
    top = rows_for(out, "PAPER_TOP_PARITY")
    target = next(r for r in top if r["exit_decision_horizon_ms"] == 500)
    assert target["status"] == "FULL_ROUND_TRIP"
    gross = 5 * (0.52 - 0.50)
    assert gross > 0
    assert float(target["net_pnl_usd"]) < 0
    assert float(target["entry"]["fee_usd"]) > 0
    assert float(target["exit"]["fee_usd"]) > 0
    assert summary["observed_fill_claim"] is False


def test_top_parity_and_visible_depth_are_not_mixed(tmp_path):
    p = tmp_path / "x.jsonl"
    depth = [[4900, 2_000_000], [5000, 3_000_000]]
    write_capture(p, capture_rows(entry_depth=depth))
    out, _ = m.build([p], require_closed=True)
    top = next(r for r in rows_for(out, "PAPER_TOP_PARITY")
               if r["exit_decision_horizon_ms"] == 500)
    full = next(r for r in rows_for(out, "VISIBLE_DEPTH_VWAP")
                if r["exit_decision_horizon_ms"] == 500)
    assert top["entry"]["filled_microunits"] == 2_000_000
    assert top["status"] == "PARTIAL_ENTRY_ROUND_TRIP"
    assert top["entry_unfilled_microunits"] == 3_000_000
    assert full["entry"]["filled_microunits"] == 5_000_000
    assert full["status"] == "FULL_ROUND_TRIP"


def test_partial_exit_is_censored_not_realized_pnl(tmp_path):
    p = tmp_path / "x.jsonl"
    write_capture(p, capture_rows(exit_depth=[[5200, 2_000_000]]))
    out, _ = m.build([p], require_closed=True)
    row = next(r for r in rows_for(out, "VISIBLE_DEPTH_VWAP")
               if r["exit_decision_horizon_ms"] == 500)
    assert row["status"] == "OPEN_RESIDUAL"
    assert row["remaining_inventory_microunits"] == 3_000_000
    assert row["net_pnl_usd"] is None
    assert float(row["cash_delta_usd"]) < 0


def test_no_fill_is_explicit_zero_not_missing(tmp_path):
    p = tmp_path / "x.jsonl"
    data = capture_rows()
    for row in data:
        if row.get("repricing_horizon_ms") == 250:
            row["asks"] = [[5100, 5_000_000]]
    write_capture(p, data)
    out, _ = m.build([p], require_closed=True)
    row = next(r for r in rows_for(out, "PAPER_TOP_PARITY")
               if r["exit_decision_horizon_ms"] == 500)
    assert row["status"] == "NO_FILL"
    assert row["net_pnl_usd"] == "0"
    assert row["entry"]["filled_microunits"] == 0


def test_down_signal_requires_no_token_depth(tmp_path):
    p = tmp_path / "x.jsonl"
    write_capture(p, capture_rows(token="no", direction=-1))
    out, _ = m.build([p], require_closed=True)
    assert out
    assert {r["token_id"] for r in out} == {"no"}
    assert {r["direction"] for r in out} == {-1}


def test_economic_token_can_differ_from_signal_direction(tmp_path):
    p = tmp_path / "x.jsonl"
    # Probability/EV may select NO even when the causal external shock is UP.
    # Token identity drives executable depth; direction remains signal provenance.
    write_capture(p, capture_rows(token="no", direction=1))
    out, _ = m.build([p], require_closed=True)
    assert out
    assert {r["token_id"] for r in out} == {"no"}
    assert {r["direction"] for r in out} == {1}


def test_old_yes_only_future_depth_cannot_complete_down_label(tmp_path):
    p = tmp_path / "x.jsonl"
    data = capture_rows(token="no", direction=-1)
    for row in data[1:]:
        row["token_id"] = "yes"
    write_capture(p, data)
    out, summary = m.build([p], require_closed=True)
    assert out == []
    assert summary["excluded"]["INVALID_OR_UNAVAILABLE_ENTRY"] == 1


@pytest.mark.parametrize("venue,transport", [(-1, 250_000_000), (0, 333_000_000)])
def test_unknown_or_off_grid_delay_is_censored(tmp_path, venue, transport):
    p = tmp_path / "x.jsonl"
    data = capture_rows()
    for row in data:
        row["paper_venue_delay_ns"] = venue
        row["paper_assumed_transport_delay_ns"] = transport
    write_capture(p, data)
    out, summary = m.build([p], require_closed=True)
    assert out == []
    assert summary["excluded"]["INVALID_OR_UNAVAILABLE_ENTRY"] == 1


def test_gap_before_exit_censors_later_round_trips(tmp_path):
    p = tmp_path / "x.jsonl"
    data = capture_rows()
    gap = base_row(5, token="yes")
    gap["observed_monotonic_ns"] = DECISION_NS + 600_000_000
    data.insert(4, gap)
    for i, row in enumerate(data, 1):
        row["sequence"] = i
    write_capture(p, data)
    out, summary = m.build([p], require_closed=True)
    assert not any(r["exit_arrival_horizon_ms"] >= 750 for r in out)
    assert summary["excluded"]["INVALID_OR_UNAVAILABLE_EXIT"] > 0


def test_exit_limit_is_frozen_at_exit_decision(tmp_path):
    p = tmp_path / "x.jsonl"
    data = capture_rows()
    # Decision at 500ms sees bid 0.52; arrival at 750ms has only 0.51.
    for row in data:
        if row.get("repricing_horizon_ms") == 750:
            row["bids"] = [[5100, 10_000_000]]
    write_capture(p, data)
    out, _ = m.build([p], require_closed=True)
    row = next(r for r in rows_for(out, "VISIBLE_DEPTH_VWAP")
               if r["exit_decision_horizon_ms"] == 500)
    assert row["exit_limit_e4"] == 5200
    assert row["status"] == "OPEN_RESIDUAL"
    assert row["exit"]["filled_microunits"] == 0


def test_fee_matches_native_runtime_formula(tmp_path):
    p = tmp_path / "x.jsonl"
    write_capture(p, capture_rows())
    out, _ = m.build([p], require_closed=True)
    row = next(r for r in rows_for(out, "PAPER_TOP_PARITY")
               if r["exit_decision_horizon_ms"] == 500)
    expected_entry = 5 * 0.07 * (0.50 * 0.50)
    expected_exit = 5 * 0.07 * (0.52 * 0.48)
    assert float(row["entry"]["fee_usd"]) == pytest.approx(expected_entry)
    assert float(row["exit"]["fee_usd"]) == pytest.approx(expected_exit)


def test_label_hash_is_deterministic(tmp_path):
    p = tmp_path / "x.jsonl"
    write_capture(p, capture_rows())
    a, _ = m.build([p], require_closed=True)
    b, _ = m.build([p], require_closed=True)
    assert a == b
    for row in a:
        payload = {k: v for k, v in row.items() if k != "label_hash"}
        assert row["label_hash"] == m.canonical_hash(payload)


def test_cli_requires_sealed_capture_and_never_overwrites(tmp_path):
    p = tmp_path / "x.jsonl"
    data = capture_rows()
    write_capture(p, data)
    out = tmp_path / "out.jsonl"
    summary = tmp_path / "summary.json"
    cmd = [sys.executable, str(ROOT / "scripts/v7_native_executable_dataset.py"),
           "--input", str(p), "--output", str(out), "--summary", str(summary)]
    first = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert first.returncode == 0, first.stderr
    assert out.exists() and summary.exists()
    second = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert second.returncode == 2
    meta = json.loads(summary.read_text())
    assert meta["observed_fill_claim"] is False
    assert meta["real_money_authority"] is False
