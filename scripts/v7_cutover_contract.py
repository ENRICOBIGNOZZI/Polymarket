#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from v7_polymarket_v2_contracts import ContractRegistryError, load as load_v2_contract_registry

SHA40 = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_COSTS = {"fee", "slippage", "unwind_loss", "capital_cost", "latency_cost"}


def fail(message: str) -> None:
    raise SystemExit(message)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"{path} must contain a JSON object")
    return value


def safe_relative(value: object, field: str) -> str:
    text = str(value or "")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        fail(f"invalid {field}: {text!r}")
    return text


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.STDOUT).strip()


def number(value: object, field: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        fail(f"{field} must be numeric")
    if not math.isfinite(out):
        fail(f"{field} must be finite")
    return out


def require_close(actual: object, expected: object, field: str, tol: float = 1e-12) -> None:
    a, e = number(actual, field), number(expected, f"operator.{field}")
    if abs(a - e) > tol:
        fail(f"V7 cutover blocked: {field}={a} does not match operator authorization {e}")


def validate(root: Path, expected_head: str | None) -> dict[str, str]:
    root = root.resolve()
    directives = load_json(root / "config/operator_directives.json")
    if directives.get("authority") != "latest_explicit_user_instruction":
        fail("V7 cutover blocked: operator authority is not latest_explicit_user_instruction")
    authorization = directives.get("paper_v7_authorization")
    if not isinstance(authorization, dict):
        fail("V7 cutover blocked: paper_v7_authorization is missing")
    if (authorization.get("paper_only") is not True
            or authorization.get("authenticated_execution") is not False
            or authorization.get("real_order_submission") is not False
            or authorization.get("real_capital_at_risk") is not False):
        fail("V7 cutover blocked: operator PAPER/authenticated boundary invalid")

    manifest = load_json(root / "config/live_champion.json")
    if (manifest.get("enabled") is not True or manifest.get("version") != 7
            or isinstance(manifest.get("version"), bool)
            or manifest.get("execution_mode") != "PAPER_SIMULATED"):
        fail("V7 cutover blocked: enabled champion version must be exactly 7")
    if manifest.get("paper_only") is not True or manifest.get("authenticated_execution") is not False:
        fail("V7 cutover blocked: champion must be PAPER-only with authenticated execution disabled")
    if manifest.get("real_order_submission") not in (None, False):
        fail("V7 cutover blocked: champion real_order_submission must be false")

    loop_rel = safe_relative(manifest.get("loop"), "champion loop")
    config_rel = safe_relative(manifest.get("config"), "champion config")
    run_root_rel = safe_relative(manifest.get("run_root"), "champion run_root")
    if loop_rel != "scripts/paper_v7_execution_loop.sh":
        fail(f"V7 cutover blocked: canonical loop must be scripts/paper_v7_execution_loop.sh, got {loop_rel}")
    if config_rel != "config/paper_v7.json":
        fail(f"V7 cutover blocked: canonical config must be config/paper_v7.json, got {config_rel}")
    if run_root_rel != "runs/paper_v7_live":
        fail(f"V7 cutover blocked: canonical run_root must be runs/paper_v7_live, got {run_root_rel}")

    required_files = (
        loop_rel,
        config_rel,
        "scripts/v7_execution_ledger.py",
        "scripts/v7_ledger_spool.py",
        "scripts/v7_canonical_economics.py",
        "scripts/v7_joint_execution_policy.py",
        "scripts/v7_capital_allocator.py",
        "scripts/v7_evidence_capital_allocator.py",
        "scripts/v7_fee_reward_registry.py",
        "scripts/v7_generate_economic_artifacts.py",
        "scripts/v7_portfolio_guard.py",
        "scripts/v7_fast_structural_paper_executor.py",
        "scripts/v7_research_shadow_supervisor.py",
        "scripts/v7_slow_economic_shadow_supervisor.py",
        "scripts/v7_semantic_mapping.py",
        "scripts/v7_sports_collector.py",
        "scripts/v7_cross_platform_collector.py",
        "scripts/v7_osint_mapping_collector.py",
        "scripts/v7_learned_execution_model.py",
        "scripts/v7_polymarket_v2_contracts.py",
        "scripts/v7_real_pnl_evidence.py",
        "scripts/v7_execution_provenance.py",
        "scripts/v7_real_pnl_verifier.py",
        "scripts/v7_real_pnl_scorecard.py",
        "scripts/v7_generate_pnl_attestation.py",
        "scripts/v7_verify_pnl_attestation.py",
        "scripts/v7_secret_scan.py",
        "scripts/v7_security_audit.py",
        "scripts/v7_current_truth_audit.py",
        "scripts/v7_release_provenance.py",
        "scripts/v7_live_canary_orchestrator.py",
        "scripts/v7_reconcile_account.py",
        "scripts/v7_world_class_scorecard.py",
        "config/v7_execution_modes.json",
        "config/v7_risk_tiers.json",
        "schemas/v7/execution_mode.schema.json",
        "schemas/v7/pnl_attestation.schema.json",
        "schemas/v7/world_class_scorecard.schema.json",
        "config/v7_polymarket_v2_contracts.json",
        "config/v7_runtime_supervision.json",
        "config/v7_strategy_registry.json",
        "config/v7_live_model_scope.json",
        "config/v7_external_inputs.json",
        "config/v7_external_mappings.json",
        "config/v7_frequency_matrix.json",
        "config/v7_external_fair.json",
        "config/v7_scheduler_freeze.json",
        "config/v7_capability_matrix.json",
        "config/v7_incumbent_identity.json",
        "scripts/v7_convergence_audit.py",
    )
    for rel in required_files:
        if not (root / rel).is_file():
            fail(f"V7 cutover blocked: required file missing: {rel}")

    registry = load_json(root / "config/v7_strategy_registry.json")
    enabled_families = {
        str(row.get("family") or "")
        for row in registry.get("strategies", [])
        if isinstance(row, dict) and row.get("enabled") is True
    }
    if registry.get("schema") != "polymarket_v7_strategy_registry_v1" or len(enabled_families) != 15:
        fail("V7 cutover blocked: strategy registry must contain exactly 15 enabled V7 families")
    scope = load_json(root / "config/v7_live_model_scope.json")
    target_live = set(scope.get("target_live_families") or [])
    excluded_live = set(scope.get("excluded_live_families") or [])
    research_shadow = set(scope.get("research_shadow_supervised_families") or [])
    if (scope.get("schema") != "polymarket_v7_live_model_scope_v1"
            or scope.get("version") != 7
            or scope.get("target_live_count") != 12
            or scope.get("paper_only") is not True
            or scope.get("authenticated_execution") is not False
            or scope.get("real_order_submission") is not False):
        fail("V7 cutover blocked: live model scope identity/safety contract invalid")
    if target_live | excluded_live != enabled_families or target_live & excluded_live:
        fail("V7 cutover blocked: live/excluded model scope must exactly partition the 15-family registry")
    if excluded_live != {"ranking", "pca", "local_factor"}:
        fail("V7 cutover blocked: only ranking, pca and local_factor may be excluded from live-PAPER")
    if set(scope.get("always_on_economic_shadow_families") or []) != excluded_live:
        fail("V7 cutover blocked: excluded slow families must remain always-on economic shadow")
    if research_shadow != {"sports_latency", "cross_platform", "wallet_intelligence"}:
        fail("V7 cutover blocked: research-shadow supervisor scope is not the exact approved three-family set")
    governance = scope.get("governance") if isinstance(scope.get("governance"), dict) else {}
    if (governance.get("single_execution_owner") is not True
            or governance.get("research_has_capital") is not False
            or governance.get("research_has_oms_authority") is not False
            or governance.get("research_has_ledger_writer_authority") is not False
            or governance.get("automatic_promotion") is not False):
        fail("V7 cutover blocked: research-shadow ownership/governance contract invalid")

    external_inputs = load_json(root / "config/v7_external_inputs.json")
    if (external_inputs.get("schema") != "polymarket_v7_external_inputs_v1"
            or external_inputs.get("version") != 7
            or external_inputs.get("paper_only") is not True
            or external_inputs.get("authenticated_execution") is not False
            or external_inputs.get("real_order_submission") is not False
            or external_inputs.get("automatic_promotion") is not False):
        fail("V7 cutover blocked: external input safety contract invalid")
    sports = external_inputs.get("sports_latency") if isinstance(external_inputs.get("sports_latency"), dict) else {}
    if sports.get("primary_provider") != "sportradar_soccer_v4_push":
        fail("V7 cutover blocked: canonical sports provider is not Sportradar soccer v4 push")
    cross = external_inputs.get("cross_platform") if isinstance(external_inputs.get("cross_platform"), dict) else {}
    if cross.get("second_venue") != "kalshi":
        fail("V7 cutover blocked: canonical read-only second venue is not Kalshi")
    external_mappings = load_json(root / "config/v7_external_mappings.json")
    if (external_mappings.get("schema") != "polymarket_v7_external_mapping_registry_v1"
            or external_mappings.get("version") != 7
            or external_mappings.get("paper_only") is not True
            or external_mappings.get("automatic_promotion") is not False
            or any(not isinstance(external_mappings.get(name), list)
                   for name in ("osint", "sports_latency", "cross_platform"))):
        fail("V7 cutover blocked: external semantic mapping registry invalid")

    adaptive_universe = load_json(root / "config/v7_adaptive_universe.json")
    if (adaptive_universe.get("schema") != "polymarket_v7_adaptive_universe_config_v1"
            or adaptive_universe.get("version") != 7
            or adaptive_universe.get("paper_only") is not True
            or adaptive_universe.get("authenticated_execution") is not False
            or adaptive_universe.get("real_order_submission") is not False):
        fail("V7 cutover blocked: adaptive universe safety contract invalid")
    resources = adaptive_universe.get("resource_budget") if isinstance(adaptive_universe.get("resource_budget"), dict) else {}
    if any(not isinstance(resources.get(name), dict) for name in ("hot", "warm", "structural")):
        fail("V7 cutover blocked: adaptive universe resource budgets missing")

    cfg = load_json(root / config_rel)
    if (cfg.get("engine_version") != 7 or cfg.get("paper_only") is not True
            or cfg.get("execution_mode") != "PAPER_SIMULATED"):
        fail("V7 cutover blocked: config must be engine_version=7 and PAPER-only")
    require_close(cfg.get("market_limit"), authorization.get("market_limit"), "market_limit")
    require_close(cfg.get("min_liquidity"), authorization.get("min_liquidity"), "min_liquidity")
    require_close(cfg.get("min_net_edge"), authorization.get("min_net_edge"), "min_net_edge")
    require_close(cfg.get("uncertainty_penalty"), authorization.get("uncertainty_penalty"), "uncertainty_penalty")

    if cfg.get("fixed_dollar_trade_cap_enabled") is not False:
        fail("V7 cutover blocked: fixed-dollar trade cap must remain disabled")
    if number(cfg.get("fractional_kelly"), "fractional_kelly") > number(authorization.get("fractional_kelly_ceiling"), "operator.fractional_kelly_ceiling") + 1e-12:
        fail("V7 cutover blocked: fractional Kelly exceeds operator ceiling")
    for cfg_key, auth_key in (("max_trade_fraction","max_trade_fraction"),("max_market_fraction","max_market_fraction"),("max_event_fraction","max_event_fraction"),("max_gross_fraction","max_gross_fraction")):
        if number(cfg.get(cfg_key), cfg_key) > number(authorization.get(auth_key), f"operator.{auth_key}") + 1e-12:
            fail(f"V7 cutover blocked: {cfg_key} exceeds operator ceiling")
    if number(cfg.get("max_drawdown"), "max_drawdown") > number(authorization.get("max_drawdown"), "operator.max_drawdown") + 1e-12:
        fail("V7 cutover blocked: max_drawdown exceeds operator ceiling")

    multi = cfg.get("multi_strategy") if isinstance(cfg.get("multi_strategy"), dict) else {}
    if multi.get("paper_only") is not True or multi.get("single_account_allocator") is not True or multi.get("single_canonical_ledger_writer") is not True:
        fail("V7 cutover blocked: account-level PAPER allocator/single-writer contract missing")
    require_close(multi.get("global_max_drawdown"), authorization.get("max_drawdown"), "multi_strategy.global_max_drawdown")
    if number(multi.get("global_max_gross_fraction"), "multi_strategy.global_max_gross_fraction") > number(authorization.get("max_gross_fraction"), "operator.max_gross_fraction") + 1e-12:
        fail("V7 cutover blocked: global gross fraction exceeds operator ceiling")

    v7 = cfg.get("v7")
    if not isinstance(v7, dict):
        fail("V7 cutover blocked: config.v7 must be an object")
    if v7.get("adaptive_universe_policy") != "config/v7_adaptive_universe.json":
        fail("V7 cutover blocked: canonical adaptive universe policy is not configured")
    for key in ("paper_only","authoritative_fee_required","shared_execution_ledger_required","single_canonical_ledger_writer","joint_fill_state_required_for_multileg","queue_never_grants_size","partial_unwind_required"):
        if v7.get(key) is not True:
            fail(f"V7 cutover blocked: v7.{key} must be true")
    if v7.get("authenticated_execution") is not False or v7.get("real_order_submission") is not False:
        fail("V7 cutover blocked: V7 authenticated/real execution must remain disabled")
    if (v7.get("execution_mode") != "PAPER_SIMULATED"
            or v7.get("execution_modes_policy") != "config/v7_execution_modes.json"):
        fail("V7 cutover blocked: canonical typed execution mode is invalid")
    if (v7.get("contract_registry") != "config/v7_polymarket_v2_contracts.json"
            or v7.get("require_v2_contract_registry") is not True):
        fail("V7 cutover blocked: pinned CLOB V2/pUSD registry is required")
    if v7.get("real_pnl_provenance_required") is not True:
        fail("V7 cutover blocked: immutable real-PnL execution provenance is required")
    if v7.get("real_pnl_economic_scorecard_required") is not True:
        fail("V7 cutover blocked: real-PnL economic scorecard is required")
    if v7.get("pre_canary_security") != {
            "full_history_secret_scan_required": True,
            "findings_must_equal": 0,
            "remediation_evidence_required": True}:
        fail("V7 cutover blocked: pre-canary secret-remediation contract invalid")
    try:
        load_v2_contract_registry(root / "config/v7_polymarket_v2_contracts.json")
    except ContractRegistryError as exc:
        fail(f"V7 cutover blocked: invalid CLOB V2/pUSD contract registry: {exc}")

    runtime_supervision = load_json(root / "config/v7_runtime_supervision.json")
    if (runtime_supervision.get("schema") != "polymarket_v7_runtime_supervision_v1"
            or runtime_supervision.get("version") != 7
            or runtime_supervision.get("execution_mode") != "PAPER_SIMULATED"
            or runtime_supervision.get("execution_modes_policy") != "config/v7_execution_modes.json"
            or runtime_supervision.get("paper_only") is not True
            or runtime_supervision.get("authenticated_execution") is not False
            or runtime_supervision.get("real_order_submission") is not False
            or runtime_supervision.get("real_capital_at_risk") is not False):
        fail("V7 cutover blocked: runtime supervision safety contract invalid")
    clob_recovery = runtime_supervision.get("clob_v2_recovery")
    if not isinstance(clob_recovery, dict) or (
            clob_recovery.get("http_425_backoff_initial_seconds") != 1
            or clob_recovery.get("http_425_backoff_max_seconds") != 30
            or clob_recovery.get("post_restart_post_only_seconds") != 120
            or clob_recovery.get("cancel_only_allows_cancel") is not True
            or clob_recovery.get("private_reconciliation_required_after_stream_disconnect") is not True
            or clob_recovery.get("paper_only") is not True
            or clob_recovery.get("authenticated_execution") is not False
            or clob_recovery.get("real_order_submission") is not False):
        fail("V7 cutover blocked: CLOB V2 restart/reconciliation policy invalid")
    failure_domains = runtime_supervision.get("failure_domains")
    if (not isinstance(failure_domains, dict)
            or failure_domains.get("matching_engine_restart") != {
                "scope": "venue", "action": "cancel_and_backoff_until_post_only_recovery", "critical": True}
            or failure_domains.get("matching_engine_cancel_only") != {
                "scope": "venue", "action": "cancel_only_until_reconciled", "critical": True}
            or failure_domains.get("order_heartbeat_expired") != {
                "scope": "account", "action": "cancel_all_and_reconcile", "critical": True}
            or failure_domains.get("signer_rate_limit") != {
                "scope": "signer", "action": "quarantine_signer_and_reconcile", "critical": True}):
        fail("V7 cutover blocked: CLOB V2 matching-engine failure actions invalid")
    order_heartbeat = runtime_supervision.get("order_heartbeat")
    if not isinstance(order_heartbeat, dict) or (
            order_heartbeat.get("interval_seconds") != 5
            or order_heartbeat.get("maximum_ack_age_seconds") != 10
            or order_heartbeat.get("missing_ack_action") != "cancel_all_and_reconcile"
            or order_heartbeat.get("paper_only") is not True
            or order_heartbeat.get("authenticated_execution") is not False
            or order_heartbeat.get("real_order_submission") is not False):
        fail("V7 cutover blocked: order-heartbeat safety policy invalid")
    signer_rate_limit = runtime_supervision.get("signer_rate_limit")
    if not isinstance(signer_rate_limit, dict) or (
            signer_rate_limit.get("rolling_window_seconds") != 1
            or signer_rate_limit.get("maximum_regular_requests") != 10
            or signer_rate_limit.get("maximum_emergency_requests") != 10
            or signer_rate_limit.get("maximum_total_requests") != 20
            or signer_rate_limit.get("reserve_emergency_for") != ["cancel", "heartbeat"]
            or signer_rate_limit.get("clock_regression_action") != "quarantine_signer_and_reconcile"
            or signer_rate_limit.get("paper_only") is not True
            or signer_rate_limit.get("authenticated_execution") is not False
            or signer_rate_limit.get("real_order_submission") is not False):
        fail("V7 cutover blocked: per-signer rate-limit policy invalid")
    if set(v7.get("cost_vector_required") or []) != REQUIRED_COSTS:
        fail("V7 cutover blocked: complete fee/slippage/unwind/capital/latency cost vector required")
    if sorted(int(x) for x in v7.get("markout_horizons_seconds") or []) != [1,10,45,60,300]:
        fail("V7 cutover blocked: canonical markout horizons must be 1/10/45/60/300s")
    if v7.get("hard_arb_fixed_dollar_trade_cap_enabled") is not authorization.get("hard_arb_fixed_dollar_trade_cap_enabled"):
        fail("V7 cutover blocked: Hard Arb fixed-dollar cap setting does not match operator authority")
    if number(v7.get("hard_arb_max_trade_fraction"), "v7.hard_arb_max_trade_fraction") > number(authorization.get("hard_arb_max_trade_fraction"), "operator.hard_arb_max_trade_fraction") + 1e-12:
        fail("V7 cutover blocked: Hard Arb trade fraction exceeds operator ceiling")

    head = git(root, "rev-parse", "HEAD")
    if expected_head is not None:
        if not SHA40.fullmatch(expected_head):
            fail(f"invalid expected SHA: {expected_head!r}")
        if head != expected_head:
            fail(f"V7 cutover blocked: checkout {head} != expected {expected_head}")
    return {"V7_CUTOVER_SHA": head,"V7_CHAMPION_VERSION":"7","V7_CHAMPION_LOOP":loop_rel,"V7_CHAMPION_CONFIG":config_rel,"V7_CHAMPION_RUN_ROOT":run_root_rel}


def main() -> int:
    parser=argparse.ArgumentParser(description="Fail-closed V7 PAPER cutover contract")
    parser.add_argument("--repository-root",type=Path,default=Path(".")); parser.add_argument("--expected-head"); parser.add_argument("--github-env",type=Path)
    args=parser.parse_args(); env=validate(args.repository_root,args.expected_head); output="\n".join(f"{k}={v}" for k,v in env.items())+"\n"
    if args.github_env:
        with args.github_env.open("a",encoding="utf-8") as handle: handle.write(output)
    print(output,end=""); return 0

if __name__=="__main__": raise SystemExit(main())
