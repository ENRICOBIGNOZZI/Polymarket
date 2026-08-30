#!/usr/bin/env python3
"""Canonical V7 PAPER economics from the append-only execution ledger.

This module accepts only canonical ledger evidence:
- one economic unit per bundle/position/order, never leg fills divided by bundle submissions;
- exact-SHA and PAPER-only evidence through ``v7_execution_ledger``;
- explicit non-overlapping fee/slippage/unwind/capital/latency cost vectors;
- frozen-observation cost stress at 1x/1.5x/2x without trade reselection;
- dynamic strategy/model-family and horizon identity (no silent dropping of unknown V7 families);
- fail-closed completion/PnL maturity for multi-leg states;
- promotion inference clusters repeated economic units by canonical ``event_id`` and preserves chronology.

It is an evidence consumer only. It cannot submit orders, mutate allocation, change risk,
or enable authenticated/real-money execution.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
LEDGER_SCRIPT = ROOT / "scripts" / "v7_execution_ledger.py"
_spec = importlib.util.spec_from_file_location("v7_execution_ledger_canonical_economics", LEDGER_SCRIPT)
if _spec is None or _spec.loader is None:
    raise RuntimeError("cannot_load_v7_execution_ledger")
ledger = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ledger
_spec.loader.exec_module(ledger)

SCHEMA = "polymarket_v7_canonical_economics_v1"
COST_COMPONENTS = ("fee", "slippage", "unwind_loss", "capital_cost", "latency_cost")
PNL_COMPONENTS = (
    "trading_pnl", "spread_capture", "adverse_markout", "inventory_pnl",
    "maker_rebates", "liquidity_rewards",
)
STRESS_MULTIPLIERS = (1.0, 1.5, 2.0)
MARKOUT_HORIZONS_SECONDS = (1, 5, 10, 15, 30, 45, 60, 300)
MIN_EVENT_CLUSTERS_FOR_PROMOTION = 12
CHRONOLOGICAL_EVENT_FOLDS = 4
MIN_POSITIVE_EVENT_FOLD_FRACTION = 0.60


class EconomicsContractError(ValueError):
    """Raised when exact V7 economic evidence is ambiguous or internally inconsistent."""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool_true(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _horizon_seconds(event: Any) -> int | None:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    for key in ("horizon_seconds", "hold_horizon_seconds", "target_horizon_seconds"):
        value = _finite(metadata.get(key))
        if value is not None and value >= 0 and float(value).is_integer():
            return int(value)
    if event.event_type == "MARKOUT" and event.markouts:
        key = next(iter(event.markouts))
        if isinstance(key, str) and key.endswith("s") and key[:-1].isdigit():
            return int(key[:-1])
    return None


def _family(event: Any) -> str:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    return _text(metadata.get("model_family")) or _text(event.strategy)


def _economic_unit_id(event: Any, order_positions: dict[str, str] | None = None) -> str:
    if _text(event.bundle_id):
        return f"bundle:{event.bundle_id}"
    order_id = _text(event.order_id)
    if order_id and order_positions and order_id in order_positions:
        return f"position:{order_positions[order_id]}"
    if _text(event.position_id):
        return f"position:{event.position_id}"
    if order_id:
        return f"order:{order_id}"
    if _text(event.candidate_id):
        return f"candidate:{event.candidate_id}"
    if _text(event.opportunity_id):
        return f"opportunity:{event.opportunity_id}"
    return ""


def _leg_id(event: Any) -> str:
    return _text(event.leg_id) or _text(event.token_id)


def _required_leg_contract(metadata: dict[str, Any]) -> tuple[dict[str, float], bool]:
    """Return required leg -> target quantity and whether provenance was explicit."""
    if not isinstance(metadata, dict):
        return {}, False

    raw_targets = metadata.get("target_quantities")
    if isinstance(raw_targets, dict) and raw_targets:
        targets: dict[str, float] = {}
        for key, value in raw_targets.items():
            leg = _text(key)
            qty = _finite(value)
            if not leg or qty is None or qty <= 0:
                raise EconomicsContractError("target_quantities:invalid")
            targets[leg] = qty
        return targets, True

    for key in ("joint_target_legs", "required_legs"):
        raw = metadata.get(key)
        if isinstance(raw, list) and raw:
            legs: dict[str, float] = {}
            for item in raw:
                if isinstance(item, dict):
                    leg = _text(item.get("leg_id") or item.get("token_id") or item.get("id"))
                    qty = _finite(item.get("target_quantity") or item.get("quantity") or item.get("size"))
                    if not leg or qty is None or qty <= 0:
                        raise EconomicsContractError(f"{key}:invalid")
                    legs[leg] = qty
                else:
                    leg = _text(item)
                    if not leg:
                        raise EconomicsContractError(f"{key}:invalid")
                    legs[leg] = math.nan
            return legs, True
    return {}, False


def _merge_required_contract(current: dict[str, float], incoming: dict[str, float]) -> dict[str, float]:
    result = dict(current)
    for leg, qty in incoming.items():
        if leg in result:
            old = result[leg]
            if math.isfinite(old) and math.isfinite(qty) and not math.isclose(old, qty, rel_tol=1e-12, abs_tol=1e-12):
                raise EconomicsContractError(f"required_leg_target_conflict:{leg}")
            if not math.isfinite(old) and math.isfinite(qty):
                result[leg] = qty
        else:
            result[leg] = qty
    return result


@dataclass
class UnitState:
    unit_id: str
    family: str
    strategy: str
    horizon_seconds: int | None = None
    required_legs: dict[str, float] = field(default_factory=dict)
    required_contract_explicit: bool = False
    submitted_legs: dict[str, float] = field(default_factory=dict)
    fill_qty: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    fill_ids: set[str] = field(default_factory=set)
    terminal_ids: set[str] = field(default_factory=set)
    final_events: list[Any] = field(default_factory=list)
    event_ids: set[str] = field(default_factory=set)
    final_event_ids: set[str] = field(default_factory=set)
    latest_recorded_ts_ms: int = 0
    markouts_by_horizon: dict[int, list[float]] = field(default_factory=lambda: defaultdict(list))
    cost_totals: dict[str, float] = field(default_factory=lambda: {name: 0.0 for name in COST_COMPONENTS})
    cost_component_observed: dict[str, bool] = field(default_factory=lambda: {name: False for name in COST_COMPONENTS})
    pnl_components: dict[str, float] = field(default_factory=lambda: {name: 0.0 for name in PNL_COMPONENTS})
    pnl_component_observed: dict[str, bool] = field(default_factory=lambda: {name: False for name in PNL_COMPONENTS})
    cost_vector_complete_flag: bool = False
    capital_duration_ms: int = 0
    reasons: set[str] = field(default_factory=set)
    economic_authorities: set[str] = field(default_factory=set)
    counterfactual: bool = False
    internal_inventory_transform: bool = False

    def observe_identity(self, event: Any) -> None:
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        authority = _text(metadata.get("economic_authority")) or "PAPER"
        self.economic_authorities.add(authority)
        self.counterfactual = self.counterfactual or metadata.get("counterfactual") is True
        if metadata.get("excluded_from_portfolio_equity") is True:
            self.counterfactual = True
        fam = _family(event)
        if fam and self.family and fam != self.family:
            self.reasons.add("model_family_mismatch")
        elif fam:
            self.family = fam
        strategy = _text(event.strategy)
        if strategy and self.strategy and strategy != self.strategy:
            self.reasons.add("strategy_mismatch")
        elif strategy:
            self.strategy = strategy
        horizon = _horizon_seconds(event)
        if horizon is not None:
            if self.horizon_seconds is not None and self.horizon_seconds != horizon:
                if event.event_type != "MARKOUT":
                    self.reasons.add("horizon_mismatch")
            elif event.event_type != "MARKOUT":
                self.horizon_seconds = horizon
        event_id = _text(getattr(event, "event_id", None))
        if event_id:
            self.event_ids.add(event_id)
        recorded = getattr(event, "recorded_ts_ms", 0)
        if isinstance(recorded, int) and not isinstance(recorded, bool):
            self.latest_recorded_ts_ms = max(self.latest_recorded_ts_ms, recorded)

    def observe_contract(self, event: Any) -> None:
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        try:
            required, explicit = _required_leg_contract(metadata)
        except EconomicsContractError as exc:
            self.reasons.add(str(exc))
            return
        if required:
            try:
                self.required_legs = _merge_required_contract(self.required_legs, required)
            except EconomicsContractError as exc:
                self.reasons.add(str(exc))
        self.required_contract_explicit = self.required_contract_explicit or explicit
        self.cost_vector_complete_flag = self.cost_vector_complete_flag or _bool_true(metadata.get("cost_vector_complete"))

    def observe_submission(self, event: Any) -> None:
        leg = _leg_id(event)
        qty = _finite(event.intended_size)
        if not leg:
            self.reasons.add("submission_leg_identity_missing")
            return
        if qty is None or qty <= 0:
            self.reasons.add("submission_target_size_missing")
            return
        if leg in self.submitted_legs and not math.isclose(self.submitted_legs[leg], qty, rel_tol=1e-12, abs_tol=1e-12):
            self.reasons.add(f"submission_target_size_conflict:{leg}")
        else:
            self.submitted_legs[leg] = qty

    def observe_inventory_transform(self, event: Any) -> None:
        """Treat a proven internal merge as a complete non-market economic leg."""
        if event.event_type != "INVENTORY_MERGE":
            return
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        quantity = _finite(event.intended_size)
        if (quantity is None or quantity <= 0
                or metadata.get("consumed_inventory_provenance_complete") is not True):
            self.reasons.add("inventory_merge_provenance_incomplete")
            return
        leg = "INVENTORY_MERGE"
        self.internal_inventory_transform = True
        self.required_contract_explicit = True
        self.required_legs[leg] = quantity
        self.submitted_legs[leg] = quantity
        self.fill_qty[leg] += quantity
        self.fill_ids.add(_text(event.record_id))

    def observe_fill(self, event: Any) -> None:
        fill_id = _text(event.fill_id)
        leg = _leg_id(event)
        qty = _finite(event.filled_size)
        if not fill_id:
            self.reasons.add("fill_identity_missing")
            return
        if fill_id in self.fill_ids:
            return
        self.fill_ids.add(fill_id)
        if not leg or qty is None or qty <= 0:
            self.reasons.add("fill_leg_or_size_missing")
            return
        self.fill_qty[leg] += qty

    def observe_costs(self, event: Any) -> None:
        for component in COST_COMPONENTS:
            value = _finite(getattr(event, component, None))
            if value is None:
                continue
            if value < 0:
                self.reasons.add(f"{component}:negative")
                continue
            self.cost_totals[component] += value
            self.cost_component_observed[component] = True
        duration = getattr(event, "capital_duration_ms", None)
        if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0:
            self.capital_duration_ms = max(self.capital_duration_ms, duration)

    def observe_markout(self, event: Any) -> None:
        if event.event_type != "MARKOUT" or not isinstance(event.markouts, dict):
            return
        for key, value in event.markouts.items():
            if not isinstance(key, str) or not key.endswith("s") or not key[:-1].isdigit():
                self.reasons.add("markout_horizon_invalid")
                continue
            horizon = int(key[:-1])
            amount = _finite(value)
            if amount is None:
                self.reasons.add("markout_value_invalid")
                continue
            self.markouts_by_horizon[horizon].append(amount)

    def observe_final(self, event: Any) -> None:
        if event.event_type != "FINAL":
            return
        terminal_id = _text((event.metadata or {}).get("terminal_id") if isinstance(event.metadata, dict) else "") or _text(event.position_id) or _text(event.bundle_id) or _text(event.order_id) or _text(event.record_id)
        if terminal_id in self.terminal_ids:
            return
        self.terminal_ids.add(terminal_id)
        self.final_events.append(event)
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        decomposition = metadata.get("pnl_decomposition")
        if isinstance(decomposition, dict):
            for component in PNL_COMPONENTS:
                value = _finite(decomposition.get(component))
                if value is None:
                    continue
                # A configured reward pool is not own reward. Unknown own share
                # is represented as zero, never as projected PnL.
                if component == "liquidity_rewards" and decomposition.get("own_reward_share_verified") is not True:
                    value = 0.0
                self.pnl_components[component] += value
                self.pnl_component_observed[component] = True
        event_id = _text(getattr(event, "event_id", None))
        if event_id:
            self.final_event_ids.add(event_id)

    @property
    def is_multileg(self) -> bool:
        legs = set(self.required_legs) | set(self.submitted_legs) | set(self.fill_qty)
        return bool(_text(self.unit_id).startswith("bundle:")) and len(legs) > 1

    def target_quantities(self) -> dict[str, float]:
        targets = dict(self.required_legs)
        for leg, qty in self.submitted_legs.items():
            if leg not in targets or not math.isfinite(targets[leg]):
                targets[leg] = qty
        return targets

    def completion_state(self) -> str:
        targets = self.target_quantities()
        if self.is_multileg and not self.required_contract_explicit:
            return "UNVERIFIABLE"
        if not targets:
            return "UNVERIFIABLE"
        filled_required = [leg for leg, target in targets.items() if math.isfinite(target) and target > 0 and self.fill_qty.get(leg, 0.0) + 1e-12 >= target]
        any_fill = any(qty > 0 for qty in self.fill_qty.values())
        if len(filled_required) == len(targets):
            return "COMPLETE"
        if any_fill:
            return "PARTIAL"
        return "NONE"

    def realized_terminal_pnl(self) -> float | None:
        realized: list[float] = []
        for event in self.final_events:
            metadata = event.metadata if isinstance(event.metadata, dict) else {}
            if metadata.get("realized") is not True:
                continue
            pnl = _finite(event.final_pnl)
            if pnl is not None:
                realized.append(pnl)
        if len(realized) > 1:
            self.reasons.add("multiple_realized_terminal_pnl")
            return None
        return realized[0] if realized else None

    def economic_event_id(self) -> str | None:
        if len(self.event_ids) != 1 or len(self.final_event_ids) != 1:
            return None
        if self.event_ids != self.final_event_ids:
            return None
        return next(iter(self.event_ids))

    def event_identity_reason(self) -> str | None:
        if not self.event_ids or not self.final_event_ids:
            return "economic_event_id_missing"
        if len(self.event_ids) != 1 or len(self.final_event_ids) != 1 or self.event_ids != self.final_event_ids:
            return "economic_event_id_ambiguous"
        return None

    def partial_unwind_accounted(self) -> bool:
        if self.completion_state() != "PARTIAL":
            return True
        for event in self.final_events:
            metadata = event.metadata if isinstance(event.metadata, dict) else {}
            if metadata.get("realized") is True and metadata.get("unwind_accounted") is True:
                return True
        return False

    def cost_vector_verifiable(self) -> bool:
        return self.cost_vector_complete_flag and all(self.cost_component_observed[name] for name in COST_COMPONENTS)

    def baseline_cost(self) -> float:
        return sum(self.cost_totals.values())


def _load_units(path: Path, expected_model_sha: str) -> tuple[dict[str, UnitState], list[str]]:
    units: dict[str, UnitState] = {}
    global_reasons: list[str] = []
    try:
        events = list(ledger.iter_events(path, expected_model_sha=expected_model_sha))
    except (OSError, ledger.LedgerContractError) as exc:
        return {}, [f"canonical_ledger_unreadable:{type(exc).__name__}:{exc}"]
    # A submitted order does not necessarily know its eventual position id,
    # while its FILL and FINAL do. Resolve that canonical relationship in a
    # first pass so one economic trade cannot be split into an order unit and
    # a position unit merely because identity becomes richer over time.
    order_positions: dict[str, str] = {}
    conflicted_orders: set[str] = set()
    for event in events:
        order_id = _text(event.order_id)
        position_id = _text(event.position_id)
        if not order_id or not position_id:
            continue
        previous = order_positions.get(order_id)
        if previous is not None and previous != position_id:
            conflicted_orders.add(order_id)
            continue
        order_positions[order_id] = position_id
    for order_id in conflicted_orders:
        order_positions.pop(order_id, None)
        global_reasons.append(f"order_position_identity_conflict:{order_id}")

    for event in events:
        unit_id = _economic_unit_id(event, order_positions)
        if not unit_id:
            if event.event_type in {"ORDER_SUBMITTED", "FILL", "FINAL"}:
                global_reasons.append(f"{event.event_type.lower()}_economic_unit_missing")
            continue
        state = units.get(unit_id)
        if state is None:
            state = UnitState(unit_id=unit_id, family=_family(event), strategy=_text(event.strategy))
            units[unit_id] = state
        state.observe_identity(event)
        state.observe_contract(event)
        if event.event_type == "ORDER_SUBMITTED":
            state.observe_submission(event)
        if event.event_type == "INVENTORY_MERGE":
            state.observe_inventory_transform(event)
        if event.event_type == "FILL":
            state.observe_fill(event)
        state.observe_costs(event)
        state.observe_markout(event)
        state.observe_final(event)
    return units, sorted(set(global_reasons))


def _stress_pnl(final_pnl: float, baseline_cost: float, multiplier: float) -> float:
    return final_pnl - max(0.0, multiplier - 1.0) * baseline_cost


def _mean_or_none(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    return statistics.fmean(vals) if vals else None


def _event_cluster_economics(mature: list[tuple[UnitState, float]]) -> tuple[dict[str, dict[str, float]], list[str], list[float], float | None]:
    clusters: dict[str, list[tuple[UnitState, float]]] = defaultdict(list)
    latest: dict[str, int] = {}
    for unit, pnl in mature:
        event_id = unit.economic_event_id()
        if event_id is None:
            continue
        clusters[event_id].append((unit, pnl))
        latest[event_id] = max(latest.get(event_id, 0), unit.latest_recorded_ts_ms)

    stress: dict[str, dict[str, float]] = {}
    for event_id, rows in clusters.items():
        stress[event_id] = {
            f"{multiplier:g}x": sum(_stress_pnl(pnl, unit.baseline_cost(), multiplier) for unit, pnl in rows)
            for multiplier in STRESS_MULTIPLIERS
        }

    ordered = sorted(clusters, key=lambda event_id: (latest.get(event_id, 0), event_id))
    fold_totals: list[float] = []
    if ordered:
        folds = min(CHRONOLOGICAL_EVENT_FOLDS, len(ordered))
        fold_totals = [0.0 for _ in range(folds)]
        for index, event_id in enumerate(ordered):
            fold_index = min(folds - 1, index * folds // len(ordered))
            fold_totals[fold_index] += stress[event_id]["2x"]
    positive_fraction = (
        sum(value > 0.0 for value in fold_totals) / len(fold_totals)
        if fold_totals else None
    )
    return stress, ordered, fold_totals, positive_fraction


def assess(ledger_path: Path, *, expected_model_sha: str, family: str | None = None, horizon_seconds: int | None = None) -> dict[str, Any]:
    units, global_reasons = _load_units(ledger_path, expected_model_sha)
    eligible: list[UnitState] = []
    for unit in units.values():
        if family and unit.family != family:
            continue
        if horizon_seconds is not None and unit.horizon_seconds != horizon_seconds:
            continue
        eligible.append(unit)

    # Shadow counterfactual evidence belongs in the same append-only ledger for
    # lineage and replay, but it must never inflate authoritative PAPER equity,
    # PnL or promotion statistics.
    selected = [unit for unit in eligible if not unit.counterfactual]
    shadow_selected = [unit for unit in eligible if unit.counterfactual]

    submitted = [u for u in selected if u.submitted_legs]
    complete = [u for u in submitted if u.completion_state() == "COMPLETE"]
    partial = [u for u in submitted if u.completion_state() == "PARTIAL"]
    unverifiable_completion = [u for u in submitted if u.completion_state() == "UNVERIFIABLE"]

    shadow_submitted = [u for u in shadow_selected if u.submitted_legs]
    shadow_complete = [u for u in shadow_submitted if u.completion_state() == "COMPLETE"]
    shadow_mature: list[tuple[UnitState, float]] = []
    for unit in shadow_submitted:
        pnl = unit.realized_terminal_pnl()
        if (pnl is not None and unit.cost_vector_verifiable()
                and unit.completion_state() in {"COMPLETE", "PARTIAL"}
                and unit.economic_event_id() is not None):
            shadow_mature.append((unit, pnl))

    mature: list[tuple[UnitState, float]] = []
    for unit in submitted:
        state = unit.completion_state()
        if state == "UNVERIFIABLE":
            unit.reasons.add("completion_contract_unverifiable")
        if state == "PARTIAL" and not unit.partial_unwind_accounted():
            unit.reasons.add("partial_unwind_unaccounted")
        pnl = unit.realized_terminal_pnl()
        if pnl is None:
            unit.reasons.add("realized_terminal_pnl_missing")
        if pnl is not None and not unit.cost_vector_verifiable():
            unit.reasons.add("full_cost_vector_unverifiable")
        if pnl is not None and unit.cost_vector_verifiable() and state in {"COMPLETE", "PARTIAL"}:
            identity_reason = unit.event_identity_reason()
            if identity_reason:
                unit.reasons.add(identity_reason)
            mature.append((unit, pnl))

    unit_reasons = {unit.unit_id: sorted(unit.reasons) for unit in submitted if unit.reasons}
    event_mature = [(unit, pnl) for unit, pnl in mature if unit.economic_event_id() is not None]

    completion_rate = len(complete) / len(submitted) if submitted else None
    if completion_rate is not None and not (0.0 <= completion_rate <= 1.0):
        global_reasons.append("completion_rate_out_of_bounds")

    stress_totals: dict[str, float | None] = {}
    for multiplier in STRESS_MULTIPLIERS:
        key = f"{multiplier:g}x"
        stress_totals[key] = sum(_stress_pnl(pnl, unit.baseline_cost(), multiplier) for unit, pnl in event_mature) if event_mature else None

    costs_by_component = {component: sum(unit.cost_totals[component] for unit, _ in event_mature) for component in COST_COMPONENTS}
    strategy_net_pnl: dict[str, float] = defaultdict(float)
    strategy_mature_terminal_units: dict[str, int] = defaultdict(int)
    strategy_stressed_net_pnl: dict[str, dict[str, float]] = defaultdict(
        lambda: {f"{multiplier:g}x": 0.0 for multiplier in STRESS_MULTIPLIERS}
    )
    strategy_capital_hours: dict[str, float] = defaultdict(float)
    for unit, pnl in event_mature:
        strategy_net_pnl[unit.strategy] += pnl
        strategy_mature_terminal_units[unit.strategy] += 1
        strategy_capital_hours[unit.strategy] += unit.capital_duration_ms / 3_600_000.0
        for multiplier in STRESS_MULTIPLIERS:
            strategy_stressed_net_pnl[unit.strategy][f"{multiplier:g}x"] += _stress_pnl(
                pnl, unit.baseline_cost(), multiplier,
            )
    pnl_decomposition = {
        component: (
            sum(unit.pnl_components[component] for unit, _ in event_mature)
            if any(unit.pnl_component_observed[component] for unit, _ in event_mature)
            else (0.0 if component == "liquidity_rewards" else None)
        )
        for component in PNL_COMPONENTS
    }
    markouts: dict[str, dict[str, float | int | None]] = {}
    for horizon in MARKOUT_HORIZONS_SECONDS:
        vals = [value for unit in selected for value in unit.markouts_by_horizon.get(horizon, [])]
        markouts[f"{horizon}s"] = {"observations": len(vals), "mean": _mean_or_none(vals)}

    cluster_stress, ordered_clusters, chronological_folds_2x, positive_fold_fraction = _event_cluster_economics(event_mature)
    distinct_event_clusters = len(ordered_clusters)

    family_values = sorted({u.family for u in selected if u.family})
    horizon_values = sorted({u.horizon_seconds for u in selected if u.horizon_seconds is not None})
    if not family and len(family_values) > 1:
        global_reasons.append("mixed_model_families_require_explicit_filter")
    if horizon_seconds is None and len(horizon_values) > 1:
        global_reasons.append("mixed_model_horizons_require_explicit_filter")
    if not submitted:
        global_reasons.append("no_submitted_economic_units")
    if submitted and unverifiable_completion:
        global_reasons.append("completion_provenance_incomplete")
    if submitted and not complete:
        global_reasons.append("no_completed_economic_units")
    if partial and any("partial_unwind_unaccounted" in u.reasons for u in partial):
        global_reasons.append("partial_unwind_provenance_incomplete")
    if not mature:
        global_reasons.append("no_mature_full_cost_terminal_observations")
    if mature and len(event_mature) != len(mature):
        global_reasons.append("economic_event_identity_incomplete")
    if event_mature and distinct_event_clusters < MIN_EVENT_CLUSTERS_FOR_PROMOTION:
        global_reasons.append("insufficient_distinct_event_clusters")
    if distinct_event_clusters >= MIN_EVENT_CLUSTERS_FOR_PROMOTION:
        if positive_fold_fraction is None or positive_fold_fraction + 1e-12 < MIN_POSITIVE_EVENT_FOLD_FRACTION:
            global_reasons.append("event_cluster_chronological_stability_gate")

    stress_1x = stress_totals["1x"]
    stress_15x = stress_totals["1.5x"]
    stress_2x = stress_totals["2x"]
    positive_under_all_stress = bool(event_mature and stress_1x is not None and stress_1x > 0 and stress_15x is not None and stress_15x > 0 and stress_2x is not None and stress_2x > 0)
    if event_mature and not positive_under_all_stress:
        global_reasons.append("positive_pnl_stress_gate")

    state = "ECONOMIC_EVIDENCE_READY" if not global_reasons and positive_under_all_stress else "MORE_EVIDENCE_REQUIRED"
    return {
        "schema": SCHEMA,
        "expected_model_sha": expected_model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "family_filter": family,
        "horizon_seconds_filter": horizon_seconds,
        "state": state,
        "promotion_ready": state == "ECONOMIC_EVIDENCE_READY",
        "economic_units": len(selected),
        "shadow_counterfactual": {
            "economic_units": len(shadow_selected),
            "submitted_units": len(shadow_submitted),
            "complete_units": len(shadow_complete),
            "mature_terminal_units": len(shadow_mature),
            "net_pnl": sum(pnl for _, pnl in shadow_mature) if shadow_mature else None,
            "excluded_from_portfolio_equity": True,
            "authorities": sorted({
                authority for unit in shadow_selected
                for authority in unit.economic_authorities
            }),
        },
        "submitted_units": len(submitted),
        "complete_units": len(complete),
        "partial_units": len(partial),
        "unverifiable_completion_units": len(unverifiable_completion),
        "completion_rate": completion_rate,
        "mature_terminal_units": len(mature),
        "event_eligible_mature_terminal_units": len(event_mature),
        "distinct_event_clusters": distinct_event_clusters,
        "minimum_event_clusters_for_promotion": MIN_EVENT_CLUSTERS_FOR_PROMOTION,
        "event_cluster_stress": cluster_stress,
        "event_cluster_order_chronological": ordered_clusters,
        "chronological_event_folds_2x": chronological_folds_2x,
        "minimum_positive_event_fold_fraction": MIN_POSITIVE_EVENT_FOLD_FRACTION,
        "positive_chronological_event_fold_fraction_2x": positive_fold_fraction,
        "net_pnl": stress_1x,
        "strategy_net_pnl": dict(sorted(strategy_net_pnl.items())),
        "strategy_mature_terminal_units": dict(sorted(strategy_mature_terminal_units.items())),
        "strategy_stressed_net_pnl": {
            strategy: values for strategy, values in sorted(strategy_stressed_net_pnl.items())
        },
        "strategy_capital_hours": dict(sorted(strategy_capital_hours.items())),
        "pnl_decomposition": {
            **pnl_decomposition,
            "fees": costs_by_component["fee"],
            "slippage": costs_by_component["slippage"],
            "unwind_cost": costs_by_component["unwind_loss"],
            "capital_cost": costs_by_component["capital_cost"],
            "latency_cost": costs_by_component["latency_cost"],
            "liquidity_reward_unknown_share_policy": "ZERO",
        },
        "stressed_net_pnl": stress_totals,
        "costs": {"components": costs_by_component, "baseline_total": sum(costs_by_component.values()), "stress_observations_frozen": True, "multipliers": list(STRESS_MULTIPLIERS)},
        "markouts": markouts,
        "capital_hours": sum(unit.capital_duration_ms for unit, _ in event_mature) / 3_600_000.0,
        "model_families_observed": family_values,
        "model_horizons_seconds_observed": horizon_values,
        "positive_under_1x_1_5x_2x": positive_under_all_stress,
        "reason_codes": sorted(set(global_reasons)),
        "unit_reason_codes": unit_reasons,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--expected-model-sha", required=True)
    parser.add_argument("--family")
    parser.add_argument("--horizon-seconds", type=int)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = assess(args.ledger, expected_model_sha=args.expected_model_sha, family=args.family, horizon_seconds=args.horizon_seconds)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_suffix(args.output.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(args.output)
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
