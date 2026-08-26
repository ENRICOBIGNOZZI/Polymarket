from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def load_cutover_module():
    path = ROOT / "scripts" / "v7_cutover_contract.py"
    spec = importlib.util.spec_from_file_location("v7_cutover_contract_tested", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CUTOVER = load_cutover_module()


class V7CutoverContractTest(unittest.TestCase):
    def make_tree(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "config").mkdir(parents=True)
        (root / "scripts").mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "enabled": True,
            "version": 7,
            "loop": "scripts/paper_v7_loop.sh",
            "config": "config/paper_v7.json",
            "run_root": "runs/paper_v7_live",
            "paper_only": True,
            "authenticated_execution": False,
        }
        config = {
            "engine_version": 7,
            "paper_only": True,
            "max_drawdown": 0.15,
            "v7": {
                "paper_only": True,
                "authenticated_execution": False,
                "authoritative_fee_required": True,
                "shared_execution_ledger_required": True,
                "joint_fill_state_required_for_multileg": True,
            },
        }
        (root / "config/live_champion.json").write_text(json.dumps(manifest), encoding="utf-8")
        (root / "config/paper_v7.json").write_text(json.dumps(config), encoding="utf-8")
        (root / "config/v7_frequency_matrix.json").write_text("{}\n", encoding="utf-8")
        (root / "config/v7_execution_evidence.json").write_text("{}\n", encoding="utf-8")
        for rel in (
            "scripts/paper_v7_loop.sh",
            "scripts/paper_v7_execution_loop.sh",
            "scripts/v7_runtime_status.py",
        ):
            (root / rel).write_text("# test fixture\n", encoding="utf-8")
        return temporary, root

    def validate(self, root: Path, expected: str = SHA):
        with mock.patch.object(CUTOVER, "_git", return_value=SHA):
            return CUTOVER.validate(root, expected)

    def mutate_json(self, path: Path, mutator) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutator(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_accepts_only_canonical_v7_paper_contract(self) -> None:
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        env = self.validate(root)
        self.assertEqual(env["V7_CUTOVER_SHA"], SHA)
        self.assertEqual(env["V7_CHAMPION_VERSION"], "7")
        self.assertEqual(env["V7_CHAMPION_LOOP"], "scripts/paper_v7_loop.sh")
        self.assertEqual(env["V7_CHAMPION_CONFIG"], "config/paper_v7.json")
        self.assertEqual(env["V7_CHAMPION_RUN_ROOT"], "runs/paper_v7_live")

    def test_disabled_or_non_v7_champion_fails_closed(self) -> None:
        for key, value in (("enabled", False), ("version", 6)):
            with self.subTest(key=key):
                temporary, root = self.make_tree()
                self.addCleanup(temporary.cleanup)
                self.mutate_json(root / "config/live_champion.json", lambda m, k=key, v=value: m.__setitem__(k, v))
                with self.assertRaises(SystemExit):
                    self.validate(root)

    def test_manifest_paper_auth_boundary_fails_closed(self) -> None:
        for key, value in (("paper_only", False), ("authenticated_execution", True)):
            with self.subTest(key=key):
                temporary, root = self.make_tree()
                self.addCleanup(temporary.cleanup)
                self.mutate_json(root / "config/live_champion.json", lambda m, k=key, v=value: m.__setitem__(k, v))
                with self.assertRaises(SystemExit):
                    self.validate(root)

    def test_authenticated_or_non_paper_config_fails_closed(self) -> None:
        mutations = (
            lambda c: c["v7"].__setitem__("authenticated_execution", True),
            lambda c: c.__setitem__("paper_only", False),
            lambda c: c["v7"].__setitem__("paper_only", False),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                temporary, root = self.make_tree()
                self.addCleanup(temporary.cleanup)
                self.mutate_json(root / "config/paper_v7.json", mutate)
                with self.assertRaises(SystemExit):
                    self.validate(root)

    def test_execution_evidence_primitives_and_drawdown_are_hard_gates(self) -> None:
        cases = (
            lambda c: c["v7"].__setitem__("authoritative_fee_required", False),
            lambda c: c["v7"].__setitem__("shared_execution_ledger_required", False),
            lambda c: c["v7"].__setitem__("joint_fill_state_required_for_multileg", False),
            lambda c: c.__setitem__("max_drawdown", 0.1501),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                temporary, root = self.make_tree()
                self.addCleanup(temporary.cleanup)
                self.mutate_json(root / "config/paper_v7.json", mutate)
                with self.assertRaises(SystemExit):
                    self.validate(root)

    def test_wrong_sha_or_noncanonical_paths_fail_closed(self) -> None:
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        with mock.patch.object(CUTOVER, "_git", return_value=SHA):
            with self.assertRaises(SystemExit):
                CUTOVER.validate(root, "b" * 40)
        self.mutate_json(
            root / "config/live_champion.json",
            lambda m: m.__setitem__("run_root", "runs/other"),
        )
        with self.assertRaises(SystemExit):
            self.validate(root)


class V7CutoverControlPlaneStaticTest(unittest.TestCase):
    def test_scheduler_registry_is_self_consistent(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "validate_scheduler_registry_tested", ROOT / "scripts/validate_scheduler_registry.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        errors, items = module.validate(ROOT, ROOT / "config/scheduler_registry.json")
        self.assertEqual(errors, [])
        self.assertGreater(len(items), 0)

    def test_authority_is_single_writer_and_v7_only(self) -> None:
        registry = json.loads((ROOT / "config/scheduler_registry.json").read_text(encoding="utf-8"))
        schedulers = registry["schedulers"]
        merge = [row["id"] for row in schedulers if row["merge_authority"]]
        deploy = [row["id"] for row in schedulers if row["deploy_authority"]]
        dispatch = [row["id"] for row in schedulers if row["validation_dispatch_authority"]]
        self.assertEqual(merge, ["integration-merge"])
        self.assertEqual(deploy, ["v7-paper-server-deploy"])
        self.assertEqual(dispatch, ["post-merge-validation"])

    def test_live_validation_requires_exact_green_sha_before_nonforce_paper_validated(self) -> None:
        text = (ROOT / ".github/workflows/v7-live-paper-validation.yml").read_text(encoding="utf-8")
        for required in (
            'workflows: ["ci", "monitoring"]',
            'test "$main_sha" = "$VALIDATION_SHA"',
            "scripts/v7_cutover_contract.py",
            "scripts/paper_v7_loop.sh",
            "scripts/v7_execution_evidence_hardened.py",
            "paper-validated",
            "force=false",
            "merged_pr",
        ):
            self.assertIn(required, text)
        self.assertNotIn("POLYMARKET_DEPLOY_REF=", text)
        self.assertNotIn("gh pr merge", text)

    def test_deploy_is_only_exact_paper_validated_v7_and_uses_existing_updater(self) -> None:
        text = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text(encoding="utf-8")
        for required in (
            "ref: paper-validated",
            "scripts/v7_cutover_contract.py",
            "EXPECTED_VALIDATED_SHA",
            "POLYMARKET_DEPLOY_REF=paper-validated",
            "ops/update_server_macos.sh",
            "ops/update_server.sh",
        ):
            self.assertIn(required, text)
        for forbidden in ("authenticated_execution: true", "gh pr merge", "git push"):
            self.assertNotIn(forbidden, text)

    def test_health_checks_exact_sha_single_writers_data_and_economics(self) -> None:
        text = (ROOT / ".github/workflows/v7-paper-server-health.yml").read_text(encoding="utf-8")
        for required in (
            'test "$head_sha" = "$validated_sha"',
            "paper_v7_loop",
            "paper_v7_execution_loop",
            "recorder_count",
            "broker_count",
            "polymarket_v7_market_proxy_status_v1",
            "polymarket_runtime_pnl_usd",
            "polymarket_runtime_equity_usd",
            "GRAFANA_DASHBOARD_UID",
        ):
            self.assertIn(required, text)
        self.assertNotIn("POLYMARKET_DEPLOY_REF=", text)


if __name__ == "__main__":
    unittest.main()
