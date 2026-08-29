from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "scripts" / "paper_v6_loop.sh"
RUNTIME = ROOT / "scripts" / "v6_task_runtime.sh"


class V6LoopConcurrencyTest(unittest.TestCase):
    def test_one_shots_are_background_single_flight(self) -> None:
        loop = LOOP.read_text(encoding="utf-8")
        while_body = loop.split("while true;do", 1)[1]
        expected = {
            "maker": "maker_pid",
            "micro_taker": "micro_taker_pid",
            "hard_arb": "hard_arb_pid",
            "factor": "factor_pid",
            "relation": "relation_pid",
            "external_bridge": "external_bridge_pid",
            "report": "report_pid",
        }
        for task, pid in expected.items():
            self.assertIn(f"v6_task_start {task}", while_body)
            self.assertIn(f"{pid} == 0", while_body)
            self.assertIn(f'{pid}="$V6_TASK_STARTED_PID"', while_body)

        # The heartbeat loop schedules wrappers only; network/model commands
        # live in task bodies above it and therefore cannot block supervision.
        for command in (
            "polymarket_maker_paper --config",
            "v6_queue_filter.py micro --config",
            "v6_queue_filter.py hard --config",
            "v6_local_factor_intents.py --config",
            "v6_relation_intents.py --config",
            "v6_external_bridge.py --output",
            "v6_runtime_status.py --config",
        ):
            self.assertNotIn(command, while_body)

        self.assertIn("v6_queue_filter.py hard", loop)
        self.assertIn("--leg-latency-ms 100", loop)
        self.assertNotIn("v6_hard_arb_paper.py", loop)

    def test_runtime_is_bash_32_compatible_and_cleanup_covers_every_task(self) -> None:
        source = RUNTIME.read_text(encoding="utf-8")
        loop = LOOP.read_text(encoding="utf-8")
        for unsupported in ("declare -A", "wait -n", "mapfile", "readarray", "coproc", "local -n"):
            self.assertNotIn(unsupported, source)
            self.assertNotIn(unsupported, loop)
        self.assertIn("trap - EXIT", source)
        self.assertIn("v6_task_stop_child", source)
        self.assertIn("v6_task_terminate_pids", loop)
        for pid in (
            "maker_pid",
            "micro_taker_pid",
            "hard_arb_pid",
            "factor_pid",
            "relation_pid",
            "external_bridge_pid",
            "report_pid",
        ):
            self.assertIn(f'"${pid}"', loop)

        subprocess.run(["bash", "-n", str(RUNTIME), str(LOOP)], cwd=ROOT, check=True)

    def test_single_writer_probe_distinguishes_task_wrappers_from_runtime_owner(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "private-runtime-single-writer-validation.yml").read_text(encoding="utf-8")
        for path in ("scripts/v6_task_runtime.sh", "scripts/v6_queue_filter.py", "scripts/graph_research_ev.py"):
            self.assertIn(f'- "{path}"', workflow)
        self.assertIn("'ppid':ppid", workflow)
        self.assertIn("marker != 'paper_v6_loop.sh' or row['ppid'] == owner", workflow)
        self.assertIn("'v6_queue_filter.py micro'", workflow)
        self.assertIn("'v6_queue_filter.py hard'", workflow)

    def test_task_status_is_atomic_and_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = f"""
set -euo pipefail
source {shlex.quote(str(RUNTIME))}
V6_TASK_STATUS_DIR={shlex.quote(td)}
probe_body() {{
  v6_task_run_child bash -c 'sleep 0.2; exit 7'
}}
started="$(date +%s)"
v6_task_start probe "$started" probe_body
probe_pid="$V6_TASK_STARTED_PID"
python3 - "$V6_TASK_STATUS_DIR/probe.json" <<'PY'
import json, sys
x=json.load(open(sys.argv[1], encoding='utf-8'))
assert x['state']=='running' and x['finished'] is None and x['last_rc'] is None
PY
rc=0
wait "$probe_pid" || rc=$?
test "$rc" -eq 7
"""
            subprocess.run(["bash", "-c", script], cwd=ROOT, check=True, timeout=10)
            status = json.loads((Path(td) / "probe.json").read_text(encoding="utf-8"))
            self.assertEqual(status["schema_version"], "polymarket_v6_task_status_v1")
            self.assertEqual(status["task"], "probe")
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["last_rc"], 7)
            self.assertGreaterEqual(status["finished"], status["started"])
            self.assertEqual(list(Path(td).glob("*.tmp.*")), [])

    def test_term_is_forwarded_and_publishes_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = f"""
set -euo pipefail
source {shlex.quote(str(RUNTIME))}
V6_TASK_STATUS_DIR={shlex.quote(td)}
slow_body() {{
  v6_task_run_child python3 -c 'import time; time.sleep(30)'
}}
started="$(date +%s)"
v6_task_start slow "$started" slow_body
slow_pid="$V6_TASK_STARTED_PID"
sleep 0.2
v6_task_terminate_pids "$slow_pid"
"""
            subprocess.run(["bash", "-c", script], cwd=ROOT, check=True, timeout=10)
            status = json.loads((Path(td) / "slow.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "terminated")
            self.assertEqual(status["last_rc"], 143)
            self.assertGreaterEqual(status["finished"], status["started"])


if __name__ == "__main__":
    unittest.main()
