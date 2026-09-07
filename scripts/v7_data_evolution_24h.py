#!/usr/bin/env python3
"""Freeze and evaluate a 24h data-evolution screen for BTC M5.

Research-only. It never mutates a champion, never grants execution authority,
and never weakens the production promotion gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

try:
    from v7_external_economic_common import atomic_json, canonical_sha256, finite
    from v7_external_settlement_model import predict
    from v7_external_settlement_train import read_dataset, train_artifact
    from v7_external_settlement_validate import fee_per_share, _entry_policy
    from v7_fair_value_registry import FairModelArtifact
except ModuleNotFoundError:
    from scripts.v7_external_economic_common import atomic_json, canonical_sha256, finite
    from scripts.v7_external_settlement_model import predict
    from scripts.v7_external_settlement_train import read_dataset, train_artifact
    from scripts.v7_external_settlement_validate import fee_per_share, _entry_policy
    from scripts.v7_fair_value_registry import FairModelArtifact

SCHEMA_FREEZE = "polymarket_v7_data_evolution_24h_freeze_v1"
SCHEMA_REPORT = "polymarket_v7_data_evolution_24h_report_v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_artifact(path: Path) -> FairModelArtifact:
    raw = json.loads(path.read_text(encoding="utf-8"))
    allowed = {field.name for field in fields(FairModelArtifact)}
    artifact = FairModelArtifact(**{k: v for k, v in raw.items() if k in allowed})
    artifact.validate()
    return artifact


def market_cluster_brier(rows: list[dict[str, Any]], probs: list[float]) -> float | None:
    by_market: dict[str, list[tuple[float, float]]] = {}
    for row, prob in zip(rows, probs):
        by_market.setdefault(str(row["market_id"]), []).append((prob, float(row["actual_yes"])))
    if not by_market:
        return None
    scores = []
    for values in by_market.values():
        scores.append(statistics.fmean((p - y) ** 2 for p, y in values))
    return statistics.fmean(scores)


def market_probabilities(rows: list[dict[str, Any]]) -> list[float | None]:
    out: list[float | None] = []
    for row in rows:
        value = finite((row.get("features") or {}).get("market_yes"))
        out.append(float(value) if value is not None and 0.0 <= value <= 1.0 else None)
    return out


def bootstrap_interval(values: list[float], *, seed: int, draws: int) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(seed)
    samples = sorted(
        statistics.fmean(rng.choice(values) for _ in values)
        for _ in range(draws)
    )
    lo = samples[max(0, int(0.025 * draws))]
    hi = samples[min(draws - 1, int(0.975 * draws) - 1)]
    return lo, hi


def executable_action(row: dict[str, Any], artifact: FairModelArtifact,
                      runtime_config: dict[str, Any], threshold: float,
                      probability_mode: str = "point") -> dict[str, Any] | None:
    prediction = predict(artifact, row["features"])
    execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
    schedule = execution.get("fee_schedule") if isinstance(execution.get("fee_schedule"), dict) else {}
    tte = float(row["features"]["tte_seconds"])
    _, base_risk = _entry_policy(tte, runtime_config)
    choices: list[tuple[float, str, float, float, float]] = []
    if probability_mode not in {"point", "robust"}:
        raise ValueError("probability_mode")
    yes_probability = prediction["yes"] if probability_mode == "point" else prediction["lower"]
    no_probability = 1.0 - prediction["yes"] if probability_mode == "point" else 1.0 - prediction["upper"]
    for outcome, probability, ask_key, size_key, minimum_key in (
        ("YES", yes_probability, "yes_best_ask", "yes_best_ask_visible_size", "yes_min_order_size"),
        ("NO", no_probability, "no_best_ask", "no_best_ask_visible_size", "no_min_order_size"),
    ):
        ask = finite(execution.get(ask_key))
        visible = finite(execution.get(size_key))
        minimum = finite(execution.get(minimum_key))
        if ask is None or visible is None or minimum is None or minimum <= 0.0 or visible < minimum:
            continue
        fee = fee_per_share(float(ask), schedule)
        if fee is None:
            continue
        edge = float(probability) - float(ask) - float(fee) - float(base_risk)
        choices.append((edge, outcome, float(ask), float(fee), float(minimum)))
    if not choices:
        return None
    edge, outcome, ask, fee, quantity = max(choices)
    if edge <= threshold:
        return None
    won = (float(row["actual_yes"]) == 1.0) == (outcome == "YES")
    gross = quantity * ((1.0 if won else 0.0) - ask - fee)
    conservative = gross - quantity * float(base_risk)
    stress = quantity * ((1.0 if won else 0.0) - ask - 2.0 * fee) - quantity * 2.0 * float(base_risk)
    return {
        "market_id": str(row["market_id"]), "observed_ms": int(row["observed_ms"]),
        "outcome": outcome, "edge": edge, "ask": ask, "fee": fee,
        "quantity": quantity, "pnl": gross, "conservative_pnl": conservative,
        "stress_conservative_pnl": stress,
        "model_yes": float(prediction["yes"]),
        "market_yes": finite((row.get("features") or {}).get("market_yes")),
    }


def policy_actions(rows: list[dict[str, Any]], artifact: FairModelArtifact,
                   runtime_config: dict[str, Any], threshold: float,
                   probability_mode: str = "point") -> list[dict[str, Any]]:
    selected: set[str] = set()
    actions: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: (int(r["observed_ms"]), str(r["market_id"]))):
        market = str(row["market_id"])
        if market in selected:
            continue
        action = executable_action(row, artifact, runtime_config, threshold, probability_mode)
        if action is None:
            continue
        selected.add(market)
        actions.append(action)
    return actions


def summarize_actions(actions: list[dict[str, Any]], *, draws: int, seed: int) -> dict[str, Any]:
    primary = [float(a["conservative_pnl"]) for a in actions]
    stress = [float(a["stress_conservative_pnl"]) for a in actions]
    plo, phi = bootstrap_interval(primary, seed=seed, draws=draws)
    slo, shi = bootstrap_interval(stress, seed=seed ^ 0xA5A5A5, draws=draws)
    return {
        "actions": len(actions),
        "markets": len({a["market_id"] for a in actions}),
        "net_pnl": sum(float(a["pnl"]) for a in actions),
        "net_conservative_pnl": sum(primary),
        "mean_conservative_pnl_per_market": statistics.fmean(primary) if primary else None,
        "bootstrap95_conservative_pnl_per_market": [plo, phi],
        "stress_net_conservative_pnl": sum(stress),
        "stress_mean_conservative_pnl_per_market": statistics.fmean(stress) if stress else None,
        "stress_bootstrap95_conservative_pnl_per_market": [slo, shi],
        "positive_market_fraction": (
            sum(value > 0.0 for value in primary) / len(primary) if primary else None
        ),
    }


def select_threshold(rows: list[dict[str, Any]], artifact: FairModelArtifact,
                     runtime_config: dict[str, Any], thresholds: list[float],
                     draws: int, seed: int) -> tuple[float, dict[str, Any]]:
    candidates: list[tuple[float, float, int, dict[str, Any]]] = []
    for threshold in thresholds:
        actions = policy_actions(rows, artifact, runtime_config, threshold, "point")
        summary = summarize_actions(actions, draws=draws, seed=seed ^ int(threshold * 1e6))
        lo = summary["bootstrap95_conservative_pnl_per_market"][0]
        score = float(lo) if lo is not None and summary["actions"] >= 5 else -math.inf
        candidates.append((score, threshold, summary["actions"], summary))
    best = max(candidates, key=lambda row: (row[0], row[2], -row[1]))
    if not math.isfinite(best[0]):
        best = max(candidates, key=lambda row: (row[2], -row[1]))
    return float(best[1]), best[3]


def safe_config(config: dict[str, Any]) -> None:
    if (config.get("schema") != "polymarket_v7_data_evolution_24h_v1"
            or config.get("paper_only") is not True
            or config.get("authenticated_execution") is not False
            or config.get("real_order_submission") is not False
            or config.get("automatic_promotion") is not False
            or config.get("production_gates_unchanged") is not True):
        raise ValueError("unsafe_24h_config")


def freeze(args: argparse.Namespace) -> dict[str, Any]:
    config = json.loads(args.screen_config.read_text(encoding="utf-8"))
    runtime_config = json.loads(args.runtime_config.read_text(encoding="utf-8"))
    dataset_manifest = json.loads(args.dataset_manifest.read_text(encoding="utf-8"))
    safe_config(config)
    rows = read_dataset(args.dataset)
    if not rows or dataset_manifest.get("causality_failures") != 0 or dataset_manifest.get("fail_closed") is True:
        raise ValueError("baseline_dataset_not_causal")
    repo_head = args.code_sha
    policy_version = canonical_sha256(runtime_config)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    candidates = []
    draws = int(config["screening"]["bootstrap_draws"])
    for index, ridge in enumerate(config["ridge_grid"]):
        artifact, report = train_artifact(
            rows, code_sha=repo_head, policy_version=policy_version,
            dataset_sha256=str(dataset_manifest["dataset_sha256"]), ridge=float(ridge),
            minimum_contracts=30, artifact_role="RESEARCH",
        )
        artifact_path = output / f"candidate_ridge_{index:02d}.json"
        atomic_json(artifact_path, asdict(artifact))
        holdout = [
            row for row in rows
            if int(row["observed_ms"]) * 1_000_000 > int(artifact.training_end_ns)
        ]
        threshold, holdout_summary = select_threshold(
            holdout, artifact, runtime_config,
            [float(x) for x in config["gating_threshold_grid"]],
            draws=draws, seed=int(artifact.model_hash[:16], 16),
        )
        candidates.append({
            "ridge": float(ridge), "artifact": str(artifact_path),
            "artifact_sha256": sha256_file(artifact_path),
            "model_hash": artifact.model_hash, "model_version": artifact.model_version,
            "validation_brier": report["splits"]["validation"]["brier"],
            "test_brier": report["splits"]["test"]["brier"],
            "gating_threshold": threshold,
            "holdout_policy": holdout_summary,
        })
    ranked = sorted(
        candidates,
        key=lambda row: (float(row["validation_brier"]), float(row["ridge"])),
    )
    frozen = ranked[: int(config["frozen_candidate_count"])]
    now_ms = int(time.time() * 1000)
    value = {
        "schema": SCHEMA_FREEZE, "frozen_at_ms": now_ms,
        "forward_boundary_ms": now_ms,
        "window_seconds": int(config["window_seconds"]),
        "repository_head": repo_head,
        "baseline_dataset_sha256": str(dataset_manifest["dataset_sha256"]),
        "baseline_dataset_file_sha256": sha256_file(args.dataset),
        "baseline_manifest_file_sha256": sha256_file(args.dataset_manifest),
        "baseline_contracts": len({str(row["market_id"]) for row in rows}),
        "baseline_rows": len(rows),
        "candidate_selection": "lowest validation Brier; threshold selected only on chronological test holdout",
        "frozen_candidates": frozen,
        "screening": config["screening"],
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "automatic_promotion": False,
        "production_gates_unchanged": True,
        "new_generation_requires_new_forward_boundary": True,
    }
    value["content_sha256"] = canonical_sha256(value)
    atomic_json(output / "freeze.json", value)
    return value


def evaluate_candidate(rows: list[dict[str, Any]], candidate: dict[str, Any],
                       runtime_config: dict[str, Any], screening: dict[str, Any]) -> dict[str, Any]:
    artifact = load_artifact(Path(candidate["artifact"]))
    probabilities = [float(predict(artifact, row["features"])["yes"]) for row in rows]
    model_brier = market_cluster_brier(rows, probabilities)
    market_probs = market_probabilities(rows)
    comparable_rows = [row for row, prob in zip(rows, market_probs) if prob is not None]
    comparable_probs = [float(prob) for prob in market_probs if prob is not None]
    market_brier = market_cluster_brier(comparable_rows, comparable_probs)
    threshold = float(candidate["gating_threshold"])
    actions = policy_actions(rows, artifact, runtime_config, threshold, "point")
    robust_actions = policy_actions(rows, artifact, runtime_config, threshold, "robust")
    draws = int(screening["bootstrap_draws"])
    summary = summarize_actions(
        actions, draws=draws, seed=int(artifact.model_hash[:16], 16)
    )
    brier_gap = None
    if model_brier is not None and market_brier is not None:
        brier_gap = float(model_brier) - float(market_brier)
    return {
        "model_hash": artifact.model_hash,
        "model_version": artifact.model_version,
        "ridge": candidate["ridge"],
        "gating_threshold": threshold,
        "forward_model_brier": model_brier,
        "forward_market_brier": market_brier,
        "model_minus_market_brier": brier_gap,
        "policy": summary,
        "policy_mode": "POINT_ESTIMATE_PAPER_PROBE_SCREEN_ONLY",
        "robust_policy": summarize_actions(
            robust_actions, draws=draws, seed=int(artifact.model_hash[16:32], 16)),
    }


def screening_state(result: dict[str, Any], *, contracts: int, rows: int,
                    causality_failures: int, screening: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    policy = result["policy"]
    if contracts < int(screening["minimum_forward_contracts"]):
        reasons.append("INSUFFICIENT_FORWARD_CONTRACTS")
    if rows < int(screening["minimum_forward_rows"]):
        reasons.append("INSUFFICIENT_FORWARD_ROWS")
    if int(policy["actions"]) < int(screening["minimum_policy_actions"]):
        reasons.append("INSUFFICIENT_POLICY_ACTIONS")
    if causality_failures and screening.get("require_zero_causality_failures") is True:
        reasons.append("CAUSALITY_FAILURE")
    if any(reason.startswith("INSUFFICIENT") for reason in reasons):
        return "INSUFFICIENT_24H_EVIDENCE", reasons
    primary_lcb = policy["bootstrap95_conservative_pnl_per_market"][0]
    stress_lcb = policy["stress_bootstrap95_conservative_pnl_per_market"][0]
    positive = policy["positive_market_fraction"]
    brier_gap = result["model_minus_market_brier"]
    if causality_failures:
        reasons.append("CAUSALITY_FAILURE")
    if primary_lcb is None or float(primary_lcb) <= 0.0:
        reasons.append("PRIMARY_MARKET_BOOTSTRAP_LCB_NOT_POSITIVE")
    if stress_lcb is None or float(stress_lcb) <= 0.0:
        reasons.append("STRESS_MARKET_BOOTSTRAP_LCB_NOT_POSITIVE")
    if positive is None or float(positive) < float(screening["minimum_positive_market_fraction"]):
        reasons.append("POSITIVE_MARKET_FRACTION_TOO_LOW")
    if brier_gap is None or float(brier_gap) > float(screening["maximum_global_brier_disadvantage"]):
        reasons.append("GLOBAL_BRIER_DISADVANTAGE_TOO_LARGE")
    if not reasons:
        return "PROMISING_FOR_PAPER_EXPLORATION_PROBE", []
    primary_mean = policy["mean_conservative_pnl_per_market"]
    stress_mean = policy["stress_mean_conservative_pnl_per_market"]
    if (brier_gap is not None
            and float(brier_gap) > float(screening["kill_global_brier_disadvantage"])):
        return "REJECT", sorted(set(reasons + ["GLOBAL_BRIER_KILL_GATE"]))
    if (primary_mean is not None and stress_mean is not None
            and float(primary_mean) < 0.0 and float(stress_mean) < 0.0):
        return "REJECT", sorted(set(reasons + ["NEGATIVE_PRIMARY_AND_STRESS_MEAN"]))
    return "CONTINUE_SHADOW", sorted(set(reasons))


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    freeze_value = json.loads(args.freeze.read_text(encoding="utf-8"))
    if freeze_value.get("schema") != SCHEMA_FREEZE or freeze_value.get("production_gates_unchanged") is not True:
        raise ValueError("freeze_contract_invalid")
    runtime_config = json.loads(args.runtime_config.read_text(encoding="utf-8"))
    dataset_manifest = json.loads(args.dataset_manifest.read_text(encoding="utf-8"))
    all_rows = read_dataset(args.dataset)
    boundary = int(freeze_value["forward_boundary_ms"])
    forward_rows = [row for row in all_rows if int(row["observed_ms"]) > boundary]
    forward_contracts = len({str(row["market_id"]) for row in forward_rows})
    causality_failures = sum(row.get("causality_valid") is not True for row in forward_rows)
    screening = freeze_value["screening"]
    results = []
    for candidate in freeze_value["frozen_candidates"]:
        result = evaluate_candidate(forward_rows, candidate, runtime_config, screening)
        state, reasons = screening_state(
            result, contracts=forward_contracts, rows=len(forward_rows),
            causality_failures=causality_failures, screening=screening,
        )
        result.update({"state": state, "reason_codes": reasons})
        results.append(result)
    promising = [row for row in results if row["state"] == "PROMISING_FOR_PAPER_EXPLORATION_PROBE"]
    best = None
    if promising:
        best = max(
            promising,
            key=lambda row: float(row["policy"]["stress_bootstrap95_conservative_pnl_per_market"][0]),
        )["model_hash"]
    overall = (
        "PROMISING_FOR_PAPER_EXPLORATION_PROBE" if promising else
        "INSUFFICIENT_24H_EVIDENCE" if results and all(r["state"] == "INSUFFICIENT_24H_EVIDENCE" for r in results) else
        "REJECT" if results and all(r["state"] == "REJECT" for r in results) else
        "CONTINUE_SHADOW"
    )
    value = {
        "schema": SCHEMA_REPORT,
        "generated_at_ms": int(time.time() * 1000),
        "freeze_sha256": sha256_file(args.freeze),
        "forward_boundary_ms": boundary,
        "forward_rows": len(forward_rows),
        "forward_contracts": forward_contracts,
        "causality_failures": causality_failures,
        "dataset_sha256": dataset_manifest.get("dataset_sha256"),
        "overall_state": overall,
        "best_promising_model_hash": best,
        "candidates": results,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "automatic_promotion": False,
        "production_gates_unchanged": True,
        "operator_action_required_for_any_paper_promotion": True,
    }
    value["content_sha256"] = canonical_sha256(value)
    atomic_json(args.output, value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("--dataset", type=Path, required=True)
    freeze_parser.add_argument("--dataset-manifest", type=Path, required=True)
    freeze_parser.add_argument("--screen-config", type=Path, required=True)
    freeze_parser.add_argument("--runtime-config", type=Path, required=True)
    freeze_parser.add_argument("--output-dir", type=Path, required=True)
    freeze_parser.add_argument("--code-sha", required=True)
    eval_parser = sub.add_parser("evaluate")
    eval_parser.add_argument("--freeze", type=Path, required=True)
    eval_parser.add_argument("--dataset", type=Path, required=True)
    eval_parser.add_argument("--dataset-manifest", type=Path, required=True)
    eval_parser.add_argument("--runtime-config", type=Path, required=True)
    eval_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = freeze(args) if args.mode == "freeze" else evaluate(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"v7_data_evolution_24h: {exc}", file=__import__("sys").stderr)
        return 2
    if args.mode == "freeze":
        print(json.dumps({
            "state": "FROZEN", "boundary_ms": value["forward_boundary_ms"],
            "baseline_contracts": value["baseline_contracts"],
            "candidate_count": len(value["frozen_candidates"]),
            "content_sha256": value["content_sha256"],
        }, sort_keys=True))
    else:
        print(json.dumps({
            "state": value["overall_state"], "forward_contracts": value["forward_contracts"],
            "best_promising_model_hash": value["best_promising_model_hash"],
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
