from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def load_contract():
    path = ROOT / "scripts" / "v7_evidence_candidate_contract.py"
    spec = importlib.util.spec_from_file_location("v7_evidence_candidate_contract_tested", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTRACT = load_contract()


class EvidenceCandidateContractTest(unittest.TestCase):
    def make_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        base = Path(temporary.name)
        main = base / "main"
        candidate = base / "candidate"
        for root in (main, candidate):
            (root / "config").mkdir(parents=True)
            (root / "scripts").mkdir(parents=True)
        policy = {
            "schema_version": 2,
            "paper_only": True,
            "authenticated_execution": False,
            "candidate_contract": {
                "require_enabled_v7_champion": True,
                "champion_loop": "scripts/paper_v7_execution_loop.sh",
                "champion_config": "config/paper_v7.json",
                "champion_run_root": "runs/paper_v7_live",
                "require_operator_directives_match_main": True,
                "require_authoritative_fee": True,
                "require_shared_execution_ledger": True,
                "require_single_canonical_ledger_writer": True,
                "require_joint_fill_state_for_multileg": True,
                "require_complete_cost_vector": True,
                "require_account_drawdown_guard": True,
                "max_drawdown": 0.15,
            },
        }
        (main / "config/v7_evidence_runtime.json").write_text(json.dumps(policy), encoding="utf-8")
        directives = '{"authority":"current"}\n'
        (main / "config/operator_directives.json").write_text(directives, encoding="utf-8")
        (candidate / "config/operator_directives.json").write_text(directives, encoding="utf-8")
        (candidate / "config/v7_evidence_runtime.json").write_text(json.dumps(policy), encoding="utf-8")
        manifest = {
            "enabled": True,
            "version": 7,
            "loop": "scripts/paper_v7_execution_loop.sh",
            "config": "config/paper_v7.json",
            "run_root": "runs/paper_v7_live",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
        }
        paper = {
            "engine_version": 7,
            "paper_only": True,
            "max_drawdown": 0.15,
            "multi_strategy": {
                "single_account_allocator": True,
                "global_max_drawdown": 0.15,
            },
            "v7": {
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "authoritative_fee_required": True,
                "shared_execution_ledger_required": True,
                "single_canonical_ledger_writer": True,
                "joint_fill_state_required_for_multileg": True,
                "cost_vector_required": ["fee", "slippage", "unwind_loss", "capital_cost", "latency_cost"],
            },
        }
        (candidate / "config/live_champion.json").write_text(json.dumps(manifest), encoding="utf-8")
        (candidate / "config/paper_v7.json").write_text(json.dumps(paper), encoding="utf-8")
        (candidate / "config/v7_frequency_matrix.json").write_text("{}\n", encoding="utf-8")
        (candidate / "config/v7_execution_evidence.json").write_text("{}\n", encoding="utf-8")
        for rel in (
            "scripts/paper_v7_execution_loop.sh",
            "scripts/v7_execution_ledger.py",
            "scripts/v7_ledger_spool.py",
            "scripts/v7_canonical_economics.py",
            "scripts/v7_joint_execution_policy.py",
            "scripts/v7_capital_allocator.py",
            "scripts/v7_portfolio_guard.py",
            "scripts/v7_learned_execution_model.py",
        ):
            (candidate / rel).write_text("# fixture\n", encoding="utf-8")
        return temporary, main, candidate

    def validate(self, main: Path, candidate: Path):
        with mock.patch.object(CONTRACT, "git", return_value=SHA):
            return CONTRACT.validate(candidate, main, SHA)

    @staticmethod
    def mutate(path: Path, fn) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fn(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_accepts_only_final_exact_v7_paper_candidate(self):
        temporary, main, candidate = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        result = self.validate(main, candidate)
        self.assertEqual(result["source_sha"], SHA)
        self.assertEqual(result["champion_version"], "7")
        self.assertEqual(result["run_root"], "runs/paper_v7_live")

    def test_rejects_disabled_or_authenticated_champion(self):
        cases = (("enabled", False), ("version", 6), ("paper_only", False), ("authenticated_execution", True))
        for key, value in cases:
            with self.subTest(key=key):
                temporary, main, candidate = self.make_fixture()
                self.addCleanup(temporary.cleanup)
                self.mutate(candidate / "config/live_champion.json", lambda m, k=key, v=value: m.__setitem__(k, v))
                with self.assertRaises(SystemExit):
                    self.validate(main, candidate)

    def test_rejects_missing_execution_economics_contract(self):
        mutations = (
            lambda c: c["v7"].__setitem__("authoritative_fee_required", False),
            lambda c: c["v7"].__setitem__("shared_execution_ledger_required", False),
            lambda c: c["v7"].__setitem__("single_canonical_ledger_writer", False),
            lambda c: c["v7"].__setitem__("joint_fill_state_required_for_multileg", False),
            lambda c: c["v7"].__setitem__("cost_vector_required", ["fee"]),
            lambda c: c.__setitem__("max_drawdown", 0.151),
        )
        for index, fn in enumerate(mutations):
            with self.subTest(index=index):
                temporary, main, candidate = self.make_fixture()
                self.addCleanup(temporary.cleanup)
                self.mutate(candidate / "config/paper_v7.json", fn)
                with self.assertRaises(SystemExit):
                    self.validate(main, candidate)

    def test_rejects_stale_operator_authority(self):
        temporary, main, candidate = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        (candidate / "config/operator_directives.json").write_text('{"authority":"stale"}\n', encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.validate(main, candidate)

    def test_canonical_ledger_primitive_is_required(self):
        temporary, main, candidate = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        (candidate / "scripts/v7_execution_ledger.py").unlink()
        with self.assertRaises(SystemExit):
            self.validate(main, candidate)


class EvidenceRouterStaticContractTest(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/v7-unified-paper-evidence.yml").read_text(encoding="utf-8")
        self.config = json.loads((ROOT / "config/v7_evidence_runtime.json").read_text(encoding="utf-8"))

    def test_no_hardcoded_superseded_candidate_or_champion(self):
        for forbidden in (
            "source_pr: 546",
            "research/v7-unified-final-evidence-20260826",
            "version') == 6",
            'version"] == 6',
        ):
            self.assertNotIn(forbidden, self.workflow)
        for deprecated_key in (
            "source_pr",
            "source_branch",
            "require_source_draft",
            "require_source_live_champion_unchanged",
        ):
            self.assertNotIn(deprecated_key, self.config)

    def test_router_requires_unique_canonical_integration_candidate(self):
        selection = self.config["source_selection"]
        self.assertEqual(selection["mode"], "single_open_canonical_integration_pr")
        self.assertEqual(selection["base_branch"], "main")
        self.assertEqual(selection["head_prefix"], "integration/v7-")
        for required in (
            "integration/v7-",
            "eligible_count",
            "if len(eligible)==0: raise SystemExit(10)",
            "if len(eligible)!=1: raise SystemExit(11)",
            "git merge-base --is-ancestor",
            "candidate-contract.json",
        ):
            self.assertIn(required, self.workflow)

    def test_zero_candidate_noops_but_ambiguity_fails(self):
        self.assertIn('if [[ "$rc" -eq 10 ]]', self.workflow)
        self.assertIn("selection=no_open_integration_v7_candidate", self.workflow)
        self.assertIn('test "$rc" -eq 0', self.workflow)
        self.assertIn("raise SystemExit(11)", self.workflow)

    def test_exact_head_green_checks_gate_private_runtime(self):
        self.assertEqual(
            set(self.config["required_successful_workflows"]),
            {"ci", "Polymarket Research Policy", "monitoring", "Private runtime single-writer validation"},
        )
        for required in (
            "head_sha=${SOURCE_SHA}",
            "row.get('head_sha')!=sha",
            "row.get('status')!='completed'",
            "row.get('conclusion')!='success'",
            "steps.source.outputs.ready == 'true'",
        ):
            self.assertIn(required, self.workflow)

    def test_runtime_is_isolated_by_sha_and_never_promotes_or_deploys(self):
        for required in (
            "$BASE/by-sha",
            "$WORKTREES/$SOURCE_SHA",
            "$RUNS/$SOURCE_SHA",
            "git -C \"$worktree\" rev-parse HEAD",
            "scripts/paper_v7_execution_loop.sh",
            "active.env",
            "SOURCE_SHA",
            "canonical_economics.json",
        ):
            self.assertIn(required, self.workflow)
        for forbidden in (
            "git push origin paper-validated",
            "gh pr merge",
            "POLYMARKET_DEPLOY_REF=",
            "force=true",
            "authenticated_execution=true",
        ):
            self.assertNotIn(forbidden, self.workflow)

    def test_paginated_pr_search_is_valid_json(self):
        self.assertIn("gh api --paginate --slurp", self.workflow)


if __name__ == "__main__":
    unittest.main()
