from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_v6_receive_time_completion_audit.py"
SOURCE = ROOT / "src" / "multileg_paper.cpp"
RECORDER = ROOT / "src" / "trade_recorder.cpp"


def load_module():
    spec = importlib.util.spec_from_file_location("lf_receive_time_audit", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load receive-time audit")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class ReceiveTimeCompletionAuditTest(unittest.TestCase):
    def test_delayed_observation_can_create_false_event_time_fill(self):
        mod = load_module()
        result = mod.audit()["delayed_observation"]
        self.assertTrue(result["event_time_eligible"])
        self.assertFalse(result["receive_time_eligible"])
        self.assertEqual(result["event_time_fill_shares"], 10.0)
        self.assertEqual(result["receive_time_fill_shares"], 0.0)

    def test_second_resolution_event_time_can_hide_true_post_arrival_ordering(self):
        mod = load_module()
        result = mod.audit()["same_second_ordering"]
        self.assertFalse(result["event_time_eligible"])
        self.assertTrue(result["receive_time_eligible"])

    def test_recorder_persists_receive_time_but_multileg_discards_it(self):
        recorder = RECORDER.read_text(encoding="utf-8")
        broker = SOURCE.read_text(encoding="utf-8")
        self.assertIn('"timestamp,received_ms,lag_ms,condition_id,asset_id,outcome,side,price,size', recorder)
        self.assertIn("t.received_ms = now_ms();", recorder)
        self.assertIn("std::int64_t arrival_ms = 0;", broker)
        self.assertIn("t.ts=std::stoll(x[0]); t.asset_id=x[4]", broker)
        self.assertIn("const auto trade_ms=t.ts*1000;", broker)
        tape_trade = broker.split("struct TapeTrade", 1)[1].split("};", 1)[0]
        self.assertNotIn("received_ms", tape_trade)


if __name__ == "__main__":
    unittest.main()
