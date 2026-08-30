#!/usr/bin/env python3
"""Rotate the PAPER Maker process cohort only through a proven flat handoff.

The C++ maker and its two evidence observers consume one immutable selection per
process generation. This supervisor follows the continuously refreshed
candidate selection without ever discarding hypothetical inventory or live
PAPER orders: it freezes new risk, waits for a fresh flat state, atomically
promotes the candidate, and restarts all three consumers as one cohort.
"""
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
        ):
            raise ValueError("maker_rotation_market_invalid")
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
        opportunities = market.get("quote_opportunities")
        if not isinstance(opportunities, list):
            continue
        for opportunity in opportunities:
            if not isinstance(opportunity, dict):
                continue
            try:
                probability = float(
                    opportunity.get("projected_best_fill_probability") or 0.0
                )
            except (TypeError, ValueError):
                probability = 0.0
            if not (0.0 <= probability <= 1.0):
                continue
            token_id = str(opportunity.get("token_id") or "")
            quote_side = str(opportunity.get("quote_side") or "").upper()
            cell = "|".join((market_id, token_id, quote_side))
            if market_id and token_id and quote_side in {"BUY", "SELL"}:
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
    cold_start = not fresh_flow_eligible(runtime)
    threshold = max(
        max(0.0, minimum_projected_fill_probability),
        incumbent_probability + max(0.0, minimum_absolute_improvement),
        incumbent_probability * max(1.0, minimum_relative_multiplier),
    )
    allowed = bool(challenger_cell) and (
        (cold_start and challenger_probability >= minimum_projected_fill_probability)
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
            max(0.0, minimum_projected_fill_probability) if cold_start else threshold
        ),
    }


def inventory_flat_state(
    value: dict[str, Any],
    model_sha: str,
    *,
    newer_than_ms: int = 0,
) -> bool:
    if (
        value.get("schema") != "polymarket_v7_professional_maker_state_v2"
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("model_sha") != model_sha
        or int(value.get("timestamp_ms") or 0) < newer_than_ms
    ):
        return False
    inventory = value.get("inventory")
    if not isinstance(inventory, dict):
        return False
    for row in inventory.values():
        if not isinstance(row, dict):
            return False
        for key in ("yes_shares", "no_shares", "yes_cost", "no_cost"):
            try:
                if abs(float(row.get(key) or 0.0)) > 1e-9:
                    return False
            except (TypeError, ValueError):
                return False
    return True


def inventory_drainable_state(
    value: dict[str, Any],
    model_sha: str,
    *,
    newer_than_ms: int = 0,
) -> bool:
    """Accept inventory the frozen PAPER engine can merge or unwind to cash.

    The maker intentionally seeds balanced YES/NO complete sets. Requiring zero
    inventory before requesting a drain therefore makes every cohort rotation
    impossible. Balanced complete sets are merged; a directional residual is
    crossed only by the canonical PAPER execution owner when a fresh L1 bid
    covers the entire quantity. In-flight split/merge accounting still fails
    closed. The post-drain handoff requires ``flat_state`` and proves that all
    merge/unwind work completed before any process is stopped.
    """
    if (
        value.get("schema") != "polymarket_v7_professional_maker_state_v2"
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("model_sha") != model_sha
        or int(value.get("timestamp_ms") or 0) < newer_than_ms
    ):
        return False
    inventory = value.get("inventory")
    if not isinstance(inventory, dict):
        return False
    for row in inventory.values():
        if not isinstance(row, dict):
            return False
        try:
            yes = float(row.get("yes_shares") or 0.0)
            no = float(row.get("no_shares") or 0.0)
            yes_cost = float(row.get("yes_cost") or 0.0)
            no_cost = float(row.get("no_cost") or 0.0)
            pending = sum(abs(float(row.get(key) or 0.0)) for key in (
                "pending_split_shares", "pending_merge_shares",
            ))
        except (TypeError, ValueError):
            return False
        if (
            yes < -1e-9 or no < -1e-9
            or yes_cost < -1e-12 or no_cost < -1e-12
            or pending > 1e-9
        ):
            return False
    return True


def flat_state(
    value: dict[str, Any],
    model_sha: str,
    *,
    newer_than_ms: int = 0,
    require_frozen: bool = False,
) -> bool:
    if not inventory_flat_state(
        value, model_sha, newer_than_ms=newer_than_ms
    ):
        return False
    return (
        int(value.get("active_order_count") or 0) == 0
        and value.get("flat_restart_safe") is True
        and (not require_frozen or value.get("new_risk_frozen") is True)
    )


class CohortSupervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_root = args.run_root
        self.control = self.run_root / "control"
        self.selection = args.selection
        self.candidate = args.candidate
        self.state = self.run_root / "micro_maker" / "state.json"
        self.drain = self.control / "MAKER_ROTATION_DRAIN"
        self.status = self.run_root / "micro_maker" / "rotation_status.json"
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.logs: list[Any] = []
        self.stop_requested = False
        self.rotation_count = 0
        self.last_rotation_ms = 0
        self.pending_membership_sha256 = ""
        self.pending_rotation_key = ""
        self.pending_last_generation_ms = 0
        self.pending_confirmations = 0
        self.flow_pause_latched = False
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
        payload: dict[str, Any] = {
            "schema": "polymarket_v7_maker_cohort_rotation_status_v1",
            "timestamp_ms": time.time_ns() // 1_000_000,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": self.args.model_sha,
            "state": state,
            "rotation_count": self.rotation_count,
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
            "fresh_flow_pause_active": self.flow_pause_latched,
            "cohort_pids": {name: process.pid for name, process in self.processes.items()},
        }
        payload.update(self.rotation_gate_metrics)
        payload.update(extra)
        atomic_json(self.status, payload)

    def command_specs(self) -> dict[str, tuple[list[str], Path, dict[str, str]]]:
        base_env = os.environ.copy()
        maker_env = dict(base_env)
        maker_env["PM_V7_WS_JSON_ARENA_MAX_BYTES"] = str(self.args.maker_arena_bytes)
        observer_env = dict(base_env)
        observer_env["PM_V7_WS_JSON_ARENA_MAX_BYTES"] = str(self.args.observer_arena_bytes)
        fillability_env = dict(base_env)
        fillability_env["PM_V7_WS_JSON_ARENA_MAX_BYTES"] = str(
            self.args.fillability_arena_bytes
        )
        return {
            "maker": ([
                str(self.args.maker_runtime),
                "--config", str(self.args.config),
                "--maker-policy", str(self.args.maker_policy),
                "--run-root", str(self.run_root),
                "--selection", str(self.selection),
                "--model", str(self.args.model),
                "--model-sha", self.args.model_sha,
            ], self.run_root / "micro_maker" / "runtime.log", maker_env),
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
        return len(self.processes) == 3 and all(
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

    def write_flow_pause_drain(self) -> None:
        atomic_json(self.drain, {
            "schema": "polymarket_v7_maker_rotation_drain_v1",
            "timestamp_ms": time.time_ns() // 1_000_000,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": self.args.model_sha,
            "reason": "no_fresh_aggressive_flow",
        })

    def sync_no_fresh_flow_pause(self) -> bool:
        """Remove the retired flow-only pause; book staleness remains in C++.

        Quiet aggressor flow is an economic feature, not a feed-integrity
        failure. The bilateral selector can route to inventory-backed asks,
        collateral-backed bids or bounded exploration. Only the canonical
        WebSocket lineage/freshness controls may withdraw for a genuinely stale
        book.
        """
        if read_json(self.drain).get("reason") == "no_fresh_aggressive_flow":
            self.drain.unlink(missing_ok=True)
        self.flow_pause_latched = False
        return False

    def rotate_if_safe(self, candidate: dict[str, Any]) -> None:
        target_membership = membership_sha256(candidate)
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
        now_ms = time.time_ns() // 1_000_000
        if not inventory_drainable_state(
            read_json(self.state), self.args.model_sha,
            newer_than_ms=now_ms - 5_000,
        ):
            self.write_status("PENDING_NONFLAT")
            return
        requested_ms = time.time_ns() // 1_000_000
        atomic_json(self.drain, {
            "schema": "polymarket_v7_maker_rotation_drain_v1",
            "timestamp_ms": requested_ms,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": self.args.model_sha,
            "candidate_membership_sha256": target_membership,
        })
        self.write_status("DRAINING")
        deadline = time.monotonic() + self.args.drain_timeout_seconds
        while not self.stop_requested and time.monotonic() < deadline:
            if not self.cohort_healthy():
                raise RuntimeError("maker_cohort_died_during_rotation_drain")
            if flat_state(
                read_json(self.state), self.args.model_sha,
                newer_than_ms=requested_ms, require_frozen=True,
            ):
                latest = read_json(self.candidate)
                try:
                    validate_selection(latest, self.args.model_sha)
                except ValueError:
                    if self.flow_pause_latched:
                        self.write_flow_pause_drain()
                    else:
                        self.drain.unlink(missing_ok=True)
                    self.reset_pending_confirmation()
                    self.write_status("PENDING_CONFIRMATION", reason="candidate_invalid_during_drain")
                    return
                # Recent-flow membership is expected to move while the old
                # cohort drains.  Once the PAPER engine proves frozen and flat,
                # atomically hand off to the newest valid non-degraded cohort
                # instead of releasing the freeze and reseeding the old one.
                # The 300-second post-handoff cooldown bounds turnover.
                if latest.get("degraded") is True:
                    self.drain.unlink(missing_ok=True)
                    self.reset_pending_confirmation()
                    self.write_status("PENDING_CONFIRMATION", reason="candidate_degraded_during_drain")
                    return
                runtime = read_json(self.selection)
                still_allowed, latest_metrics = rotation_gate(
                    runtime, latest,
                    minimum_projected_fill_probability=(
                        self.args.rotation_min_projected_fill_probability),
                    minimum_absolute_improvement=(
                        self.args.rotation_min_absolute_fill_improvement),
                    minimum_relative_multiplier=(
                        self.args.rotation_min_relative_fill_multiplier),
                )
                self.rotation_gate_metrics = latest_metrics
                if (
                    not still_allowed
                    or latest_metrics.get("rotation_target_cell") != target_cell
                ):
                    self.drain.unlink(missing_ok=True)
                    self.reset_pending_confirmation()
                    self.write_status(
                        "PENDING_CONFIRMATION",
                        reason="material_rotation_target_changed_during_drain",
                    )
                    return
                self.stop_cohort()
                # Re-read after the maker's final state write. A late PAPER fill
                # must abort promotion and restart the old cohort unchanged.
                if not flat_state(
                    read_json(self.state), self.args.model_sha, require_frozen=True
                ):
                    if self.flow_pause_latched:
                        self.write_flow_pause_drain()
                    else:
                        self.drain.unlink(missing_ok=True)
                    self.start_cohort()
                    self.write_status("PENDING_NONFLAT")
                    return
                atomic_json(self.selection, latest)
                self.flow_pause_latched = False
                self.rotation_count += 1
                self.last_rotation_ms = time.time_ns() // 1_000_000
                self.reset_pending_confirmation()
                self.drain.unlink(missing_ok=True)
                self.start_cohort()
                self.write_status("RUNNING")
                return
            time.sleep(0.1)
        if self.flow_pause_latched:
            self.write_flow_pause_drain()
        else:
            self.drain.unlink(missing_ok=True)
        self.write_status("PENDING_DRAIN_TIMEOUT")

    def run(self) -> int:
        self.control.mkdir(parents=True, exist_ok=True)
        self.drain.unlink(missing_ok=True)
        try:
            self.start_cohort()
            self.write_status("RUNNING")
            while not self.stop_requested and not (self.control / "KILL").exists():
                if not self.cohort_healthy():
                    self.write_status("FAILED", reason="cohort_process_exit")
                    return 2
                if self.sync_no_fresh_flow_pause():
                    time.sleep(self.args.poll_seconds)
                    continue
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
                    self.write_status("RUNNING")
                time.sleep(self.args.poll_seconds)
            return 0
        finally:
            self.drain.unlink(missing_ok=True)
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
    parser.add_argument("--maker-runtime", type=Path, required=True)
    parser.add_argument("--markout-observer", type=Path, required=True)
    parser.add_argument("--fillability-observer", type=Path, required=True)
    parser.add_argument("--maker-arena-bytes", type=int, required=True)
    parser.add_argument("--observer-arena-bytes", type=int, required=True)
    parser.add_argument("--fillability-arena-bytes", type=int, required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--drain-timeout-seconds", type=float, default=20.0)
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
