from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_slow_economic_shadow_supervisor as module  # noqa: E402

SHA = "a" * 40


class FakeProcess:
    next_pid = 1000

    def __init__(self, command, **_kwargs):
        self.command = command
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = 0
        output = Path(command[command.index("--output-json") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"paper_only": True, "research_only": True}))
        if "--output-shadow-intents" in command:
            intents = Path(command[command.index("--output-shadow-intents") + 1])
            intents.write_text("timestamp,action\n")

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        del timeout
        return self.returncode

    def kill(self):
        self.returncode = -9


class SlowEconomicShadowSupervisorTests(unittest.TestCase):
    def test_all_three_families_publish_exact_sha_shadow_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            scope.write_text(json.dumps({
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False,
                "always_on_economic_shadow_families": ["ranking", "pca", "local_factor"],
            }))
            args = argparse.Namespace(
                repository_root=ROOT, run_root=root / "run", scope=scope,
                model_sha=SHA, interval_seconds=7200, heartbeat_seconds=0.01,
                worker_timeout_seconds=10, market_limit=10, bootstrap_reps=50,
                once=True,
            )
            supervisor = module.Supervisor(args)
            with mock.patch.object(module.subprocess, "Popen", FakeProcess):
                supervisor.run_cycle()
            manifest = json.loads((root / "run/control/slow_research_shadow_manifest.json").read_text())
            self.assertEqual(set(manifest["families"]), {"ranking", "pca", "local_factor"})
            self.assertEqual(manifest["model_sha"], SHA)
            self.assertTrue(manifest["always_on"])
            for row in manifest["families"].values():
                self.assertEqual(row["process_state"], "ACTIVE_SHADOW_EVIDENCE")
                self.assertTrue(row["report_present"])
                self.assertFalse(row["has_capital"])
                self.assertFalse(row["has_oms_authority"])


if __name__ == "__main__":
    unittest.main()
