#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("multi_strategy_paper", ROOT / "scripts" / "multi_strategy_paper.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class MultiStrategyPaperTests(unittest.TestCase):
    def sample_config(self) -> dict:
        return {
            "starting_capital": 10000.0,
            "run_dir": "unused",
            "max_drawdown": 0.15,
            "max_gross_fraction": 0.25,
            "expert_weights": {
                "micro": 0.0,
                "pca": 0.0,
                "graph": 0.0,
                "semantic": 0.0,
                "external": 0.0,
            },
            "multi_strategy": {
                "schema_version": 1,
                "paper_only": True,
                "reserve_fraction": 0.10,
                "global_max_drawdown": 0.15,
                "global_max_gross_fraction": 0.35,
                "status_interval_seconds": 0.01,
                "restart_backoff_seconds": 0.0,
                "strategies": [
                    {
                        "name": "graph",
                        "expert": "graph",
                        "capital_fraction": 0.50,
                        "overrides": {"min_net_edge": 0.001, "max_drawdown": 0.15, "max_gross_fraction": 0.35},
                    },
                    {
                        "name": "external",
                        "expert": "external",
                        "capital_fraction": 0.40,
                        "overrides": {"min_net_edge": 0.002, "max_drawdown": 0.15, "max_gross_fraction": 0.35},
                    },
                ],
            },
        }

    def write_config(self, directory: Path, config: dict | None = None) -> Path:
        path = directory / "config.json"
        path.write_text(json.dumps(config or self.sample_config()), encoding="utf-8")
        return path

    def test_repository_config_is_fail_closed_and_complete(self) -> None:
        config = json.loads((ROOT / "config" / "paper_v5.json").read_text(encoding="utf-8"))
        spec = MODULE.load_manager_spec(config)
        self.assertEqual(set(config["expert_weights"].values()), {0.0})
        self.assertAlmostEqual(sum(item.capital_fraction for item in spec.strategies) + spec.reserve_fraction, 1.0)
        self.assertEqual(
            {item.expert for item in spec.strategies},
            {"micro", "pca", "graph", "semantic", "external"},
        )

    def test_generated_child_has_exactly_one_active_expert(self) -> None:
        config = self.sample_config()
        manager = MODULE.load_manager_spec(config)
        for strategy in manager.strategies:
            child = MODULE.build_child_config(config, manager, strategy, Path("runs") / strategy.name)
            nonzero = {name: value for name, value in child["expert_weights"].items() if value != 0.0}
            self.assertEqual(nonzero, {strategy.expert: 1.0})
            self.assertAlmostEqual(child["starting_capital"], manager.starting_capital * strategy.capital_fraction)
            self.assertNotIn("multi_strategy", child)

    def test_invalid_fraction_parent_weight_and_protected_override_fail(self) -> None:
        config = self.sample_config()
        config["multi_strategy"]["reserve_fraction"] = 0.11
        with self.assertRaises(ValueError):
            MODULE.load_manager_spec(config)

        config = self.sample_config()
        config["expert_weights"]["graph"] = 1.0
        with self.assertRaises(ValueError):
            MODULE.load_manager_spec(config)

        config = self.sample_config()
        config["multi_strategy"]["strategies"][0]["overrides"]["expert_weights"] = {"graph": 1.0}
        with self.assertRaises(ValueError):
            MODULE.load_manager_spec(config)

    def test_aggregate_equity_and_global_kill_are_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_config(root)
            manager = MODULE.MultiStrategyManager(config_path, root / "run", Path("/bin/true"))
            initial = manager.publish()
            self.assertAlmostEqual(initial["equity"], 10000.0)
            self.assertFalse(initial["killed"])

            for child in manager.children.values():
                (child.run_dir / "status.json").write_text(
                    json.dumps(
                        {
                            "cash": child.starting_capital,
                            "equity": child.starting_capital,
                            "peak_equity": child.starting_capital,
                            "drawdown": 0.0,
                            "gross_exposure": 0.0,
                            "open_positions": 0,
                            "killed": False,
                        }
                    ),
                    encoding="utf-8",
                )
            manager.publish()

            graph = manager.children["graph"]
            (graph.run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "cash": 3000.0,
                        "equity": 3000.0,
                        "peak_equity": graph.starting_capital,
                        "drawdown": 0.40,
                        "gross_exposure": 0.0,
                        "open_positions": 0,
                        "killed": True,
                    }
                ),
                encoding="utf-8",
            )
            killed = manager.publish()
            self.assertAlmostEqual(killed["equity"], 8000.0)
            self.assertTrue(killed["killed"])
            state = json.loads((manager.run_root / "allocator_state.json").read_text(encoding="utf-8"))
            runtime = json.loads((manager.run_root / "runtime_status.json").read_text(encoding="utf-8"))
            self.assertTrue(state["killed"])
            self.assertEqual(runtime["mode"], "paper-multi-strategy-v5")
            self.assertTrue(runtime["paper_only"])

            restarted = MODULE.MultiStrategyManager(config_path, root / "run", Path("/bin/true"))
            self.assertTrue(restarted.killed)

    def test_once_launches_isolated_generated_configs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_config(root)
            engine = root / "fake_engine.py"
            engine.write_text(
                """#!/usr/bin/env python3
import json
import pathlib
import sys
args = sys.argv[1:]
run = pathlib.Path(args[args.index('--run-dir') + 1])
config = json.loads(pathlib.Path(args[args.index('--config') + 1]).read_text())
run.mkdir(parents=True, exist_ok=True)
start = float(config['starting_capital'])
(run / 'status.json').write_text(json.dumps({'cash': start, 'equity': start, 'peak_equity': start, 'drawdown': 0, 'gross_exposure': 0, 'open_positions': 0, 'killed': False}))
""",
                encoding="utf-8",
            )
            engine.chmod(engine.stat().st_mode | stat.S_IXUSR)
            manager = MODULE.MultiStrategyManager(config_path, root / "run", engine, markets=10, scan_only=True)
            self.assertEqual(manager.run_once(), 0)
            self.assertTrue((manager.run_root / "strategy_status.csv").exists())
            for child in manager.children.values():
                generated = json.loads(child.config_path.read_text(encoding="utf-8"))
                self.assertTrue(generated["scan_only"])
                self.assertTrue((child.run_dir / "status.json").exists())


if __name__ == "__main__":
    unittest.main()
