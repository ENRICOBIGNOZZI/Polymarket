from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "v7-freeze-maker-forward-window.yml"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "v7-deploy-paper-server.yml"


class FreezeMakerForwardWindowWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.deploy_text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    def test_only_successful_paper_deploy_can_trigger_freeze(self) -> None:
        text = self.text
        self.assertIn("workflow_run:", text)
        self.assertIn('workflows: ["V7 deploy PAPER server"]', text)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", text)
        self.assertNotIn("workflow_dispatch:", text)
        self.assertNotIn("schedule:", text)

    def test_deploy_must_explicitly_arm_window_and_default_is_off(self) -> None:
        deploy = self.deploy_text; freeze = self.text; marker = "freeze_maker_forward_window:"
        self.assertIn(marker, deploy)
        section = deploy.split(marker, 1)[1].split("permissions:", 1)[0]
        self.assertIn("required: true", section); self.assertIn("default: false", section)
        self.assertIn("FREEZE_MAKER_FORWARD_WINDOW", deploy)
        self.assertIn("freeze_maker_forward_window=%s", deploy)
        self.assertIn('freeze_requested="$(awk', freeze)
        self.assertIn('$1=="freeze_maker_forward_window"', freeze)
        self.assertIn('if [[ "$freeze_requested" != "true" ]]', freeze)
        self.assertIn("Deploy completed without explicit forward-window arming", freeze)

    def test_github_permissions_do_not_grant_write_authority(self) -> None:
        text = self.text
        self.assertIn("actions: read", text); self.assertIn("contents: read", text); self.assertIn("id-token: write", text)
        for forbidden in ("contents: write", "actions: write", "pull-requests: write",
                          "packages: write", "deployments: write", "statuses: write"):
            self.assertNotIn(forbidden, text)

    def test_exact_deploy_provenance_and_current_main_are_required(self) -> None:
        text = self.text
        for value in ("deploy-evidence.txt", "deployed_sha", "^[0-9a-f]{40}$",
                      "git rev-parse origin/main", "scripts/v7_cutover_contract.py",
                      "recovered_after_transport_loss"):
            self.assertIn(value, text)

    def test_remote_freeze_routes_through_tested_helper(self) -> None:
        text = self.text
        for value in ("scripts/v7_freeze_maker_forward_window_after_deploy.py", "--expected-sha",
                      "--deploy-run-id", "--wait-seconds 180", "--poll-seconds 2",
                      "runs/paper_v7_live", "runs/paper_v7_experiments"):
            self.assertIn(value, text)

    def test_manifest_is_returned_and_revalidated_cryptographically(self) -> None:
        text = self.text
        for value in ("maker-forward-manifest.json", "paper_only", "authenticated_execution",
                      "real_order_submission", "automatic_promotion", "2*60*60*1000",
                      ".deploy_freeze/${DEPLOY_RUN_ID}.manifest.json", "manifest_sha256",
                      "hashlib.sha256", "SHA256_EXACT_PREFIX_AT_RECORDED_BYTE_COUNT",
                      "completed <= prepared < start", "start-prepared == 5000"):
            self.assertIn(value, text)

    def test_ssh_and_scp_use_distinct_port_flags(self) -> None:
        text = self.text
        self.assertIn("ssh_common=(", text); self.assertIn("scp_common=(", text)
        self.assertRegex(text, r'ssh_common=\([\s\S]*?-p "\$SERVER_PORT"')
        self.assertRegex(text, r'scp_common=\([\s\S]*?-P "\$SERVER_PORT"')
        self.assertIn("$SERVER_USER@$SERVER_HOST:polymarket/runs/paper_v7_experiments/", text)
        self.assertNotIn("$SERVER_USER@$SERVER_HOST:$HOME/polymarket/runs/paper_v7_experiments/", text)

    def test_all_referenced_actions_are_commit_pinned(self) -> None:
        uses = re.findall(r"^\s*uses:\s*([^\s#]+)", self.text, flags=re.MULTILINE)
        self.assertGreaterEqual(len(uses), 5)
        for value in uses:
            self.assertRegex(value, r"^[^@\s]+@[0-9a-f]{40}$")

    def test_no_trading_endpoint_or_execution_command_is_introduced(self) -> None:
        lowered = self.text.lower()
        for forbidden in ("/order", "create_order", "post_order", "submit_order",
                          "authenticated_execution=true", "real_order_submission=true",
                          "paper_only=false", "real_capital_at_risk=true"):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
