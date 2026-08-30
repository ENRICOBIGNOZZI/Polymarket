#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v7_profitability_audit import audit  # noqa: E402


def event(record_id: str, event_type: str, strategy: str, **values):
    return {"record_id": record_id, "recorded_ts_ms": len(record_id),
            "event_type": event_type, "strategy": strategy, "model_sha": "a" * 40, **values}


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "archive" / "ledger"
        live = root / "live" / "ledger"
        archive.mkdir(parents=True); live.mkdir(parents=True)
        rows = [
            event("o1", "ORDER_SUBMITTED", "MICRO_MAKER_PRO", order_id="order-1"),
            event("mf1", "FILL", "MICRO_MAKER_PRO", order_id="order-1", fill_id="maker-fill-1"),
            event("mm1", "MARKOUT", "MICRO_MAKER_PRO", fill_id="maker-fill-1", markouts={"1s": -0.02}),
            event("mfin", "FINAL", "MICRO_MAKER_PRO", position_id="maker-pos-1", final_pnl=-0.5),
            event("ef1", "FILL", "CRYPTO_INFORMED_TAKER", position_id="external-pos", fill_id="external-fill",
                  fill_price=0.7, filled_size=10, metadata={"outcome": "YES", "fair_yes": 0.9,
                                                           "pm_mid": 0.7, "robust_net_ev": 2.0}),
            event("efin", "FINAL", "CRYPTO_INFORMED_TAKER", position_id="external-pos",
                  fill_id="external-fill", final_pnl=-7.0, realized_cashflow=0.0),
        ]
        (archive / "execution.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        (live / "execution.jsonl").write_text(json.dumps(rows[0]) + "\n")
        tape = root / "live" / "external_fair" / "counterfactuals.jsonl"
        tape.parent.mkdir(parents=True)
        tape.write_text(json.dumps({
            "record_id": "cf-final", "model_sha": "a" * 40,
            "event_type": "FORECAST_FINAL", "model_yes": 0.9,
            "market_yes": 0.7, "actual_yes": 0.0,
        }) + "\n")
        report = audit([root])
        assert report["data_quality"]["raw_records"] == 7
        assert report["data_quality"]["unique_records"] == 6
        assert report["data_quality"]["duplicate_records_removed"] == 1
        assert report["professional_maker"]["unique_order_ids"] == 1
        assert report["professional_maker"]["unique_fill_ids"] == 1
        assert report["professional_maker"]["markout_per_share"]["1s"]["mean"] == -0.02
        assert report["external_fair"]["matched_terminal_positions"] == 1
        assert report["external_fair"]["realized_pnl"] == -7.0
        assert report["external_fair"]["predicted_robust_net_ev"] == 2.0
        assert report["external_fair"]["model_brier_minus_market"] > 0.0
        assert report["external_fair_counterfactual"]["events"]["FORECAST_FINAL"] == 1
        assert report["external_fair_counterfactual"]["forecast_model_score"]["brier"] > report["external_fair_counterfactual"]["forecast_market_benchmark_score"]["brier"]
        assert report["selected_sleeves_realized_pnl"] == -7.5
        assert report["profitability_proven"] is False


if __name__ == "__main__":
    main()
