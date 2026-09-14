from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "v7_prepare_maker_forward_window",
    SCRIPTS / "v7_prepare_maker_forward_window.py",
)
assert SPEC and SPEC.loader
freeze = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = freeze
SPEC.loader.exec_module(freeze)
import v7_maker_forward_window_evaluator as evaluator

SHA = "a" * 40
NOW = 1_800_000_000_000


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def healthy_runtime(root: Path, *, sha: str = SHA, now_ms: int = NOW) -> None:
    (root / "control").mkdir(parents=True, exist_ok=True)
    (root / "control" / "deployed_sha").write_text(sha + "\n", encoding="utf-8")
    write_json(root / "control" / "runtime_status.json", {
        "version": 7,
        "model_sha": sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "state": "running",
        "killed": False,
        "economic_new_risk_ready": False,
        "timestamp": now_ms // 1000,
    })
    write_json(root / "micro_maker" / "reward_selection.json", {
        "model_sha": sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "timestamp_ms": now_ms - 100,
    })
    write_json(root / "micro_maker" / "fillability_ws_status.json", {
        "model_sha": sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "state": "running",
        "evidence_complete": True,
        "dropped_events": 0,
        "decoder_failures": 0,
        "timestamp_ms": now_ms - 50,
        "observer_session_id": "session-1",
        "connection_epoch": 3,
    })


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def init_git(root: Path) -> str:
    subprocess.check_call(["git", "init", "-q"], cwd=root)
    (root / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "tracked.txt"], cwd=root)
    subprocess.check_call([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-q", "-m", "initial",
    ], cwd=root)
    head = git(root, "rev-parse", "HEAD")
    subprocess.check_call([
        "git", "update-ref", "refs/remotes/origin/main", head,
    ], cwd=root)
    return head


class MakerForwardWindowFreezeTests(unittest.TestCase):
    def test_manifest_is_exactly_eight_hours_and_matches_frozen_evaluator_contract(self) -> None:
        manifest = freeze.build_manifest(
            expected_sha=SHA,
            start_ms=NOW,
            repository_proof={"head_sha": SHA, "origin_main_sha": SHA, "tracked_worktree_clean": True},
            runtime_proof={"ok": True},
            policy_proof={"ok": True},
            frozen_artifacts={"v2": {"sha256": "b" * 64}},
            evidence_baselines={"ledger": {"bytes": 10}},
            experiment_id="test-window",
        )
        self.assertEqual(
            manifest["window_end_ms"] - manifest["window_start_ms"],
            freeze.WINDOW_MS,
        )
        self.assertEqual(
            manifest["evidence_sufficiency"]["minimum_independent_fill_clusters"], 20
        )
        self.assertEqual(manifest["evidence_sufficiency"]["minimum_filled_shares"], 50.0)
        self.assertFalse(manifest["automatic_promotion"])
        self.assertEqual(manifest["required_authority_basis"], "FRESH_OPPOSITE_FLOW")
        self.assertEqual(manifest["repository_preflight"]["head_sha"], SHA)
        self.assertEqual(evaluator.validate_manifest(manifest), manifest)

    def test_runtime_preflight_requires_exact_sha_and_complete_fresh_observer(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            healthy_runtime(root)
            proof = freeze.validate_runtime(root, SHA, NOW)
            self.assertEqual(proof["observer_session_id"], "session-1")
            self.assertEqual(proof["connection_epoch"], 3)
            with self.assertRaisesRegex(ValueError, "runtime:deployed_sha"):
                freeze.validate_runtime(root, "b" * 40, NOW)

    def test_runtime_preflight_rejects_stale_or_lossy_observer(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            healthy_runtime(root)
            status_path = root / "micro_maker" / "fillability_ws_status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["dropped_events"] = 1
            write_json(status_path, status)
            with self.assertRaisesRegex(ValueError, "evidence_loss"):
                freeze.validate_runtime(root, SHA, NOW)
            status["dropped_events"] = 0
            status["timestamp_ms"] = NOW - 6_000
            write_json(status_path, status)
            with self.assertRaisesRegex(ValueError, "stale"):
                freeze.validate_runtime(root, SHA, NOW)

    def test_current_policy_is_flow_first_and_cold_start_fallback_is_off(self) -> None:
        proof = freeze.validate_policy_config(ROOT)
        self.assertTrue(proof["anchor_causal_flow_authority_enabled"])
        self.assertFalse(proof["anchor_execution_authority_enabled"])
        self.assertFalse(proof["zero_flow_execution_fallback_enabled"])
        self.assertEqual(proof["rotation_min_projected_fill_probability"], 0.004)

    def test_repository_state_requires_clean_head_equal_origin_main(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            head = init_git(root)
            proof = freeze.validate_repository_state(root, head)
            self.assertEqual(proof["head_sha"], head)
            self.assertEqual(proof["origin_main_sha"], head)
            self.assertTrue(proof["tracked_worktree_clean"])

            (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "tracked_worktree_dirty"):
                freeze.validate_repository_state(root, head)

            subprocess.check_call(["git", "checkout", "--", "tracked.txt"], cwd=root)
            (root / "tracked.txt").write_text("two\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "tracked.txt"], cwd=root)
            subprocess.check_call([
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-q", "-m", "second",
            ], cwd=root)
            new_head = git(root, "rev-parse", "HEAD")
            with self.assertRaisesRegex(ValueError, "origin_main_sha"):
                freeze.validate_repository_state(root, new_head)

    def test_file_baseline_required_missing_and_symlink_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "evidence_baseline:missing"):
                freeze.file_baseline(root / "missing.jsonl", required=True)
            target = root / "target"
            target.write_text("x", encoding="utf-8")
            link = root / "link"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "not_regular_file"):
                freeze.file_baseline(link, required=True)

    def test_book_evidence_tree_is_content_hashed_and_required(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "books"
            evidence.mkdir()
            (evidence / "a.jsonl").write_text('{"x":1}\n', encoding="utf-8")
            (evidence / "b.json").write_text('{"y":2}\n', encoding="utf-8")
            first = freeze.evidence_tree_baseline([evidence])
            second = freeze.evidence_tree_baseline([evidence])
            self.assertEqual(first["files"], 2)
            self.assertEqual(first["tree_sha256"], second["tree_sha256"])
            self.assertGreater(first["bytes"], 0)
            with self.assertRaisesRegex(ValueError, "missing_argument"):
                freeze.evidence_tree_baseline([])
            with self.assertRaisesRegex(ValueError, "missing_or_symlink"):
                freeze.evidence_tree_baseline([root / "missing"])


if __name__ == "__main__":
    unittest.main()
