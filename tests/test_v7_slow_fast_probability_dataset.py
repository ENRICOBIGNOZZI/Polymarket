from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_slow_fast_probability_dataset as m


def slow_cut(*, bad_future=False):
    decision = 1_000_000
    fields = {name: None for name in m.SLOW_FIELDS}
    receive = decision + 1 if bad_future else decision - 10
    fields["return_5s"] = {
        "value": 0.001,
        "receive_monotonic_ns": receive,
        "expires_monotonic_ns": decision + 100,
        "source_version": 7,
    }
    return {
        "schema": "polymarket_v7_slow_context_cut_v1",
        "fresh_mask": 1 << m.SLOW_FIELDS.index("return_5s"),
        "model_used_mask": 0,
        "decision_monotonic_ns": decision,
        "max_input_receive_monotonic_ns": receive,
        "fields": fields,
    }


def row(cut):
    return {
        "order_id": "o1", "market_id": "m1", "run_id": "r1",
        "asset": "BTC", "horizon": "M5", "decision_ts_ms": 10,
        "close_ts_ms": 20, "feature_join": "UNIQUE", "selected_outcome": 1.0,
        "features": {"direction": 1, "bid_e4": 4900, "ask_e4": 5000},
        "external_features": {"return_250ms": 0.001},
        "slow_context": cut,
    }


def test_preserves_missingness_and_causal_slow_clocks():
    rows, summary = m.build([row(slow_cut())])
    assert len(rows) == 1
    cut = rows[0]["slow_context"]
    assert cut["values"]["return_5s"] == 0.001
    assert cut["values"]["oracle_value"] is None
    assert rows[0]["slow_context_observational_only"] is True
    assert summary["slow_field_coverage"]["return_5s"]["fraction"] == 1.0
    assert summary["slow_field_coverage"]["oracle_value"]["fraction"] == 0.0
    assert summary["no_imputation"] is True


def test_rejects_future_slow_input_instead_of_reconstructing_it():
    rows, summary = m.build([row(slow_cut(bad_future=True))])
    assert rows == []
    assert summary["excluded"]["INVALID_SLOW_CAUSAL_CUT"] == 1


def test_keeps_rows_with_no_slow_context_as_explicit_missing():
    rows, summary = m.build([row(None)])
    assert len(rows) == 1
    assert rows[0]["slow_context"]["fresh_mask"] == 0
    assert all(v is None for v in rows[0]["slow_context"]["values"].values())
    assert summary["rows"] == 1
