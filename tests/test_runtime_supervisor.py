from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeSupervisorContractTest(unittest.TestCase):
    def test_execution_supervisor_is_decoupled_from_alpha_loop(self):
        script = (ROOT / "scripts" / "paper_v4_loop.sh").read_text(encoding="utf-8")
        self.assertIn("supervise_execution()", script)
        self.assertIn("start_supervisor()", script)
        self.assertIn('SUPERVISOR_PID=$!', script)
        self.assertIn('supervise_execution >> "$RUN_ROOT/runtime_supervisor.log" 2>&1 &', script)
        self.assertIn('kill -0 "$SUPERVISOR_PID"', script)
        self.assertNotIn("fatal: trade recorder or multi-leg broker exited", script)

    def test_supervisor_restarts_children_and_publishes_health(self):
        script = (ROOT / "scripts" / "paper_v4_loop.sh").read_text(encoding="utf-8")
        self.assertIn("start_recorder()", script)
        self.assertIn("start_broker()", script)
        self.assertIn("runtime_supervisor.csv", script)
        self.assertIn("runtime_supervisor_events.csv", script)
        self.assertIn("recorder_alive", script)
        self.assertIn("broker_alive", script)
        self.assertIn("recorder_restarts", script)
        self.assertIn("broker_restarts", script)
        self.assertIn('sleep 5', script)


if __name__ == "__main__":
    unittest.main()
