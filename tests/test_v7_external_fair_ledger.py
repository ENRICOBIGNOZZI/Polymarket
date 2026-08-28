#!/usr/bin/env python3
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import LedgerEvent  # noqa: E402
from v7_external_fair_ledger import (  # noqa: E402
    ExternalFairLedgerError,
    ExternalFairProvenance,
    attach_external_fair_metadata,
)

SHA = "1" * 40
HASH = "a" * 64


def provenance(action: str = "TAKE") -> ExternalFairProvenance:
    return ExternalFairProvenance(
        contract_version=1,
        contract_rules_hash=HASH,
        reference_version=2,
        fair_model_family="structural_bridge",
        fair_model_version="fv-1",
        fair_model_hash="b" * 64,
        feature_schema="fs-1",
        fair_yes=0.70,
        fair_yes_lower=0.65,
        fair_yes_upper=0.75,
        structural_probability=0.68,
        calibrated_probability=0.69,
        settlement_margin=25.0,
        settlement_margin_sigma=50.0,
        oracle_state_version=3,
        external_state_version=4,
        pm_state_version=5,
        inventory_state_version=6,
        private_state_version=7,
        risk_state_version=8,
        causal_cut_id=9,
        oracle_age_ns=1_000_000,
        external_age_ns=2_000_000,
        decision_trigger="EXTERNAL_PRICE_UPDATE",
        expected_fee=0.002,
        expected_slippage=0.001,
        expected_execution_risk=0.001,
        expected_adverse_selection=0.0,
        expected_robust_ev=0.04,
        action=action,
        action_purpose="ALPHA",
        fee_schedule_version="fee-v1",
        fee_authoritative=True,
        external_fair_required=True,
        model_mature=True,
        maker_execution_evidence="COLD_START",
    )


def event(action: str = "TAKE") -> LedgerEvent:
    return LedgerEvent(
        event_type="CANDIDATE",
        strategy="EXTERNAL_INFORMATION",
        model_sha=SHA,
        model_version="fv-1",
        opportunity_id="opp-1",
        candidate_id="candidate-1",
        market_id="market-1",
        event_id="event-1",
        token_id="yes-1",
        exchange_ts_ms=100,
        receive_ts_ms=101,
        decision_ts_ms=102,
        book_snapshot_id="book-1",
        side="BUY",
        bid=0.50,
        ask=0.51,
        expected_ev=0.04,
        intended_action=action,
        intended_size=2.0,
    )


def must_fail(fn, contains: str) -> None:
    try:
        fn()
    except ExternalFairLedgerError as exc:
        assert contains in str(exc), str(exc)
    else:
        raise AssertionError("expected ExternalFairLedgerError")


def main() -> None:
    enriched = attach_external_fair_metadata(event(), provenance())
    external = enriched.metadata["external_fair"]
    assert external["causal_cut_id"] == 9
    assert external["action"] == "TAKE"
    assert external["contract_rules_hash"] == HASH
    assert enriched.metadata["lineage_contract"] == "V7_SETTLEMENT_AWARE_EXTERNAL_FAIR"

    no_fee = replace(provenance(), fee_authoritative=False)
    must_fail(no_fee.validate, "fee_not_authoritative")

    immature = replace(provenance(), model_mature=False)
    must_fail(immature.validate, "model_not_mature")

    mismatch = replace(provenance(), expected_robust_ev=0.03)
    must_fail(lambda: attach_external_fair_metadata(event(), mismatch), "expected_ev:lineage_mismatch")

    duplicate = replace(event(), metadata={"external_fair": {"old": True}})
    must_fail(lambda: attach_external_fair_metadata(duplicate, provenance()), "already_present")

    wrong_strategy = replace(event(), strategy="PCA")
    must_fail(lambda: attach_external_fair_metadata(wrong_strategy, provenance()), "strategy:not_external_fair_capable")

    maker = replace(
        provenance(action="MAKE"),
        action_purpose="ALPHA",
        model_mature=False,
        fee_authoritative=False,
        expected_robust_ev=0.01,
    )
    maker_event = replace(event("MAKE"), strategy="PROFESSIONAL_MAKER", expected_ev=0.01)
    maker_enriched = attach_external_fair_metadata(maker_event, maker)
    assert maker_enriched.metadata["external_fair"]["maker_execution_evidence"] == "COLD_START"


if __name__ == "__main__":
    main()
