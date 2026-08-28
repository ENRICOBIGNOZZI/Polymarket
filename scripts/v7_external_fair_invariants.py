#!/usr/bin/env python3
"""Fail-closed architecture/promotion invariants for settlement-aware V7.

This is deliberately stricter than configuration parsing. A challenger can
exist in SHADOW with incomplete provider bindings. Execution authority cannot.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SHADOW_AUTHORITIES = {"SHADOW", "SHADOW_ZERO_AUTHORITY", "ZERO_EXECUTION_AUTHORITY"}
ACTIVE_AUTHORITIES = {"PAPER_EXECUTION_OWNER", "PAPER_CANCEL_ONLY_OWNER"}


class InvariantError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvariantError(f"cannot_read:{path}:{exc}") from exc
    if not isinstance(value, dict):
        raise InvariantError(f"not_object:{path}")
    return value


def check_external_fair_invariants(
    external: dict[str, Any],
    paper: dict[str, Any],
    runtime_status: dict[str, Any] | None = None,
) -> list[str]:
    failures: list[str] = []
    runtime_status = runtime_status or {}

    authority = str(
        runtime_status.get("execution_authority")
        or external.get("execution_authority")
        or "SHADOW_ZERO_AUTHORITY"
    ).upper()
    active = authority not in SHADOW_AUTHORITIES

    if str(external.get("architecture") or "").upper() != "V7":
        failures.append("ARCHITECTURE_NOT_V7")
    if external.get("paper_only") is not True or paper.get("paper_only") is not True:
        failures.append("PAPER_ONLY_REQUIRED")
    if external.get("authenticated_execution") is not False:
        failures.append("AUTHENTICATED_EXECUTION_MUST_BE_FALSE")
    if external.get("real_order_submission") is not False:
        failures.append("REAL_ORDER_SUBMISSION_MUST_BE_FALSE")

    incumbent = external.get("incumbent_policy") if isinstance(external.get("incumbent_policy"), dict) else {}
    if incumbent.get("keep_healthy_incumbent_running") is not True:
        failures.append("INCUMBENT_CONTINUITY_REQUIRED")
    if incumbent.get("mutate_incumbent_worktree") is not False:
        failures.append("INCUMBENT_WORKTREE_IMMUTABLE")
    if incumbent.get("simultaneous_blue_green_execution") is not False:
        failures.append("BLUE_GREEN_DUAL_EXECUTION_FORBIDDEN")

    unified = external.get("unified_action") if isinstance(external.get("unified_action"), dict) else {}
    for key, reason in (
        ("single_oms", "SINGLE_OMS_REQUIRED"),
        ("single_inventory_truth", "SINGLE_INVENTORY_REQUIRED"),
        ("single_capital_allocator", "SINGLE_ALLOCATOR_REQUIRED"),
        ("single_ledger_writer", "SINGLE_LEDGER_WRITER_REQUIRED"),
    ):
        if unified.get(key) is not True:
            failures.append(reason)

    oracle = external.get("oracle") if isinstance(external.get("oracle"), dict) else {}
    if oracle.get("exchange_price_may_replace_oracle") is not False:
        failures.append("EXCHANGE_MAY_NOT_REPLACE_ORACLE")
    if oracle.get("hardcoded_window_seconds") is not None:
        failures.append("ORACLE_WINDOW_MUST_COME_FROM_CONTRACT")

    venues = external.get("external_venues") if isinstance(external.get("external_venues"), dict) else {}
    if venues.get("receive_time_authoritative") is not True:
        failures.append("RECEIVE_TIME_MUST_BE_AUTHORITATIVE")
    if venues.get("rest_polling_hft_trigger") is not False:
        failures.append("REST_HFT_TRIGGER_FORBIDDEN")
    if int(venues.get("min_healthy_sources") or 0) < 2:
        failures.append("MIN_TWO_EXTERNAL_SOURCES")

    hot = external.get("hot_path") if isinstance(external.get("hot_path"), dict) else {}
    for key in (
        "python", "rest", "filesystem", "database", "synchronous_logging",
        "remote_model_inference", "hot_model_loading", "unbounded_queue",
        "global_decision_mutex",
    ):
        if hot.get(key) is not False:
            failures.append(f"HOT_PATH_{key.upper()}_FORBIDDEN")

    training = external.get("training") if isinstance(external.get("training"), dict) else {}
    if training.get("random_shuffle") is not False:
        failures.append("RANDOM_SHUFFLE_FORBIDDEN")
    if training.get("refit_promotes") is not False:
        failures.append("REFIT_MAY_NOT_PROMOTE")

    promotion = external.get("promotion") if isinstance(external.get("promotion"), dict) else {}
    if promotion.get("automatic_promotion") is not False:
        failures.append("AUTOMATIC_PROMOTION_FORBIDDEN")

    # Static configuration can declare PAPER authority, but process ownership is
    # attested only by the live runtime status.  Deployment/cutover calls this
    # check with that status; ordinary schema checks must not fabricate it.
    if active:
        if authority not in ACTIVE_AUTHORITIES:
            failures.append("UNKNOWN_ACTIVE_AUTHORITY")
        transport = str(oracle.get("transport_binding") or "").upper()
        if "UNBOUND" in transport or not transport:
            failures.append("ACTIVE_AUTHORITY_REQUIRES_VERIFIED_ORACLE_TRANSPORT")
        old_taker = external.get("old_micro_taker_migration")
        old_taker = old_taker if isinstance(old_taker, dict) else {}
        if old_taker.get("overlapping_execution_authority_removed") is not True:
            failures.append("OLD_MICRO_TAKER_OVERLAP_NOT_PROVEN_REMOVED")
        if runtime_status:
            if runtime_status.get("single_execution_owner") is not True:
                failures.append("RUNTIME_SINGLE_EXECUTION_OWNER_NOT_PROVEN")
            if runtime_status.get("canonical_state_reconciled") is not True:
                failures.append("CANONICAL_STATE_RECONCILIATION_NOT_PROVEN")
            if runtime_status.get("exact_sha_ci_green") is not True:
                failures.append("EXACT_SHA_CI_NOT_GREEN")

    gates = external.get("gate_classes") if isinstance(external.get("gate_classes"), dict) else {}
    hard = gates.get("A_HARD_CORRECTNESS_SAFETY") if isinstance(gates.get("A_HARD_CORRECTNESS_SAFETY"), dict) else {}
    economic = gates.get("B_ECONOMIC_MATURITY") if isinstance(gates.get("B_ECONOMIC_MATURITY"), dict) else {}
    real_money = gates.get("C_FUTURE_REAL_MONEY") if isinstance(gates.get("C_FUTURE_REAL_MONEY"), dict) else {}
    if hard.get("may_block_paper") is not True:
        failures.append("HARD_SAFETY_MUST_BLOCK_PAPER")
    if economic.get("may_block_paper") is not False:
        failures.append("ECONOMIC_MATURITY_MAY_NOT_BLOCK_PAPER")
    if real_money.get("in_scope") is not False:
        failures.append("REAL_MONEY_MUST_REMAIN_OUT_OF_SCOPE")

    # Cancel-only must precede maker/taker authority. Shadow implementation may
    # contain all candidates, but active authority has explicit phase semantics.
    cancel = external.get("cancel_overlay") if isinstance(external.get("cancel_overlay"), dict) else {}
    maker = external.get("maker") if isinstance(external.get("maker"), dict) else {}
    taker = external.get("taker") if isinstance(external.get("taker"), dict) else {}
    if authority == "PAPER_CANCEL_ONLY_OWNER":
        if cancel.get("enabled") is not True:
            failures.append("CANCEL_ONLY_OWNER_REQUIRES_CANCEL_OVERLAY")
        if maker.get("external_fair_enabled_for_live_quotes") is not False:
            failures.append("CANCEL_ONLY_MAY_NOT_REPRICE_MAKER")
        if taker.get("enabled_for_execution") is not False:
            failures.append("CANCEL_ONLY_MAY_NOT_EXECUTE_TAKER")

    return sorted(set(failures))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-config", type=Path, default=Path("config/v7_external_fair.json"))
    parser.add_argument("--paper-config", type=Path, default=Path("config/paper_v7.json"))
    parser.add_argument("--runtime-status", type=Path)
    args = parser.parse_args()
    external = _load(args.external_config)
    paper = _load(args.paper_config)
    runtime = _load(args.runtime_status) if args.runtime_status and args.runtime_status.exists() else {}
    failures = check_external_fair_invariants(external, paper, runtime)
    print(json.dumps({"ok": not failures, "failures": failures}, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
