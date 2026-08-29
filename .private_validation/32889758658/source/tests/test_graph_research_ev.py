from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import execution_ev

spec = importlib.util.spec_from_file_location("graph_research_ev_test", SCRIPTS / "graph_research_ev.py")
assert spec and spec.loader
graph = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = graph
spec.loader.exec_module(graph)


INTENT_FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]
LEDGER_FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "closed_ts", "status",
    "expected_edge", "max_notional", "entry_cash", "gross_pnl", "fees", "slippage",
    "net_pnl", "return_on_capital", "fill_fraction", "adverse_mark_pnl", "abort_reason",
]
EVENT_FIELDS = ["timestamp", "event", "bundle_id", "market_id", "side"]


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def candidate_rows(now: int, bundle: str = "candidate") -> list[dict]:
    common = {
        "bundle_id": bundle,
        "strategy": "GRAPH_RV",
        "event_id": "event",
        "created_ts": now,
        "mode": "MAKER",
        "expected_edge": "0.02",
        "max_notional": "20",
        "weight": "1",
        "limit_price": "0.40",
        "execution_deadline_ts": now + 120,
        "hold_deadline_ts": now + 3600,
    }
    return [
        {**common, "market_id": "m1", "side": "YES"},
        {**common, "market_id": "m2", "side": "NO"},
    ]


class GraphResearchEvTest(unittest.TestCase):
    def test_marginal_fill_product_is_explicitly_rejected(self) -> None:
        result = execution_ev.assess_candidate(
            {
                "candidate_id": "bad",
                "leg_count": 2,
                "marginal_fill_probabilities": [0.8, 0.8],
                "joint_completion": {
                    "full": 0.64,
                    "partial": 0.10,
                    "zero": 0.26,
                    "source": "product_of_marginals",
                    "observations": 100,
                },
                "conditional_alpha_usd": 1.0,
                "conditional_costs_usd": 0.1,
                "conditional_adverse_markout_usd": 0.1,
                "conditional_unwind_loss_usd": 0.1,
                "capital_latency_cost_usd": 0.01,
            }
        )
        self.assertFalse(result["admissible"])
        self.assertIn("marginal_leg_probabilities_not_admissible", result["reason_codes"])

    def test_no_joint_history_stays_research_only_and_writes_no_broker_intents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, ledger, events = root / "input.csv", root / "ledger.csv", root / "events.csv"
            output, status = root / "research.csv", root / "status.json"
            write_csv(source, INTENT_FIELDS, candidate_rows(1000))
            write_csv(ledger, LEDGER_FIELDS, [])
            write_csv(events, EVENT_FIELDS, [])
            self.assertEqual(
                graph.main(
                    [
                        "--input", str(source), "--ledger", str(ledger), "--events", str(events),
                        "--output", str(output), "--status", str(status), "--min-observations", "30", "--now", "1000",
                    ]
                ),
                0,
            )
            report = json.loads(status.read_text(encoding="utf-8"))
            with output.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
        self.assertTrue(report["paper_only"])
        self.assertEqual(report["graph_mode"], "RESEARCH_ONLY")
        self.assertFalse(report["broker_routing_enabled"])
        self.assertFalse(report["raw_scanner_edge_is_execution_edge"])
        self.assertEqual(report["broker_intents_written"], 0)
        self.assertEqual(row["research_state"], "RESEARCH_INSUFFICIENT_EVIDENCE")
        self.assertIn("insufficient_empirical_joint_fill_observations", row["reason_codes"])

    def test_empirical_basket_history_can_score_but_stays_research_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, ledger, events = root / "input.csv", root / "ledger.csv", root / "events.csv"
            output, status = root / "research.csv", root / "status.json"
            write_csv(source, INTENT_FIELDS, candidate_rows(1000))
            ledger_rows: list[dict] = []
            event_rows: list[dict] = []
            for index in range(30):
                bundle = f"history-{index}"
                ledger_rows.append(
                    {
                        "bundle_id": bundle, "strategy": "GRAPH_RV", "event_id": "event",
                        "created_ts": 1, "closed_ts": 2, "status": "CLOSED", "expected_edge": "0.02",
                        "max_notional": "100", "entry_cash": "100", "gross_pnl": "2",
                        "fees": "0.1", "slippage": "0.1", "net_pnl": "1.8", "return_on_capital": "0.018",
                        "fill_fraction": "1", "adverse_mark_pnl": "0", "abort_reason": "",
                    }
                )
                event_rows.extend(
                    [
                        {"timestamp": 1, "event": "POST", "bundle_id": bundle, "market_id": f"m1-{index}", "side": "YES"},
                        {"timestamp": 1, "event": "POST", "bundle_id": bundle, "market_id": f"m2-{index}", "side": "NO"},
                    ]
                )
            write_csv(ledger, LEDGER_FIELDS, ledger_rows)
            write_csv(events, EVENT_FIELDS, event_rows)
            self.assertEqual(
                graph.main(
                    [
                        "--input", str(source), "--ledger", str(ledger), "--events", str(events),
                        "--output", str(output), "--status", str(status), "--min-observations", "30", "--now", "1000",
                    ]
                ),
                0,
            )
            report = json.loads(status.read_text(encoding="utf-8"))
            with output.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
        self.assertEqual(row["research_state"], "RESEARCH_ECONOMIC_CANDIDATE")
        self.assertGreater(float(row["joint_fill_ev_usd"]), 0.0)
        self.assertEqual(int(row["joint_observations"]), 30)
        self.assertIn("research_only_no_broker_route", row["reason_codes"])
        self.assertEqual(report["broker_intents_written"], 0)
        self.assertFalse(report["broker_routing_enabled"])


if __name__ == "__main__":
    unittest.main()
