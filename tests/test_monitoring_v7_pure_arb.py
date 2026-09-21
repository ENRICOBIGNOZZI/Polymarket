from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"
if str(MONITORING) not in sys.path:
    sys.path.insert(0, str(MONITORING))

import exporter_v7 as exporter  # noqa: E402


def test_canonical_exporter_emits_pure_arb_metrics() -> None:
    status = {
        "schema": "polymarket_v7_pure_arb_paper_status_v1",
        "state": "running",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "cycles_total": 3,
        "paper_locked_pnl_pre_gas_total": 1.25,
        "evaluations": 100,
        "fee_blocked_evaluations": 2,
        "fee_ready_contexts": 30,
        "last_decision_compute_ns": 1200,
        "max_decision_compute_ns": 9000,
        "last_receive_to_decision_ns": 18000,
        "max_receive_to_decision_ns": 42000,
        "contexts": [{
            "asset": "BTC",
            "horizon": "M5",
            "buy_complete_set": {
                "active": True,
                "cycles": 2,
                "paper_locked_pnl_pre_gas": 1.0,
                "last_edge_per_share": 0.002,
                "max_edge_per_share": 0.003,
                "last_executable_shares_l1": 12.0,
                "last_locked_pnl_pre_gas": 0.024,
            },
            "sell_complete_set": {
                "active": False,
                "cycles": 1,
                "paper_locked_pnl_pre_gas": 0.25,
                "last_edge_per_share": -0.001,
                "max_edge_per_share": 0.001,
                "last_executable_shares_l1": 5.0,
                "last_locked_pnl_pre_gas": 0.005,
            },
        }],
    }
    rendered = "\n".join(exporter._render_pure_arb_metrics(status))
    assert "polymarket_pure_arb_up 1" in rendered
    assert "polymarket_pure_arb_cycles_total 3" in rendered
    assert "polymarket_pure_arb_paper_locked_pnl_pre_gas_usd_total 1.25" in rendered
    assert 'asset="BTC"' in rendered
    assert 'horizon="M5"' in rendered
    assert 'kind="BUY_COMPLETE_SET"' in rendered
    assert "polymarket_pure_arb_context_last_edge_per_share" in rendered


def test_unsafe_status_never_exports_context_economics() -> None:
    status = {
        "schema": "polymarket_v7_pure_arb_paper_status_v1",
        "state": "running",
        "paper_only": False,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "contexts": [{
            "asset": "BTC", "horizon": "M5",
            "buy_complete_set": {"active": True, "cycles": 99},
        }],
    }
    rendered = "\n".join(exporter._render_pure_arb_metrics(status))
    assert "polymarket_pure_arb_up 0" in rendered
    assert "polymarket_pure_arb_context_cycles_total" not in rendered


if __name__ == "__main__":
    test_canonical_exporter_emits_pure_arb_metrics()
    test_unsafe_status_never_exports_context_economics()
