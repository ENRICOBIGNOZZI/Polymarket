from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_research_shadow_supervisor as supervisor


SHA = "a" * 40


class Clock:
    def __init__(self, value: int = 1_000):
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += seconds


def write_scope(path: Path, *, supervised=None, excluded=None) -> Path:
    excluded_values = list(
        excluded if excluded is not None else supervisor.EXCLUDED_LIVE_FAMILIES
    )
    supervised_values = list(
        supervised if supervised is not None else supervisor.SUPERVISED_FAMILIES
    )
    value = {
        "schema": "polymarket_v7_live_model_scope_v1",
        "version": 7,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "target_live_count": 12,
        "target_live_families": [
            "professional_maker", "fast_structural", "hard_arb", "graph_rv",
            "crypto_settlement_fair", "crypto_informed_taker", "micro_taker",
            "osint", "market_open", "sports_latency", "cross_platform",
            "wallet_intelligence",
        ],
        "research_shadow_supervised_families": supervised_values,
        "excluded_live_families": excluded_values,
        "governance": {
            "single_execution_owner": True,
            "research_has_capital": False,
            "research_has_oms_authority": False,
            "research_has_ledger_writer_authority": False,
            "automatic_promotion": False,
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class ResearchShadowSupervisorTest(unittest.TestCase):
    def make(self, root: Path, clock: Clock):
        scope = write_scope(root / "scope.json")
        app = supervisor.ResearchShadowSupervisor(
            run_root=root / "run", model_sha=SHA, scope_path=scope, clock=clock,
        )
        self.addCleanup(app.release_lock)
        return app

    def test_manifest_has_exact_schema_path_and_only_three_families(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.make(root, Clock())
            self.assertEqual(app.manifest_path, root / "run/control/research_sleeves_manifest.json")
            manifest = json.loads(app.manifest_path.read_text())
            self.assertEqual(manifest["schema"], supervisor.MANIFEST_SCHEMA)
            self.assertEqual(manifest["version"], 7)
            self.assertEqual(manifest["model_sha"], SHA)
            self.assertTrue(manifest["paper_only"])
            self.assertFalse(manifest["authenticated_execution"])
            self.assertFalse(manifest["real_order_submission"])
            names = set(manifest["families"])
            self.assertEqual(names, set(supervisor.SUPERVISED_FAMILIES))
            self.assertEqual(len(manifest["families"]), 3)
            self.assertTrue(set(supervisor.EXCLUDED_LIVE_FAMILIES).isdisjoint(names))

    def test_each_family_is_running_shadow_but_evidence_blocked_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.make(root, Clock())
            manifest = json.loads(app.manifest_path.read_text())
            for row in manifest["families"].values():
                self.assertEqual(row["authority"], "RESEARCH")
                self.assertEqual(row["process_state"], "RUNNING")
                self.assertEqual(row["evidence_state"], "BLOCKED_CONFIG")
                self.assertEqual(row["last_attempt_ts"], 0)
                self.assertEqual(row["last_success_ts"], 0)
                self.assertFalse(row["execution_authority"])
                self.assertFalse(row["capital_authority"])
                self.assertFalse(row["oms_authority"])
                self.assertFalse(row["ledger_write_authority"])
                status = json.loads(Path(row["status_path"]).read_text())
                self.assertEqual(status["evidence_state"], "BLOCKED_CONFIG")
                self.assertTrue(status["reason_codes"])
                self.assertFalse(status["authenticated_execution"])
                self.assertFalse(status["real_order_submission"])

    def test_atomic_heartbeat_preserves_blocked_state_and_exact_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            root, clock = Path(directory), Clock()
            app = self.make(root, clock)
            clock.advance(5)
            app.write_heartbeat()
            heartbeat = json.loads(app.heartbeat_path.read_text())
            self.assertEqual(heartbeat["timestamp"], 1005)
            self.assertEqual(heartbeat["model_sha"], SHA)
            self.assertEqual(set(heartbeat["families"]), set(supervisor.SUPERVISED_FAMILIES))
            self.assertTrue(all(
                row["evidence_state"] == "BLOCKED_CONFIG"
                for row in heartbeat["families"].values()
            ))
            self.assertEqual(list(app.control_root.glob("*.tmp.*")), [])

    def test_measured_component_status_replaces_generic_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            component = root / "run/shadow/cross_platform/component_status.json"
            component.parent.mkdir(parents=True)
            component.write_text(json.dumps({
                "schema": "polymarket_v7_external_input_component_status_v1",
                "version": 7, "family": "cross_platform", "authority": "RESEARCH",
                "model_sha": SHA, "timestamp": 998, "paper_only": True,
                "research_only": True, "authenticated_execution": False,
                "real_order_submission": False, "execution_authority": False,
                "capital_authority": False, "oms_authority": False,
                "ledger_write_authority": False, "promotion_authority": False,
                "implementation_complete": True, "evidence_state": "BLOCKED_EXTERNAL",
                "feed_status": "OPERATIONAL", "feed_operational": True,
                "mapping_status": "NO_VERIFIED_EQUIVALENCE", "verified_mappings": 0,
                "forward_collection_active": False, "last_attempt_ts": 998,
                "last_success_ts": 998, "blocker": "BLOCKED_NO_VERIFIED_EQUIVALENCE",
                "reason_codes": ["BLOCKED_NO_VERIFIED_EQUIVALENCE"],
            }), encoding="utf-8")
            app = self.make(root, Clock())
            manifest = json.loads(app.manifest_path.read_text())
            row = manifest["families"]["cross_platform"]
            self.assertEqual(row["evidence_state"], "BLOCKED_EXTERNAL")
            self.assertEqual(row["feed_status"], "OPERATIONAL")
            self.assertTrue(row["implementation_complete"])
            self.assertEqual(row["verified_mappings"], 0)
            self.assertEqual(row["blocker"], "BLOCKED_NO_VERIFIED_EQUIVALENCE")

    def test_stopped_heartbeat_remains_fail_closed_without_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.make(root, Clock())
            app.write_heartbeat(process_state="STOPPED")
            manifest = json.loads(app.manifest_path.read_text())
            for row in manifest["families"].values():
                self.assertEqual(row["process_state"], "STOPPED")
                self.assertEqual(row["evidence_state"], "BLOCKED_CONFIG")
                self.assertEqual(row["last_attempt_ts"], 0)
                self.assertEqual(row["last_success_ts"], 0)

    def test_scope_must_match_supervised_and_excluded_sets_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_supervised = write_scope(
                root / "bad-supervised.json", supervised=("sports_latency", "cross_platform"),
            )
            with self.assertRaisesRegex(supervisor.SupervisorError, "supervised_families_mismatch"):
                supervisor.validate_scope(bad_supervised)
            bad_excluded = write_scope(
                root / "bad-excluded.json", excluded=("ranking", "pca"),
            )
            with self.assertRaisesRegex(supervisor.SupervisorError, "excluded_families_mismatch"):
                supervisor.validate_scope(bad_excluded)

    def test_no_command_runner_or_execution_surface_is_present(self):
        self.assertFalse(hasattr(supervisor, "SubprocessRunner"))
        self.assertFalse(hasattr(supervisor, "JobSpec"))
        self.assertFalse(hasattr(supervisor.ResearchShadowSupervisor, "run_job"))
        self.assertFalse(hasattr(supervisor.ResearchShadowSupervisor, "tick"))

    def test_exact_sha_validation(self):
        self.assertEqual(supervisor.validate_model_sha(SHA.upper()), SHA)
        with self.assertRaisesRegex(supervisor.SupervisorError, "40_hex"):
            supervisor.validate_model_sha("abc")

    def test_status_and_manifest_are_rewritten_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root, clock = Path(directory), Clock()
            app = self.make(root, clock)
            for _ in range(3):
                clock.advance(1)
                app.write_heartbeat()
            self.assertTrue(app.manifest_path.is_file())
            self.assertTrue(app.heartbeat_path.is_file())
            for family in supervisor.SUPERVISED_FAMILIES:
                self.assertTrue((root / "run/shadow" / family / "status.json").is_file())
            self.assertEqual(list((root / "run").rglob("*.tmp.*")), [])

    def test_duplicate_live_supervisor_is_rejected_by_exact_sha_pid_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.make(root, Clock())
            owner = json.loads(first.lock_owner_path.read_text())
            self.assertEqual(owner["schema"], supervisor.LOCK_SCHEMA)
            self.assertEqual(owner["model_sha"], SHA)
            self.assertEqual(owner["pid"], os.getpid())
            with self.assertRaisesRegex(
                supervisor.SupervisorError, "supervisor_already_active"
            ):
                self.make(root, Clock())
            first.release_lock()
            released = json.loads(first.lock_owner_path.read_text())
            self.assertEqual(released["pid"], 0)
            self.assertEqual(released["state"], "STOPPED")

    def test_dead_owner_lock_is_recovered_and_release_is_owner_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = write_scope(root / "scope.json")
            lock = root / "run/control/research_shadow_supervisor.lock"
            lock.mkdir(parents=True)
            (lock / "owner.json").write_text(json.dumps({
                "schema": supervisor.LOCK_SCHEMA,
                "version": 7,
                "model_sha": SHA,
                "pid": 2_147_483_647,
            }), encoding="utf-8")
            app = supervisor.ResearchShadowSupervisor(
                run_root=root / "run", model_sha=SHA, scope_path=scope, clock=Clock(),
            )
            self.addCleanup(app.release_lock)
            owner = json.loads(app.lock_owner_path.read_text())
            self.assertEqual(owner["pid"], os.getpid())
            owner["pid"] = os.getpid() + 1
            app.lock_owner_path.write_text(json.dumps(owner), encoding="utf-8")
            app.release_lock()
            self.assertTrue(app.lock_path.is_dir())


if __name__ == "__main__":
    unittest.main()
