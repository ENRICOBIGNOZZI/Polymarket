#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


WATCHDOG = load_module("v5_stale_watchdog_test", ROOT / "scripts" / "v5_stale_watchdog.py")
REPORT = load_module("model_operability_report_test", ROOT / "scripts" / "model_operability_report.py")


class V5OperabilityTests(unittest.TestCase):
    def test_safe_launcher_always_forces_scan_only(self) -> None:
        captured: list[list[str]] = []
        stub = types.ModuleType("multi_strategy_paper")
        stub.main = lambda argv: captured.append(list(argv)) or 0
        previous = sys.modules.get("multi_strategy_paper")
        sys.modules["multi_strategy_paper"] = stub
        try:
            safe = load_module("multi_strategy_paper_safe_test", ROOT / "scripts" / "multi_strategy_paper_safe.py")
            self.assertEqual(safe.main(["--config", "x.json"]), 0)
            self.assertEqual(captured[-1][-1], "--scan-only")
            self.assertEqual(safe.force_scan_only(["--scan-only"]).count("--scan-only"), 1)
        finally:
            if previous is None:
                sys.modules.pop("multi_strategy_paper", None)
            else:
                sys.modules["multi_strategy_paper"] = previous

    def test_repository_routing_is_execution_aware_and_fail_closed(self) -> None:
        config = json.loads((ROOT / "config" / "paper_v5.json").read_text(encoding="utf-8"))
        operability = config["multi_strategy"]["operability"]
        routing = operability["execution_routing"]
        self.assertTrue(operability["generic_children_scan_only"])
        self.assertEqual(routing["micro"]["backend"], "maker_paper")
        self.assertEqual(routing["pca"]["backend"], "multileg_b2")
        self.assertEqual(routing["stat_arb_pairs"]["backend"], "multileg_b1")
        self.assertFalse(routing["graph"]["entry_enabled"])
        self.assertFalse(routing["semantic"]["entry_enabled"])
        self.assertFalse(routing["external"]["entry_enabled"])
        champion = json.loads((ROOT / "config" / "live_champion.json").read_text(encoding="utf-8"))
        self.assertEqual(champion["loop"], "scripts/paper_v5_safe_loop.sh")
        wrapper = (ROOT / "scripts" / "paper_v5_safe_loop.sh").read_text(encoding="utf-8")
        self.assertIn("multi_strategy_paper_safe.py", wrapper)
        self.assertIn("v5_stale_watchdog.py", wrapper)
        self.assertIn("model_operability_report.py", wrapper)

    def test_watchdog_detects_alive_but_stale_allocator_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime_supervisor.csv").write_text(
                "timestamp,recorder_alive,broker_alive,allocator_alive,recorder_restarts,broker_restarts,allocator_restarts,recorder_pid,broker_pid,allocator_pid\n"
                "1000,1,1,1,0,0,0,10,11,12345\n",
                encoding="utf-8",
            )
            (root / "allocator_status.json").write_text(json.dumps({"killed": False}), encoding="utf-8")
            (root / "strategy_status.csv").write_text(
                "name,killed,status_age_seconds\n"
                "micro,0,10\n"
                "pca,0,900\n",
                encoding="utf-8",
            )
            os.utime(root / "strategy_status.csv", (1000, 1000))
            state = WATCHDOG.WatchState()
            decision = WATCHDOG.evaluate(root, state, stale_seconds=600, grace_seconds=0, now=1000)
            self.assertTrue(decision["restart_required"])
            self.assertEqual(decision["reason"], "child_status_stale")
            self.assertEqual(decision["stale_models"], ["pca"])

            (root / "strategy_status.csv").write_text(
                "name,killed,status_age_seconds\n"
                "micro,0,10\n"
                "pca,0,20\n",
                encoding="utf-8",
            )
            os.utime(root / "strategy_status.csv", (1001, 1001))
            healthy = WATCHDOG.evaluate(root, state, stale_seconds=600, grace_seconds=0, now=1001)
            self.assertEqual(healthy["state"], "HEALTHY")
            self.assertFalse(healthy["restart_required"])

    def test_operability_report_distinguishes_abstention_shadow_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = int(time.time())
            external = root / "external.csv"
            external.write_text("market_id,q_yes,confidence,source,timestamp\n", encoding="utf-8")
            config = {
                "external_signals_file": str(external),
                "multi_strategy": {
                    "operability": {
                        "generic_children_scan_only": True,
                        "execution_routing": {
                            "micro": {"backend": "maker_paper", "entry_enabled": True},
                            "pca": {"backend": "multileg_b2", "entry_enabled": True},
                            "stat_arb_pairs": {"backend": "multileg_b1", "entry_enabled": True},
                            "graph": {"backend": "negrisk_basket_scan", "entry_enabled": False},
                            "semantic": {"backend": "shadow_only", "entry_enabled": False},
                            "external": {"backend": "shadow_only", "entry_enabled": False},
                        },
                    }
                },
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            (root / "strategy_status.csv").write_text(
                "name,alive,killed,status_age_seconds\n"
                "micro,1,0,1\n"
                "pca,1,0,1\n"
                "graph,1,0,1\n"
                "semantic,1,0,1\n"
                "external,1,0,1\n",
                encoding="utf-8",
            )
            (root / "runtime_supervisor.csv").write_text(
                "timestamp,broker_alive\n" + f"{now},1\n", encoding="utf-8"
            )

            maker = root / "maker"
            maker.mkdir()
            (maker / "maker_order_log.csv").write_text(
                "timestamp,action,market_id\n" + f"{now},POST,m1\n", encoding="utf-8"
            )
            (maker / "maker_fills.csv").write_text(
                "timestamp,market_id\n", encoding="utf-8"
            )
            (maker / "maker_positions.csv").write_text(
                "market_id,event_id\n", encoding="utf-8"
            )
            (maker / "maker_equity.csv").write_text(
                "timestamp,cash,equity\n" + f"{now},100,100\n", encoding="utf-8"
            )
            (root / "maker.log").write_text("fresh\n", encoding="utf-8")

            (root / "stat_arb_pca.csv").write_text(
                "raw_expected_edge,maker_entry_net_edge\n0.02,-0.001\n", encoding="utf-8"
            )
            (root / "stat_arb_pairs.csv").write_text(
                "raw_expected_edge,maker_entry_net_edge\n0.01,-0.02\n", encoding="utf-8"
            )
            (root / "intents.csv").write_text("strategy,bundle_id\n", encoding="utf-8")
            (root / "multileg_bundles.csv").write_text("strategy,bundle_id\n", encoding="utf-8")
            (root / "structural_latest.csv").write_text(
                "type,event_id,raw_edge,net_edge_pre_gas,executable_shares\n"
                "BUY_ALL_YES,e1,0.01,-0.01,10\n",
                encoding="utf-8",
            )

            report = REPORT.build_report(config_path, root, now=now, stale_seconds=600)
            models = {row["name"]: row for row in report["models"]}
            self.assertEqual(models["micro"]["state"], "QUOTING_NO_FILL")
            self.assertEqual(models["pca"]["state"], "ABSTAIN_NEGATIVE_POST_COST_EDGE")
            self.assertEqual(models["stat_arb_pairs"]["state"], "ABSTAIN_NEGATIVE_POST_COST_EDGE")
            self.assertEqual(models["graph"]["state"], "SHADOW_NO_POST_COST_EDGE")
            self.assertEqual(models["semantic"]["state"], "SHADOW_UNIDENTIFIED_RELATION")
            self.assertEqual(models["external"]["state"], "BLOCKED_NO_FRESH_APPROVED_FEED")
            json.dumps(report, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
