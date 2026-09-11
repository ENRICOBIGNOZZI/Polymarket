#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


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

    # Research PAPER runtime has one canonical loop/config/run root; no published
    # deployment manifest exists in research mode.
    loop_rel = "scripts/paper_v7_execution_loop.sh"
    config_rel = "config/paper_v7.json"
    run_root_rel = "runs/paper_v7_live"

    required_files = (
        loop_rel, config_rel,
        "scripts/v7_execution_ledger.py", "scripts/v7_ledger_spool.py",
        "scripts/v7_global_portfolio_coordinator.py", "scripts/v7_opportunity.py",
        "scripts/v7_canonical_economics.py", "scripts/v7_capital_allocator.py",
        "scripts/v7_portfolio_guard.py", "scripts/v7_fee_reward_registry.py",
        "scripts/v7_generate_economic_artifacts.py", "scripts/v7_exact_sha_economic_bundle.py",
        "scripts/v7_external_loss_attribution.py", "scripts/v7_execution_latency_distribution.py",
        "scripts/v7_external_policy_replay.py", "scripts/v7_learned_execution_model.py",
        "scripts/v7_joint_execution_policy.py", "scripts/v7_maker_durable_learning.py",
        "scripts/v7_maker_opportunity_bridge.py", "scripts/v7_market_maker_rewards.py",
        "scripts/v7_external_cancel_opportunity_bridge.py", "scripts/v7_external_fair_paper_router.py",
        "scripts/v7_rtds_external_fair_monitor.py", "scripts/v7_external_rich_model.py",
        "scripts/v7_fair_model_artifact.py", "scripts/v7_external_lead_lag_collector.py",
        "scripts/v7_external_cancel_signal_journal.py", "scripts/v7_pm_repricing_shadow.py",
        "scripts/v7_fast_cancel_latency_report.py", "scripts/v7_maker_execution_horse_race.py",
        "scripts/v7_compressed_journal.py",
        "scripts/v7_binance_usdm_rest_collector.py", "scripts/v7_deribit_rest_collector.py",
        "scripts/v7_coinbase_l2_rest_collector.py",
        "scripts/v7_causal_book.py",
        "scripts/v7_profit_attribution.py", "scripts/v7_profit_protocol.py",
        "scripts/v7_profit_experiments.py", "scripts/v7_profit_report.py",
        "scripts/v7_profit_cohorts.py", "scripts/v7_profit_inference.py",
        "scripts/v7_profit_signal_analysis.py", "scripts/v7_maker_research_window.py",
        "scripts/v7_evidence_store.py", "scripts/v7_evidence_catalog.py",
        "scripts/v7_evidence_contract.py", "scripts/v7_evidence_capacity.py",
        "scripts/v7_storage_budget.py", "scripts/v7_aggregate_retention.py",
        "scripts/v7_permanent_evidence.py", "scripts/v7_lossless_data_compaction.py",
        "scripts/v7_permanent_datasets.py", "scripts/v7_permanent_benchmark.py",
        "scripts/v7_economic_decision_report.py",
        "config/v7_evidence_catalog.json", "config/v7_data_retention.json",
        "src/v7_maker_research_replay.cpp",
        "config/v7_profit_experiment.json",
        "config/v7_pm_repricing_250ms_shadow.json",
        "scripts/v7_crypto_settlement_engine_contract.py", "scripts/v7_crypto_settlement.py",
        "scripts/v7_process_manifest.py", "scripts/v7_process_runtime.sh",
        "scripts/v7_secret_scan.py", "scripts/v7_entropy_secret_scan.py",
        "scripts/v7_security_audit.py", "scripts/v7_release_provenance.py",
        "config/v7_execution_modes.json", "config/v7_runtime_supervision.json",
        "config/v7_strategy_registry.json", "config/v7_live_model_scope.json",
        "config/v7_external_fair.json", "config/v7_crypto_execution_alpha.json",
        "config/v7_crypto_settlement_engine.json", "config/v7_crypto_settlement_markets.json",
        "config/v7_crypto_settlement_model_registry.json", "config/v7_professional_market_maker.json",
        "config/v7_process_manifest.json", "config/v7_authority_registry.json",
        "config/v7_structural_arb_engine.json",
        "config/v7_adaptive_universe.json", "schemas/v7/opportunity_envelope.schema.json",
    )
    for rel in required_files:
        if not (root / rel).is_file():
            fail(f"V7 cutover blocked: required file missing: {rel}")

    registry = load_json(root / "config/v7_strategy_registry.json")
    enabled_algorithms = {
        str(row.get("id") or "")
        for row in registry.get("live_algorithms", [])
        if isinstance(row, dict) and row.get("enabled") is True
    }
    expected_algorithms = {"CRYPTO_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE"}
    if (registry.get("schema") != "polymarket_v7_live_algorithm_registry_v2"
            or enabled_algorithms != expected_algorithms
            or registry.get("component_independent_authority") is not False):
        fail("V7 cutover blocked: registry must contain exactly the two live algorithms")
    scope = load_json(root / "config/v7_live_model_scope.json")
    paper_engines = set(scope.get("live_algorithms") or [])
    if (scope.get("schema") != "polymarket_v7_live_engine_scope_v2"
            or scope.get("version") != 7
            or scope.get("live_algorithm_count") != 2
            or scope.get("paper_only") is not True
            or scope.get("authenticated_execution") is not False
            or scope.get("real_order_submission") is not False
            or scope.get("real_capital_at_risk") is not False
            or scope.get("component_independent_authority") is not False):
        fail("V7 cutover blocked: two-algorithm scope identity/safety contract invalid")
    if paper_engines != expected_algorithms:
        fail("V7 cutover blocked: exactly two economic engines must own PAPER decisions")
    invariants = scope.get("runtime_invariants") if isinstance(scope.get("runtime_invariants"), dict) else {}
    if (invariants.get("single_execution_owner") is not True
            or invariants.get("global_portfolio_coordinator") != "V7_GLOBAL_PORTFOLIO_COORDINATOR"):
        fail("V7 cutover blocked: runtime invariant contract invalid")

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
    return {"V7_CUTOVER_SHA": head,"V7_RUNTIME_VERSION":"7","V7_RUNTIME_LOOP":loop_rel,"V7_RUNTIME_CONFIG":config_rel,"V7_RUNTIME_RUN_ROOT":run_root_rel}


def main() -> int:
    parser=argparse.ArgumentParser(description="Fail-closed V7 PAPER cutover contract")
    parser.add_argument("--repository-root",type=Path,default=Path(".")); parser.add_argument("--expected-head"); parser.add_argument("--github-env",type=Path)
    args=parser.parse_args(); env=validate(args.repository_root,args.expected_head); output="\n".join(f"{k}={v}" for k,v in env.items())+"\n"
    if args.github_env:
        with args.github_env.open("a",encoding="utf-8") as handle: handle.write(output)
    print(output,end=""); return 0

if __name__=="__main__": raise SystemExit(main())
