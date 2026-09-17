from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_lead_lag_capacity_history as history
import v7_lead_lag_capacity_ladder as ladder


def base_event(event_type: str, order_id: str = "o1", market: str = "m1") -> dict:
    return {
        "event_type": event_type,
        "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "order_id": order_id,
        "market_id": market,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "metadata": {"model_family": "lead_lag_taker_v1", "paper_forward_test": True},
    }


def ledger_rows(*, with_ladder: bool = True) -> list[dict]:
    order = base_event("ORDER_SUBMITTED")
    order.update({"ask": 0.20, "ask_depth": 130.0, "recorded_ts_ms": 1000, "book_snapshot_id": "snap"})
    order["metadata"].update({"arrival_best_ask": 0.20, "arrival_best_ask_size": 10.0, "arrival_fee_per_share": 0.0112})
    if with_ladder:
        order["metadata"]["capacity_book"] = {
            "schema": "polymarket_v7_lead_lag_capacity_book_v1",
            "book_snapshot_id": "snap",
            "receive_ts_ms": 1000,
            "ask_levels": [
                {"price": 0.20, "size": 10.0},
                {"price": 0.21, "size": 20.0},
                {"price": 0.25, "size": 100.0},
            ],
            "fee_schedule": {"rate": 0.07, "exponent": 1.0, "takerOnly": True},
            "candidate_limit_price": 0.20,
        }
    fill = base_event("FILL")
    fill.update({"filled_size": 5.0, "fee": 0.056, "recorded_ts_ms": 1001})
    final = base_event("FINAL")
    final.update({"final_pnl": 3.944, "recorded_ts_ms": 2000})
    final["metadata"]["won"] = True
    return [order, fill, final]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def make_run_root(root: Path, *, with_ladder: bool = True) -> Path:
    run = root / "runs/paper_v7_live"
    write_jsonl(run / "ledger/execution.jsonl", ledger_rows(with_ladder=with_ladder))
    write_jsonl(run / "research/lead_lag_taker_v1/events.jsonl", [
        {"event": "CANDIDATE", "market_id": "m1"},
    ])
    runtime = {
        "model_sha": "a" * 40,
        "run_id": "run-1",
        "server_id": "test",
        "state": "running",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
    }
    control = run / "control/runtime_status.json"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text(json.dumps(runtime), encoding="utf-8")
    return run


class FullLadderCapacityTests(unittest.TestCase):
    def test_exact_multilevel_vwap_uses_only_retained_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            write_jsonl(path, ledger_rows())
            trades = ladder.collect_ladder_trades(path)
            result = ladder.execute_ladder(
                trades[0], target=25.0, dimension="shares",
                price_impact_cap=0.01, strict_full_fill=True,
            )
        self.assertTrue(result["full"])
        self.assertAlmostEqual(result["filled_shares"], 25.0)
        self.assertAlmostEqual(result["vwap"], (10 * 0.20 + 15 * 0.21) / 25)
        self.assertEqual(result["max_price"], 0.21)

    def test_price_impact_cap_fails_closed_before_deeper_level(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            write_jsonl(path, ledger_rows())
            trade = ladder.collect_ladder_trades(path)[0]
            result = ladder.execute_ladder(
                trade, target=25.0, dimension="shares",
                price_impact_cap=0.0, strict_full_fill=True,
            )
        self.assertFalse(result["full"])
        self.assertEqual(result["filled_shares"], 0.0)

    def test_report_is_unavailable_without_retained_ladders(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            write_jsonl(path, ledger_rows(with_ladder=False))
            report = ladder.build_ladder_report(
                path, share_grid=(5.0, 25.0), notional_grid=(5.0, 25.0)
            )
        self.assertFalse(report["available"])
        self.assertEqual(report["evidence_markets"], 0)

    def test_full_ladder_report_contains_impact_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            write_jsonl(path, ledger_rows())
            report = ladder.build_ladder_report(
                path, share_grid=(5.0, 25.0), notional_grid=(5.0, 25.0)
            )
        self.assertTrue(report["available"])
        self.assertEqual(report["evidence_markets"], 1)
        self.assertIn("0c_from_best_ask", report["by_price_impact_cap"])
        self.assertIn("1c_from_best_ask", report["by_price_impact_cap"])
        row = report["by_price_impact_cap"]["1c_from_best_ask"]["strict_fixed_share_grid"][1]
        self.assertEqual(row["full_fill_markets"], 1)


class CapacityHistoryTests(unittest.TestCase):
    def test_identical_evidence_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = make_run_root(root)
            output = root / "capacity"
            first = history.build_snapshot(
                run_root=run, output_root=output,
                share_grid=(5.0, 25.0), notional_grid=(5.0, 25.0),
                bootstrap_draws=0, seed=1, label="test", force=False,
            )
            second = history.build_snapshot(
                run_root=run, output_root=output,
                share_grid=(5.0, 25.0), notional_grid=(5.0, 25.0),
                bootstrap_draws=0, seed=1, label="test", force=False,
            )
            self.assertEqual(first["state"], "SNAPSHOT_CREATED")
            self.assertEqual(second["state"], "UNCHANGED_EVIDENCE")
            self.assertEqual(first["snapshot_id"], second["snapshot_id"])
            snapshots = history.list_snapshots(output / "history")
            self.assertEqual(len(snapshots), 1)
            self.assertTrue((output / "report.json").is_file())
            self.assertTrue((output / "history.csv").is_file())
            self.assertTrue((output / "ladder_scenarios.csv").is_file())

    def test_new_candidate_evidence_creates_delta_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = make_run_root(root)
            output = root / "capacity"
            first = history.build_snapshot(
                run_root=run, output_root=output,
                share_grid=(5.0, 25.0), notional_grid=(5.0, 25.0),
                bootstrap_draws=0, seed=1, label=None, force=False,
            )
            events = run / "research/lead_lag_taker_v1/events.jsonl"
            with events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "CANDIDATE", "market_id": "m2"}) + "\n")
            second = history.build_snapshot(
                run_root=run, output_root=output,
                share_grid=(5.0, 25.0), notional_grid=(5.0, 25.0),
                bootstrap_draws=0, seed=1, label=None, force=False,
            )
            comparison = json.loads((Path(second["path"]) / "comparison.json").read_text())
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(comparison["candidate_markets_delta"], 1.0)
        self.assertEqual(comparison["settled_markets_delta"], 0.0)
        self.assertEqual(comparison["observed_pnl_usd_delta"], 0.0)

    def test_latest_report_is_enriched_with_full_ladder_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = make_run_root(root, with_ladder=True)
            output = root / "capacity"
            history.build_snapshot(
                run_root=run, output_root=output,
                share_grid=(5.0, 25.0), notional_grid=(5.0, 25.0),
                bootstrap_draws=0, seed=1, label=None, force=False,
            )
            report = json.loads((output / "report.json").read_text())
        self.assertTrue(report["full_ladder_capacity"]["available"])
        self.assertEqual(report["full_ladder_capacity"]["evidence_markets"], 1)


if __name__ == "__main__":
    unittest.main()
