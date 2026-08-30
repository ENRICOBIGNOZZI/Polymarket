#!/usr/bin/env python3
"""Generate the V7 economic evidence pack without inventing missing runtime data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from typing import Any

try:
    from v7_economic_loop_audit import build as build_loop_audit
    from v7_profitability_audit import audit as profitability_audit
except ModuleNotFoundError:  # imported as scripts.v7_generate_economic_artifacts
    from scripts.v7_economic_loop_audit import build as build_loop_audit
    from scripts.v7_profitability_audit import audit as profitability_audit


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def envelope(schema: str, sha: str, runtime_available: bool, **values: Any) -> dict[str, Any]:
    return {
        "schema": schema, "version": 7, "generated_at_unix_ms": int(time.time() * 1000),
        "repository_head": sha, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "runtime_evidence_available": runtime_available,
        "missing_runtime_data_is_not_zero_economic_activity": not runtime_available,
        **values,
    }


def generate(repo: Path, run_root: Path, output: Path, baseline_path: Path,
             *, include_archives: bool = False) -> dict[str, dict[str, Any]]:
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    ledger = run_root / "ledger" / "execution.jsonl"
    runtime = load(run_root / "control" / "runtime_status.json")
    input_runtime_model_sha = str(runtime.get("model_sha") or "")
    runtime_available = bool(ledger.is_file() and input_runtime_model_sha == sha)
    inputs = [run_root]
    archives = run_root.parent / "paper_v7_archives"
    if include_archives and archives.exists():
        inputs.append(archives)
    profit = profitability_audit(inputs)
    args = SimpleNamespace(repo=str(repo), ledger_root=[str(path) for path in inputs],
                           prometheus_url=None, kind="postchange")
    postchange = build_loop_audit(args)
    postchange["input_runtime_model_sha"] = input_runtime_model_sha or None
    postchange["exact_sha_runtime_evidence_available"] = runtime_available
    if not runtime_available:
        postchange["interpretation"]["runtime_evidence_state"] = "INCUMBENT_OR_UNAVAILABLE_NOT_POSTCHANGE_SHA"
    baseline = load(baseline_path)
    capability = envelope(
        "polymarket_v7_capability_runtime_proof_v1", sha, runtime_available,
        source="repository_call_site_analysis_plus_optional_exact_sha_runtime",
        input_runtime_model_sha=input_runtime_model_sha or None,
        exact_sha_runtime=input_runtime_model_sha == sha,
        capabilities=postchange["capability_runtime_proof"],
        profitability_proven=False,
    )
    replay = envelope(
        "polymarket_v7_replay_comparison_v1", sha, runtime_available,
        baseline_repository_head=baseline.get("repository_head"),
        baseline_capabilities=baseline.get("capability_runtime_proof", {}),
        postchange_capabilities=postchange["capability_runtime_proof"],
        baseline_ledger=baseline.get("ledger", {}), postchange_ledger=postchange.get("ledger", {}),
        comparison_scope="CAPABILITY_AND_AVAILABLE_DEDUPLICATED_PAPER_EVIDENCE",
        economic_claim="NO_PROFITABILITY_CLAIM_WITHOUT_FORWARD_TERMINAL_EVIDENCE",
    )
    canonical = load(run_root / "canonical_economics.json")
    reconciliation_runtime = load(run_root / "control" / "portfolio_reconciliation.json")
    reconciliation = envelope(
        "polymarket_v7_reconciliation_report_v1", sha, runtime_available,
        canonical_economics=canonical, runtime_reconciliation=reconciliation_runtime,
        shadow_excluded_from_authoritative_equity=True,
        state=("AVAILABLE" if canonical else "PENDING_EXACT_SHA_RUNTIME"),
    )
    external = envelope(
        "polymarket_v7_external_fair_forecast_to_pnl_v1", sha, runtime_available,
        cohorts={
            "external_only_fair": profit.get("external_fair_counterfactual", {}).get("forecast_model_score", {}),
            "hybrid_fair": profit.get("external_fair_counterfactual", {}).get("forecast_hybrid_score", {}),
            "pm_mid_benchmark": profit.get("external_fair_counterfactual", {}).get("forecast_market_benchmark_score", {}),
        },
        paper_execution=profit.get("external_fair", {}),
        shadow_counterfactual=profit.get("external_fair_counterfactual", {}),
        execution_model_id="external_only_fair", hybrid_authority="SHADOW",
        profitability_proven=False,
    )
    maker = envelope(
        "polymarket_v7_maker_bilateral_fillability_report_v1", sha, runtime_available,
        economics=profit.get("professional_maker", {}),
        runtime_status=load(run_root / "micro_maker" / "status.json"),
        fillability_status=load(run_root / "micro_maker" / "fillability_status.json"),
        terminal_inventory_cost_lineage_required=True,
        profitability_proven=False,
    )
    strategies = profit.get("strategy_economics", {})
    arb = envelope(
        "polymarket_v7_arb_coverage_report_v1", sha, runtime_available,
        strategies={name: strategies.get(name, {}) for name in (
            "FAST_STRUCTURAL", "HARD_ARB", "GRAPH_RV"
        )},
        verified_relation_registry=load(run_root / "graph_rv" / "relation_registry.json"),
        no_text_similarity_relations=True, partial_bundle_unwind_required=True,
        profitability_proven=False,
    )
    research = envelope(
        "polymarket_v7_research_shadow_report_v1", sha, runtime_available,
        fast_shadow_manifest=load(run_root / "control" / "research_sleeves_manifest.json"),
        slow_shadow_manifest=load(run_root / "control" / "slow_research_shadow_manifest.json"),
        zero_capital=True, oms_authority=False, ledger_writer_authority=False,
        automatic_promotion=False,
    )
    lineage = envelope(
        "polymarket_v7_lineage_report_v1", sha, runtime_available,
        data_quality=profit.get("data_quality", {}),
        dedup_identity=["model_sha", "record_id"],
        exact_sha_required=True, single_canonical_ledger_writer=True,
        durable_maker_learning=True,
    )
    profit_enveloped = {
        **profit, "repository_head": sha, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "input_runtime_model_sha": input_runtime_model_sha or None,
        "exact_sha_runtime_evidence_available": runtime_available,
    }
    files = {
        "v7_economic_loop_postchange.json": postchange,
        "v7_replay_comparison.json": replay,
        "v7_profitability_audit.json": profit_enveloped,
        "v7_capability_runtime_proof.json": capability,
        "v7_reconciliation_report.json": reconciliation,
        "v7_external_fair_forecast_to_pnl.json": external,
        "v7_maker_bilateral_fillability_report.json": maker,
        "v7_arb_coverage_report.json": arb,
        "v7_research_shadow_report.json": research,
        "v7_lineage_report.json": lineage,
    }
    for name, value in files.items():
        write(output / name, value)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v7_live"))
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    parser.add_argument("--baseline", type=Path, default=Path("artifacts/v7_economic_loop_baseline.json"))
    parser.add_argument("--include-archives", action="store_true")
    args = parser.parse_args()
    generate(args.repo.resolve(), args.run_root.resolve(), args.output.resolve(), args.baseline.resolve(),
             include_archives=args.include_archives)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
