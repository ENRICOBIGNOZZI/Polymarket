#!/usr/bin/env python3
"""Rotate the zero-authority PAPER maker evidence-observer cohort."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any


ALLOWED_SOURCES = {
    "public_clob_rewards",
    "adaptive_universe_fallback",
    "adaptive_universe_recent_flow",
}
# The owning shell grants its children a five-second bounded shutdown window.
# Finish the nested cohort before that deadline so it can never orphan a WS
# process group during a whole-runtime cutover.
TERMINATION_GRACE_SECONDS = 3.0


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def validate_selection(value: dict[str, Any], model_sha: str) -> list[dict[str, Any]]:
    markets = value.get("markets")
    capacity = int(value.get("resource_capacity_markets") or 0)
    if (
        value.get("schema") != "polymarket_v7_maker_reward_selection_v1"
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("model_sha") != model_sha
        or value.get("execution_cell_authority_required") is not True
        or value.get("execution_authority_semantics") != "token_action_side_v2"
        or value.get("source") not in ALLOWED_SOURCES
        or not isinstance(markets, list)
        or not markets
        or int(value.get("selected_count") or 0) != len(markets)
        or capacity <= 0
        or len(markets) > capacity
    ):
        raise ValueError("maker_rotation_selection_invalid")
    keys = ("condition_id", "market_id", "yes_token", "no_token")
    for row in markets:
        if (
            not isinstance(row, dict)
            or not all(str(row.get(key) or "") for key in keys)
            or str(row.get("yes_token")) == str(row.get("no_token"))
            or not isinstance(row.get("authorized_execution_cells"), list)
            or not isinstance(row.get("inventory_seed_authorized"), bool)
        ):
            raise ValueError("maker_rotation_market_invalid")
        tokens = {str(row["yes_token"]), str(row["no_token"])}
        sell_authorized = False
        for cell in row["authorized_execution_cells"]:
            if not isinstance(cell, dict):
                raise ValueError("maker_rotation_cell_invalid")
            token = str(cell.get("token_id") or "")
            action = str(cell.get("action") or "").upper()
            side = str(cell.get("quote_side") or "").upper()
            try:
                projected_fill = float(cell.get("projected_fill_probability") or 0.0)
            except (TypeError, ValueError):
                raise ValueError("maker_rotation_cell_invalid") from None
            if (
                token not in tokens
                or action not in {"JOIN", "IMPROVE1", "FADE1", "FADE2"}
                or side not in {"BUY", "SELL"}
                or not 0.0 <= projected_fill <= 1.0
            ):
                raise ValueError("maker_rotation_cell_invalid")
            sell_authorized = sell_authorized or side == "SELL"
        if bool(row["inventory_seed_authorized"]) != sell_authorized:
            raise ValueError("maker_rotation_inventory_seed_authority_invalid")
    return markets


def membership_sha256(value: dict[str, Any]) -> str:
    rows = validate_selection(value, str(value.get("model_sha") or ""))
    membership = sorted(
        (str(row["condition_id"]), str(row["yes_token"]), str(row["no_token"]))
        for row in rows
    )
    encoded = json.dumps(membership, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def safe_membership_sha256(value: dict[str, Any]) -> str:
    try:
        return membership_sha256(value)
    except (KeyError, TypeError, ValueError):
        return ""


def fresh_flow_eligible(value: dict[str, Any]) -> bool:
    return (
        value.get("source") == "adaptive_universe_recent_flow"
        and value.get("degraded") is not True
    )


def degraded_fallback_control_refresh_eligible(
    runtime: dict[str, Any], candidate: dict[str, Any]
) -> bool:
    """Allow only an exact, non-escalating cold-start cell handoff.

    A quiet/stale public trade feed makes the selector generation degraded, but
    it does not make its deterministic no-fill evidence stale.  The fallback
    selector deliberately keeps the same warm observation cohort and moves one
    bounded JOIN/YES/BUY control cell to the least-attempted exact cell.  Refuse
    every other degraded refresh so a fallback can neither replace membership
    nor acquire normal quote or inventory-seed authority.
    """
    if (
        runtime.get("source") != "adaptive_universe_fallback"
        or candidate.get("source") != "adaptive_universe_fallback"
        or runtime.get("degraded") is not True
        or candidate.get("degraded") is not True
        or candidate.get("selection_mode") != "FLOW_FILLABILITY_FALLBACK"
    ):
        return False
    try:
        if membership_sha256(runtime) != membership_sha256(candidate):
            return False
    except (KeyError, TypeError, ValueError):
        return False

    authorized = 0
    for row in candidate.get("markets", []):
        if not isinstance(row, dict):
            return False
        cells = row.get("authorized_execution_cells")
        if not isinstance(cells, list):
            return False
        if bool(row.get("inventory_seed_authorized")):
            return False
        if row.get("quote_opportunities") not in (None, []):
            return False
        if int(row.get("authorized_execution_cell_count") or 0) != len(cells):
            return False
        if not cells:
            if bool(row.get("control_exploration_authorized")):
                return False
            continue
        if (
            len(cells) != 1
            or row.get("control_exploration_authorized") is not True
            or row.get("execution_role") != "COLD_START_CONTROL"
        ):
            return False
        cell = cells[0]
        if (
            not isinstance(cell, dict)
            or str(cell.get("token_id") or "") != str(row.get("yes_token") or "")
            or str(cell.get("outcome") or "").upper() != "YES"
            or str(cell.get("action") or "").upper() != "JOIN"
            or str(cell.get("quote_side") or "").upper() != "BUY"
            or cell.get("authority_basis") != "COLD_START_CONTROL"
        ):
            return False
        try:
            probabilities = (
                float(cell.get("projected_flow_reach_probability") or 0.0),
                float(cell.get("projected_queue_depletion_probability") or 0.0),
                float(cell.get("projected_fill_probability") or 0.0),
            )
        except (TypeError, ValueError):
            return False
        if any(abs(probability) > 1e-12 for probability in probabilities):
            return False
        authorized += 1
    return authorized == 1


def projected_cells(
    value: dict[str, Any], *, market_ids: set[str] | None = None,
    exclude_market_ids: set[str] | None = None,
) -> list[tuple[float, str]]:
    """Return selector fill projections keyed by a stable execution cell.

    Rotation is about a market x token x quote-side cell, not about incidental
    changes to the bottom of a ranked market list.  Invalid/missing projections
    are deliberately zero-authority.
    """
    output: list[tuple[float, str]] = []
    markets = value.get("markets")
    if not isinstance(markets, list):
        return output
    for market in markets:
        if not isinstance(market, dict):
            continue
        market_id = str(market.get("market_id") or "")
        if market_ids is not None and market_id not in market_ids:
            continue
        if exclude_market_ids is not None and market_id in exclude_market_ids:
            continue
        authority_cells = market.get("authorized_execution_cells")
        if not isinstance(authority_cells, list):
            continue
        for authority in authority_cells:
            if not isinstance(authority, dict):
                continue
            try:
                probability = float(
                    authority.get("projected_fill_probability") or 0.0
                )
            except (TypeError, ValueError):
                probability = 0.0
            if not (0.0 <= probability <= 1.0):
                continue
            token_id = str(authority.get("token_id") or "")
            action = str(authority.get("action") or "").upper()
            quote_side = str(authority.get("quote_side") or "").upper()
            cell = "|".join((market_id, token_id, action, quote_side))
            if (
                market_id and token_id
                and action in {"JOIN", "IMPROVE1", "FADE1", "FADE2"}
                and quote_side in {"BUY", "SELL"}
            ):
                output.append((probability, cell))
    return sorted(output, key=lambda item: (-item[0], item[1]))


def rotation_gate(
    runtime: dict[str, Any], candidate: dict[str, Any], *,
    minimum_projected_fill_probability: float,
    minimum_absolute_improvement: float,
    minimum_relative_multiplier: float,
) -> tuple[bool, dict[str, Any]]:
    """Require a material new fill cell before destroying incumbent queues."""
    runtime_ids = {
        str(row.get("market_id") or "")
        for row in runtime.get("markets", []) if isinstance(row, dict)
    }
    incumbent = projected_cells(candidate, market_ids=runtime_ids)
    challenger = projected_cells(candidate, exclude_market_ids=runtime_ids)
    incumbent_probability = incumbent[0][0] if incumbent else 0.0
    challenger_probability = challenger[0][0] if challenger else 0.0
    challenger_cell = challenger[0][1] if challenger else ""
    minimum_probability = max(0.0, minimum_projected_fill_probability)
    # A recent-flow snapshot is an observation contract, not proof that the
    # incumbent execution cell is fillable. Treat a zero/sub-threshold cell as
    # cold start even when the surrounding 40-market cohort is fresh.
    cold_start = (
        not fresh_flow_eligible(runtime)
        or incumbent_probability < minimum_probability
    )
    # Projected maker fills are commonly sub-percent. A configuration value
    # from a coarser probability regime must not silently demand a five-point
    # absolute jump from a 0.3% incumbent. Bound the absolute increment by the
    # minimum economically admissible probability; the relative hurdle still
    # protects incumbent queue priority at larger probabilities.
    effective_absolute_improvement = min(
        max(0.0, minimum_absolute_improvement), minimum_probability)
    threshold = max(
        minimum_probability,
        incumbent_probability + effective_absolute_improvement,
        incumbent_probability * max(1.0, minimum_relative_multiplier),
    )
    allowed = bool(challenger_cell) and (
        (cold_start and challenger_probability >= minimum_probability)
        or (not cold_start and challenger_probability >= threshold)
    )
    reason = (
        "COLD_START_TO_FILLABLE_CELL" if allowed and cold_start
        else "MATERIAL_NEW_FILL_CELL" if allowed
        else "NO_NEW_PROJECTED_FILL_CELL" if not challenger_cell
        else "CHALLENGER_FILL_NOT_MATERIALLY_SUPERIOR"
    )
    return allowed, {
        "rotation_gate_reason": reason,
        "rotation_target_cell": challenger_cell,
        "incumbent_projected_fill_probability": incumbent_probability,
        "challenger_projected_fill_probability": challenger_probability,
        "required_challenger_projected_fill_probability": (
            minimum_probability if cold_start else threshold
        ),
        "configured_absolute_fill_improvement": max(
            0.0, minimum_absolute_improvement),
        "effective_absolute_fill_improvement": effective_absolute_improvement,
        "incumbent_below_minimum_fill_probability": (
            incumbent_probability < minimum_probability),
    }


class CohortSupervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_root = args.run_root
        self.control = self.run_root / "control"
        self.selection = args.selection
        self.candidate = args.candidate
        self.status = self.run_root / "micro_maker" / "rotation_status.json"
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.logs: list[Any] = []
        self.stop_requested = False
        self.rotation_count = 0
        self.cell_authority_refresh_count = 0
        self.last_cell_authority_refresh_ms = 0
        self.last_rotation_ms = 0
        self.pending_membership_sha256 = ""
        self.pending_rotation_key = ""
        self.pending_last_generation_ms = 0
        self.pending_confirmations = 0
        self.rotation_gate_metrics: dict[str, Any] = {
            "rotation_gate_reason": "NOT_EVALUATED",
        }

    def reset_pending_confirmation(self) -> None:
        self.pending_membership_sha256 = ""
        self.pending_rotation_key = ""
        self.pending_last_generation_ms = 0
        self.pending_confirmations = 0

    def observe_candidate_generation(self, candidate: dict[str, Any]) -> bool:
        """Confirm persistent rotation demand in distinct selector generations.

        Membership can change at the bottom of a recent-flow rank without
        changing the proposed execution cell. Confirm the same material new
        market x token x side target across generations; a different target
        starts again at one rather than inheriting confirmation credit.
        """
        membership = membership_sha256(candidate)
        rotation_key = str(self.rotation_gate_metrics.get("rotation_target_cell") or "")
        generation_ms = int(candidate.get("timestamp_ms") or 0)
        if generation_ms <= 0 or not rotation_key:
            self.reset_pending_confirmation()
            return False
        if generation_ms > self.pending_last_generation_ms:
            if self.pending_rotation_key and rotation_key != self.pending_rotation_key:
                self.pending_confirmations = 0
            self.pending_membership_sha256 = membership
            self.pending_rotation_key = rotation_key
            self.pending_last_generation_ms = generation_ms
            self.pending_confirmations = max(1, self.pending_confirmations + 1)
        return self.pending_confirmations >= self.args.candidate_confirmations

    def rotation_cooldown_remaining_seconds(self, now_ms: int | None = None) -> float:
        if self.last_rotation_ms <= 0:
            return 0.0
        current = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        elapsed = max(0.0, (current - self.last_rotation_ms) / 1000.0)
        return max(0.0, self.args.min_rotation_interval_seconds - elapsed)

    def write_status(self, state: str, **extra: Any) -> None:
        runtime = read_json(self.selection)
        candidate = read_json(self.candidate)
        cutover_drain_requested = (self.control / "CUTOVER_DRAIN").exists()
        payload: dict[str, Any] = {
            "schema": "polymarket_v7_maker_cohort_rotation_status_v1",
            "timestamp_ms": time.time_ns() // 1_000_000,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": self.args.model_sha,
            "state": state,
            "rotation_count": self.rotation_count,
            "cell_authority_refresh_count": self.cell_authority_refresh_count,
            "last_cell_authority_refresh_ms": self.last_cell_authority_refresh_ms,
            "last_rotation_ms": self.last_rotation_ms,
            "candidate_confirmations": self.pending_confirmations,
            "candidate_required_confirmations": self.args.candidate_confirmations,
            "rotation_cooldown_remaining_seconds": self.rotation_cooldown_remaining_seconds(),
            "runtime_membership_sha256": (
                safe_membership_sha256(runtime) if runtime else ""
            ),
            "candidate_membership_sha256": (
                safe_membership_sha256(candidate) if candidate else ""
            ),
            "fresh_flow_pause_active": False,
            "directional_drain_market_count": 0,
            "directional_drain_markets": [],
            "cohort_pids": {name: process.pid for name, process in self.processes.items()},
            "cohort_mode": "SHADOW_OBSERVERS_ONLY",
            "execution_authority": "SHADOW_ZERO_AUTHORITY",
        }
        payload.update(self.rotation_gate_metrics)
        payload.update(extra)
        atomic_json(self.status, payload)
        atomic_json(self.run_root / "micro_maker" / "status.json", {
                "schema": "polymarket_v7_professional_maker_status_v1",
                "timestamp": int(time.time()),
                "timestamp_ms": time.time_ns() // 1_000_000,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "execution_authority": "SHADOW_ZERO_AUTHORITY",
                "capital_authority": False,
                "ledger_writer_authority": False,
                "model_sha": self.args.model_sha,
                "source": "shadow_markout_and_fillability_observers",
                "state": state,
                "cash": 0.0,
                "equity": 0.0,
                "peak": 0.0,
                "drawdown": 0.0,
                "killed": False,
                "new_risk_frozen": True,
                "drain_requested": cutover_drain_requested,
                "drain_complete": cutover_drain_requested,
                "open_orders": 0,
                "open_positions": 0,
                "observer_pids": {
                    name: process.pid for name, process in self.processes.items()
                },
        })

    def command_specs(self) -> dict[str, tuple[list[str], Path, dict[str, str]]]:
        base_env = os.environ.copy()
        observer_env = dict(base_env)
        observer_env["PM_V7_WS_JSON_ARENA_MAX_BYTES"] = str(self.args.observer_arena_bytes)
        fillability_env = dict(base_env)
        fillability_env["PM_V7_WS_JSON_ARENA_MAX_BYTES"] = str(
            self.args.fillability_arena_bytes
        )
        specifications = {
            "markout": ([
                str(self.args.markout_observer),
                "--config", str(self.args.config),
                "--run-root", str(self.run_root),
                "--selection", str(self.selection),
                "--model-sha", self.args.model_sha,
            ], self.run_root / "micro_maker" / "markout_observer.log", observer_env),
            "fillability": ([
                str(self.args.fillability_observer),
                "--config", str(self.args.config),
                "--run-root", str(self.run_root),
                "--selection", str(self.selection),
                "--model-sha", self.args.model_sha,
            ], self.run_root / "micro_maker" / "fillability_observer.log", fillability_env),
        }
        return specifications

    def start_cohort(self) -> None:
        validate_selection(read_json(self.selection), self.args.model_sha)
        self.processes.clear()
        self.logs.clear()
        for name, (command, log_path, environment) in self.command_specs().items():
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("ab", buffering=0)
            self.logs.append(log)
            self.processes[name] = subprocess.Popen(
                command,
                cwd=self.args.repository_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def stop_cohort(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline and any(
            process.poll() is None for process in self.processes.values()
        ):
            time.sleep(0.05)
        for process in self.processes.values():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        self.processes.clear()
        for log in self.logs:
            log.close()
        self.logs.clear()

    def cohort_healthy(self) -> bool:
        expected = 2
        return len(self.processes) == expected and all(
            process.poll() is None for process in self.processes.values()
        )

    def pending_candidate(self) -> dict[str, Any] | None:
        runtime = read_json(self.selection)
        candidate = read_json(self.candidate)
        try:
            validate_selection(runtime, self.args.model_sha)
            validate_selection(candidate, self.args.model_sha)
        except ValueError:
            self.rotation_gate_metrics = {
                "rotation_gate_reason": "INVALID_SELECTION_REJECTED",
            }
            return None
        if membership_sha256(runtime) == membership_sha256(candidate):
            self.rotation_gate_metrics = {
                "rotation_gate_reason": "MEMBERSHIP_UNCHANGED",
            }
            return None
        # A degraded fallback is useful only to cold-start a runtime that has no
        # selection.  Once a valid cohort is live, a stale/failed flow source
        # must not turn an unrelated 24h-liquidity fallback into a drain and
        # whole-cohort restart.  Keep the last-known-good membership quoting and
        # wait for a non-degraded generation.
        if candidate.get("degraded") is True:
            self.rotation_gate_metrics = {
                "rotation_gate_reason": "DEGRADED_CANDIDATE_REJECTED",
            }
            return None
        allowed, metrics = rotation_gate(
            runtime, candidate,
            minimum_projected_fill_probability=(
                self.args.rotation_min_projected_fill_probability),
            minimum_absolute_improvement=(
                self.args.rotation_min_absolute_fill_improvement),
            minimum_relative_multiplier=(
                self.args.rotation_min_relative_fill_multiplier),
        )
        self.rotation_gate_metrics = metrics
        if not allowed:
            return None
        return candidate

    def refresh_same_membership_authority(self) -> bool:
        """Project fresh authority onto every already-warm market identity.

        Candidate ranking churn must not force a process restart when its best
        cell is already subscribed by the runtime observation universe. Missing
        candidate rows lose authority but remain warm; new identities still go
        through the explicit rotation gate.
        """
        runtime = read_json(self.selection)
        candidate = read_json(self.candidate)
        try:
            validate_selection(runtime, self.args.model_sha)
            validate_selection(candidate, self.args.model_sha)
        except ValueError:
            return False
        if int(candidate.get("timestamp_ms") or 0) <= int(
            runtime.get("timestamp_ms") or 0
        ):
            return False
        if (
            candidate.get("degraded") is True
            and not degraded_fallback_control_refresh_eligible(runtime, candidate)
        ):
            return False
        runtime_rows = {
            str(row.get("market_id") or ""): row
            for row in runtime["markets"] if isinstance(row, dict)
        }
        candidate_rows = {
            str(row.get("market_id") or ""): row
            for row in candidate["markets"] if isinstance(row, dict)
        }
        projected_rows: list[dict[str, Any]] = []
        overlap = 0
        for market_id, incumbent in runtime_rows.items():
            challenger = candidate_rows.get(market_id)
            if challenger is not None and all(
                str(challenger.get(key) or "") == str(incumbent.get(key) or "")
                for key in ("condition_id", "yes_token", "no_token")
            ):
                projected_rows.append(dict(challenger))
                overlap += 1
                continue
            observation = dict(incumbent)
            observation.update({
                "execution_role": "WARM_RUNTIME_OBSERVATION",
                "control_exploration_authorized": False,
                "authorized_execution_cells": [],
                "authorized_execution_cell_count": 0,
                "inventory_seed_authorized": False,
                "quote_opportunities": [],
            })
            projected_rows.append(observation)
        if overlap <= 0:
            return False
        projected = dict(candidate)
        projected["markets"] = projected_rows
        projected["selected_count"] = len(projected_rows)
        projected["resource_capacity_markets"] = runtime[
            "resource_capacity_markets"]
        projected["in_place_authority_projection"] = True
        projected["candidate_membership_sha256"] = membership_sha256(candidate)
        projected["runtime_warm_membership_sha256"] = membership_sha256(runtime)
        projected["warm_identity_overlap_count"] = overlap
        projected["directional_drain_markets"] = []
        projected["authorized_execution_cell_count"] = sum(
            len(row.get("authorized_execution_cells") or [])
            for row in projected_rows
        )
        atomic_json(self.selection, projected)
        self.cell_authority_refresh_count += 1
        self.last_cell_authority_refresh_ms = time.time_ns() // 1_000_000
        self.rotation_gate_metrics = {
            "rotation_gate_reason": "WARM_MEMBERSHIP_CELL_AUTHORITY_REFRESHED",
            "warm_identity_overlap_count": overlap,
            "directional_drain_market_count": 0,
        }
        return True

    def rotate_if_safe(self, candidate: dict[str, Any]) -> None:
        target_cell = str(self.rotation_gate_metrics.get("rotation_target_cell") or "")
        if not target_cell:
            allowed, metrics = rotation_gate(
                read_json(self.selection), candidate,
                minimum_projected_fill_probability=(
                    self.args.rotation_min_projected_fill_probability),
                minimum_absolute_improvement=(
                    self.args.rotation_min_absolute_fill_improvement),
                minimum_relative_multiplier=(
                    self.args.rotation_min_relative_fill_multiplier),
            )
            self.rotation_gate_metrics = metrics
            if not allowed:
                self.write_status("PENDING_CONFIRMATION")
                return
            target_cell = str(metrics.get("rotation_target_cell") or "")
        latest = read_json(self.candidate)
        try:
            validate_selection(latest, self.args.model_sha)
        except ValueError:
            self.reset_pending_confirmation()
            self.write_status("PENDING_CONFIRMATION", reason="candidate_invalid_before_observer_rotation")
            return
        if latest.get("degraded") is True:
            self.reset_pending_confirmation()
            self.write_status("PENDING_CONFIRMATION", reason="candidate_degraded_before_observer_rotation")
            return
        still_allowed, latest_metrics = rotation_gate(
            read_json(self.selection), latest,
            minimum_projected_fill_probability=self.args.rotation_min_projected_fill_probability,
            minimum_absolute_improvement=self.args.rotation_min_absolute_fill_improvement,
            minimum_relative_multiplier=self.args.rotation_min_relative_fill_multiplier,
        )
        self.rotation_gate_metrics = latest_metrics
        if not still_allowed or latest_metrics.get("rotation_target_cell") != target_cell:
            self.reset_pending_confirmation()
            self.write_status(
                "PENDING_CONFIRMATION", reason="material_rotation_target_changed_before_observer_rotation",
            )
            return
        self.stop_cohort()
        atomic_json(self.selection, latest)
        self.rotation_count += 1
        self.last_rotation_ms = time.time_ns() // 1_000_000
        self.reset_pending_confirmation()
        self.start_cohort()
        self.write_status("RUNNING")

    def run(self) -> int:
        self.control.mkdir(parents=True, exist_ok=True)
        try:
            self.start_cohort()
            self.write_status("RUNNING")
            while not self.stop_requested and not (self.control / "KILL").exists():
                if not self.cohort_healthy():
                    self.write_status("FAILED", reason="cohort_process_exit")
                    return 2
                authority_refreshed = self.refresh_same_membership_authority()
                candidate = self.pending_candidate()
                if candidate is not None:
                    if not self.observe_candidate_generation(candidate):
                        self.write_status("PENDING_CONFIRMATION")
                    elif self.rotation_cooldown_remaining_seconds() > 0.0:
                        self.write_status("PENDING_COOLDOWN")
                    else:
                        self.rotate_if_safe(candidate)
                else:
                    self.reset_pending_confirmation()
                    self.write_status(
                        "RUNNING_CELL_REFRESHED" if authority_refreshed else "RUNNING"
                    )
                time.sleep(self.args.poll_seconds)
            return 0
        finally:
            self.stop_cohort()
            if self.stop_requested or (self.control / "KILL").exists():
                self.write_status("STOPPED")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--maker-policy", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--markout-observer", type=Path, required=True)
    parser.add_argument("--fillability-observer", type=Path, required=True)
    parser.add_argument("--observer-arena-bytes", type=int, required=True)
    parser.add_argument("--fillability-arena-bytes", type=int, required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--candidate-confirmations", type=int, default=2)
    parser.add_argument("--min-rotation-interval-seconds", type=float, default=300.0)
    parser.add_argument("--rotation-min-projected-fill-probability", type=float, default=0.05)
    parser.add_argument("--rotation-min-absolute-fill-improvement", type=float, default=0.05)
    parser.add_argument("--rotation-min-relative-fill-multiplier", type=float, default=1.5)
    args = parser.parse_args()
    if args.candidate_confirmations < 2:
        parser.error("--candidate-confirmations must be at least 2")
    if args.min_rotation_interval_seconds < 0.0:
        parser.error("--min-rotation-interval-seconds must be non-negative")
    if not 0.0 <= args.rotation_min_projected_fill_probability <= 1.0:
        parser.error("--rotation-min-projected-fill-probability must be in [0,1]")
    if not 0.0 <= args.rotation_min_absolute_fill_improvement <= 1.0:
        parser.error("--rotation-min-absolute-fill-improvement must be in [0,1]")
    if args.rotation_min_relative_fill_multiplier < 1.0:
        parser.error("--rotation-min-relative-fill-multiplier must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    supervisor = CohortSupervisor(args)

    def stop(_signum: int, _frame: Any) -> None:
        supervisor.stop_requested = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
