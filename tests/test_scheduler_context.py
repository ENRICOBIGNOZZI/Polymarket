from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_validator():
    path = ROOT / "scripts" / "validate_scheduler_context.py"
    spec = importlib.util.spec_from_file_location("validate_scheduler_context", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SchedulerContextContractTest(unittest.TestCase):
    def test_context_is_valid_and_assigns_every_registered_scheduler(self) -> None:
        validator = load_validator()
        context = validator.load_context(ROOT / "config" / "scheduler_context.json")
        self.assertEqual(validator.validate_context(context), [])
        registry = json.loads(
            (ROOT / "config" / "scheduler_registry.json").read_text(encoding="utf-8")
        )
        registered = {item["id"] for item in registry["schedulers"]}
        assigned = set(context["scheduler_contract"]["assignments"])
        self.assertEqual(registered, assigned)
        self.assertEqual(
            registry["administrator"]["scheduler_context"],
            "config/scheduler_context.json",
        )

    def test_remote_contract_matches_existing_channel_without_credentials(self) -> None:
        text = (ROOT / "config" / "scheduler_context.json").read_text(encoding="utf-8")
        remote = json.loads(text)["remote_runtime"]
        self.assertEqual(remote["access_path"], "GitHub Actions -> Tailscale -> SSH")
        self.assertEqual(remote["default_host"], "100.104.183.109")
        self.assertEqual(remote["default_user"], "enrico")
        self.assertEqual(remote["default_port"], 22)
        self.assertEqual(remote["home_relative_repository_path"], "polymarket")
        self.assertEqual(remote["deployment_ref"], "paper-validated")
        self.assertEqual(remote["ssh_key_secret"], "POLYMARKET_SERVER_SSH_KEY")
        self.assertEqual(remote["tailscale_auth_secret"], "TS_AUTHKEY")
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", text)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", text)

    def test_quantitative_contract_preserves_universal_architecture(self) -> None:
        architecture = json.loads(
            (ROOT / "config" / "scheduler_context.json").read_text(encoding="utf-8")
        )["quantitative_architecture"]
        self.assertEqual(
            set(architecture["universal_state"]),
            {
                "market_microstructure",
                "contract_characteristics",
                "cross_market_information",
                "external_information",
            },
        )
        self.assertEqual(len(architecture["alpha_engines"]), 5)
        self.assertEqual(
            architecture["ensemble"]["type"], "adaptive_mixture_of_experts"
        )
        self.assertEqual(
            architecture["separation"],
            [
                "probability_estimation",
                "trade_decision",
                "portfolio_construction_and_risk",
                "execution_and_reconciliation",
            ],
        )
        self.assertEqual(
            architecture["portfolio_and_risk"]["maximum_drawdown_ratio"], 0.15
        )
        self.assertFalse(
            architecture["operating_mode"]["authenticated_order_submission"]
        )
        self.assertTrue(
            architecture["operating_mode"]["real_money_requires_separate_approval"]
        )

    def test_autonomous_and_remote_workflows_load_context(self) -> None:
        workflows = {
            "administrator-supervisor": ".github/workflows/admin-supervisor.yml",
            "research-policy": ".github/workflows/research-policy.yml",
            "research-queue": ".github/workflows/research-queue.yml",
            "integration-merge": ".github/workflows/integration-merge.yml",
            "post-merge-validation": ".github/workflows/post-merge-validation.yml",
            "paper-server-deploy": ".github/workflows/deploy-paper-server.yml",
            "paper-server-health": ".github/workflows/server-health.yml",
            "forward-maker-research": ".github/workflows/forward-maker-research.yml",
            "alpha-factory": ".github/workflows/alpha-factory.yml",
            "meta-supervisor": ".github/workflows/control-plane.yml",
            "fast-arb-shadow-research": ".github/workflows/fast-arb-hourly.yml",
            "arb-theory-research": ".github/workflows/arb-theory-hourly.yml",
        }
        for scheduler_id, relative in workflows.items():
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("scripts/validate_scheduler_context.py", text, scheduler_id)
            self.assertIn(f"--scheduler-id {scheduler_id}", text, scheduler_id)

    def test_remote_workflows_use_context_repository_and_ref(self) -> None:
        deploy = (ROOT / ".github/workflows/deploy-paper-server.yml").read_text(
            encoding="utf-8"
        )
        health = (ROOT / ".github/workflows/server-health.yml").read_text(
            encoding="utf-8"
        )
        for text in (deploy, health):
            self.assertIn("POLYMARKET_REMOTE_REPO_REL", text)
            self.assertIn("POLYMARKET_DEPLOYMENT_REF", text)
            self.assertIn("$HOME/$REMOTE_REPO_REL", text)
            self.assertIn("POLYMARKET_SERVER_SSH_KEY", text)
            self.assertIn("TS_AUTHKEY", text)

    def test_theory_cycle_carries_context_into_candidate_evidence(self) -> None:
        theory = (ROOT / ".github/workflows/arb-theory-hourly.yml").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "scheduler-context.md",
            "scheduler-context.json",
            "combined-report.md",
            "docs/generated/scheduler_context.md",
            "config/candidates/scheduler_context.json",
        ):
            self.assertIn(phrase, theory)


if __name__ == "__main__":
    unittest.main()
