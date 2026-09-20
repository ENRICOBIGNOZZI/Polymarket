from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "monitoring"))

from v7_native_crypto_engine_manager import Manager
from exporter_v7 import render_prometheus


def status(asset, horizon, market, counts, *, accepted, rejected):
    return {
        "schema": "polymarket_v7_native_evidence_status_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": "a" * 40,
        "run_id": "run",
        "market_id": market,
        "asset": asset,
        "horizon": horizon,
        "healthy": True,
        "published": 0,
        "written": 0,
        "dropped": 0,
        "queue_depth": 0,
        "observations_published": accepted + rejected,
        "observations_written": accepted + rejected,
        "observations_dropped": 0,
        "observations_queue_depth": 0,
        "decision_observations": accepted + rejected,
        "accepted_decision_observations": accepted,
        "rejected_decision_observations": rejected,
        "decision_reason_counts": counts,
    }


def test_native_manager_aggregates_funnel_by_context(tmp_path):
    directory = tmp_path / "control" / "native_evidence"
    directory.mkdir(parents=True)
    (directory / "m1.json").write_text(json.dumps(
        status("BTC", "M5", "m1", {"ACCEPTED": 2, "MARKET_ALREADY_REPRICED": 3},
               accepted=2, rejected=3)))
    (directory / "m2.json").write_text(json.dumps(
        status("ETH", "M15", "m2", {"ACCEPTED": 1, "WEAK_SIGNAL": 4},
               accepted=1, rejected=4)))

    manager = Manager.__new__(Manager)
    manager.run_root = tmp_path
    manager.args = SimpleNamespace(model_sha="a" * 40, run_id="run")
    aggregate = manager._aggregate_evidence()

    assert aggregate["decision_observations"] == 10
    assert aggregate["accepted_decision_observations"] == 3
    assert aggregate["rejected_decision_observations"] == 7
    assert aggregate["decision_reason_counts"]["ACCEPTED"] == 3
    assert aggregate["context_decision_reason_counts"]["BTC:M5"]["MARKET_ALREADY_REPRICED"] == 3
    assert aggregate["context_decision_reason_counts"]["ETH:M15"]["WEAK_SIGNAL"] == 4


def test_exporter_emits_native_reason_and_context_metrics():
    native = {
        "native_decision_observations": 10,
        "native_accepted_decision_observations": 3,
        "native_rejected_decision_observations": 7,
        "native_decision_reason_counts": {"ACCEPTED": 3, "MARKET_ALREADY_REPRICED": 3},
        "native_context_decision_reason_counts": {
            "BTC:M5": {"ACCEPTED": 2, "MARKET_ALREADY_REPRICED": 3},
        },
    }
    text = render_prometheus({"native_engine_manager": native})
    assert 'polymarket_v7_native_decision_reason_total{reason="ACCEPTED"} 3' in text
    assert (
        'polymarket_v7_native_context_decision_reason_total'
        '{asset="BTC",horizon="M5",reason="MARKET_ALREADY_REPRICED"} 3'
    ) in text
