#!/usr/bin/env python3
"""Evidence-driven, paper-only Alpha Factory for the Polymarket engine.

The factory diagnoses where alpha is lost, evaluates challenger evidence with
OOS/cost/FDR gates, maintains a single-candidate registry, and recommends the
next experiment. It never edits the live champion, submits orders, weakens risk
limits, or authorizes real-money execution.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "polymarket_alpha_factory_report_v1"
STATE_SCHEMA = "polymarket_alpha_factory_state_v1"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def parse_timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw // 1000 if raw > 10_000_000_000 else raw
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        raw = int(float(text))
        return raw // 1000 if raw > 10_000_000_000 else raw
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    output: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            output.append(value)
    return output


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema") != "polymarket_alpha_factory_config_v1":
        errors.append("unexpected Alpha Factory config schema")
    if config.get("paper_only") is not True:
        errors.append("paper_only must be true")
    if config.get("allow_authenticated_execution") is not False:
        errors.append("allow_authenticated_execution must be false")
    if config.get("allow_direct_champion_mutation") is not False:
        errors.append("allow_direct_champion_mutation must be false")

    coordination = config.get("coordination") or {}
    allowlist = set(coordination.get("allowlisted_dispatches") or [])
    forbidden = set(coordination.get("forbidden_dispatches") or [])
    overlap = sorted(allowlist.intersection(forbidden))
    if overlap:
        errors.append("dispatch allowlist overlaps forbidden set: " + ", ".join(overlap))
    if "deploy-paper-server.yml" in allowlist:
        errors.append("the Alpha Factory may never dispatch deployment directly")

    multipliers = {finite(x) for x in (config.get("gates") or {}).get("cost_stress_multipliers", [])}
    if not {1.5, 2.0}.issubset(multipliers):
        errors.append("cost stress policy must include 1.5x and 2.0x")
    if errors:
        raise ValueError("; ".join(errors))


def benjamini_hochberg(pvalues: dict[str, float], q: float) -> dict[str, dict[str, Any]]:
    """Benjamini-Hochberg rejection and monotone adjusted p-values."""
    cleaned = {
        key: min(1.0, max(0.0, finite(value, 1.0)))
        for key, value in pvalues.items()
    }
    ordered = sorted(cleaned.items(), key=lambda item: (item[1], item[0]))
    m = len(ordered)
    if m == 0:
        return {}

    largest = 0
    for rank, (_, pvalue) in enumerate(ordered, start=1):
        if pvalue <= q * rank / m:
            largest = rank

    adjusted_by_key: dict[str, float] = {}
    running = 1.0
    for rank in range(m, 0, -1):
        key, pvalue = ordered[rank - 1]
        running = min(running, pvalue * m / rank)
        adjusted_by_key[key] = min(1.0, running)

    return {
        key: {
            "raw_pvalue": pvalue,
            "adjusted_pvalue": adjusted_by_key[key],
            "rejected": rank <= largest,
            "rank": rank,
            "tests": m,
        }
        for rank, (key, pvalue) in enumerate(ordered, start=1)
    }


def circular_block_bootstrap_pvalue(
    values: list[float], *, block: int = 5, reps: int = 2000, seed: int = 20260824
) -> float:
    """One-sided p-value for a positive mean under a centered circular-block null."""
    xs = [finite(value) for value in values if math.isfinite(finite(value, math.nan))]
    n = len(xs)
    if n < 2 or reps <= 0:
        return 1.0
    observed = statistics.fmean(xs)
    centered = [value - observed for value in xs]
    width = max(1, min(block, n))
    rng = random.Random(seed)
    exceed = 0
    for _ in range(reps):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(centered[(start + offset) % n] for offset in range(width))
        if statistics.fmean(sample[:n]) >= observed:
            exceed += 1
    return (exceed + 1) / (reps + 1)


def cost_stressed_pnl(summary: dict[str, Any], multiplier: float) -> float:
    gross = finite(summary.get("gross_pnl"))
    costs = finite(summary.get("fees")) + finite(summary.get("slippage"))
    return gross - multiplier * costs


def positive_count(rows: Iterable[dict[str, Any]], key: str, threshold: float = 0.0) -> int:
    return sum(finite(row.get(key), float("-inf")) > threshold for row in rows)


def best_value(rows: Iterable[dict[str, Any]], key: str) -> float:
    return max((finite(row.get(key), float("-inf")) for row in rows), default=0.0)


def forward_candidates(
    history: list[dict[str, Any]], gates: dict[str, Any]
) -> list[dict[str, Any]]:
    by_policy: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for run in history:
        ts = parse_timestamp(run.get("generated_ts"))
        aggregate = run.get("aggregate_by_policy") or {}
        if not isinstance(aggregate, dict):
            continue
        for policy, metrics in aggregate.items():
            if isinstance(metrics, dict):
                by_policy.setdefault(str(policy), []).append((ts, metrics))

    output: list[dict[str, Any]] = []
    min_runs = integer(gates.get("min_shadow_runs"), 24)
    min_pair_fills = finite(gates.get("min_shadow_pair_fills"), 20.0)
    max_one_sided = finite(gates.get("max_one_sided_fill_rate"), 0.25)

    for policy, runs in sorted(by_policy.items()):
        pnls = [finite(metrics.get("conservative_pnl_ex_rewards_usd")) for _, metrics in runs]
        probes = sum(max(0, integer(metrics.get("probes"))) for _, metrics in runs)
        pair_fills = sum(
            max(0, integer(metrics.get("probes"))) * finite(metrics.get("pair_fill_rate"))
            for _, metrics in runs
        )
        one_sided = sum(
            max(0, integer(metrics.get("probes"))) * finite(metrics.get("one_sided_only_rate"))
            for _, metrics in runs
        )
        one_sided_rate = one_sided / probes if probes else 0.0
        pvalue = circular_block_bootstrap_pvalue(
            pnls,
            block=max(1, min(5, len(pnls))),
            reps=2000,
            seed=20260824 + sum(ord(char) for char in policy),
        )
        reasons: list[str] = []
        if len(runs) < min_runs:
            reasons.append(f"insufficient_shadow_runs:{len(runs)}<{min_runs}")
        if pair_fills < min_pair_fills:
            reasons.append(f"insufficient_pair_fills:{pair_fills:.6g}<{min_pair_fills:.6g}")
        if sum(pnls) <= 0.0:
            reasons.append("nonpositive_forward_pnl_ex_rewards")
        if one_sided_rate > max_one_sided:
            reasons.append(f"one_sided_fill_rate:{one_sided_rate:.6g}>{max_one_sided:.6g}")
        # The current forward probe deliberately does not claim a fully identified
        # 1.5x/2.0x cost surface. This blocks promotion but not continued research.
        reasons.append("missing_explicit_1.5x_and_2.0x_cost_stress")

        output.append(
            {
                "candidate_id": f"forward_maker:{policy}",
                "family": "execution_alpha",
                "specification": policy,
                "evidence_type": "forward_read_only_shadow",
                "observations": len(runs),
                "latest_evidence_ts": max((ts for ts, _ in runs), default=0),
                "metrics": {
                    "runs": len(runs),
                    "probes": probes,
                    "pair_fills_estimated": pair_fills,
                    "one_sided_fill_rate": one_sided_rate,
                    "total_pnl_ex_rewards_usd": sum(pnls),
                    "mean_pnl_ex_rewards_usd": statistics.fmean(pnls) if pnls else 0.0,
                },
                "raw_pvalue": pvalue,
                "gate_pass_before_fdr": False,
                "integration_evidence_pass": False,
                "reasons": reasons,
                "critical_failures": [
                    reason for reason in reasons if reason.startswith("one_sided_fill_rate")
                ],
            }
        )
    return output


def portfolio_candidate(live: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    walk = live.get("walk_forward") or {}
    oos = walk.get("oos") or {}
    stress15 = walk.get("oos_cost_stress") or {}
    trades = integer(oos.get("trades"))
    active_folds = integer(walk.get("active_folds"))
    positive_folds = integer(walk.get("positive_active_folds"))
    positive_fraction = positive_folds / active_folds if active_folds else 0.0
    pvalue = finite(walk.get("bootstrap_one_sided_pvalue"), 1.0)
    stress2 = cost_stressed_pnl(oos, 2.0)

    reasons: list[str] = []
    critical: list[str] = []
    min_trades = integer(gates.get("min_oos_trades"), 30)
    max_drawdown = finite(gates.get("max_drawdown"), 0.10)
    min_pf = finite(gates.get("min_profit_factor"), 1.10)
    max_p = finite(gates.get("max_bootstrap_pvalue"), 0.10)
    min_folds = integer(gates.get("min_active_folds"), 2)
    min_positive = finite(gates.get("min_positive_fold_fraction"), 0.50)

    if trades < min_trades:
        reasons.append(f"insufficient_oos_trades:{trades}<{min_trades}")
    if finite(oos.get("net_pnl")) <= 0.0:
        reasons.append("nonpositive_oos_net_pnl")
        critical.append("nonpositive_oos_net_pnl")
    if finite(stress15.get("net_pnl")) <= 0.0:
        reasons.append("nonpositive_1.5x_cost_stressed_pnl")
        critical.append("nonpositive_1.5x_cost_stressed_pnl")
    if stress2 <= 0.0:
        reasons.append("nonpositive_2.0x_cost_stressed_pnl")
        critical.append("nonpositive_2.0x_cost_stressed_pnl")
    if finite(oos.get("max_drawdown")) > max_drawdown:
        reasons.append("drawdown_gate")
        critical.append("drawdown_gate")
    if finite(oos.get("profit_factor")) < min_pf:
        reasons.append("profit_factor_gate")
    if pvalue > max_p:
        reasons.append("bootstrap_gate")
    if active_folds < min_folds:
        reasons.append(f"insufficient_active_folds:{active_folds}<{min_folds}")
    if active_folds and positive_fraction <= min_positive:
        reasons.append("fold_stability_gate")
    if walk.get("production_threshold") is None:
        reasons.append("no_frozen_production_threshold")

    incremental = walk.get("incremental_utility")
    compatible = walk.get("single_model_compatible") is True
    integration_reasons: list[str] = []
    if incremental is None:
        integration_reasons.append("missing_incremental_utility_vs_champion")
    elif finite(incremental) <= finite(gates.get("min_incremental_utility"), 0.0):
        integration_reasons.append("nonpositive_incremental_utility_vs_champion")
    if not compatible:
        integration_reasons.append("single_model_compatibility_not_certified")

    return {
        "candidate_id": "portfolio:unified_bundle_engine",
        "family": "unified_portfolio",
        "specification": "current research ledger under frozen threshold",
        "evidence_type": "purged_walk_forward_realized_paper",
        "observations": trades,
        "latest_evidence_ts": parse_timestamp(live.get("generated_ts")),
        "metrics": {
            "oos_trades": trades,
            "oos_net_pnl_usd": finite(oos.get("net_pnl")),
            "stressed_1_5x_net_pnl_usd": finite(stress15.get("net_pnl")),
            "stressed_2_0x_net_pnl_usd": stress2,
            "max_drawdown": finite(oos.get("max_drawdown")),
            "profit_factor": finite(oos.get("profit_factor")),
            "active_folds": active_folds,
            "positive_active_folds": positive_folds,
            "positive_fold_fraction": positive_fraction,
            "production_threshold": walk.get("production_threshold"),
            "incremental_utility": incremental,
            "single_model_compatible": compatible,
        },
        "raw_pvalue": pvalue,
        "gate_pass_before_fdr": not reasons,
        "integration_evidence_pass": not integration_reasons,
        "integration_reasons": integration_reasons,
        "reasons": reasons,
        "critical_failures": critical,
    }


def build_diagnostics(live: dict[str, Any], forward: dict[str, Any], now: int, config: dict[str, Any]) -> dict[str, Any]:
    telemetry = config.get("telemetry") or {}
    generated = parse_timestamp(live.get("generated_ts"))
    forward_ts = parse_timestamp(forward.get("generated_ts"))
    live_age = max(0, now - generated) if generated else None
    forward_age = max(0, now - forward_ts) if forward_ts else None

    candidates = live.get("candidates") or {}
    b1 = candidates.get("b1") if isinstance(candidates, dict) else []
    b2 = candidates.get("b2") if isinstance(candidates, dict) else []
    b3 = candidates.get("b3_rewards") if isinstance(candidates, dict) else []
    b1 = b1 if isinstance(b1, list) else []
    b2 = b2 if isinstance(b2, list) else []
    b3 = b3 if isinstance(b3, list) else []

    walk = live.get("walk_forward") or {}
    action = live.get("action_report") or {}
    external = (((action.get("candidate_funnel") or {}).get("external")) or {})

    return {
        "live_smoke_present": bool(live),
        "live_smoke_generated_ts": generated,
        "live_smoke_age_seconds": live_age,
        "live_smoke_fresh": bool(
            generated and live_age is not None
            and live_age <= integer(telemetry.get("max_live_smoke_age_seconds"), 10800)
        ),
        "forward_maker_present": bool(forward),
        "forward_maker_generated_ts": forward_ts,
        "forward_maker_age_seconds": forward_age,
        "forward_maker_fresh": bool(
            forward_ts and forward_age is not None
            and forward_age <= integer(telemetry.get("max_forward_age_seconds"), 10800)
        ),
        "b1": {
            "candidates": len(b1),
            "raw_positive": positive_count(b1, "raw_expected_edge"),
            "maker_positive": positive_count(b1, "maker_entry_net_edge"),
            "best_raw_edge": best_value(b1, "raw_expected_edge"),
            "best_maker_edge": best_value(b1, "maker_entry_net_edge"),
        },
        "b2": {
            "candidates": len(b2),
            "raw_positive": positive_count(b2, "raw_expected_edge"),
            "maker_positive": positive_count(b2, "maker_entry_net_edge"),
            "best_raw_edge": best_value(b2, "raw_expected_edge"),
            "best_maker_edge": best_value(b2, "maker_entry_net_edge"),
        },
        "b3_rewards": {
            "candidates": len(b3),
            "standalone_positive": positive_count(b3, "conservative_daily_score"),
            "best_daily_score": best_value(b3, "conservative_daily_score"),
        },
        "oos": {
            "input_trades": integer(walk.get("input_trades")),
            "selected_trades": integer((walk.get("oos") or {}).get("trades")),
            "eligible_for_tiny_pilot": walk.get("eligible_for_tiny_pilot") is True,
            "gate_failures": list(walk.get("gate_failures") or []),
        },
        "external": {
            "rows": integer(external.get("rows")),
            "fresh_rows": integer(external.get("fresh_rows")),
        },
    }


def next_experiments(diagnostics: dict[str, Any], forward_candidates_: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []

    def add(identifier: str, priority: int, hypothesis: str, evidence: str, metric: str, workflow: str) -> None:
        if any(item["experiment_id"] == identifier for item in queue):
            return
        queue.append(
            {
                "experiment_id": identifier,
                "priority": priority,
                "hypothesis": hypothesis,
                "triggering_evidence": evidence,
                "success_metric": metric,
                "owner_workflow": workflow,
            }
        )

    if not diagnostics.get("live_smoke_fresh"):
        add(
            "restore_fresh_research_ledger",
            1,
            "Fresh deterministic telemetry is a prerequisite for every alpha decision.",
            "live-smoke evidence is missing or stale",
            "fresh live-smoke snapshot with reconciled ledger and no runtime degradation",
            "v4-live-smoke.yml",
        )

    oos = diagnostics.get("oos") or {}
    if integer(oos.get("selected_trades")) == 0:
        add(
            "execution_fillability_frontier",
            2,
            "The current bottleneck is observed paired execution rather than a lower signal threshold.",
            "zero finalized OOS bundles",
            "future paired fills, net PnL ex rewards, and 60s/300s executable markouts",
            "forward-maker-research.yml",
        )

    b1 = diagnostics.get("b1") or {}
    if integer(b1.get("raw_positive")) > 0 and integer(b1.get("maker_positive")) == 0:
        add(
            "b1_execution_cost_surface",
            3,
            "Quote placement and horizon selection may recover a subset of B1 raw edge without lowering the production gate.",
            f"B1 raw-positive={b1.get('raw_positive')} maker-positive=0",
            "forward net edge and paired fill probability by quote policy and holding horizon",
            "forward-maker-research.yml",
        )

    b2 = diagnostics.get("b2") or {}
    if integer(b2.get("raw_positive")) > 0 and integer(b2.get("maker_positive")) == 0:
        add(
            "b2_clustered_dynamic_factor",
            4,
            "Event/semantic-cluster factors may preserve hedge coherence while reducing hedge cost and error.",
            f"B2 raw-positive={b2.get('raw_positive')} maker-positive=0",
            "purged OOS net edge after all hedge legs, stability, and factor-neutralization error",
            "alpha-factory.yml",
        )

    if forward_candidates_:
        worst_one_sided = max(
            finite((candidate.get("metrics") or {}).get("one_sided_fill_rate"))
            for candidate in forward_candidates_
        )
        if worst_one_sided > 0.25:
            add(
                "paired_quote_coordination",
                3,
                "Coordinated quote admission/cancellation can reduce naked one-sided inventory.",
                f"observed one-sided fill rate up to {worst_one_sided:.4f}",
                "lower one-sided-only rate with nonnegative 2x-cost-stressed matched PnL",
                "forward-maker-research.yml",
            )
        if max(integer(candidate.get("observations")) for candidate in forward_candidates_) < 24:
            add(
                "accumulate_forward_execution_evidence",
                5,
                "More independent forward windows are needed before inference or promotion.",
                "fewer than 24 forward runs per policy",
                "at least 24 runs and 20 paired fills per policy",
                "forward-maker-research.yml",
            )

    b3 = diagnostics.get("b3_rewards") or {}
    if integer(b3.get("candidates")) > 0 and integer(b3.get("standalone_positive")) == 0:
        add(
            "reward_share_and_payout_calibration",
            6,
            "Observed reward share and payout-floor frequency, not advertised pool size, determine B3 economics.",
            "no standalone payout-aware B3 opportunity",
            "realized payout share net of capital, adverse selection, and one-sided inventory",
            "forward-maker-research.yml",
        )

    external = diagnostics.get("external") or {}
    if integer(external.get("fresh_rows")) == 0:
        add(
            "external_terminal_information",
            7,
            "Fresh calibrated external information may add terminal alpha orthogonal to B1/B2 execution signals.",
            "no fresh positive-confidence external signal",
            "purged terminal Brier/log-loss improvement and incremental portfolio utility",
            "alpha-factory.yml",
        )

    return sorted(queue, key=lambda item: (item["priority"], item["experiment_id"]))[:7]


def finalize_candidates(
    candidates: list[dict[str, Any]], previous: dict[str, Any], config: dict[str, Any], now: int
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    gates = config.get("gates") or {}
    q = finite(gates.get("fdr_q"), 0.10)
    pvalues = {
        candidate["candidate_id"]: finite(candidate.get("raw_pvalue"), 1.0)
        for candidate in candidates
        if integer(candidate.get("observations")) > 0
    }
    fdr = benjamini_hochberg(pvalues, q)
    old_candidates = previous.get("candidates") or {}
    min_passes = integer(gates.get("min_consecutive_passes"), 3)

    completed: list[dict[str, Any]] = []
    for candidate in candidates:
        identifier = candidate["candidate_id"]
        result = fdr.get(
            identifier,
            {
                "raw_pvalue": finite(candidate.get("raw_pvalue"), 1.0),
                "adjusted_pvalue": 1.0,
                "rejected": False,
                "rank": None,
                "tests": len(fdr),
            },
        )
        candidate["fdr"] = result
        statistical_pass = bool(result.get("rejected"))
        gate_pass = bool(candidate.get("gate_pass_before_fdr")) and statistical_pass
        prior = old_candidates.get(identifier) if isinstance(old_candidates, dict) else {}
        prior = prior if isinstance(prior, dict) else {}
        consecutive = integer(prior.get("consecutive_passes")) + 1 if gate_pass else 0
        candidate["consecutive_passes"] = consecutive

        reasons = list(candidate.get("reasons") or [])
        if candidate.get("gate_pass_before_fdr") and not statistical_pass:
            reasons.append("fdr_gate")
        if gate_pass and consecutive < min_passes:
            reasons.append(f"consecutive_passes:{consecutive}<{min_passes}")

        if gate_pass and consecutive >= min_passes:
            if candidate.get("integration_evidence_pass"):
                decision = "integration_ready"
            else:
                decision = "paper_canary_ready"
                reasons.extend(candidate.get("integration_reasons") or [])
        elif candidate.get("critical_failures") and integer(candidate.get("observations")) > 0:
            decision = "reject_current_specification"
        else:
            decision = "continue_shadow"

        candidate["decision"] = decision
        candidate["reasons"] = sorted(set(str(reason) for reason in reasons))
        candidate["first_seen_ts"] = integer(prior.get("first_seen_ts"), now)
        candidate["last_seen_ts"] = now
        completed.append(candidate)

    ready = [
        candidate for candidate in completed
        if candidate["decision"] in {"integration_ready", "paper_canary_ready"}
    ]
    ready.sort(
        key=lambda candidate: (
            0 if candidate["decision"] == "integration_ready" else 1,
            finite((candidate.get("fdr") or {}).get("adjusted_pvalue"), 1.0),
            -finite((candidate.get("metrics") or {}).get("incremental_utility"), 0.0),
            candidate["candidate_id"],
        )
    )
    recommendation = ready[0]["candidate_id"] if ready else None

    previous_canary = previous.get("active_canary")
    rollback = {"recommended": False, "candidate_id": previous_canary, "reasons": []}
    if previous_canary:
        current = next(
            (candidate for candidate in completed if candidate["candidate_id"] == previous_canary),
            None,
        )
        if current is None:
            rollback = {
                "recommended": True,
                "candidate_id": previous_canary,
                "reasons": ["canary_evidence_missing"],
            }
        elif current.get("critical_failures"):
            rollback = {
                "recommended": True,
                "candidate_id": previous_canary,
                "reasons": list(current.get("critical_failures") or []),
            }

    registry = {
        "schema": STATE_SCHEMA,
        "updated_ts": now,
        "paper_only": True,
        "active_canary": previous_canary,
        "recommended_canary": recommendation,
        "champion": previous.get("champion"),
        "candidates": {candidate["candidate_id"]: candidate for candidate in completed},
        "rollback": rollback,
        "invariants": {
            "single_active_canary": previous_canary is None or isinstance(previous_canary, str),
            "direct_champion_mutation": False,
            "authenticated_execution": False,
        },
    }
    return completed, registry, rollback


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Polymarket Alpha Factory",
        "",
        f"- generated: `{report['generated_utc']}`",
        f"- status: **{report['status']}**",
        f"- champion version: `{(report.get('champion') or {}).get('version', 'unknown')}`",
        "- execution boundary: **paper/read-only only**",
        "- direct live promotion: **disabled**",
        "",
        "## Diagnostics",
        "",
        "```json",
        json.dumps(report.get("diagnostics") or {}, indent=2, sort_keys=True),
        "```",
        "",
        "## Candidate decisions",
        "",
    ]
    candidates = report.get("candidates") or []
    if not candidates:
        lines.append("- No candidate has sufficient evidence yet.")
    for candidate in candidates:
        adjusted = finite((candidate.get("fdr") or {}).get("adjusted_pvalue"), 1.0)
        lines.extend(
            [
                f"### `{candidate['candidate_id']}`",
                f"- decision: **{candidate['decision']}**",
                f"- observations: {candidate.get('observations', 0)}",
                f"- FDR-adjusted p-value: {adjusted:.6g}",
                f"- consecutive passes: {candidate.get('consecutive_passes', 0)}",
                "- reasons: " + (", ".join(candidate.get("reasons") or []) or "none"),
                "",
            ]
        )

    lines.extend(["## Next experiments", ""])
    experiments = report.get("next_experiments") or []
    if not experiments:
        lines.append("- No new experiment is justified by the current evidence.")
    for experiment in experiments:
        lines.extend(
            [
                f"### {experiment['priority']}. `{experiment['experiment_id']}`",
                f"- hypothesis: {experiment['hypothesis']}",
                f"- evidence: {experiment['triggering_evidence']}",
                f"- success metric: {experiment['success_metric']}",
                f"- owner: `{experiment['owner_workflow']}`",
                "",
            ]
        )

    rollback = report.get("rollback") or {}
    lines.extend(
        [
            "## Promotion and rollback",
            "",
            f"- recommended canary: `{report.get('recommended_canary') or 'none'}`",
            f"- rollback recommended: `{str(bool(rollback.get('recommended'))).lower()}`",
            "- actual champion changes require an approved `integration/*` pull request and the existing validation/deploy chain.",
            "",
        ]
    )
    return "\n".join(lines)


def build_report(
    config: dict[str, Any],
    champion: dict[str, Any],
    live: dict[str, Any],
    forward: dict[str, Any],
    history: list[dict[str, Any]],
    previous: dict[str, Any],
    now: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_config(config)
    gates = config.get("gates") or {}
    diagnostics = build_diagnostics(live, forward, now, config)

    candidates = [portfolio_candidate(live, gates)]
    candidates.extend(forward_candidates(history, gates))
    previous = dict(previous) if isinstance(previous, dict) else {}
    previous["champion"] = champion
    completed, registry, rollback = finalize_candidates(candidates, previous, config, now)
    experiments = next_experiments(diagnostics, completed)

    if not diagnostics.get("live_smoke_fresh"):
        status = "DEGRADED_STALE_EVIDENCE"
    elif rollback.get("recommended"):
        status = "ROLLBACK_RECOMMENDED"
    elif any(candidate["decision"] == "integration_ready" for candidate in completed):
        status = "INTEGRATION_RECOMMENDED"
    elif any(candidate["decision"] == "paper_canary_ready" for candidate in completed):
        status = "PAPER_CANARY_RECOMMENDED"
    else:
        status = "RESEARCHING"

    report = {
        "schema": SCHEMA,
        "generated_ts": now,
        "generated_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "status": status,
        "paper_only": True,
        "submitted_orders": 0,
        "authenticated_execution": False,
        "direct_champion_mutation": False,
        "champion": champion,
        "diagnostics": diagnostics,
        "candidates": completed,
        "recommended_canary": registry.get("recommended_canary"),
        "rollback": rollback,
        "next_experiments": experiments,
        "promotion_contract": {
            "research_to_live": "approved integration/* PR only",
            "required_post_merge_chain": [
                "ci.yml",
                "monitoring.yml",
                "v4-live-smoke.yml",
                "paper-validated",
                "deploy-paper-server.yml",
                "server-health.yml",
            ],
            "real_money_automation": False,
        },
    }
    return report, registry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/alpha_factory.json"))
    parser.add_argument("--champion", type=Path, default=Path("config/live_champion.json"))
    parser.add_argument("--live-smoke", type=Path, required=True)
    parser.add_argument("--forward-maker", type=Path, required=True)
    parser.add_argument("--forward-history", type=Path, required=True)
    parser.add_argument("--state-in", type=Path, required=True)
    parser.add_argument("--state-out", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()

    config = read_json(args.config, {})
    champion = read_json(args.champion, {})
    live = read_json(args.live_smoke, {})
    forward = read_json(args.forward_maker, {})
    history = read_jsonl(args.forward_history)
    previous = read_json(args.state_in, {})
    now = int(time.time()) if args.now is None else args.now

    report, registry = build_report(config, champion, live, forward, history, previous, now)
    atomic_json(args.output_json, report)
    atomic_json(args.state_out, registry)
    atomic_write(args.output_markdown, render_markdown(report) + "\n")
    print(
        "alpha_factory"
        f" status={report['status']}"
        f" candidates={len(report['candidates'])}"
        f" experiments={len(report['next_experiments'])}"
        f" recommended_canary={report.get('recommended_canary') or 'none'}"
        f" rollback={int(bool((report.get('rollback') or {}).get('recommended')))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
