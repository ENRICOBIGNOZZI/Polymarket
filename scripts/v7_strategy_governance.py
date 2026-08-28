#!/usr/bin/env python3
"""V7-wide strategy, experiment, evidence and allocation contracts.

This is deliberately a cold-plane control module.  It cannot submit orders,
change the canonical allocator, or promote a challenger.  Its job is to make
all strategy families comparable in account-wealth units while preserving the
single V7 runtime/OMS/ledger/risk ownership contract.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "polymarket_v7_strategy_registry_v1"
REPORT_SCHEMA = "polymarket_v7_strategy_report_v1"
FAMILIES = {
    "professional_maker", "fast_structural", "hard_arb", "graph_rv",
    "crypto_settlement_fair", "crypto_informed_taker", "micro_taker",
    "osint", "sports_latency", "cross_platform", "wallet_intelligence",
    "market_open", "ranking", "pca", "local_factor",
}
FREQUENCIES = {"HFT-0", "FAST-1", "EVENT-2", "SLOW-3"}
ACTIONS = {"MAKE", "TAKE", "ARB", "CANCEL", "WITHDRAW", "NOTHING"}
REQUIRED_REPORT_FIELDS = (
    "economic_mechanism", "frequency", "model_family", "data_sources",
    "independent_sample_count", "opportunities", "actions", "fills_completions",
    "predicted_ev", "realized_net_pnl", "calibration", "capacity",
    "latency_sensitivity", "main_bottleneck", "champion", "challenger",
    "next_experiment",
)


class ContractError(ValueError):
    pass


class Authority(IntEnum):
    RESEARCH = 0
    SHADOW = 1
    PAPER = 2
    CANARY = 3
    LIVE_BOUNDED = 4
    LIVE_SCALED = 5


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def capital_time_score(expected_robust_net_pnl: float, capital: float, duration_seconds: float) -> float:
    values = tuple(_finite(x) for x in (expected_robust_net_pnl, capital, duration_seconds))
    if any(x is None for x in values):
        raise ContractError("non_finite_capital_time_input")
    pnl, cap, duration = values
    if cap <= 0.0 or duration <= 0.0:
        raise ContractError("capital_and_duration_must_be_positive")
    return pnl / (cap * duration)


@dataclass(frozen=True)
class StrategySpec:
    family: str
    priority: str
    frequency: str
    authority: Authority
    actions: tuple[str, ...]
    independent_sample_unit: str
    strategy_specific_execution_model: str
    enabled: bool
    reason: str

    @staticmethod
    def from_json(raw: Mapping[str, Any]) -> "StrategySpec":
        try:
            authority = Authority[str(raw["authority"]).upper()]
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("invalid_authority") from exc
        out = StrategySpec(
            family=str(raw.get("family") or ""), priority=str(raw.get("priority") or ""),
            frequency=str(raw.get("frequency") or ""), authority=authority,
            actions=tuple(str(x).upper() for x in raw.get("actions") or ()),
            independent_sample_unit=str(raw.get("independent_sample_unit") or ""),
            strategy_specific_execution_model=str(raw.get("strategy_specific_execution_model") or ""),
            enabled=raw.get("enabled") is True, reason=str(raw.get("reason") or ""),
        )
        out.validate()
        return out

    def validate(self) -> None:
        if self.family not in FAMILIES:
            raise ContractError(f"unknown_strategy_family:{self.family}")
        if self.frequency not in FREQUENCIES:
            raise ContractError(f"invalid_frequency:{self.family}")
        if not self.priority.startswith(("P0", "P1", "P2", "RESEARCH")):
            raise ContractError(f"invalid_priority:{self.family}")
        if not self.actions or not set(self.actions) <= ACTIONS:
            raise ContractError(f"invalid_actions:{self.family}")
        if not self.independent_sample_unit or not self.strategy_specific_execution_model:
            raise ContractError(f"missing_evidence_contract:{self.family}")
        if not self.reason:
            raise ContractError(f"missing_state_reason:{self.family}")


@dataclass(frozen=True)
class Registry:
    strategies: tuple[StrategySpec, ...]
    paper_only: bool
    authenticated_execution: bool
    real_order_submission: bool
    automatic_promotion: bool
    single_runtime_owner: bool
    single_oms: bool
    single_ledger: bool
    single_inventory: bool
    single_allocator: bool
    single_risk: bool

    @staticmethod
    def load(path: Path) -> "Registry":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema") != SCHEMA:
            raise ContractError("invalid_registry_schema")
        ownership = raw.get("ownership") or {}
        safety = raw.get("safety") or {}
        registry = Registry(
            strategies=tuple(StrategySpec.from_json(x) for x in raw.get("strategies") or ()),
            paper_only=safety.get("paper_only") is True,
            authenticated_execution=safety.get("authenticated_execution") is True,
            real_order_submission=safety.get("real_order_submission") is True,
            automatic_promotion=(raw.get("governance") or {}).get("automatic_promotion") is True,
            single_runtime_owner=ownership.get("single_runtime_owner") is True,
            single_oms=ownership.get("single_oms") is True,
            single_ledger=ownership.get("single_ledger") is True,
            single_inventory=ownership.get("single_inventory") is True,
            single_allocator=ownership.get("single_allocator") is True,
            single_risk=ownership.get("single_risk") is True,
        )
        registry.validate()
        return registry

    def validate(self) -> None:
        found = [x.family for x in self.strategies]
        if len(found) != len(set(found)) or set(found) != FAMILIES:
            raise ContractError(f"strategy_registry_must_cover_exactly_15_families:{sorted(set(FAMILIES)-set(found))}")
        if not self.paper_only or self.authenticated_execution or self.real_order_submission:
            raise ContractError("repository_must_remain_paper_only")
        if self.automatic_promotion:
            raise ContractError("automatic_promotion_forbidden")
        if not all((self.single_runtime_owner, self.single_oms, self.single_ledger,
                    self.single_inventory, self.single_allocator, self.single_risk)):
            raise ContractError("canonical_ownership_split")
        if any(x.authority > Authority.PAPER for x in self.strategies):
            raise ContractError("paper_repository_cannot_authorize_real_money")


@dataclass(frozen=True)
class Evidence:
    independent_samples: int
    chronological: bool
    causal: bool
    exact_model_sha: bool
    forward_shadow: bool
    robust_net_pnl: float
    stressed_2x_net_pnl: float
    positive_fold_fraction: float
    calibration_passed: bool
    execution_model_mature: bool
    zero_invariant_failures: bool

    def eligible(self, minimum_samples: int) -> tuple[bool, tuple[str, ...]]:
        blockers: list[str] = []
        if self.independent_samples < minimum_samples: blockers.append("insufficient_independent_samples")
        if not self.chronological: blockers.append("non_chronological_evidence")
        if not self.causal: blockers.append("causality_not_proved")
        if not self.exact_model_sha: blockers.append("exact_model_sha_missing")
        if not self.forward_shadow: blockers.append("forward_shadow_missing")
        if self.robust_net_pnl <= 0.0: blockers.append("nonpositive_robust_net_pnl")
        if self.stressed_2x_net_pnl <= 0.0: blockers.append("nonpositive_2x_cost_pnl")
        if self.positive_fold_fraction < 0.60: blockers.append("unstable_chronological_folds")
        if not self.calibration_passed: blockers.append("calibration_failed")
        if not self.execution_model_mature: blockers.append("execution_model_immature")
        if not self.zero_invariant_failures: blockers.append("invariant_failures_present")
        return not blockers, tuple(blockers)


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    strategy: str
    hypothesis: str
    mechanism: str
    champion: str
    challenger: str
    single_primary_change: str
    data_cutoff_ms: int
    oos_protocol: str
    success_gate: str
    failure_gate: str

    def validate(self) -> None:
        if self.strategy not in FAMILIES:
            raise ContractError("unknown_experiment_strategy")
        values = (self.experiment_id, self.hypothesis, self.mechanism, self.champion,
                  self.challenger, self.single_primary_change, self.oos_protocol,
                  self.success_gate, self.failure_gate)
        if not all(str(x).strip() for x in values) or self.data_cutoff_ms <= 0:
            raise ContractError("incomplete_experiment_contract")
        if self.champion == self.challenger:
            raise ContractError("challenger_must_be_distinct")


def promotion_assessment(spec: StrategySpec, evidence: Evidence, minimum_samples: int) -> dict[str, Any]:
    eligible, blockers = evidence.eligible(minimum_samples)
    target = Authority(min(int(spec.authority) + 1, int(Authority.LIVE_SCALED)))
    if target > Authority.PAPER:
        blockers = tuple(sorted(set((*blockers, "explicit_real_money_operator_authorization_required"))))
        eligible = False
    return {
        "family": spec.family, "current_authority": spec.authority.name,
        "requested_next_authority": target.name, "statistically_eligible": not tuple(b for b in blockers if b != "explicit_real_money_operator_authorization_required"),
        "promotion_authorized": False, "automatic_promotion": False,
        "blockers": list(blockers), "operator_decision_required": True,
    }


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    family: str
    action: str
    purpose: str
    event_id: str
    market_ids: tuple[str, ...]
    expected_robust_net_pnl: float
    capital: float
    duration_seconds: float
    uncertainty: float

    @property
    def score(self) -> float:
        return capital_time_score(self.expected_robust_net_pnl - max(0.0, self.uncertainty), self.capital, self.duration_seconds)


_PURPOSE_PRIORITY = {"RISK": 6, "LIQUIDATION": 5, "CANCEL": 4, "STRUCTURAL_GUARANTEE": 3, "ALPHA": 2, "MAKER": 1}


def resolve_conflicts(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """Select one candidate per overlapping market/event exposure deterministically."""
    ordered = sorted(candidates, key=lambda x: (-_PURPOSE_PRIORITY.get(x.purpose, 0), -x.score, x.candidate_id))
    selected: list[Candidate] = []
    occupied_markets: set[str] = set()
    occupied_events: set[str] = set()
    for row in ordered:
        if row.family not in FAMILIES or row.action not in ACTIONS:
            raise ContractError("invalid_candidate")
        if row.expected_robust_net_pnl <= 0.0 and row.purpose not in {"RISK", "LIQUIDATION", "CANCEL"}:
            continue
        if occupied_markets.intersection(row.market_ids) or (row.event_id and row.event_id in occupied_events):
            continue
        selected.append(row)
        occupied_markets.update(row.market_ids)
        if row.event_id: occupied_events.add(row.event_id)
    return tuple(selected)


def validate_strategy_report(report: Mapping[str, Any]) -> None:
    if report.get("schema") != REPORT_SCHEMA or report.get("family") not in FAMILIES:
        raise ContractError("invalid_strategy_report_identity")
    missing = [key for key in REQUIRED_REPORT_FIELDS if key not in report]
    if missing:
        raise ContractError(f"missing_strategy_report_fields:{','.join(missing)}")
    if report.get("frequency") not in FREQUENCIES:
        raise ContractError("invalid_strategy_report_frequency")
    if int(report.get("independent_sample_count") or 0) < 0:
        raise ContractError("negative_independent_sample_count")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate V7 strategy registry and optional reports")
    parser.add_argument("--registry", type=Path, default=Path("config/v7_strategy_registry.json"))
    parser.add_argument("--report", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    registry = Registry.load(args.registry)
    for path in args.report:
        validate_strategy_report(json.loads(path.read_text(encoding="utf-8")))
    result = {"schema": SCHEMA, "valid": True, "paper_only": registry.paper_only,
              "strategies": [asdict(x) | {"authority": x.authority.name} for x in registry.strategies]}
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
