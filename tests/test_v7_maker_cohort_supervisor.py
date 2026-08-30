from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_maker_cohort_supervisor import (  # noqa: E402
    CohortSupervisor,
    atomic_json,
    flat_state,
    fresh_flow_eligible,
    inventory_drainable_state,
    inventory_flat_state,
    membership_sha256,
    read_json,
    validate_selection,
)


SHA = "a" * 40


def selection(market_id: str = "market-1") -> dict:
    return {
        "schema": "polymarket_v7_maker_reward_selection_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": SHA,
        "timestamp_ms": 1_000,
        "source": "adaptive_universe_recent_flow",
        "selected_count": 1,
        "resource_capacity_markets": 10,
        "markets": [{
            "condition_id": "condition-1",
            "market_id": market_id,
            "yes_token": "yes-1",
            "no_token": "no-1",
        }],
    }


def state(**changes: object) -> dict:
    value = {
        "schema": "polymarket_v7_professional_maker_state_v2",
        "timestamp_ms": 2_000,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": SHA,
        "active_order_count": 0,
        "flat_restart_safe": True,
        "new_risk_frozen": True,
        "inventory": {
            "market-1": {
                "yes_shares": 0.0,
                "no_shares": 0.0,
                "yes_cost": 0.0,
                "no_cost": 0.0,
            }
        },
    }
    value.update(changes)
    return value


class MakerCohortSupervisorTests(unittest.TestCase):
    @staticmethod
    def supervisor_args(run_root: Path, selection_path: Path, candidate_path: Path) -> SimpleNamespace:
        return SimpleNamespace(
            run_root=run_root,
            selection=selection_path,
            candidate=candidate_path,
            model_sha=SHA,
            drain_timeout_seconds=1.0,
            candidate_confirmations=2,
            min_rotation_interval_seconds=300.0,
        )

    def test_selection_validation_is_exact_sha_and_paper_only(self) -> None:
        self.assertEqual(len(validate_selection(selection(), SHA)), 1)
        unsafe = selection()
        unsafe["real_order_submission"] = True
        with self.assertRaisesRegex(ValueError, "selection_invalid"):
            validate_selection(unsafe, SHA)

    def test_membership_hash_ignores_non_membership_refresh_fields(self) -> None:
        first = selection()
        second = json.loads(json.dumps(first))
        second["timestamp_ms"] = 9_999
        second["markets"][0]["score"] = 123.0
        self.assertEqual(membership_sha256(first), membership_sha256(second))

    def test_rotation_requires_fresh_flat_and_acknowledged_freeze(self) -> None:
        self.assertTrue(flat_state(
            state(), SHA, newer_than_ms=1_999, require_frozen=True
        ))
        self.assertFalse(flat_state(state(timestamp_ms=1_000), SHA, newer_than_ms=1_999))
        self.assertFalse(flat_state(state(new_risk_frozen=False), SHA, require_frozen=True))
        self.assertFalse(flat_state(state(active_order_count=1), SHA))
        self.assertTrue(inventory_flat_state(state(active_order_count=1), SHA))

        held = state()
        held["inventory"]["market-1"]["yes_shares"] = 1.0
        self.assertFalse(flat_state(held, SHA))

        seeded = state(new_risk_frozen=False)
        seeded["inventory"]["market-1"].update({
            "yes_shares": 10.0,
            "no_shares": 10.0,
            "yes_cost": 5.0,
            "no_cost": 5.0,
            "yes_reserved_sell_shares": 0.0,
            "no_reserved_sell_shares": 0.0,
            "pending_split_shares": 0.0,
            "pending_merge_shares": 0.0,
        })
        self.assertTrue(inventory_drainable_state(seeded, SHA))
        seeded["inventory"]["market-1"]["yes_reserved_sell_shares"] = 2.0
        self.assertTrue(inventory_drainable_state(seeded, SHA))
        seeded["inventory"]["market-1"]["pending_merge_shares"] = 1.0
        self.assertFalse(inventory_drainable_state(seeded, SHA))
        seeded["inventory"]["market-1"]["pending_merge_shares"] = 0.0
        seeded["inventory"]["market-1"]["yes_shares"] = 9.0
        self.assertFalse(inventory_drainable_state(seeded, SHA))

    def test_runtime_contract_uses_one_flat_handoff_cohort(self) -> None:
        loop = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        runtime = (ROOT / "src" / "v7_market_maker_runtime.cpp").read_text(encoding="utf-8")
        supervisor = (ROOT / "scripts" / "v7_maker_cohort_supervisor.py").read_text(encoding="utf-8")
        self.assertIn("v7_maker_cohort_supervisor.py", loop)
        self.assertIn('"MAKER_ROTATION_DRAIN"', runtime)
        self.assertIn('root["active_order_count"]', runtime)
        self.assertIn('root["new_risk_frozen"]', runtime)
        self.assertIn("require_frozen=True", supervisor)
        self.assertIn("atomic_json(self.selection, latest)", supervisor)
        self.assertIn("--candidate-confirmations 2", loop)
        self.assertIn("--min-rotation-interval-seconds 300", loop)
        self.assertIn("authenticated_execution\") is not False", supervisor)
        self.assertIn("real_order_submission\") is not False", supervisor)

    def test_flat_handoff_promotes_candidate_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = root / "run"
            selection_path = run_root / "micro_maker/reward_selection.json"
            candidate_path = run_root / "micro_maker/reward_selection_candidate.json"
            current = selection("market-1")
            candidate = selection("market-2")
            candidate["markets"][0]["condition_id"] = "condition-2"
            candidate["markets"][0]["yes_token"] = "yes-2"
            candidate["markets"][0]["no_token"] = "no-2"
            atomic_json(selection_path, current)
            atomic_json(candidate_path, candidate)
            maker_state = state(timestamp_ms=2_000)
            atomic_json(run_root / "micro_maker/state.json", maker_state)
            args = self.supervisor_args(run_root, selection_path, candidate_path)
            supervisor = CohortSupervisor(args)
            calls: list[str] = []
            supervisor.cohort_healthy = lambda: True  # type: ignore[method-assign]
            supervisor.stop_cohort = lambda: calls.append("stop")  # type: ignore[method-assign]
            supervisor.start_cohort = lambda: calls.append("start")  # type: ignore[method-assign]
            with patch("v7_maker_cohort_supervisor.time.time_ns", return_value=1_000_000_000):
                supervisor.rotate_if_safe(candidate)
            self.assertEqual(calls, ["stop", "start"])
            self.assertEqual(supervisor.rotation_count, 1)
            self.assertEqual(
                membership_sha256(read_json(selection_path)), membership_sha256(candidate)
            )
            self.assertFalse(supervisor.drain.exists())

    def test_candidate_confirmation_counts_distinct_generations_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = CohortSupervisor(self.supervisor_args(
                root, root / "selection.json", root / "candidate.json"
            ))
            candidate = selection("market-2")
            self.assertFalse(supervisor.observe_candidate_generation(candidate))
            self.assertFalse(supervisor.observe_candidate_generation(candidate))
            self.assertEqual(supervisor.pending_confirmations, 1)
            candidate["timestamp_ms"] = 2_000
            self.assertTrue(supervisor.observe_candidate_generation(candidate))
            self.assertEqual(supervisor.pending_confirmations, 2)

    def test_distinct_noisy_candidate_generations_confirm_rotation_demand(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = CohortSupervisor(self.supervisor_args(
                root, root / "selection.json", root / "candidate.json"
            ))
            first = selection("market-2")
            second = selection("market-3")
            second["timestamp_ms"] = 2_000
            second["markets"][0].update({
                "condition_id": "condition-3", "yes_token": "yes-3", "no_token": "no-3",
            })
            self.assertFalse(supervisor.observe_candidate_generation(first))
            self.assertTrue(supervisor.observe_candidate_generation(second))
            self.assertEqual(supervisor.pending_confirmations, 2)
            supervisor.last_rotation_ms = 10_000
            self.assertEqual(supervisor.rotation_cooldown_remaining_seconds(70_000), 240.0)

    def test_quiet_flow_fallback_keeps_last_known_good_without_global_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection_path = root / "micro_maker/reward_selection.json"
            candidate_path = root / "micro_maker/reward_selection_candidate.json"
            current = selection("market-1")
            fallback = selection("market-2")
            fallback.update({"source": "adaptive_universe_fallback", "degraded": True})
            fallback["markets"][0].update({
                "condition_id": "condition-2", "yes_token": "yes-2", "no_token": "no-2",
            })
            atomic_json(selection_path, current)
            atomic_json(candidate_path, fallback)
            supervisor = CohortSupervisor(self.supervisor_args(
                root, selection_path, candidate_path
            ))

            self.assertTrue(fresh_flow_eligible(current))
            self.assertFalse(fresh_flow_eligible(fallback))
            self.assertIsNone(supervisor.pending_candidate())
            self.assertFalse(supervisor.sync_no_fresh_flow_pause())
            self.assertFalse(supervisor.drain.exists())
            self.assertEqual(
                membership_sha256(read_json(selection_path)), membership_sha256(current)
            )

            different_fresh = selection("market-3")
            different_fresh["timestamp_ms"] = 2_000
            different_fresh["markets"][0].update({
                "condition_id": "condition-3", "yes_token": "yes-3", "no_token": "no-3",
            })
            atomic_json(candidate_path, different_fresh)
            self.assertFalse(supervisor.sync_no_fresh_flow_pause())
            self.assertFalse(supervisor.drain.exists())

            resumed = json.loads(json.dumps(current))
            resumed["timestamp_ms"] = 3_000
            atomic_json(candidate_path, resumed)
            self.assertFalse(supervisor.sync_no_fresh_flow_pause())
            self.assertFalse(supervisor.drain.exists())


if __name__ == "__main__":
    unittest.main()
