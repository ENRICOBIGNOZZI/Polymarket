#!/usr/bin/env python3
"""V7 PAPER Alpha Factory.

The factory consumes canonical V7 live-paper execution evidence plus independent
forward-maker evidence. It ranks evidence gaps and challengers, but it never
mutates the champion, deploys, reallocates capital, or submits authenticated
orders. Promotion authority remains in the exact-source Promotion Controller.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def one_sided_positive_pvalue(values: list[float]) -> float:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if len(clean) < 2:
        return 1.0
    mean = statistics.fmean(clean)
    stdev = statistics.stdev(clean)
    if stdev <= 1e-12:
        return 0.0 if mean > 0 else 1.0
    z = mean / (stdev / math.sqrt(len(clean)))
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def benjamini_hochberg(pvalues: dict[str, float], q: float) -> dict[str, dict[str, Any]]:
    ordered = sorted(
        ((identifier, min(1.0, max(0.0, finite(value, 1.0)))) for identifier, value in pvalues.items()),
        key=lambda item: (item[1], item[0]),
    )
    tests = len(ordered)
    if tests == 0:
        return {}
    adjusted: dict[str, float] = {}
    running = 1.0
    for rank in range(tests, 0, -1):
        identifier, pvalue = ordered[rank - 1]
        running = min(running, pvalue * tests / rank)
        adjusted[identifier] = min(1.0, running)
    return {
        identifier: {
            "raw_pvalue": pvalue,
            "adjusted_pvalue": adjusted[identifier],
            "rejected": adjusted[identifier] <= q,
            "rank": rank,
            "tests": tests,
        }
        for rank, (identifier, pvalue) in enumerate(ordered, start=1)
    }


def execution_candidates(live: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = live.get("execution_evidence") if isinstance(live.get("execution_evidence"), dict) else {}
    models = evidence.get("models") if isinstance(evidence.get("models"), dict) else {}
    runtime = live.get("runtime") if isinstance(live.get("runtime"), dict) else {}
    generated = parse_timestamp(evidence.get("generated_ts") or live.get("generated_ts"))
    candidates: list[dict[str, Any]] = []
    for model, raw in sorted(models.items()):
        if not isinstance(raw, dict):
            continue
        pvalue_raw = raw.get("bootstrap_one_sided_pvalue")
        pvalue = finite(pvalue_raw, 1.0) if pvalue_raw is not None else 1.0
        observations = max(integer(raw.get("fills")), integer(raw.get("realized_pnl_observations")))
        reason_codes = [str(item) for item in raw.get("reason_codes") or []]
        candidates.append(
            {
                "candidate_id": f"v7_execution:{model}",
                "family": str(model),
                "specification": str(raw.get("target") or "canonical_v7_contract"),
                "evidence_type": "canonical_v7_execution_ledger",
                "observations": observations,
                "latest_evidence_ts": generated,
                "metrics": {
                    "fills": integer(raw.get("fills")),
                    "fill_rate": raw.get("fill_rate"),
                    "oos_net_pnl_usd": finite(raw.get("net_pnl")),
                    "stressed_1_5x_net_pnl_usd": finite(raw.get("stressed_net_pnl")),
                    "max_drawdown": finite(runtime.get("drawdown")),
                    "active_folds": integer(raw.get("active_folds")),
                    "positive_fold_fraction": raw.get("positive_fold_fraction"),
                    "markout_observations": integer(raw.get("forward_markout_observations")),
                    "mean_forward_markout": raw.get("mean_forward_markout"),
                    "terminal_calibration_observations": integer(raw.get("terminal_calibration_observations")),
                    "brier_improvement_over_market": raw.get("brier_improvement_over_market"),
                    "incremental_utility": None,
                    "single_model_compatible": True,
                },
                "raw_pvalue": pvalue,
                "gate_pass_before_fdr": raw.get("paper_eligible") is True,
                "integration_evidence_pass": False,
                "integration_reasons": [
                    "promotion_controller_exact_source_evidence_required",
                    "missing_incremental_utility_vs_current_v7",
                ],
                "reasons": reason_codes,
                "critical_failures": [],
            }
        )
    return candidates


def forward_candidates(forward: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    gates = config.get("gates") or {}
    min_runs = integer(gates.get("min_shadow_runs"), 24)
    min_pairs = integer(gates.get("min_shadow_pair_fills"), 20)
    max_one_sided = finite(gates.get("max_one_sided_fill_rate"), 0.25)
    rows = forward.get("policies") if isinstance(forward.get("policies"), dict) else {}
    generated = parse_timestamp(forward.get("generated_ts"))
    output: list[dict[str, Any]] = []
    for policy, history in sorted(rows.items()):
        history = history if isinstance(history, dict) else {}
        runs = integer(history.get("runs"))
        paired = integer(history.get("paired_fills"))
        one_sided = finite(history.get("one_sided_fill_rate"), 1.0)
        pnl = finite(history.get("total_pnl_ex_rewards_usd"))
        stress = finite(history.get("stressed_2x_pnl_ex_rewards_usd"), pnl)
        run_pnls = [finite(value) for value in (history.get("run_pnl_ex_rewards") or [])]
        pvalue = one_sided_positive_pvalue(run_pnls)
        reasons: list[str] = []
        if runs < min_runs:
            reasons.append(f"insufficient_forward_runs:{runs}<{min_runs}")
        if paired < min_pairs:
            reasons.append(f"insufficient_paired_fills:{paired}<{min_pairs}")
        if one_sided > max_one_sided:
            reasons.append("one_sided_fill_rate_gate")
        if pnl <= 0.0:
            reasons.append("nonpositive_net_pnl_ex_rewards")
        if stress <= 0.0:
            reasons.append("nonpositive_2x_cost_stressed_pnl")
        output.append(
            {
                "candidate_id": f"maker_forward:{policy}",
                "family": "micro_maker",
                "specification": str(policy),
                "evidence_type": "prospective_forward_execution",
                "observations": runs,
                "latest_evidence_ts": generated,
                "metrics": {
                    "runs": runs,
                    "paired_fills": paired,
                    "one_sided_fill_rate": one_sided,
                    "total_pnl_ex_rewards_usd": pnl,
                    "stressed_2_0x_net_pnl_usd": stress,
                    "incremental_utility": history.get("incremental_utility"),
                    "single_model_compatible": history.get("single_model_compatible") is True,
                },
                "raw_pvalue": pvalue,
                "gate_pass_before_fdr": not reasons,
                "integration_evidence_pass": False,
                "integration_reasons": [
                    "promotion_controller_exact_source_evidence_required",
                    "maker_forward_policy_is_research_only",
                ],
                "reasons": reasons,
                "critical_failures": [],
            }
        )
    return output


def build_diagnostics(live: dict[str, Any], forward: dict[str, Any], now: int, config: dict[str, Any]) -> dict[str, Any]:
    telemetry = config.get("telemetry") or {}
    generated = parse_timestamp(live.get("generated_ts"))
    forward_ts = parse_timestamp(forward.get("generated_ts"))
    live_age = max(0, now - generated) if generated else None
    forward_age = max(0, now - forward_ts) if forward_ts else None
    runtime = live.get("runtime") if isinstance(live.get("runtime"), dict) else {}
    evidence = live.get("execution_evidence") if isinstance(live.get("execution_evidence"), dict) else {}
    eligible = evidence.get("eligible_models") if isinstance(evidence.get("eligible_models"), list) else []
    insufficient = evidence.get("insufficient_models") if isinstance(evidence.get("insufficient_models"), list) else []
    models = evidence.get("models") if isinstance(evidence.get("models"), dict) else {}
    return {
        "live_smoke_present": bool(live),
        "live_smoke_generated_ts": generated,
        "live_smoke_age_seconds": live_age,
        "live_smoke_fresh": bool(generated and live_age is not None and live_age <= integer(telemetry.get("max_live_smoke_age_seconds"), 10800)),
        "runtime_v7_valid": bool(
            runtime.get("present") is True
            and integer(runtime.get("version")) == 7
            and runtime.get("paper_only") is True
            and runtime.get("authenticated_execution") is False
        ),
        "runtime_total_fills": integer(runtime.get("total_fills")),
        "runtime_pnl_usd": finite(runtime.get("pnl_usd")),
        "runtime_realized_pnl_usd": finite(runtime.get("realized_pnl_usd")),
        "runtime_drawdown": finite(runtime.get("drawdown")),
        "runtime_strategy_count": integer(runtime.get("strategy_count")),
        "execution_evidence_present": bool(evidence.get("present")),
        "execution_evidence_eligible_models": len(eligible),
        "execution_evidence_insufficient_models": len(insufficient),
        "execution_evidence_models": {
            name: {
                "paper_eligible": raw.get("paper_eligible") is True,
                "fills": integer(raw.get("fills")),
                "net_pnl": finite(raw.get("net_pnl")),
                "stressed_net_pnl": raw.get("stressed_net_pnl"),
                "reason_codes": list(raw.get("reason_codes") or []),
            }
            for name, raw in sorted(models.items()) if isinstance(raw, dict)
        },
        "forward_maker_present": bool(forward),
        "forward_maker_generated_ts": forward_ts,
        "forward_maker_age_seconds": forward_age,
        "forward_maker_fresh": bool(forward_ts and forward_age is not None and forward_age <= integer(telemetry.get("max_forward_age_seconds"), 10800)),
    }


def next_experiments(diagnostics: dict[str, Any], forward: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []

    def add(identifier: str, priority: int, hypothesis: str, evidence: str, metric: str, workflow: str) -> None:
        if any(row["experiment_id"] == identifier for row in queue):
            return
        queue.append({
            "experiment_id": identifier,
            "priority": priority,
            "hypothesis": hypothesis,
            "triggering_evidence": evidence,
            "success_metric": metric,
            "owner_workflow": workflow,
        })

    models = diagnostics.get("execution_evidence_models") if isinstance(diagnostics.get("execution_evidence_models"), dict) else {}
    maker = models.get("micro_maker") if isinstance(models.get("micro_maker"), dict) else {}
    if not maker.get("paper_eligible"):
        add(
            "maker_candidate_specific_fillability",
            1,
            "Candidate-specific queue/flow admission can improve fill-conditioned net PnL without chasing fill count.",
            ",".join(str(x) for x in maker.get("reason_codes") or []) or "maker execution evidence insufficient",
            "paired fills plus positive fill-conditioned PnL and adverse markout under fixed V7 economics",
            "forward-maker-research.yml",
        )

    relative = models.get("relative_value") if isinstance(models.get("relative_value"), dict) else {}
    if not relative.get("paper_eligible"):
        add(
            "graph_joint_completion_unwind",
            2,
            "Prospective joint completion and explicit partial-state unwind evidence can identify executable Graph/RV subsets.",
            ",".join(str(x) for x in relative.get("reason_codes") or []) or "relative-value execution evidence insufficient",
            "positive prospective joint-state PnL after partial-fill/unwind and cost stress",
            "arb-theory-hourly.yml",
        )

    hard = models.get("graph_hard") if isinstance(models.get("graph_hard"), dict) else {}
    if not hard.get("paper_eligible"):
        add(
            "hard_arb_freshness_recurrence",
            3,
            "Strict receive/exchange freshness with sequential revalidation should be measured over recurrent executable hard-arb opportunities.",
            ",".join(str(x) for x in hard.get("reason_codes") or []) or "hard-arb execution evidence insufficient",
            "recurrent positive net PnL with verified depth, fees, skew, legging and unwind accounting",
            "fast-arb-hourly.yml",
        )

    external = models.get("external") if isinstance(models.get("external"), dict) else {}
    if not external.get("paper_eligible"):
        add(
            "external_probability_mapping",
            4,
            "External features require a calibrated terminal-probability mapping with chronological scoring before they can enter V7 execution.",
            ",".join(str(x) for x in external.get("reason_codes") or []) or "external execution evidence insufficient",
            "chronological Brier/log-loss improvement plus executable incremental utility",
            "external-intelligence.yml",
        )

    if forward:
        max_one_sided = max(finite((row.get("metrics") or {}).get("one_sided_fill_rate"), 0.0) for row in forward)
        min_runs = min(integer(row.get("observations")) for row in forward)
        if max_one_sided > 0.25:
            add(
                "maker_paired_quote_coordination",
                1,
                "Coordinated quote admission/cancellation can reduce naked one-sided inventory.",
                f"one-sided fill rate observed up to {max_one_sided:.4f}",
                "lower one-sided rate with nonnegative stressed matched PnL",
                "forward-maker-research.yml",
            )
        if min_runs < 24:
            add(
                "maker_accumulate_forward_windows",
                5,
                "More independent prospective maker windows are required before stable inference.",
                f"minimum observed forward runs={min_runs}",
                "at least 24 independent forward windows and adequate paired fills per policy",
                "forward-maker-research.yml",
            )

    return sorted(queue, key=lambda row: (row["priority"], row["experiment_id"]))[:8]


def finalize_candidates(
    candidates: list[dict[str, Any]], previous: dict[str, Any], config: dict[str, Any], now: int
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    gates = config.get("gates") or {}
    q = finite(gates.get("fdr_q"), 0.10)
    pvalues = {
        row["candidate_id"]: finite(row.get("raw_pvalue"), 1.0)
        for row in candidates
        if integer(row.get("observations")) > 0
    }
    fdr = benjamini_hochberg(pvalues, q)
    old_candidates = previous.get("candidates") if isinstance(previous.get("candidates"), dict) else {}
    min_passes = max(1, integer(gates.get("min_consecutive_passes"), 3))
    completed: list[dict[str, Any]] = []
    for candidate in candidates:
        identifier = candidate["candidate_id"]
        fdr_result = fdr.get(identifier, {
            "raw_pvalue": finite(candidate.get("raw_pvalue"), 1.0),
            "adjusted_pvalue": 1.0,
            "rejected": False,
            "rank": None,
            "tests": len(fdr),
        })
        candidate["fdr"] = fdr_result
        statistical_pass = bool(fdr_result.get("rejected"))
        gate_pass = bool(candidate.get("gate_pass_before_fdr")) and statistical_pass
        prior = old_candidates.get(identifier) if isinstance(old_candidates, dict) else {}
        prior = prior if isinstance(prior, dict) else {}
        consecutive = integer(prior.get("consecutive_passes")) + 1 if gate_pass else 0
        candidate["consecutive_passes"] = consecutive
        reasons = list(candidate.get("reasons") or [])
        if integer(candidate.get("observations")) > 0 and not statistical_pass:
            reasons.append("fdr_gate")
        if gate_pass and consecutive < min_passes:
            reasons.append(f"consecutive_passes:{consecutive}<{min_passes}")
        if gate_pass and consecutive >= min_passes and candidate.get("integration_evidence_pass"):
            decision = "integration_ready"
        else:
            decision = "continue_shadow"
            if gate_pass and consecutive >= min_passes:
                reasons.extend(candidate.get("integration_reasons") or [])
        candidate["decision"] = decision
        candidate["reasons"] = sorted(set(str(reason) for reason in reasons))
        candidate["first_seen_ts"] = integer(prior.get("first_seen_ts"), now)
        candidate["last_seen_ts"] = now
        completed.append(candidate)

    ready = [row for row in completed if row.get("decision") == "integration_ready"]
    ready.sort(key=lambda row: (finite((row.get("fdr") or {}).get("adjusted_pvalue"), 1.0), row["candidate_id"]))
    recommendation = ready[0]["candidate_id"] if ready else None
    previous_canary = previous.get("active_canary")
    rollback = {"recommended": False, "candidate_id": previous_canary, "reasons": []}
    registry = {
        "schema": STATE_SCHEMA,
        "updated_ts": now,
        "paper_only": True,
        "active_canary": previous_canary,
        "recommended_canary": recommendation,
        "champion": previous.get("champion"),
        "candidates": {row["candidate_id"]: row for row in completed},
        "rollback": rollback,
        "invariants": {
            "single_active_canary": previous_canary is None or isinstance(previous_canary, str),
            "direct_champion_mutation": False,
            "authenticated_execution": False,
            "promotion_controller_remains_authoritative": True,
        },
    }
    return completed, registry, rollback


def build_report(
    live: dict[str, Any],
    forward: dict[str, Any],
    previous: dict[str, Any],
    config: dict[str, Any],
    now: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if config.get("paper_only") is not True:
        raise ValueError("alpha factory must remain PAPER-only")
    if config.get("allow_authenticated_execution") is not False:
        raise ValueError("authenticated execution must remain disabled")
    if config.get("allow_direct_champion_mutation") is not False:
        raise ValueError("direct champion mutation must remain disabled")

    diagnostics = build_diagnostics(live, forward, now, config)
    candidates = execution_candidates(live)
    maker_candidates = forward_candidates(forward, config)
    candidates.extend(maker_candidates)
    completed, registry, rollback = finalize_candidates(candidates, previous, config, now)
    experiments = next_experiments(diagnostics, maker_candidates)
    ready = [row for row in completed if row.get("decision") == "integration_ready"]
    status = "RESEARCHING"
    if not diagnostics.get("live_smoke_fresh") or not diagnostics.get("runtime_v7_valid"):
        status = "DEGRADED_EVIDENCE"
    elif ready:
        status = "INTEGRATION_READY"
    report = {
        "schema": SCHEMA,
        "generated_ts": now,
        "generated_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "status": status,
        "paper_only": True,
        "direct_champion_mutation": False,
        "authenticated_execution": False,
        "submitted_orders": 0,
        "diagnostics": diagnostics,
        "candidates": completed,
        "recommended_canary": registry.get("recommended_canary"),
        "rollback": rollback,
        "next_experiments": experiments,
        "invariants": {
            "canonical_v7_execution_evidence_only": True,
            "retired_b1_b2_b3_pipeline": True,
            "promotion_controller_remains_authoritative": True,
            "direct_champion_mutation": False,
            "authenticated_execution": False,
        },
    }
    return report, registry


def render_markdown(report: dict[str, Any]) -> str:
    diagnostics = report.get("diagnostics") or {}
    lines = [
        "# Polymarket V7 Alpha Factory",
        "",
        f"- generated: `{report.get('generated_utc')}`",
        f"- status: **{report.get('status')}**",
        f"- V7 runtime valid: `{str(bool(diagnostics.get('runtime_v7_valid'))).lower()}`",
        f"- runtime fills: `{integer(diagnostics.get('runtime_total_fills'))}`",
        f"- execution-evidence eligible models: `{integer(diagnostics.get('execution_evidence_eligible_models'))}`",
        "- direct champion mutation: `false`",
        "- authenticated execution: `false`",
        "",
        "## Candidates",
        "",
        "| Candidate | Evidence | Obs | Decision | FDR p | Reasons |",
        "|---|---|---:|---|---:|---|",
    ]
    for row in report.get("candidates") or []:
        fdr = row.get("fdr") or {}
        reasons = ", ".join(row.get("reasons") or []) or "none"
        lines.append(
            f"| `{row.get('candidate_id')}` | {row.get('evidence_type')} | {integer(row.get('observations'))} | "
            f"`{row.get('decision')}` | {finite(fdr.get('adjusted_pvalue'), 1.0):.4g} | {reasons} |"
        )
    lines.extend(["", "## Next V7 experiments", ""])
    for row in report.get("next_experiments") or []:
        lines.append(f"- `{row.get('experiment_id')}` → `{row.get('owner_workflow')}`: {row.get('hypothesis')}")
    if not report.get("next_experiments"):
        lines.append("- none")
    lines.extend([
        "",
        "## Boundary",
        "",
        "Alpha Factory produces research evidence only. Exact-source promotion remains the Promotion Controller's responsibility.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/alpha_factory.json"))
    parser.add_argument("--live-smoke", type=Path, required=True)
    parser.add_argument("--forward-maker", type=Path, required=True)
    parser.add_argument("--forward-history", type=Path)
    parser.add_argument("--state-in", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    parser.add_argument("--state-out", type=Path, required=True)
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()
    config = read_json(args.config, {})
    live = read_json(args.live_smoke, {})
    forward = read_json(args.forward_maker, {})
    if args.forward_history and args.forward_history.exists():
        history = read_json(args.forward_history, {})
        if isinstance(history, dict) and isinstance(history.get("policies"), dict):
            forward = history
    previous = read_json(args.state_in, {}) if args.state_in else {}
    now = int(time.time()) if args.now is None else args.now
    report, state = build_report(live, forward, previous, config, now)
    atomic_json(args.output_json, report)
    atomic_write(args.output_markdown, render_markdown(report))
    atomic_json(args.state_out, state)
    print(json.dumps({
        "status": report["status"],
        "candidates": len(report["candidates"]),
        "ready": sum(row.get("decision") == "integration_ready" for row in report["candidates"]),
        "experiments": len(report["next_experiments"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
