#!/usr/bin/env python3
"""External-fair provenance adapter for the existing canonical V7 ledger.

This module deliberately does NOT create a writer or ledger. It validates the
settlement-aware decision lineage and attaches it to LedgerEvent.metadata so
the sole existing CanonicalLedgerWriter remains the only canonical writer.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from v7_execution_ledger import LedgerContractError, LedgerEvent

SCHEMA_VERSION = 1
_HASH64 = re.compile(r"^[0-9a-f]{64}$")
ACTIONS = frozenset({"MAKE", "TAKE", "CANCEL", "WITHDRAW", "NOTHING"})
PURPOSES = frozenset({"ALPHA", "INVENTORY_REDUCTION", "RISK", "LIQUIDATION"})
TRIGGERS = frozenset({
    "PM_L2_UPDATE",
    "PM_TRADE",
    "EXTERNAL_PRICE_UPDATE",
    "ORACLE_UPDATE",
    "SETTLEMENT_REFERENCE_UPDATE",
    "PRIVATE_ORDER_UPDATE",
    "INVENTORY_UPDATE",
    "RISK_UPDATE",
    "HEALTH_UPDATE",
})


class ExternalFairLedgerError(LedgerContractError):
    pass


def _finite(name: str, value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ExternalFairLedgerError(f"{name}:not_numeric") from exc
    if not math.isfinite(number):
        raise ExternalFairLedgerError(f"{name}:not_finite")
    return number


@dataclass(frozen=True)
class ExternalFairProvenance:
    contract_version: int
    contract_rules_hash: str
    reference_version: int
    fair_model_family: str
    fair_model_version: str
    fair_model_hash: str
    feature_schema: str
    fair_yes: float
    fair_yes_lower: float
    fair_yes_upper: float
    structural_probability: float
    calibrated_probability: float
    settlement_margin: float
    settlement_margin_sigma: float
    oracle_state_version: int
    external_state_version: int
    pm_state_version: int
    inventory_state_version: int
    private_state_version: int
    risk_state_version: int
    causal_cut_id: int
    oracle_age_ns: int
    external_age_ns: int
    decision_trigger: str
    expected_fee: float
    expected_slippage: float
    expected_execution_risk: float
    expected_adverse_selection: float
    expected_robust_ev: float
    action: str
    action_purpose: str
    fee_schedule_version: str
    fee_authoritative: bool
    external_fair_required: bool
    model_mature: bool
    maker_execution_evidence: str
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ExternalFairLedgerError("schema_version:unsupported")
        for name in (
            "contract_version", "reference_version", "oracle_state_version",
            "external_state_version", "pm_state_version", "inventory_state_version",
            "private_state_version", "risk_state_version", "causal_cut_id",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExternalFairLedgerError(f"{name}:invalid")
        for name in ("oracle_age_ns", "external_age_ns"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExternalFairLedgerError(f"{name}:invalid")
        if not _HASH64.fullmatch(self.contract_rules_hash):
            raise ExternalFairLedgerError("contract_rules_hash:invalid")
        if not _HASH64.fullmatch(self.fair_model_hash):
            raise ExternalFairLedgerError("fair_model_hash:invalid")
        for name in (
            "fair_model_family", "fair_model_version", "feature_schema",
            "fee_schedule_version", "maker_execution_evidence",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ExternalFairLedgerError(f"{name}:missing")
        if self.decision_trigger not in TRIGGERS:
            raise ExternalFairLedgerError("decision_trigger:unsupported")
        if self.action not in ACTIONS:
            raise ExternalFairLedgerError("action:unsupported")
        if self.action_purpose not in PURPOSES:
            raise ExternalFairLedgerError("action_purpose:unsupported")
        probabilities = (
            self.fair_yes, self.fair_yes_lower, self.fair_yes_upper,
            self.structural_probability, self.calibrated_probability,
        )
        if any(not 0.0 <= _finite("probability", value) <= 1.0 for value in probabilities):
            raise ExternalFairLedgerError("probability:out_of_range")
        if not self.fair_yes_lower <= self.fair_yes <= self.fair_yes_upper:
            raise ExternalFairLedgerError("fair_interval:not_ordered")
        if _finite("settlement_margin_sigma", self.settlement_margin_sigma) <= 0.0:
            raise ExternalFairLedgerError("settlement_margin_sigma:not_positive")
        _finite("settlement_margin", self.settlement_margin)
        _finite("expected_robust_ev", self.expected_robust_ev)
        for name in (
            "expected_fee", "expected_slippage", "expected_execution_risk",
            "expected_adverse_selection",
        ):
            if _finite(name, getattr(self, name)) < 0.0:
                raise ExternalFairLedgerError(f"{name}:negative")
        if self.action == "TAKE" and self.fee_authoritative is not True:
            raise ExternalFairLedgerError("take:fee_not_authoritative")
        if self.external_fair_required and self.action in {"MAKE", "TAKE"}:
            if not self.model_mature and self.action == "TAKE":
                raise ExternalFairLedgerError("take:model_not_mature")
        if self.action == "MAKE" and self.maker_execution_evidence not in {
            "COLD_START", "LEARNING", "MATURE"
        }:
            raise ExternalFairLedgerError("maker_execution_evidence:unsupported")

    def to_metadata(self) -> dict[str, Any]:
        self.validate()
        return {
            "external_fair": asdict(self),
            "lineage_contract": "V7_SETTLEMENT_AWARE_EXTERNAL_FAIR",
        }


def attach_external_fair_metadata(
    event: LedgerEvent,
    provenance: ExternalFairProvenance,
) -> LedgerEvent:
    """Return a validated LedgerEvent for the existing canonical writer."""
    event.validate()
    provenance.validate()
    if event.strategy.strip().upper() not in {
        "EXTERNAL_INFORMATION", "PROFESSIONAL_MAKER", "CRYPTO_LIQUIDITY_ALPHA"
    }:
        raise ExternalFairLedgerError("strategy:not_external_fair_capable")
    if event.model_version is not None and event.model_version != provenance.fair_model_version:
        raise ExternalFairLedgerError("model_version:lineage_mismatch")
    if event.expected_ev is not None and not math.isclose(
        float(event.expected_ev), float(provenance.expected_robust_ev), rel_tol=1e-9, abs_tol=1e-12
    ):
        raise ExternalFairLedgerError("expected_ev:lineage_mismatch")
    if event.intended_action is not None and event.intended_action.strip().upper() != provenance.action:
        raise ExternalFairLedgerError("intended_action:lineage_mismatch")

    metadata = dict(event.metadata)
    if "external_fair" in metadata:
        raise ExternalFairLedgerError("metadata:external_fair_already_present")
    metadata.update(provenance.to_metadata())
    enriched = replace(event, metadata=metadata)
    enriched.validate()
    return enriched
