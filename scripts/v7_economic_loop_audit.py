#!/usr/bin/env python3
"""Build a deduplicated, source-labelled V7 economic-loop audit.

The audit deliberately keeps three evidence classes separate:
  * canonical ledger rows recomputed from local live/archive JSONL files;
  * a point-in-time Prometheus snapshot queried from the running PAPER server;
  * historical reference claims supplied by the owner but not locally recomputed.

It never turns telemetry row counts into independent economic sample counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import subprocess
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from typing import Any, Iterable


EVENT_BUCKET = {
    "CANDIDATE": "candidates",
    "INTENT": "intents",
    "ORDER_SUBMITTED": "orders",
    "FILL": "fills",
    "PARTIAL_FILL": "fills",
    "FINAL": "terminal",
    "MARKOUT": "markouts",
    "INVENTORY_SPLIT_REQUESTED": "split_requests",
    "INVENTORY_SPLIT": "splits",
    "INVENTORY_SPLIT_REJECTED": "split_rejects",
    "INVENTORY_MERGE": "merges",
    "MODEL_RELOAD": "model_reloads",
}

PROMETHEUS_METRICS = (
    "polymarket_v7_deployed_sha_info",
    "polymarket_v7_runtime_uptime_seconds",
    "polymarket_v7_paper_only_contract_ok",
    "polymarket_v7_authenticated_execution_disabled",
    "polymarket_v7_live_model_operational_count",
    "polymarket_v7_live_model_target_count",
    "polymarket_maker_lab_orders",
    "polymarket_maker_lab_fills",
    "polymarket_maker_lab_filled_orders",
    "polymarket_maker_lab_realized_pnl_usd",
    "polymarket_execution_orders_submitted",
    "polymarket_execution_fills",
    "polymarket_execution_final_pnl_usd",
    "polymarket_strategy_ledger_orders_submitted",
    "polymarket_strategy_ledger_fills",
    "polymarket_strategy_final_pnl_usd",
    "polymarket_runtime_realized_pnl_usd",
    "polymarket_runtime_pnl_usd",
    "polymarket_v7_maker_quote_intents_total",
    "polymarket_v7_maker_decisions_total",
    "polymarket_v7_maker_paused_no_fresh_flow",
    "polymarket_v7_maker_selector_fallback_active",
)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def canonical_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def identity(record: dict[str, Any]) -> tuple[str, str, str, str]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    model_sha = str(record.get("model_sha") or metadata.get("model_sha") or "unknown")
    policy_hash = str(record.get("policy_hash") or metadata.get("policy_hash") or "unknown")
    config_hash = str(record.get("config_hash") or metadata.get("config_hash") or "unknown")
    record_id = str(record.get("record_id") or canonical_hash(record))
    return model_sha, policy_hash, config_hash, record_id


def iter_jsonl(paths: Iterable[pathlib.Path]) -> Iterable[tuple[pathlib.Path, int, dict[str, Any]]]:
    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, 1):
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        yield path, line_no, value
        except OSError:
            continue


def ledger_paths(roots: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    found: set[pathlib.Path] = set()
    for root in roots:
        if root.is_file() and root.suffix == ".jsonl":
            found.add(root.resolve())
        elif root.exists():
            found.update(path.resolve() for path in root.rglob("*.jsonl"))
    return sorted(found)


def finite_number(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def economic_unit(record: dict[str, Any]) -> str:
    event = str(record.get("event_type") or "UNKNOWN")
    keys = {
        "CANDIDATE": ("candidate_id",),
        "INTENT": ("intent_id", "candidate_id"),
        "ORDER_SUBMITTED": ("order_id",),
        "FILL": ("fill_id", "order_id"),
        "PARTIAL_FILL": ("fill_id", "order_id"),
        "FINAL": ("position_id", "order_id", "bundle_id"),
        "MARKOUT": ("fill_id", "order_id"),
    }.get(event, ("record_id",))
    for key in keys:
        if record.get(key) not in (None, ""):
            return f"{event}:{record[key]}"
    return f"{event}:{canonical_hash(record)}"


def summarize_ledgers(paths: list[pathlib.Path]) -> dict[str, Any]:
    rows = list(iter_jsonl(paths))
    unique: dict[tuple[str, str, str, str], tuple[pathlib.Path, int, dict[str, Any]]] = {}
    duplicate_rows = 0
    for item in rows:
        key = identity(item[2])
        if key in unique:
            duplicate_rows += 1
            continue
        unique[key] = item

    families: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "telemetry_rows": 0,
        "independent_economic_units": 0,
        "events": Counter(),
        "funnel": Counter(),
        "notional_usd": 0.0,
        "quote_exposure_seconds": 0.0,
        "capital_hours": 0.0,
        "realized_pnl_usd": 0.0,
        "markout_pnl_usd": 0.0,
        "reject_reasons": Counter(),
    })
    units: dict[str, set[str]] = defaultdict(set)
    for _, _, record in unique.values():
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        family = str(record.get("strategy") or record.get("family") or metadata.get("family") or "unknown")
        event = str(record.get("event_type") or "UNKNOWN")
        row = families[family]
        row["telemetry_rows"] += 1
        row["events"][event] += 1
        if event in EVENT_BUCKET:
            row["funnel"][EVENT_BUCKET[event]] += 1
        units[family].add(economic_unit(record))
        size = finite_number(record.get("filled_size", record.get("intended_size", 0.0)))
        price = finite_number(record.get("fill_price", record.get("limit_price", 0.0)))
        row["notional_usd"] += max(0.0, size) * max(0.0, price)
        row["quote_exposure_seconds"] += max(0.0, finite_number(record.get("quote_exposure_seconds")))
        row["capital_hours"] += max(0.0, finite_number(record.get("capital_hours")))
        if event == "FINAL":
            row["realized_pnl_usd"] += finite_number(record.get("final_pnl", record.get("realized_cashflow")))
        if event == "MARKOUT":
            row["markout_pnl_usd"] += finite_number(record.get("markout_pnl", record.get("pnl")))
        reason = record.get("reject_reason") or metadata.get("reject_reason")
        if reason:
            row["reject_reasons"][str(reason)] += 1

    output: dict[str, Any] = {}
    for family, row in sorted(families.items()):
        row["independent_economic_units"] = len(units[family])
        row["events"] = dict(sorted(row["events"].items()))
        row["funnel"] = dict(sorted(row["funnel"].items()))
        row["reject_reasons"] = dict(row["reject_reasons"].most_common())
        for key in ("notional_usd", "quote_exposure_seconds", "capital_hours",
                    "realized_pnl_usd", "markout_pnl_usd"):
            row[key] = round(row[key], 12)
        output[family] = row
    return {
        "source": "local_canonical_jsonl_recomputed",
        "files": [str(path) for path in paths],
        "raw_rows": len(rows),
        "deduplicated_rows": len(unique),
        "duplicate_rows_removed": duplicate_rows,
        "dedup_identity": ["model_sha", "policy_hash_or_unknown", "config_hash_or_unknown", "record_id_or_canonical_hash"],
        "families": output,
    }


def prometheus_query(base: str, metric: str) -> list[dict[str, Any]]:
    url = base.rstrip("/") + "/query?query=" + urllib.parse.quote(metric, safe="")
    with urllib.request.urlopen(url, timeout=15) as response:
        payload = json.load(response)
    result = payload.get("data", {}).get("result", [])
    return result if isinstance(result, list) else []


def prometheus_snapshot(base: str | None) -> dict[str, Any]:
    if not base:
        return {"source": "not_requested", "available": False, "metrics": {}}
    metrics: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for metric in PROMETHEUS_METRICS:
        try:
            metrics[metric] = prometheus_query(base, metric)
        except Exception as error:  # point-in-time evidence must not hide partial outages
            errors[metric] = f"{type(error).__name__}: {error}"
    return {
        "source": base,
        "available": bool(metrics),
        "captured_at_unix_ms": int(time.time() * 1000),
        "metrics": metrics,
        "errors": errors,
    }


def capability_map(repo: pathlib.Path) -> dict[str, Any]:
    runtime = (repo / "src/v7_market_maker_runtime.cpp").read_text(encoding="utf-8")
    execution = (repo / "src/v7_maker_execution_policy.cpp").read_text(encoding="utf-8")
    loop = (repo / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    relations = (repo / "config/v7_fast_structural_relations.csv").read_text(encoding="utf-8").splitlines()
    split_calls = runtime.count("split_complete_sets(")
    fast_executor = (repo / "scripts/v7_fast_structural_paper_executor.py").read_text(encoding="utf-8")
    canonical = (repo / "scripts/v7_canonical_economics.py").read_text(encoding="utf-8")
    fair = (repo / "scripts/v7_rtds_external_fair_monitor.py").read_text(encoding="utf-8")
    fee_registry = (repo / "scripts/v7_fee_reward_registry.py").read_text(encoding="utf-8")
    return {
        "source": "repository_call_site_analysis",
        "inventory_split_primitive_implemented": "MakerPaperExecutionPolicy::split_complete_sets" in execution,
        "inventory_split_runtime_call_count": split_calls,
        "inventory_split_ledger_writer": 'INVENTORY_SPLIT' in runtime,
        "inventory_merge_ledger_writer": 'INVENTORY_MERGE' in runtime,
        "runtime_champion_path": "runs/paper_v7_live/micro_maker/execution_model.json",
        "runtime_champion_reload_loop": "last_model_check_ms" in runtime and "model_store->publish" in runtime,
        "model_reload_ledger_event": 'MODEL_RELOAD' in runtime,
        "micro_taker_worker_started": "v7_micro_taker_worker.py" in loop,
        "micro_taker_canonical_spool_argument": "--ledger-spool" in loop or "--spool" in loop,
        "graph_relation_data_rows": max(0, len([line for line in relations if line.strip()]) - 1),
        "fast_structural_paper_executor_started": "v7_fast_structural_paper_executor.py" in loop,
        "fast_structural_revalidation_and_unwind": "partial bundle unwind" in fast_executor and "fee_verified" in fast_executor,
        "canonical_shadow_counterfactual_separation": "shadow_counterfactual" in canonical and "excluded_from_portfolio_equity" in canonical,
        "external_only_and_hybrid_models": "external_only_fair" in fair and "hybrid_fair" in fair,
        "slow_economic_shadow_always_on": "v7_slow_economic_shadow_supervisor.py" in loop,
        "evidence_allocator_advisory_only": "v7_evidence_capital_allocator.py" in loop,
        "fee_unknown_non_executable": "UNKNOWN_FEE" in fee_registry and "NON_EXECUTABLE" in fee_registry,
        "reward_unknown_forced_zero": "unknown_reward_forced_zero" in fee_registry,
    }


def historical_reference_claims() -> dict[str, Any]:
    return {
        "evidence_status": "owner_supplied_reference_claims_not_recomputed_from_local_archive",
        "starting_paper_capital_usd": 14000.0,
        "operational_target_families": {"operational": 8, "target": 12},
        "maker": {
            "buy_orders_approx": 85967,
            "sell_orders_approx": 3010,
            "buy_fills_approx": 81,
            "sell_fills_approx": 28,
            "global_fill_rate_approx": 0.0022,
            "inventory_split_events": 0,
            "markouts_1_to_60s": "predominantly_negative",
        },
        "champion_file": "reported_absent",
        "challenger": "reported_regenerated_but_not_loaded",
        "rewards": {"reward_pool_count": 0, "reward_market_count": 0},
        "profitability_proven": False,
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    repo = pathlib.Path(args.repo).resolve()
    roots = [pathlib.Path(path).resolve() for path in args.ledger_root]
    return {
        "schema": "polymarket_v7_economic_loop_audit_v1",
        "audit_kind": args.kind,
        "generated_at_unix_ms": int(time.time() * 1000),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "repository_head": subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip(),
        "ledger": summarize_ledgers(ledger_paths(roots)),
        "live_prometheus": prometheus_snapshot(args.prometheus_url),
        "capability_runtime_proof": capability_map(repo),
        "historical_reference": historical_reference_claims(),
        "interpretation": {
            "telemetry_is_not_independent_evidence": True,
            "zero_local_rows_means_archive_unavailable_not_zero_historical_activity": True,
            "profitability_proven": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--ledger-root", action="append", default=[])
    parser.add_argument("--prometheus-url")
    parser.add_argument("--kind", choices=("baseline", "postchange"), default="baseline")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not args.ledger_root:
        args.ledger_root = ["runs/paper_v7_archives", "runs/paper_v7_live"]
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build(args), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
