#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

import exporter_v7_external  # noqa: E402
from v7_external_fair import summarize_external_fair  # noqa: E402

SHA = "1" * 40


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def status(authority: str = "SHADOW_ZERO_AUTHORITY") -> dict:
    return {
        "code_sha": SHA,
        "execution_authority": authority,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "external_fair_required_markets": 1,
        "contract": {
            "verified": True,
            "rules_hash_recognized": True,
            "rules_hash": "a" * 64,
            "oracle_window_seconds": 60,
        },
        "settlement_reference": {"valid": True, "value": 65000.0, "version": 3},
        "oracle": {
            "healthy": True,
            "value": 65001.0,
            "age_ns": 5_000_000,
            "continuity": "LIVE_CONTINUOUS",
            "connection_epoch": 2,
            "reconnects": 0,
            "gaps": 0,
        },
        "external": {
            "healthy": True,
            "fresh_venue_count": 3,
            "dispersion_bps": 1.5,
            "age_ns": 4_000_000,
            "venues": [
                {"venue": "BINANCE_SPOT", "healthy": True, "age_ns": 1_000_000,
                 "price": 65002.0, "microprice": 65002.1, "spread_bps": 0.2,
                 "weight": 0.34, "basis_bps": 0.1, "actionable_lead_ms": 4.0,
                 "economic_lead_ms": 5.0, "disabled": False},
            ],
        },
        "fair": {
            "valid": True,
            "yes": 0.70,
            "lower": 0.65,
            "upper": 0.75,
            "structural": 0.69,
            "calibrated": 0.70,
            "micro_logit_adjustment": 0.02,
            "pm_mid": 0.55,
            "tte_seconds": 30.0,
            "settlement_margin": 25.0,
            "settlement_sigma": 50.0,
            "calculated_monotonic_ns": 100,
            "valid_until_monotonic_ns": 200,
        },
        "actions": {"MAKE": 1, "TAKE": 2, "CANCEL": 3, "WITHDRAW": 4, "NOTHING": 5},
        "counterfactual_actions": {"TAKE": 7},
        "paper_router": {
            "active_candidates": 1, "orders_submitted": 2, "fills": 1,
            "open_positions": 0, "cash": 4000.8,
            "equity": 4000.8, "realized_pnl": 0.8,
            "counterfactual_collection_enabled": True,
            "counterfactual_candidates": 8, "counterfactual_fills": 7,
            "counterfactual_open_positions": 2,
            "counterfactual_forecasts": 11, "counterfactual_resolved_forecasts": 6,
            "counterfactual_pending_forecasts": 5,
            "book_requests": 9, "book_request_failures": 2, "book_parse_failures": 1,
            "rejection_reasons": {"NO_ROBUST_EV": 4},
            "last_decision": {"outcome": "NO_ROBUST_EV"},
            "canonical_order_reconciliation": {
                "schema": "polymarket_v7_paper_exploration_order_reconciliation_v1",
                "model_sha": SHA, "paper_only": True,
                "authenticated_execution": False, "real_order_submission": False,
                "complete": True, "orders": 2, "filled_orders": 1,
                "terminal_nonfills": 1, "unresolved_orders": [],
                "invalid_spool_records": [], "conflicts": [],
            },
            "canonical_final_reconciliation": {
                "schema": "polymarket_v7_paper_exploration_final_reconciliation_v1",
                "model_sha": SHA, "paper_only": True,
                "authenticated_execution": False, "real_order_submission": False,
                "complete": True, "expected_terminal_positions": 1,
                "canonical_or_spooled_terminal_positions": 1,
                "missing_canonical_fills": [], "invalid_virtual_finals": [],
                "invalid_spool_records_observed": 0,
            },
            "paper_exploration_account": {
                "schema": "polymarket_v7_paper_exploration_account_v1",
                "model_sha": SHA, "paper_only": True,
                "authenticated_execution": False, "real_order_submission": False,
                "real_capital_at_risk": False,
                "accounting_owner": "V7_CANONICAL_LEDGER_AND_SINGLE_WRITER_SPOOL",
                "execution_authority": "SIMULATED_PAPER_EXPLORATION_ONLY",
                "complete": True, "orders_submitted": 2, "fills": 1,
                "terminal_nonfills": 1, "terminal_positions": 1,
                "open_positions": 0, "probe_fills": 1,
                "starting_capital": 4000.0, "entry_debit": 1.2,
                "settlement_payout": 2.0, "marked_open_value": 0.0,
                "cash": 4000.8, "realized_pnl": 0.8, "equity": 4000.8,
                "peak_equity": 4000.8, "drawdown": 0.0,
                "issues": [], "invalid_spool_records": [],
            },
        },
        "purposes": {"ALPHA": 3, "INVENTORY_REDUCTION": 1, "RISK": 5, "LIQUIDATION": 0},
        "cancel": {"fair_shock": 3, "latency_p50_ms": 1.0, "latency_p99_ms": 5.0},
        "economics": {"maker_robust_ev": 0.01, "taker_robust_ev": 0.02,
                      "realized_pnl": 0.1, "counterfactual_realized_pnl": -1.2,
                      "counterfactual_equity": 3998.8},
        "model": {"mature": False, "log_loss": 0.6, "brier": 0.2, "ece": 0.04,
                  "coverage": 0.9, "drift_score": 0.5},
        "latency": {"source_to_state": {"p50": 0.1, "p99": 0.5}},
        "tape": {"evidence_valid": True, "accepted": 10, "written": 10, "dropped": 0},
        "blockers": ["OMS_EXTERNAL_FAIR_ROUTING_NOT_RUNNING"],
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run_root = root / "runs" / "paper_v7_live"
        repo = root / "repo"
        write_json(repo / "config" / "v7_external_fair.json", {
            "execution_authority": "SHADOW_ZERO_AUTHORITY",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
        })

        # Missing SHADOW status is observable but must not kill BLUE.
        missing = summarize_external_fair(run_root, repo, runtime_sha=SHA, now_s=1)
        assert missing["present"] is False
        assert missing["healthy"] is True
        assert missing["shadow_zero_authority"] is True

        write_json(run_root / "external_fair" / "status.json", status())
        shadow = summarize_external_fair(run_root, repo, runtime_sha=SHA, now_s=1)
        assert shadow["present"] is True
        assert shadow["healthy"] is True
        assert shadow["fair"]["probability_order_ok"] is True
        assert shadow["external"]["fresh_venue_count"] == 3
        assert shadow["hard_reasons"] == []
        assert shadow["blockers"] == ["OMS_EXTERNAL_FAIR_ROUTING_NOT_RUNNING"]

        lines: list[str] = []
        exporter_v7_external._append_external_fair_metrics(lines, shadow)
        text = "\n".join(lines)
        assert "polymarket_external_fair_yes 0.7" in text
        assert 'polymarket_external_fair_actions_total{action="TAKE"} 2' in text
        assert 'polymarket_external_fair_counterfactual_actions_total{action="TAKE"} 7' in text
        assert "polymarket_external_fair_counterfactual_collection_enabled 1" in text
        assert "polymarket_external_fair_counterfactual_fills_total 7" in text
        assert "polymarket_external_fair_counterfactual_forecasts_total 11" in text
        assert "polymarket_external_fair_counterfactual_resolved_forecasts_total 6" in text
        assert "polymarket_external_fair_counterfactual_realized_pnl -1.2" in text
        assert 'polymarket_external_fair_oracle_continuity_info{continuity="LIVE_CONTINUOUS"} 1' in text
        assert 'polymarket_external_fair_venue_healthy{venue="BINANCE_SPOT"} 1' in text
        assert "polymarket_external_fair_router_book_requests_total 9" in text
        assert "polymarket_external_fair_canonical_final_reconciliation_complete 1" in text
        assert "polymarket_external_fair_canonical_order_reconciliation_complete 1" in text
        assert "polymarket_external_fair_canonical_order_nonfills 1" in text
        assert "polymarket_external_fair_canonical_terminal_positions_expected 1" in text
        assert "polymarket_external_fair_canonical_terminal_positions_present 1" in text
        assert "polymarket_external_fair_paper_account_complete 1" in text
        assert "polymarket_external_fair_paper_account_identity_ok 1" in text
        assert "polymarket_external_fair_paper_account_orders 2" in text
        assert "polymarket_external_fair_paper_account_fills 1" in text
        assert "polymarket_external_fair_paper_account_terminal_positions 1" in text
        assert "polymarket_external_fair_paper_account_cash_usd 4000.8" in text
        assert "polymarket_external_fair_paper_account_realized_pnl_usd 0.8" in text
        assert 'polymarket_external_fair_router_rejections_total{reason="NO_ROBUST_EV"} 4' in text
        assert "polymarket_external_fair_blockers 1" in text

        incomplete = status()
        incomplete["paper_router"]["canonical_final_reconciliation"]["complete"] = False
        write_json(run_root / "external_fair" / "status.json", incomplete)
        incomplete_report = summarize_external_fair(
            run_root, repo, runtime_sha=SHA, now_s=1
        )
        assert incomplete_report["healthy"] is False
        assert "PAPER_EXPLORATION_FINAL_RECONCILIATION_INCOMPLETE" in (
            incomplete_report["hard_reasons"]
        )

        incomplete_order = status()
        incomplete_order["paper_router"]["canonical_order_reconciliation"]["complete"] = False
        write_json(run_root / "external_fair" / "status.json", incomplete_order)
        order_report = summarize_external_fair(run_root, repo, runtime_sha=SHA, now_s=1)
        assert "PAPER_EXPLORATION_ORDER_RECONCILIATION_INCOMPLETE" in order_report["hard_reasons"]

        divergent_account = status()
        divergent_account["paper_router"]["cash"] = 3999.0
        write_json(run_root / "external_fair" / "status.json", divergent_account)
        account_report = summarize_external_fair(run_root, repo, runtime_sha=SHA, now_s=1)
        assert "PAPER_EXPLORATION_ACCOUNT_RECONCILIATION_INCOMPLETE" in account_report["hard_reasons"]
        assert account_report["paper_router"]["paper_exploration_account"]["identity_ok"] is False

        # An active required market with invalid oracle must become a hard reason.
        active = status("PAPER_EXECUTION_OWNER")
        active["oracle"]["healthy"] = False
        active["oracle"]["continuity"] = "CONTINUITY_UNKNOWN"
        write_json(run_root / "external_fair" / "status.json", active)
        failed = summarize_external_fair(run_root, repo, runtime_sha=SHA, now_s=1)
        assert failed["healthy"] is False
        assert "ORACLE_UNHEALTHY" in failed["hard_reasons"]
        assert "ORACLE_CONTINUITY_UNKNOWN" in failed["hard_reasons"]

        # SHADOW observes the same bad feed but does not make BLUE unhealthy.
        active["execution_authority"] = "SHADOW_ZERO_AUTHORITY"
        write_json(run_root / "external_fair" / "status.json", active)
        shadow_bad = summarize_external_fair(run_root, repo, runtime_sha=SHA, now_s=1)
        assert shadow_bad["healthy"] is True
        assert shadow_bad["hard_reasons"] == []

        # Impossible probability ordering is fail-closed once execution owns risk.
        bad_interval = status("PAPER_EXECUTION_OWNER")
        bad_interval["fair"]["lower"] = 0.8
        write_json(run_root / "external_fair" / "status.json", bad_interval)
        interval_report = summarize_external_fair(run_root, repo, runtime_sha=SHA, now_s=1)
        assert "FAIR_INTERVAL_INVALID" in interval_report["hard_reasons"]

        # Exact SHA mismatch is always visible for a present status.
        wrong_sha = status()
        wrong_sha["code_sha"] = "2" * 40
        write_json(run_root / "external_fair" / "status.json", wrong_sha)
        mismatch = summarize_external_fair(run_root, repo, runtime_sha=SHA, now_s=1)
        assert "EXTERNAL_FAIR_SHA_MISMATCH" in mismatch["hard_reasons"]


if __name__ == "__main__":
    main()
