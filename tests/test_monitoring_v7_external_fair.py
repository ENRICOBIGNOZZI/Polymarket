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
        "purposes": {"ALPHA": 3, "INVENTORY_REDUCTION": 1, "RISK": 5, "LIQUIDATION": 0},
        "cancel": {"fair_shock": 3, "latency_p50_ms": 1.0, "latency_p99_ms": 5.0},
        "economics": {"maker_robust_ev": 0.01, "taker_robust_ev": 0.02, "realized_pnl": 0.1},
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
        assert 'polymarket_external_fair_oracle_continuity_info{continuity="LIVE_CONTINUOUS"} 1' in text
        assert 'polymarket_external_fair_venue_healthy{venue="BINANCE_SPOT"} 1' in text
        assert "polymarket_external_fair_blockers 1" in text

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
