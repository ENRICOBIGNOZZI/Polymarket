#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    parent = str(path.parent)
    inserted = parent not in sys.path
    if inserted:
        sys.path.insert(0, parent)
    try:
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(parent)
    return module


class PaperObservabilityProbeContracts(unittest.TestCase):
    def test_probe_config_is_bounded_and_paper_only(self):
        cfg = json.loads((ROOT / "config/paper_observability_probe.json").read_text())
        self.assertTrue(cfg["paper_only"])
        self.assertTrue(cfg["enabled"])
        self.assertLessEqual(float(cfg["notional_usd"]), 5.0)
        self.assertLessEqual(int(cfg["max_roundtrips"]), 20)
        self.assertLessEqual(float(cfg["max_top_level_fraction"]), 0.25)

    def test_probe_is_outside_alpha_model_and_called_after_micro_taker(self):
        loop = (ROOT / "scripts/paper_v6_loop.sh").read_text()
        alpha = (ROOT / "scripts/v6_micro_taker.py").read_text()
        self.assertIn("scripts/v6_micro_taker.py", loop)
        self.assertIn("scripts/paper_observability_probe.py", loop)
        self.assertLess(loop.index("scripts/v6_micro_taker.py"), loop.index("scripts/paper_observability_probe.py"))
        self.assertNotIn("observability_probe", alpha)

    def test_probe_paths_are_operational_under_promotion_policy(self):
        gate = load_script("promotion_gate_observability_test", "scripts/promotion_gate.py")
        self.assertFalse(gate.is_economic_surface("scripts/paper_observability_probe.py"))
        self.assertFalse(gate.is_economic_surface("config/paper_observability_probe.json"))
        self.assertTrue(gate.is_economic_surface("scripts/paper_v6_loop.sh"))
        self.assertIsNotNone(gate.OPERATIONAL_RECOVERY_PATH.fullmatch("scripts/paper_v6_loop.sh"))

    def test_candidate_selection_prefers_lower_displayed_roundtrip_friction(self):
        probe = load_script("paper_observability_probe_test", "scripts/paper_observability_probe.py")
        market_a = types.SimpleNamespace(id="a", slug="a", yes="ay", no="an", fee_rate=0.0, fee_exp=1.0)
        market_b = types.SimpleNamespace(id="b", slug="b", yes="by", no="bn", fee_rate=0.0, fee_exp=1.0)
        def book(token: str, bid: float, ask: float):
            return probe.Book({
                "asset_id": token,
                "tick_size": "0.01",
                "min_order_size": "1",
                "bids": [{"price": str(bid), "size": "100"}],
                "asks": [{"price": str(ask), "size": "100"}],
            })
        books = {
            "ay": book("ay", 0.49, 0.50), "an": book("an", 0.49, 0.50),
            "by": book("by", 0.45, 0.55), "bn": book("bn", 0.45, 0.55),
        }
        selected = probe.select_candidate(
            [market_b, market_a], books, slip=0.0, min_price=0.05, max_price=0.95
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected[1].id, "a")

    def test_runtime_caps_untrusted_probe_config(self):
        probe = load_script("paper_observability_probe_caps_test", "scripts/paper_observability_probe.py")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "probe.json"
            path.write_text(json.dumps({
                "paper_only": True,
                "enabled": True,
                "notional_usd": 1000,
                "max_roundtrips": 999,
                "max_top_level_fraction": 9,
            }))
            cfg = probe.load_probe_config(path)
        self.assertEqual(cfg["notional_usd"], 5.0)
        self.assertEqual(cfg["max_roundtrips"], 20)
        self.assertEqual(cfg["max_top_level_fraction"], 0.25)


if __name__ == "__main__":
    unittest.main()
