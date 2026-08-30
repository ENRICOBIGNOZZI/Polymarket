#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_external_fair_invariants import check_external_fair_invariants  # noqa: E402


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def main() -> None:
    external = load("config/v7_external_fair.json")
    paper = load("config/paper_v7.json")

    # The immature external model has zero execution authority. It remains a
    # continuous counterfactual collector until its economic gates pass.
    failures = check_external_fair_invariants(external, paper)
    assert failures == [], failures
    assert external["execution_authority"] == "SHADOW_ZERO_AUTHORITY"
    assert external["taker"]["enabled_for_execution"] is False
    assert external["taker"]["counterfactual_enabled"] is True
    assert external["maker"]["external_fair_enabled_for_live_quotes"] is False

    active = copy.deepcopy(external)
    active["execution_authority"] = "PAPER_EXECUTION_OWNER"
    active["fair_value"]["default_model_mature"] = True
    active["maker"]["external_fair_enabled_for_live_quotes"] = True
    active["taker"].update({"authority": "PAPER", "enabled_for_execution": True})
    active["oracle"]["transport_binding"] = "UNBOUND"
    active["old_micro_taker_migration"] = {}
    failures = check_external_fair_invariants(active, paper, {
        "execution_authority": "PAPER_EXECUTION_OWNER",
        "single_execution_owner": True,
        "canonical_state_reconciled": True,
        "exact_sha_ci_green": True,
    })
    assert "ACTIVE_AUTHORITY_REQUIRES_VERIFIED_ORACLE_TRANSPORT" in failures
    assert "OLD_MICRO_TAKER_OVERLAP_NOT_PROVEN_REMOVED" in failures

    active["oracle"]["transport_binding"] = "VERIFIED_SAME_ORACLE_PROVIDER_TRANSPORT_V1"
    active["old_micro_taker_migration"] = {
        "overlapping_execution_authority_removed": True,
    }
    clean_runtime = {
        "execution_authority": "PAPER_EXECUTION_OWNER",
        "single_execution_owner": True,
        "canonical_state_reconciled": True,
        "exact_sha_ci_green": True,
    }
    assert check_external_fair_invariants(active, paper, clean_runtime) == []

    no_ci = dict(clean_runtime)
    no_ci["exact_sha_ci_green"] = False
    assert "EXACT_SHA_CI_NOT_GREEN" in check_external_fair_invariants(active, paper, no_ci)

    cancel_only = copy.deepcopy(active)
    cancel_only["execution_authority"] = "PAPER_CANCEL_ONLY_OWNER"
    cancel_only["maker"]["external_fair_enabled_for_live_quotes"] = True
    cancel_only["taker"]["enabled_for_execution"] = True
    cancel_only["taker"]["authority"] = "PAPER"
    cancel_runtime = dict(clean_runtime)
    cancel_runtime["execution_authority"] = "PAPER_CANCEL_ONLY_OWNER"
    cancel_failures = check_external_fair_invariants(cancel_only, paper, cancel_runtime)
    assert "CANCEL_ONLY_MAY_NOT_REPRICE_MAKER" in cancel_failures
    assert "CANCEL_ONLY_MAY_NOT_EXECUTE_TAKER" in cancel_failures

    dual = copy.deepcopy(external)
    dual["incumbent_policy"]["simultaneous_blue_green_execution"] = True
    assert "BLUE_GREEN_DUAL_EXECUTION_FORBIDDEN" in check_external_fair_invariants(dual, paper)


if __name__ == "__main__":
    main()
