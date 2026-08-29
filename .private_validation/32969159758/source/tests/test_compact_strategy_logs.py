from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compact_strategy_logs", ROOT / "scripts" / "compact_strategy_logs.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CompactStrategyLogsTests(unittest.TestCase):
    def write_config(self, root: Path) -> Path:
        config = {
            "multi_strategy": {
                "paper_only": True,
                "log_retention": {
                    "compaction_interval_seconds": 60,
                    "signal_rows_per_model": 3,
                    "arbitrage_rows_per_model": 2,
                    "pca_history_rows_per_market": 2,
                },
                "strategies": [
                    {"name": "graph", "expert": "graph", "enabled": True},
                    {"name": "pca", "expert": "pca", "enabled": True},
                ],
            }
        }
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    @staticmethod
    def write_rows(path: Path, header: list[str], rows: list[list[object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)

    def test_compaction_is_bounded_atomic_and_preserves_durable_fills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.write_config(root)
            run_root = root / "run"
            graph = run_root / "strategies" / "graph"
            pca = run_root / "strategies" / "pca"

            for directory in (graph, pca):
                self.write_rows(
                    directory / "signals.csv",
                    ["timestamp", "net_edge"],
                    [[index, index / 1000] for index in range(10)],
                )
                self.write_rows(
                    directory / "arbitrage.csv",
                    ["timestamp", "net_edge"],
                    [[index, index / 100] for index in range(8)],
                )
                self.write_rows(
                    directory / "fills.csv",
                    ["timestamp", "action"],
                    [[1, "BUY"], [2, "SELL"]],
                )

            history_rows = [
                [1, "A", 0.10], [2, "A", 0.11], [3, "A", 0.12],
                [1, "B", 0.20], [2, "B", 0.21], [3, "B", 0.22],
            ]
            self.write_rows(graph / "history.csv", ["timestamp", "market_id", "mid"], history_rows)
            self.write_rows(pca / "history.csv", ["timestamp", "market_id", "mid"], history_rows)
            fills_before = (graph / "fills.csv").read_bytes()

            result = MODULE.compact(config, run_root, pause_processes=False)
            self.assertTrue(result["success"])
            self.assertGreater(result["bytes_reclaimed"], 0)

            with (graph / "signals.csv").open(newline="", encoding="utf-8") as handle:
                graph_signals = list(csv.DictReader(handle))
            self.assertEqual([int(row["timestamp"]) for row in graph_signals], [7, 8, 9])

            with (graph / "arbitrage.csv").open(newline="", encoding="utf-8") as handle:
                graph_arbitrage = list(csv.DictReader(handle))
            self.assertEqual([int(row["timestamp"]) for row in graph_arbitrage], [6, 7])

            with (graph / "history.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

            with (pca / "history.csv").open(newline="", encoding="utf-8") as handle:
                pca_history = list(csv.DictReader(handle))
            self.assertEqual(
                [(row["market_id"], int(row["timestamp"])) for row in pca_history],
                [("A", 2), ("A", 3), ("B", 2), ("B", 3)],
            )
            self.assertEqual((graph / "fills.csv").read_bytes(), fills_before)

            status = json.loads((run_root / "compaction_status.json").read_text(encoding="utf-8"))
            self.assertTrue(status["success"])
            self.assertEqual(status["strategies"]["graph"]["history"]["rows_after"], 0)
            self.assertEqual(status["strategies"]["pca"]["history"]["rows_after"], 4)

            second = MODULE.compact(config, run_root, pause_processes=False)
            self.assertTrue(second["success"])
            self.assertEqual(second["bytes_reclaimed"], 0)

    def test_invalid_retention_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.write_config(root)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["multi_strategy"]["log_retention"]["signal_rows_per_model"] = 0
            config.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                MODULE.compact(config, root / "run", pause_processes=False)


if __name__ == "__main__":
    unittest.main()
