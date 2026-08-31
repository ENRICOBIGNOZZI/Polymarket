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
    degraded_fallback_control_refresh_eligible,
    directional_inventory_markets,
    flat_state,
    fresh_flow_eligible,
    inventory_drainable_state,
    inventory_flat_state,
    membership_sha256,
    read_json,
    rotation_gate,
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
        "degraded": False,
        "execution_cell_authority_required": True,
        "execution_authority_semantics": "token_action_side_v2",
        "selected_count": 1,
        "resource_capacity_markets": 10,
        "markets": [{
            "condition_id": "condition-1",
            "market_id": market_id,
            "yes_token": "yes-1",
            "no_token": "no-1",
            "inventory_seed_authorized": False,
            "authorized_execution_cells": [{
                "token_id": "yes-1", "action": "JOIN", "quote_side": "BUY",
                "projected_fill_probability": 0.40,
            }],
            "quote_opportunities": [{
                "token_id": "yes-1", "quote_side": "BUY",
                "projected_best_fill_probability": 0.40,
            }],
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
            rotation_min_projected_fill_probability=0.05,
            rotation_min_absolute_fill_improvement=0.05,
            rotation_min_relative_fill_multiplier=1.5,
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
        # Selector churn must not cross a directional residual at the current
        # bid merely to make a whole-cohort membership handoff possible.
        self.assertFalse(inventory_drainable_state(seeded, SHA))
        self.assertEqual(directional_inventory_markets(seeded, SHA), {
            "market-1": -1.0,
        })

    def test_runtime_contract_uses_one_flat_handoff_cohort(self) -> None:
        loop = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        runtime = (ROOT / "src" / "v7_market_maker_runtime.cpp").read_text(encoding="utf-8")
        supervisor = (ROOT / "scripts" / "v7_maker_cohort_supervisor.py").read_text(encoding="utf-8")
        policy = json.loads((
            ROOT / "config" / "v7_professional_market_maker.json"
        ).read_text(encoding="utf-8"))
        self.assertIn("v7_maker_cohort_supervisor.py", loop)
        self.assertIn('"MAKER_ROTATION_DRAIN"', runtime)
        self.assertIn('root["active_order_count"]', runtime)
        self.assertIn('root["new_risk_frozen"]', runtime)
        self.assertIn("directional_rotation_preservations", runtime)
        self.assertIn("DIRECTIONAL_MARKET_DRAINING", supervisor)
        self.assertIn("require_frozen=True", supervisor)
        self.assertIn("atomic_json(self.selection, latest)", supervisor)
        self.assertIn('--candidate-confirmations "$MAKER_CANDIDATE_CONFIRMATIONS"', loop)
        self.assertIn('--min-rotation-interval-seconds "$MAKER_ROTATION_INTERVAL_SECONDS"', loop)
        self.assertIn('--rotation-min-projected-fill-probability "$MAKER_ROTATION_MIN_FILL"', loop)
        self.assertIn('--rotation-min-absolute-fill-improvement "$MAKER_ROTATION_MIN_ABSOLUTE_IMPROVEMENT"', loop)
        self.assertIn('--rotation-min-relative-fill-multiplier "$MAKER_ROTATION_MIN_RELATIVE_MULTIPLIER"', loop)
        self.assertGreaterEqual(
            policy["market_selection"]["recent_flow"]["rotation_min_interval_seconds"],
            300,
        )
        # The selector reports a five-second fill probability while the
        # bounded exploration order may rest for fifteen seconds.  A 0.4%
        # five-second floor implies about 1.2% over the allowed resting
        # horizon and does not repeat the former 5% cold-start deadlock.
        self.assertEqual(
            policy["market_selection"]["recent_flow"][
                "rotation_min_projected_fill_probability"
            ],
            0.004,
        )
        self.assertIn(
            'recent.get("rotation_min_interval_seconds") or 300', loop
        )
        self.assertIn(
            'recent.get("rotation_min_projected_fill_probability") or 0.004',
            loop,
        )
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
            candidate["markets"][0]["authorized_execution_cells"][0][
                "token_id"] = "yes-2"
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

    def test_flat_handoff_aborts_when_material_target_changes_during_drain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = root / "run"
            selection_path = run_root / "micro_maker/reward_selection.json"
            candidate_path = run_root / "micro_maker/reward_selection_candidate.json"
            current = selection("market-1")
            requested = selection("market-2")
            requested["markets"][0].update({
                "condition_id": "condition-2", "yes_token": "yes-2", "no_token": "no-2",
            })
            requested["markets"][0]["authorized_execution_cells"][0][
                "token_id"] = "yes-2"
            latest = selection("market-3")
            latest["timestamp_ms"] = 2_000
            latest["markets"][0].update({
                "condition_id": "condition-3", "yes_token": "yes-3", "no_token": "no-3",
            })
            latest["markets"][0]["authorized_execution_cells"][0][
                "token_id"] = "yes-3"
            atomic_json(selection_path, current)
            atomic_json(candidate_path, latest)
            atomic_json(run_root / "micro_maker/state.json", state(timestamp_ms=2_000))
            supervisor = CohortSupervisor(self.supervisor_args(
                run_root, selection_path, candidate_path
            ))
            calls: list[str] = []
            supervisor.cohort_healthy = lambda: True  # type: ignore[method-assign]
            supervisor.stop_cohort = lambda: calls.append("stop")  # type: ignore[method-assign]
            supervisor.start_cohort = lambda: calls.append("start")  # type: ignore[method-assign]
            with patch("v7_maker_cohort_supervisor.time.time_ns", return_value=1_000_000_000):
                supervisor.rotate_if_safe(requested)
            self.assertEqual(calls, [])
            self.assertEqual(supervisor.rotation_count, 0)
            self.assertEqual(
                membership_sha256(read_json(selection_path)), membership_sha256(current)
            )
            self.assertFalse(supervisor.drain.exists())

    def test_candidate_confirmation_counts_distinct_generations_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = CohortSupervisor(self.supervisor_args(
                root, root / "selection.json", root / "candidate.json"
            ))
            candidate = selection("market-2")
            supervisor.rotation_gate_metrics = {
                "rotation_target_cell": "market-2|yes-1|BUY",
            }
            self.assertFalse(supervisor.observe_candidate_generation(candidate))
            self.assertFalse(supervisor.observe_candidate_generation(candidate))
            self.assertEqual(supervisor.pending_confirmations, 1)
            candidate["timestamp_ms"] = 2_000
            self.assertTrue(supervisor.observe_candidate_generation(candidate))
            self.assertEqual(supervisor.pending_confirmations, 2)

    def test_distinct_target_cells_do_not_share_confirmation_credit(self) -> None:
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
            second["markets"][0]["authorized_execution_cells"][0][
                "token_id"] = "yes-3"
            supervisor.rotation_gate_metrics = {"rotation_target_cell": "market-2|yes-1|BUY"}
            self.assertFalse(supervisor.observe_candidate_generation(first))
            supervisor.rotation_gate_metrics = {"rotation_target_cell": "market-3|yes-3|BUY"}
            self.assertFalse(supervisor.observe_candidate_generation(second))
            self.assertEqual(supervisor.pending_confirmations, 1)
            supervisor.last_rotation_ms = 10_000
            self.assertEqual(supervisor.rotation_cooldown_remaining_seconds(70_000), 240.0)

    def test_rotation_requires_materially_superior_new_fill_cell(self) -> None:
        current = selection("incumbent")
        candidate = selection("incumbent")
        candidate["markets"][0]["authorized_execution_cells"][0][
            "projected_fill_probability"] = 0.60
        candidate["markets"].append({
            "condition_id": "condition-2", "market_id": "challenger",
            "yes_token": "yes-2", "no_token": "no-2",
            "inventory_seed_authorized": False,
            "authorized_execution_cells": [{
                "token_id": "yes-2", "action": "JOIN", "quote_side": "BUY",
                "projected_fill_probability": 0.70,
            }],
            "quote_opportunities": [{
                "token_id": "yes-2", "quote_side": "BUY",
                "projected_best_fill_probability": 0.70,
            }],
        })
        candidate["selected_count"] = 2
        allowed, metrics = rotation_gate(
            current, candidate,
            minimum_projected_fill_probability=0.05,
            minimum_absolute_improvement=0.05,
            minimum_relative_multiplier=1.5,
        )
        self.assertFalse(allowed)
        self.assertEqual(
            metrics["rotation_gate_reason"],
            "CHALLENGER_FILL_NOT_MATERIALLY_SUPERIOR",
        )
        candidate["markets"][0]["authorized_execution_cells"][0][
            "projected_fill_probability"] = 0.0
        allowed, metrics = rotation_gate(
            current, candidate,
            minimum_projected_fill_probability=0.05,
            minimum_absolute_improvement=0.05,
            minimum_relative_multiplier=1.5,
        )
        self.assertTrue(allowed)
        self.assertEqual(
            metrics["rotation_target_cell"], "challenger|yes-2|JOIN|BUY")

    def test_subpercent_rotation_gate_does_not_apply_five_point_hurdle(self) -> None:
        current = selection("incumbent")
        current["markets"][0]["authorized_execution_cells"][0][
            "projected_fill_probability"] = 0.003
        candidate = json.loads(json.dumps(current))
        candidate["markets"].append({
            "condition_id": "condition-2", "market_id": "challenger",
            "yes_token": "yes-2", "no_token": "no-2",
            "inventory_seed_authorized": False,
            "authorized_execution_cells": [{
                "token_id": "yes-2", "action": "JOIN", "quote_side": "BUY",
                "projected_fill_probability": 0.008,
            }],
            "quote_opportunities": [],
        })
        candidate["selected_count"] = 2

        allowed, metrics = rotation_gate(
            current, candidate,
            minimum_projected_fill_probability=0.004,
            minimum_absolute_improvement=0.05,
            minimum_relative_multiplier=1.5,
        )

        self.assertTrue(allowed)
        self.assertTrue(metrics["incumbent_below_minimum_fill_probability"])
        self.assertEqual(
            metrics["required_challenger_projected_fill_probability"], 0.004)
        self.assertEqual(metrics["configured_absolute_fill_improvement"], 0.05)
        self.assertEqual(metrics["effective_absolute_fill_improvement"], 0.004)

    def test_subpercent_exploit_rotation_keeps_relative_and_bounded_absolute_hurdles(self) -> None:
        current = selection("incumbent")
        current["markets"][0]["authorized_execution_cells"][0][
            "projected_fill_probability"] = 0.006
        candidate = json.loads(json.dumps(current))
        candidate["markets"].append({
            "condition_id": "condition-2", "market_id": "challenger",
            "yes_token": "yes-2", "no_token": "no-2",
            "inventory_seed_authorized": False,
            "authorized_execution_cells": [{
                "token_id": "yes-2", "action": "JOIN", "quote_side": "BUY",
                "projected_fill_probability": 0.009,
            }],
            "quote_opportunities": [],
        })
        candidate["selected_count"] = 2

        allowed, metrics = rotation_gate(
            current, candidate,
            minimum_projected_fill_probability=0.004,
            minimum_absolute_improvement=0.05,
            minimum_relative_multiplier=1.5,
        )

        self.assertFalse(allowed)
        self.assertFalse(metrics["incumbent_below_minimum_fill_probability"])
        self.assertEqual(
            metrics["required_challenger_projected_fill_probability"], 0.01)
        candidate["markets"][1]["authorized_execution_cells"][0][
            "projected_fill_probability"] = 0.011
        allowed, _ = rotation_gate(
            current, candidate,
            minimum_projected_fill_probability=0.004,
            minimum_absolute_improvement=0.05,
            minimum_relative_multiplier=1.5,
        )
        self.assertTrue(allowed)

    def test_same_membership_refreshes_cell_authority_without_cohort_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection_path = root / "selection.json"
            candidate_path = root / "candidate.json"
            current = selection()
            candidate = json.loads(json.dumps(current))
            candidate["timestamp_ms"] = 2_000
            candidate["markets"][0]["authorized_execution_cells"][0][
                "projected_fill_probability"] = 0.75
            atomic_json(selection_path, current)
            atomic_json(candidate_path, candidate)
            supervisor = CohortSupervisor(self.supervisor_args(
                root, selection_path, candidate_path
            ))

            self.assertTrue(supervisor.refresh_same_membership_authority())
            refreshed = read_json(selection_path)
            self.assertEqual(refreshed["timestamp_ms"], 2_000)
            self.assertEqual(supervisor.rotation_count, 0)
            self.assertEqual(supervisor.cell_authority_refresh_count, 1)
            self.assertFalse(supervisor.refresh_same_membership_authority())

    def test_degraded_fallback_rotates_one_exact_control_cell_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection_path = root / "selection.json"
            candidate_path = root / "candidate.json"
            current = selection("first")
            current.update({
                "source": "adaptive_universe_fallback",
                "degraded": True,
                "selection_mode": "FLOW_FILLABILITY_FALLBACK",
            })
            current["markets"][0].update({
                "execution_role": "COLD_START_CONTROL",
                "control_exploration_authorized": True,
                "authorized_execution_cell_count": 1,
                "quote_opportunities": [],
            })
            current["markets"][0]["authorized_execution_cells"][0].update({
                "outcome": "YES",
                "authority_basis": "COLD_START_CONTROL",
                "projected_flow_reach_probability": 0.0,
                "projected_queue_depletion_probability": 0.0,
                "projected_fill_probability": 0.0,
            })
            reserve = json.loads(json.dumps(current["markets"][0]))
            reserve.update({
                "market_id": "second",
                "condition_id": "condition-2",
                "yes_token": "yes-2",
                "no_token": "no-2",
                "execution_role": "WARM_FALLBACK_OBSERVATION",
                "control_exploration_authorized": False,
                "authorized_execution_cells": [],
                "authorized_execution_cell_count": 0,
            })
            current["markets"].append(reserve)
            current["selected_count"] = 2

            candidate = json.loads(json.dumps(current))
            candidate["timestamp_ms"] = 2_000
            candidate["markets"][0].update({
                "execution_role": "WARM_FALLBACK_OBSERVATION",
                "control_exploration_authorized": False,
                "authorized_execution_cells": [],
                "authorized_execution_cell_count": 0,
            })
            candidate["markets"][1].update({
                "execution_role": "COLD_START_CONTROL",
                "control_exploration_authorized": True,
                "authorized_execution_cells": [{
                    "token_id": "yes-2",
                    "outcome": "YES",
                    "action": "JOIN",
                    "quote_side": "BUY",
                    "authority_basis": "COLD_START_CONTROL",
                    "projected_flow_reach_probability": 0.0,
                    "projected_queue_depletion_probability": 0.0,
                    "projected_fill_probability": 0.0,
                }],
                "authorized_execution_cell_count": 1,
            })
            atomic_json(selection_path, current)
            atomic_json(candidate_path, candidate)
            supervisor = CohortSupervisor(self.supervisor_args(
                root, selection_path, candidate_path
            ))

            self.assertTrue(degraded_fallback_control_refresh_eligible(
                current, candidate
            ))
            self.assertTrue(supervisor.refresh_same_membership_authority())
            refreshed = read_json(selection_path)
            rows = {row["market_id"]: row for row in refreshed["markets"]}
            self.assertEqual(rows["first"]["authorized_execution_cells"], [])
            self.assertEqual(
                rows["second"]["authorized_execution_cells"][0]["token_id"],
                "yes-2",
            )
            self.assertEqual(supervisor.cell_authority_refresh_count, 1)

            unsafe = json.loads(json.dumps(candidate))
            unsafe["markets"][1]["authorized_execution_cells"][0][
                "projected_fill_probability"
            ] = 0.01
            self.assertFalse(degraded_fallback_control_refresh_eligible(
                current, unsafe
            ))

            fresh_runtime = json.loads(json.dumps(current))
            fresh_runtime.update({
                "source": "adaptive_universe_recent_flow", "degraded": False,
            })
            self.assertFalse(degraded_fallback_control_refresh_eligible(
                fresh_runtime, candidate
            ))

    def test_overlapping_warm_market_refreshes_without_membership_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection_path = root / "selection.json"
            candidate_path = root / "candidate.json"
            current = selection("warm")
            reserve = json.loads(json.dumps(current["markets"][0]))
            reserve.update({
                "market_id": "reserve", "condition_id": "reserve-condition",
                "yes_token": "reserve-yes", "no_token": "reserve-no",
                "authorized_execution_cells": [],
                "authorized_execution_cell_count": 0,
            })
            current["markets"].append(reserve)
            current["selected_count"] = 2
            candidate = selection("warm")
            candidate["timestamp_ms"] = 2_000
            candidate["markets"][0]["authorized_execution_cells"][0][
                "projected_fill_probability"] = 0.75
            challenger = json.loads(json.dumps(reserve))
            challenger.update({
                "market_id": "new", "condition_id": "new-condition",
                "yes_token": "new-yes", "no_token": "new-no",
            })
            candidate["markets"].append(challenger)
            candidate["selected_count"] = 2
            atomic_json(selection_path, current)
            atomic_json(candidate_path, candidate)
            supervisor = CohortSupervisor(self.supervisor_args(
                root, selection_path, candidate_path
            ))

            self.assertTrue(supervisor.refresh_same_membership_authority())
            refreshed = read_json(selection_path)
            self.assertEqual(
                {row["market_id"] for row in refreshed["markets"]},
                {"warm", "reserve"},
            )
            rows = {row["market_id"]: row for row in refreshed["markets"]}
            self.assertEqual(
                rows["warm"]["authorized_execution_cells"][0][
                    "projected_fill_probability"],
                0.75,
            )
            self.assertEqual(rows["reserve"]["authorized_execution_cells"], [])
            self.assertEqual(refreshed["warm_identity_overlap_count"], 1)
            self.assertEqual(supervisor.rotation_count, 0)

    def test_directional_market_drains_in_place_without_global_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection_path = root / "micro_maker/reward_selection.json"
            candidate_path = root / "micro_maker/reward_selection_candidate.json"
            current = selection("held")
            reserve = json.loads(json.dumps(current["markets"][0]))
            reserve.update({
                "market_id": "reserve", "condition_id": "reserve-condition",
                "yes_token": "reserve-yes", "no_token": "reserve-no",
                "authorized_execution_cells": [],
                "inventory_seed_authorized": False,
            })
            current["markets"].append(reserve)
            current["selected_count"] = 2
            candidate = selection("new")
            candidate["timestamp_ms"] = 2_000
            candidate["markets"][0].update({
                "condition_id": "new-condition",
                "yes_token": "new-yes", "no_token": "new-no",
            })
            candidate["markets"][0]["authorized_execution_cells"][0][
                "token_id"
            ] = "new-yes"
            atomic_json(selection_path, current)
            atomic_json(candidate_path, candidate)
            held_state = state()
            held_state["inventory"] = {
                "held": {
                    "yes_shares": 5.0, "no_shares": 0.0,
                    "yes_cost": 1.05, "no_cost": 0.0,
                }
            }
            atomic_json(root / "micro_maker/state.json", held_state)
            supervisor = CohortSupervisor(self.supervisor_args(
                root, selection_path, candidate_path
            ))

            self.assertTrue(supervisor.refresh_same_membership_authority())
            refreshed = read_json(selection_path)
            rows = {row["market_id"]: row for row in refreshed["markets"]}
            self.assertEqual(rows["held"]["execution_role"], "DRAINING_INVENTORY")
            self.assertEqual(rows["held"]["authorized_execution_cells"], [])
            self.assertFalse(rows["held"]["inventory_seed_authorized"])
            self.assertEqual(rows["reserve"]["execution_role"], "ROTATION_CONTROL")
            self.assertEqual(len(rows["reserve"]["authorized_execution_cells"]), 1)

            supervisor.rotate_if_safe(candidate)
            self.assertFalse(supervisor.drain.exists())
            self.assertEqual(supervisor.rotation_count, 0)
            status = read_json(root / "micro_maker/rotation_status.json")
            self.assertEqual(status["state"], "RUNNING_DIRECTIONAL_DRAIN")
            self.assertEqual(status["directional_drain_markets"], ["held"])

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
            fallback["markets"][0]["authorized_execution_cells"][0][
                "token_id"] = "yes-2"
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
            different_fresh["markets"][0]["authorized_execution_cells"][0][
                "token_id"] = "yes-3"
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
