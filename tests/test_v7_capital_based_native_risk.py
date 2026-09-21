#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path

from scripts.v7_native_risk_policy import load_native_limits

ROOT=Path(__file__).resolve().parents[1]

def allocation():
    return {
        "schema":"polymarket_v7_capital_allocation_v3",
        "paper_only":True,
        "authenticated_execution":False,
        "real_order_submission":False,
        "capital_authority_owner":"V7_CANONICAL_ALLOCATOR",
        "capital_authority_owner_count":1,
        "account_starting_capital":14000,
        "reserve_budget":4000,
        "engine_budgets":{"CRYPTO_SETTLEMENT_ENGINE":10000},
    }

def test_capital_based_native_risk_contract():
    policy=json.loads((ROOT/"config/v7_native_risk_policy.json").read_text())
    receipt=load_native_limits(policy,allocation())
    assert receipt["paper_only"] is True
    assert receipt["real_order_submission"] is False
    assert receipt["limits"]["max_single_order_microdollars"]==100_000_000
    assert receipt["limits"]["max_market_exposure_microdollars"]==333_333_333
    assert receipt["target_order_fraction_of_context"]==0.25
    assert receipt["target_order_notional_cap_microdollars"]==100_000_000
    # Thirty equal contexts from a $10k sleeve imply ~$333.33 each, so the
    # intended target is ~$83.33 before the hard $100 cap.
    context=receipt["limits"]["max_total_exposure_microdollars"]//30
    target=min(
        int(context*receipt["target_order_fraction_of_context"]),
        receipt["target_order_notional_cap_microdollars"],
        receipt["limits"]["max_single_order_microdollars"],
    )
    assert 83_000_000 < target < 84_000_000

def test_invalid_fraction_or_cap_fails_closed():
    policy=json.loads((ROOT/"config/v7_native_risk_policy.json").read_text())
    bad=dict(policy);bad["target_order_fraction_of_context"]=1.1
    try:load_native_limits(bad,allocation())
    except ValueError:pass
    else:raise AssertionError("fraction>1 must fail")
    bad=dict(policy);bad["target_order_notional_cap_microdollars"]=200_000_000
    try:load_native_limits(bad,allocation())
    except ValueError:pass
    else:raise AssertionError("target cap above max order must fail")

if __name__=="__main__":
    test_capital_based_native_risk_contract()
    test_invalid_fraction_or_cap_fails_closed()
    print("ok 2 capital-based native risk tests")
