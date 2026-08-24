from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "incumbent_health_gate.py"
WORKFLOW = ROOT / ".github" / "workflows" / "integration-merge.yml"
SERVER_HEALTH_WORKFLOW = ROOT / ".github" / "workflows" / "server-health.yml"
SHA = "1" * 40
OTHER_SHA = "2" * 40
NOW_EPOCH = 1787577000
CURRENT_TIMESTAMP = "2026-08-24T13:10:00Z"
STALE_TIMESTAMP = "2026-08-24T10:10:00Z"


class IncumbentHealthGateTest(unittest.TestCase):
    def run_gate(
        self,
        *,
        main_sha: str = SHA,
        validated_sha: str = SHA,
        deploy_enabled: bool = True,
        health_text: str | None = None,
        max_age_seconds: int = 7200,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            output = temp / "report.md"
            command = [
                "python3",
                str(SCRIPT),
                "--main-sha",
                main_sha,
                "--validated-sha",
                validated_sha,
                "--deploy-enabled",
                "true" if deploy_enabled else "false",
                "--max-age-seconds",
                str(max_age_seconds),
                "--now-epoch",
                str(NOW_EPOCH),
                "--output",
                str(output),
            ]
            if health_text is not None:
                health = temp / "server-health.txt"
                health.write_text(health_text, encoding="utf-8")
                command.extend(["--server-health", str(health)])
            return subprocess.run(
                command,
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

    @staticmethod
    def healthy_text(
        sha: str = SHA,
        recorder: str = "1",
        broker: str = "1",
        timestamp: str = CURRENT_TIMESTAMP,
    ) -> str:
        return (
            f"timestamp={timestamp}\n"
            "os=Darwin\n"
            f"head={sha}\n"
            f"origin_main={sha}\n"
            f"paper_validated={sha}\n"
            f"recorder_alive={recorder}\n"
            f"broker_alive={broker}\n"
        )

    def test_deployed_current_healthy_incumbent_passes(self):
        completed = self.run_gate(health_text=self.healthy_text())
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS: incumbent is fully validated", completed.stdout)
        self.assertIn(f"server_head: `{SHA}`", completed.stdout)
        self.assertIn("server_health_age_seconds: `0`", completed.stdout)

    def test_stale_health_evidence_fails(self):
        completed = self.run_gate(
            health_text=self.healthy_text(timestamp=STALE_TIMESTAMP),
            max_age_seconds=7200,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("server health evidence is stale", completed.stdout)

    def test_stale_deployed_head_fails(self):
        completed = self.run_gate(health_text=self.healthy_text(OTHER_SHA))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("does not match main", completed.stdout)

    def test_dead_broker_fails(self):
        completed = self.run_gate(health_text=self.healthy_text(broker="0"))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("server broker_alive is 0", completed.stdout)

    def test_missing_health_evidence_fails_when_deploy_enabled(self):
        completed = self.run_gate(health_text=None)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("server health evidence is required", completed.stdout)

    def test_deployment_disabled_still_requires_main_equal_validated(self):
        completed = self.run_gate(deploy_enabled=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("deployment_gate: `disabled`", completed.stdout)

        stale = self.run_gate(
            main_sha=SHA,
            validated_sha=OTHER_SHA,
            deploy_enabled=False,
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("main and paper-validated are not equal", stale.stdout)

    def test_integration_workflow_uses_automatic_promotion_after_public_validation(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "3,18,33,48 * * * *"', workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("Require current main to be publicly paper-validated", workflow)
        self.assertIn("git fetch --no-tags origin main paper-validated", workflow)
        self.assertIn('test "$main_sha" = "$validated_sha"', workflow)
        self.assertIn("Build locally-green automatic promotion queue", workflow)
        self.assertIn("Scan numbered research sources and select first fully-green candidate", workflow)
        self.assertIn("Automatically promote the first fully-green paper champion candidate", workflow)
        self.assertIn("scripts/integration_gate.py validate", workflow)
        self.assertIn("--match-head-commit", workflow)
        self.assertIn("champion-integration-merged", workflow)
        self.assertNotIn("SERVER_DEPLOY_ENABLED", workflow)
        self.assertNotIn("gh run list --workflow server-health.yml", workflow)
        self.assertNotIn("gh run download", workflow)
        self.assertNotIn("scripts/incumbent_health_gate.py", workflow)

    def test_private_health_is_not_disabled_with_automatic_deploy(self):
        workflow = SERVER_HEALTH_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "23 * * * *"', workflow)
        self.assertIn('workflows: ["deploy-paper-server", "Grafana Permanent Access"]', workflow)
        self.assertIn("github.event_name == 'schedule'", workflow)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", workflow)
        self.assertNotIn("vars.POLYMARKET_SERVER_DEPLOY", workflow)


if __name__ == "__main__":
    unittest.main()
