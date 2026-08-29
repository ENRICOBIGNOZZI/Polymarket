from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_profit_stack_runtime as runtime


class ProfitStackRuntimeTest(unittest.TestCase):
    def test_repository_config_is_v7_paper_only_and_only_maker_executes(self) -> None:
        cfg = runtime.load_config(ROOT / "config" / "v7_profit_stack.json")
        self.assertEqual(cfg["version"], 7)
        self.assertTrue(cfg["paper_only"])
        self.assertFalse(cfg["authenticated_execution"])
        self.assertTrue(cfg["maker"]["enabled"])
        for family in ("local_factor", "pca", "ranking"):
            self.assertFalse(cfg["candidate_models"][family]["execution_enabled"])
        self.assertEqual(cfg["trade_recorder"]["market_limit"], 1000)
        self.assertGreaterEqual(cfg["trade_recorder"]["minimum_liquidity_usd"], 2.0)

    def test_runtime_rejects_premature_candidate_model_execution(self) -> None:
        cfg = json.loads((ROOT / "config" / "v7_profit_stack.json").read_text(encoding="utf-8"))
        cfg["candidate_models"]["ranking"]["execution_enabled"] = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaises(runtime.RuntimeContractError):
                runtime.load_config(path)

    def test_runtime_source_has_exactly_one_canonical_ledger_writer(self) -> None:
        text = (ROOT / "scripts" / "v7_profit_stack_runtime.py").read_text(encoding="utf-8")
        self.assertEqual(text.count("ledger.CanonicalLedgerWriter("), 1)
        self.assertIn("writer=writer", text)
        self.assertNotIn("v7_model_intent_router", text)
        self.assertNotIn("authenticated_order", text.lower())

    def test_champion_loop_binds_checked_out_sha(self) -> None:
        loop = (ROOT / "scripts" / "v7_profit_stack_loop.sh").read_text(encoding="utf-8")
        self.assertIn("POLYMARKET_EXPECTED_MODEL_SHA", loop)
        self.assertIn("git rev-parse HEAD", loop)
        self.assertIn("v7_profit_stack_runtime.py", loop)
        entry = (ROOT / "scripts" / "run_paper.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ "$VERSION" != "7" ]]', entry)
        self.assertIn('exec bash "$LOOP" "$CONFIG" "$RUN_ROOT"', entry)

    def test_trade_recorder_defaults_are_v7_native(self) -> None:
        text = (ROOT / "src" / "trade_recorder.cpp").read_text(encoding="utf-8")
        self.assertIn('config/v7_market_data_recorder.json', text)
        self.assertIn('runs/v7-paper', text)
        self.assertNotIn('config/paper_v3.json', text)
        self.assertNotIn('runs/paper_v4', text)
        self.assertIn('std::size_t markets = 1000', text)
        self.assertIn('double min_liquidity = 2.0', text)

    def test_runtime_status_keeps_other_sleeves_candidate_only(self) -> None:
        class Recorder:
            pid = 12
            def poll(self): return None
        status = runtime.runtime_status(
            model_sha="a" * 40,
            recorder=Recorder(),
            cycles=3,
            maker_report={},
            external_status={},
            economics_report={},
            last_error=None,
        )
        self.assertEqual(status["executable_sleeves"], ["maker_complete_set"])
        self.assertEqual(set(status["candidate_only_sleeves"]), {"local_factor", "pca", "ranking", "external"})
        self.assertTrue(status["single_ledger_writer"])
        self.assertTrue(status["paper_only"])
        self.assertFalse(status["authenticated_execution"])


if __name__ == "__main__":
    unittest.main()
