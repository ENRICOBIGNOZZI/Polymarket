#!/usr/bin/env python3
"""Read-only Fast Structural Arb economics from the canonical V7 execution ledger.

No fill is inferred from quoted opportunity rows. A completed basket must reconcile to
canonical per-leg FILL records on one exact SHA. Open locked edge is diagnostic only;
realized promotion economics come only from canonical FINAL observations.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from v7_execution_ledger import LedgerEvent, load_events  # noqa: E402

SCHEMA = "polymarket_v7_fast_joint_execution_v1"
SOURCE = "canonical_v7_execution_ledger"
DEFAULT_STRATEGY = "FAST_STRUCTURAL_ARB"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OBSERVATION_TYPES = frozenset({"ORDER_STATE", "POSITION_MARK", "FINAL"})


def _finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _integer(value: Any, default: int = -1) -> int:
    out = _finite(value)
    return int(out) if math.isfinite(out) and float(out).is_integer() else default


def _truth(metadata: dict[str, Any], key: str) -> bool:
    return metadata.get(key) is True


def _nonnegative_cost(name: str, value: float | None) -> float:
    if value is None:
        return 0.0
    out = _finite(value)
    if not math.isfinite(out) or out < 0.0:
        raise ValueError(f"{name}:invalid_cost")
    return out


def _observation_cost(event: LedgerEvent) -> float:
    return sum((_nonnegative_cost("fee", event.fee),
                _nonnegative_cost("slippage", event.slippage),
                _nonnegative_cost("unwind_loss", event.unwind_loss),
                _nonnegative_cost("capital_cost", event.capital_cost),
                _nonnegative_cost("latency_cost", event.latency_cost)))


def _candidate_is_point_in_time(candidate: LedgerEvent) -> bool:
    return (candidate.exchange_ts_ms is not None and candidate.receive_ts_ms is not None
            and candidate.decision_ts_ms is not None and candidate.book_snapshot_id is not None
            and candidate.exchange_ts_ms > 0
            and candidate.exchange_ts_ms <= candidate.receive_ts_ms <= candidate.decision_ts_ms
            and candidate.recorded_ts_ms >= candidate.decision_ts_ms)


def _fill_is_point_in_time(fill: LedgerEvent) -> bool:
    return (fill.exchange_ts_ms is not None and fill.receive_ts_ms is not None
            and fill.exchange_ts_ms > 0
            and fill.exchange_ts_ms <= fill.receive_ts_ms <= fill.recorded_ts_ms)


def _fill_has_authoritative_fee(fill: LedgerEvent) -> bool:
    return (fill.fee is not None and math.isfinite(float(fill.fee)) and float(fill.fee) >= 0.0
            and isinstance(fill.fee_source, str) and bool(fill.fee_source.strip()))


def _choose_observation(events: list[LedgerEvent]) -> LedgerEvent:
    finals = [event for event in events if event.event_type == "FINAL"]
    pool = finals if finals else events
    return max(pool, key=lambda event: (event.recorded_ts_ms, event.record_id))


def aggregate_events(events: Iterable[LedgerEvent], *, expected_sha: str,
                     strategy: str = DEFAULT_STRATEGY, ledger_files: int = 1) -> dict[str, Any]:
    if not SHA_RE.fullmatch(expected_sha):
        raise ValueError("expected_sha must be an exact 40-character lowercase Git SHA")
    strategy = strategy.strip()
    if not strategy:
        raise ValueError("strategy must be non-empty")
    relevant = [event for event in events if event.strategy == strategy]
    if any(event.model_sha != expected_sha for event in relevant):
        raise ValueError("mixed_or_wrong_sha_fast_execution_event")

    candidates: dict[str, LedgerEvent] = {}
    fills_by_candidate: dict[str, list[LedgerEvent]] = defaultdict(list)
    observations_by_candidate: dict[str, list[LedgerEvent]] = defaultdict(list)
    for event in relevant:
        if event.event_type == "CANDIDATE":
            if not event.candidate_id:
                raise ValueError("fast_candidate_missing_candidate_id")
            if event.candidate_id in candidates:
                raise ValueError("duplicate_fast_candidate_id")
            candidates[event.candidate_id] = event
        elif event.event_type == "FILL":
            if not event.candidate_id or not event.leg_id:
                raise ValueError("fast_fill_missing_candidate_or_leg_id")
            fills_by_candidate[event.candidate_id].append(event)
        elif event.event_type in OBSERVATION_TYPES and _truth(event.metadata, "joint_execution_observation"):
            if not event.candidate_id:
                raise ValueError("fast_joint_observation_missing_candidate_id")
            observations_by_candidate[event.candidate_id].append(event)

    joint_counts: Counter[str] = Counter()
    completed_baskets = 0
    partial_unwinds = 0
    realized_pnl_observations = 0
    fill_conditioned_net_pnl = 0.0
    explicit_cost_sum = 0.0
    realized_capital_seconds = 0.0
    observed_capital_seconds = 0.0
    locked_terminal_observations = 0
    locked_terminal_pnl_sum = 0.0
    point_in_time = bool(observations_by_candidate)
    authoritative_fees = bool(observations_by_candidate)
    depth_executable = bool(observations_by_candidate)
    partial_unwind_accounted = bool(observations_by_candidate)

    for candidate_id, observation_events in sorted(observations_by_candidate.items()):
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise ValueError(f"joint_observation_without_candidate:{candidate_id}")
        target_legs = _integer(candidate.metadata.get("joint_target_legs"))
        if target_legs <= 0:
            raise ValueError(f"candidate_missing_joint_target_legs:{candidate_id}")
        if target_legs > 1 and not candidate.bundle_id:
            raise ValueError(f"multileg_candidate_missing_bundle_id:{candidate_id}")

        fill_events = fills_by_candidate.get(candidate_id, [])
        filled_leg_ids = {event.leg_id for event in fill_events if (event.filled_size or 0.0) > 0.0}
        completed_leg_ids = {event.leg_id for event in fill_events
                             if (event.filled_size or 0.0) > 0.0 and event.complete is True}
        if len(filled_leg_ids) > target_legs or len(completed_leg_ids) > target_legs:
            raise ValueError(f"fill_count_exceeds_target:{candidate_id}")
        if fill_events:
            point_in_time = point_in_time and all(_fill_is_point_in_time(event) for event in fill_events)
            authoritative_fees = authoritative_fees and all(
                _fill_has_authoritative_fee(event) for event in fill_events)

        observation = _choose_observation(observation_events)
        metadata = observation.metadata
        joint_state = str(metadata.get("joint_state") or "").strip().upper()
        if not joint_state:
            raise ValueError(f"joint_state_missing:{candidate_id}")
        reported_filled = _integer(metadata.get("filled_legs"), len(filled_leg_ids))
        if reported_filled != len(filled_leg_ids):
            raise ValueError(f"filled_leg_count_mismatch:{candidate_id}")

        completed = _truth(metadata, "completed_basket")
        if completed and len(completed_leg_ids) != target_legs:
            raise ValueError(f"completed_basket_without_complete_leg_fills:{candidate_id}")
        if not completed and len(completed_leg_ids) == target_legs:
            raise ValueError(f"all_legs_complete_but_basket_not_completed:{candidate_id}")

        partial = _truth(metadata, "partial_unwind") or (0 < len(filled_leg_ids) < target_legs)
        if partial:
            partial_unwinds += 1
            partial_unwind_accounted = partial_unwind_accounted and (
                observation.event_type == "FINAL" and _truth(metadata, "unwind_accounted"))
        if completed:
            completed_baskets += 1
        joint_counts[joint_state] += 1

        point_in_time = point_in_time and _candidate_is_point_in_time(candidate)
        depth_executable = depth_executable and _truth(metadata, "depth_executable")
        if observation.event_type == "POSITION_MARK":
            depth_executable = depth_executable and observation.executable_liquidation_value is not None

        duration_seconds = max(0, observation.capital_duration_ms or 0) / 1000.0
        observed_capital_seconds += duration_seconds
        locked = _finite(metadata.get("locked_terminal_pnl"))
        if math.isfinite(locked):
            locked_terminal_observations += 1
            locked_terminal_pnl_sum += locked

        pnl_realized = observation.event_type == "FINAL" and _truth(metadata, "pnl_realized")
        if pnl_realized and filled_leg_ids:
            if observation.final_pnl is None or not math.isfinite(float(observation.final_pnl)):
                raise ValueError(f"realized_fast_final_missing_pnl:{candidate_id}")
            cost = _observation_cost(observation)
            realized_pnl_observations += 1
            fill_conditioned_net_pnl += float(observation.final_pnl)
            explicit_cost_sum += cost
            realized_capital_seconds += duration_seconds

    return {
        "schema": SCHEMA, "source": SOURCE, "strategy": strategy, "model_sha": expected_sha,
        "paper_only": True, "authenticated_execution": False, "ledger_files": ledger_files,
        "candidate_count": len(candidates), "point_in_time": point_in_time,
        "authoritative_fees": authoritative_fees, "depth_executable": depth_executable,
        "partial_unwind_accounted": partial_unwind_accounted,
        "joint_state_observations": sum(joint_counts.values()),
        "realized_pnl_observations": realized_pnl_observations,
        "completed_baskets": completed_baskets, "partial_unwind_observations": partial_unwinds,
        "joint_state_counts": dict(sorted(joint_counts.items())),
        "locked_terminal_observations": locked_terminal_observations,
        "locked_terminal_pnl_sum": locked_terminal_pnl_sum,
        "fill_conditioned_net_pnl": fill_conditioned_net_pnl,
        "explicit_cost_sum": explicit_cost_sum,
        "cost_stress_1_5x_net_pnl": fill_conditioned_net_pnl - 0.5 * explicit_cost_sum,
        "cost_stress_2x_net_pnl": fill_conditioned_net_pnl - explicit_cost_sum,
        "observed_capital_hours": observed_capital_seconds / 3600.0,
        "capital_hours": realized_capital_seconds / 3600.0,
        "pnl_per_capital_hour": (fill_conditioned_net_pnl / (realized_capital_seconds / 3600.0)
                                 if realized_capital_seconds > 0.0 else None),
    }


def discover_ledgers(ledger: list[Path], ledger_root: Path | None) -> list[Path]:
    paths = [path for path in ledger if path.is_file()]
    if ledger_root is not None and ledger_root.exists():
        paths.extend(ledger_root.rglob("ledger/execution.jsonl"))
    return sorted({path.resolve() for path in paths})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", action="append", type=Path, default=[])
    parser.add_argument("--ledger-root", type=Path)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = discover_ledgers(args.ledger, args.ledger_root)
    events: list[LedgerEvent] = []
    for path in paths:
        events.extend(load_events(path, expected_model_sha=args.expected_sha))
    report = aggregate_events(events, expected_sha=args.expected_sha, strategy=args.strategy,
                              ledger_files=len(paths))
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output_json.with_name(args.output_json.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, args.output_json)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
