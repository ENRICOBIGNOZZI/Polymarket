from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeSupervisorContractTest(unittest.TestCase):
    def test_paper_loop_restarts_children_without_killing_parent(self):
        script = (ROOT / "scripts" / "paper_v4_loop.sh").read_text(encoding="utf-8")
        self.assertIn("start_recorder()", script)
        self.assertIn("start_broker()", script)
        self.assertIn("restart_recorder()", script)
        self.assertIn("restart_broker()", script)
        self.assertIn("runtime_supervisor.csv", script)
        self.assertIn("runtime_supervisor_events.csv", script)
        self.assertIn('wait "$REC_PID"', script)
        self.assertIn('wait "$BROKER_PID"', script)
        self.assertNotIn("fatal: trade recorder or multi-leg broker exited", script)

    def test_supervisor_status_contains_both_child_health_flags(self):
        script = (ROOT / "scripts" / "paper_v4_loop.sh").read_text(encoding="utf-8")
        self.assertIn("recorder_alive", script)
        self.assertIn("broker_alive", script)
        self.assertIn("recorder_restarts", script)
        self.assertIn("broker_restarts", script)


if __name__ == "__main__":
    unittest.main()
