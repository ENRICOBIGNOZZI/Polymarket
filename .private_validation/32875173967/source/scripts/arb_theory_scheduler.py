#!/usr/bin/env python3
"""Deterministic hourly research gate for the fast-arbitrage sleeve.

The scheduler never edits the live champion and never submits orders. It aggregates
shadow evidence, stress-tests cost assumptions, derives a conservative candidate
policy, and materializes that candidate as JSON, Markdown, and a compilable C++
header for a research-only pull request.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


HARD_KINDS = {
    "BINARY_COMPLETE_SET",
    "NEGRISK_COMPLETE_SET",
    "NEGRISK_NO_CONVERSION",
    "LOGICAL_IMPLICATION",
    "LOGICAL_MUTUAL_EXCLUSION",
    "LOGICAL_EXHAUSTIVE_PAIR",
}
MIN_EDGE_FLOOR = 0.0005


@dataclass
class Observation:
    run: str
    timestamp_ms: int
    kind: str
    opportunity_id: str
    hard: bool
    executable: bool
    edge: float
    profit: float
    shares: float
    capital: float


@dataclass
class Evidence:
    observations: list[Observation] = field(default_factory=list)
    feed_latency_ms: list[float] = field(default_factory=list)
    decision_latency_us: list[float] = field(default_factory=list)
    ws_messages: int = 0
    book_updates: int = 0
    files: list[Path] = field(default_factory=list)
    runs: set[str] = field(default_factory=set)


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    low = math.floor(index)
    high = math.ceil(index)
    weight = index - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def discover(roots: Iterable[Path], name: str) -> list[Path]:
    found: set[Path] = set()
    for root in roots:
        if root.is_file() and root.name == name:
            found.add(root.resolve())
        elif root.exists():
            found.update(path.resolve() for path in root.rglob(name) if path.is_file())
    return sorted(found)


def run_name(path: Path) -> str:
    return str(path.parent.resolve())


def load_evidence(roots: Sequence[Path]) -> Evidence:
    evidence = Evidence()
    for path in discover(roots, "fast_arb_opportunities.csv"):
        evidence.files.append(path)
        run = run_name(path)
        evidence.runs.add(run)
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                kind = (row.get("kind") or "").strip()
                hard = safe_int(row.get("hard_arbitrage")) == 1 or kind in HARD_KINDS
                evidence.observations.append(
                    Observation(
                        run=run,
                        timestamp_ms=safe_int(row.get("observed_ts_ms")),
                        kind=kind,
                        opportunity_id=(row.get("id") or "").strip(),
                        hard=hard,
                        executable=safe_int(row.get("executable")) == 1,
                        edge=safe_float(row.get("net_edge_per_share"), -1.0),
                        profit=safe_float(row.get("expected_profit"), 0.0),
                        shares=safe_float(row.get("executable_shares"), 0.0),
                        capital=safe_float(row.get("capital_required"), 0.0),
                    )
                )

    for path in discover(roots, "fast_arb_latency.csv"):
        evidence.files.append(path)
        evidence.runs.add(run_name(path))
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                feed = safe_float(row.get("feed_latency_ms"), -1.0)
                decision = safe_float(row.get("decision_latency_us"), -1.0)
                if feed >= 0.0:
                    evidence.feed_latency_ms.append(feed)
                if decision >= 0.0:
                    evidence.decision_latency_us.append(decision)

    for path in discover(roots, "fast_arb_status.json"):
        evidence.files.append(path)
        evidence.runs.add(run_name(path))
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        evidence.ws_messages += safe_int(status.get("ws_messages"))
        evidence.book_updates += safe_int(status.get("book_updates"))
        # Status quantiles remain useful when the sampled latency CSV is absent.
        if not evidence.feed_latency_ms:
            for key in ("feed_latency_p50_ms", "feed_latency_p95_ms", "feed_latency_p99_ms"):
                value = safe_float(status.get(key), -1.0)
                if value >= 0.0:
                    evidence.feed_latency_ms.append(value)
        if not evidence.decision_latency_us:
            for key in (
                "decision_latency_p50_us",
                "decision_latency_p95_us",
                "decision_latency_p99_us",
            ):
                value = safe_float(status.get(key), -1.0)
                if value >= 0.0:
                    evidence.decision_latency_us.append(value)
    evidence.files = sorted(set(evidence.files))
    return evidence


def opportunity_lifetimes(observations: Sequence[Observation]) -> list[float]:
    """Estimate completed executable episodes from state-change logs.

    Open/censored episodes are deliberately excluded from the promotion gate.
    """
    grouped: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for observation in observations:
        if observation.opportunity_id:
            grouped[(observation.run, observation.opportunity_id)].append(observation)
    lifetimes: list[float] = []
    for rows in grouped.values():
        rows.sort(key=lambda row: row.timestamp_ms)
        started: int | None = None
        for row in rows:
            if row.executable and started is None:
                started = row.timestamp_ms
            elif not row.executable and started is not None:
                if row.timestamp_ms >= started:
                    lifetimes.append(float(row.timestamp_ms - started))
                started = None
    return lifetimes


def bootstrap_run_mean_lower_bound(
    executable: Sequence[Observation], *, samples: int = 2000, seed: int = 20260824
) -> float:
    by_run: dict[str, float] = defaultdict(float)
    for row in executable:
        by_run[row.run] += row.profit
    values = list(by_run.values())
    if len(values) < 2:
        return min(values, default=0.0)
    randomizer = random.Random(seed)
    means: list[float] = []
    for _ in range(samples):
        sample = [randomizer.choice(values) for _ in values]
        means.append(statistics.fmean(sample))
    return percentile(means, 0.05)


def evidence_hash(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()


def load_base_policy(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("real_order_submission") is not False:
        raise ValueError("base policy must explicitly disable real order submission")
    return data


def analyze(evidence: Evidence, base_policy: dict[str, object], stress_bps: float) -> dict[str, object]:
    executable = [row for row in evidence.observations if row.executable and row.edge > 0.0]
    hard_executable = [row for row in executable if row.hard]
    nonhard_executable = [row for row in executable if not row.hard]
    stress = max(0.0, stress_bps) / 10000.0
    stressed_positive = [row for row in executable if row.edge - stress > 0.0]
    hard_stressed_positive = [row for row in hard_executable if row.edge - stress > 0.0]
    lifetimes = opportunity_lifetimes(evidence.observations)

    feed_p99 = percentile(evidence.feed_latency_ms, 0.99)
    decision_p99 = percentile(evidence.decision_latency_us, 0.99)
    lifetime_p10 = percentile(lifetimes, 0.10)
    end_to_end_p99 = feed_p99 + decision_p99 / 1000.0
    stressed_share = len(stressed_positive) / len(executable) if executable else 0.0
    hard_stressed_share = (
        len(hard_stressed_positive) / len(hard_executable) if hard_executable else 0.0
    )
    bootstrap_lower = bootstrap_run_mean_lower_bound(executable)

    research_ready = (
        len(evidence.runs) >= 3
        and evidence.ws_messages >= 1_000
        and len(executable) >= 10
        and stressed_share >= 0.50
        and decision_p99 <= 10_000.0
    )
    promotion_ready = (
        len(evidence.runs) >= 24
        and evidence.ws_messages >= 100_000
        and len(hard_executable) >= 50
        and hard_stressed_share >= 0.80
        and bootstrap_lower > 0.0
        and decision_p99 <= 5_000.0
        and len(lifetimes) >= 20
        and lifetime_p10 > end_to_end_p99 + 100.0
    )

    executable_edges = [row.edge for row in executable]
    base_edge = max(MIN_EDGE_FLOOR, safe_float(base_policy.get("min_net_edge"), MIN_EDGE_FLOOR))
    candidate_edge = base_edge
    if executable_edges:
        # Candidate thresholds can only become more conservative automatically.
        candidate_edge = max(base_edge, percentile(executable_edges, 0.10) - stress)
        candidate_edge = min(candidate_edge, percentile(executable_edges, 0.50))
        candidate_edge = round(max(MIN_EDGE_FLOOR, candidate_edge), 7)

    candidate_policy = dict(base_policy)
    candidate_policy.update(
        {
            "schema_version": 1,
            "mode": "shadow_candidate",
            "real_order_submission": False,
            "min_net_edge": candidate_edge,
            "derived_from_evidence": evidence_hash(evidence.files),
            "research_ready": research_ready,
            "promotion_ready": promotion_ready,
        }
    )

    by_kind: dict[str, dict[str, object]] = {}
    kinds = sorted({row.kind for row in evidence.observations})
    for kind in kinds:
        rows = [row for row in evidence.observations if row.kind == kind]
        positive = [row for row in rows if row.executable and row.edge > 0.0]
        by_kind[kind] = {
            "observations": len(rows),
            "executable": len(positive),
            "hard": kind in HARD_KINDS,
            "edge_p50": percentile([row.edge for row in positive], 0.50),
            "edge_p90": percentile([row.edge for row in positive], 0.90),
            "profit_sum": sum(row.profit for row in positive),
            "stressed_positive_share": (
                sum(row.edge - stress > 0.0 for row in positive) / len(positive)
                if positive
                else 0.0
            ),
        }

    return {
        "schema_version": 1,
        "mode": "research_only",
        "real_order_submission": False,
        "evidence_sha256": evidence_hash(evidence.files),
        "runs": len(evidence.runs),
        "files": len(evidence.files),
        "ws_messages": evidence.ws_messages,
        "book_updates": evidence.book_updates,
        "observations": len(evidence.observations),
        "executable_observations": len(executable),
        "hard_executable_observations": len(hard_executable),
        "nonhard_executable_observations": len(nonhard_executable),
        "stress_bps": stress_bps,
        "stressed_positive_share": stressed_share,
        "hard_stressed_positive_share": hard_stressed_share,
        "bootstrap_run_profit_mean_lower_95": bootstrap_lower,
        "feed_latency_p50_ms": percentile(evidence.feed_latency_ms, 0.50),
        "feed_latency_p95_ms": percentile(evidence.feed_latency_ms, 0.95),
        "feed_latency_p99_ms": feed_p99,
        "decision_latency_p50_us": percentile(evidence.decision_latency_us, 0.50),
        "decision_latency_p95_us": percentile(evidence.decision_latency_us, 0.95),
        "decision_latency_p99_us": decision_p99,
        "completed_lifetime_samples": len(lifetimes),
        "opportunity_lifetime_p10_ms": lifetime_p10,
        "end_to_end_latency_p99_ms": end_to_end_p99,
        "research_ready": research_ready,
        "promotion_ready": promotion_ready,
        "candidate_policy": candidate_policy,
        "by_kind": by_kind,
        "gate_reasons": {
            "research": {
                "runs_at_least_3": len(evidence.runs) >= 3,
                "ws_messages_at_least_1000": evidence.ws_messages >= 1_000,
                "executable_at_least_10": len(executable) >= 10,
                "stress_survival_at_least_50pct": stressed_share >= 0.50,
                "decision_p99_at_most_10ms": decision_p99 <= 10_000.0,
            },
            "promotion": {
                "runs_at_least_24": len(evidence.runs) >= 24,
                "ws_messages_at_least_100000": evidence.ws_messages >= 100_000,
                "hard_executable_at_least_50": len(hard_executable) >= 50,
                "hard_stress_survival_at_least_80pct": hard_stressed_share >= 0.80,
                "bootstrap_lower_profit_positive": bootstrap_lower > 0.0,
                "decision_p99_at_most_5ms": decision_p99 <= 5_000.0,
                "completed_lifetimes_at_least_20": len(lifetimes) >= 20,
                "lifetime_p10_exceeds_latency_p99_plus_100ms": (
                    lifetime_p10 > end_to_end_p99 + 100.0
                ),
            },
        },
    }


def render_markdown(report: dict[str, object]) -> str:
    by_kind = report["by_kind"]
    lines = [
        "# Hourly fast-arbitrage theory report",
        "",
        "This report is generated from shadow/public data only. It cannot submit orders or alter the live champion.",
        "",
        "## Structural identities under test",
        "",
        "- Binary complete set: `YES + NO = 1`.",
        "- Complete non-augmented NegRisk event: `sum_k YES_k = 1`.",
        "- NegRisk conversion: `NO_i -> {YES_j : j != i}`, after measured conversion cost.",
        "- Implication `A subset B`: `NO_A + YES_B >= 1`.",
        "- Mutual exclusion: `NO_A + NO_B >= 1`.",
        "- Exhaustive pair: `YES_A + YES_B >= 1`.",
        "- External and maker signals are relative-value hypotheses, not hard arbitrage.",
        "",
        "## Evidence",
        "",
        f"- runs: {report['runs']}",
        f"- WebSocket messages: {report['ws_messages']}",
        f"- book updates: {report['book_updates']}",
        f"- state-change observations: {report['observations']}",
        f"- executable observations: {report['executable_observations']}",
        f"- hard executable observations: {report['hard_executable_observations']}",
        f"- additional cost stress: {report['stress_bps']} bps",
        f"- all-sleeve stress survival: {report['stressed_positive_share']:.2%}",
        f"- hard-arbitrage stress survival: {report['hard_stressed_positive_share']:.2%}",
        f"- bootstrap lower 95% bound of run-level paper opportunity profit: {report['bootstrap_run_profit_mean_lower_95']:.8f}",
        "",
        "## Latency and opportunity horizon",
        "",
        f"- feed latency p50/p95/p99: {report['feed_latency_p50_ms']:.3f} / {report['feed_latency_p95_ms']:.3f} / {report['feed_latency_p99_ms']:.3f} ms",
        f"- decision latency p50/p95/p99: {report['decision_latency_p50_us']:.3f} / {report['decision_latency_p95_us']:.3f} / {report['decision_latency_p99_us']:.3f} us",
        f"- completed opportunity lifetimes: {report['completed_lifetime_samples']}",
        f"- opportunity lifetime p10: {report['opportunity_lifetime_p10_ms']:.3f} ms",
        "",
        "## Strategy breakdown",
        "",
        "| kind | hard | observations | executable | edge p50 | edge p90 | stressed survival | paper opportunity profit |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for kind, metrics in sorted(by_kind.items()):
        lines.append(
            f"| {kind} | {int(bool(metrics['hard']))} | {metrics['observations']} | "
            f"{metrics['executable']} | {metrics['edge_p50']:.8f} | "
            f"{metrics['edge_p90']:.8f} | {metrics['stressed_positive_share']:.2%} | "
            f"{metrics['profit_sum']:.8f} |"
        )
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- research candidate ready: **{str(report['research_ready']).lower()}**",
            f"- eligible for an integration review: **{str(report['promotion_ready']).lower()}**",
            f"- candidate minimum net edge: `{report['candidate_policy']['min_net_edge']}`",
            "",
            "Promotion still requires the model-governance workflow, a non-draft `integration/*` PR, all CI/live-smoke checks, and post-merge paper validation.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_header(report: dict[str, object]) -> str:
    policy = report["candidate_policy"]
    evidence = report["evidence_sha256"]
    return f'''#pragma once

// Generated by scripts/arb_theory_scheduler.py from shadow evidence.
// Research-only: this header cannot activate authenticated order submission.
namespace pm::fast::generated {{
inline constexpr bool kResearchReady = {str(report['research_ready']).lower()};
inline constexpr bool kPromotionReady = {str(report['promotion_ready']).lower()};
inline constexpr bool kRealOrderSubmission = false;
inline constexpr double kMinNetEdge = {safe_float(policy.get('min_net_edge')):.10f};
inline constexpr double kSlippageBps = {safe_float(policy.get('slippage_bps')):.10f};
inline constexpr double kLatencyPenaltyBps = {safe_float(policy.get('latency_penalty_bps')):.10f};
inline constexpr double kMaxNotionalUsd = {safe_float(policy.get('max_notional_usd')):.10f};
inline constexpr char kEvidenceSha256[] = "{evidence}";
}} // namespace pm::fast::generated
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", action="append", default=[])
    parser.add_argument("--base-policy", required=True)
    parser.add_argument("--stress-bps", type=float, default=10.0)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown", required=True)
    parser.add_argument("--output-header", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    roots = [Path(value) for value in args.evidence_root] or [Path(".")]
    base_policy = load_base_policy(Path(args.base_policy))
    report = analyze(load_evidence(roots), base_policy, args.stress_bps)
    outputs = {
        Path(args.output_json): json.dumps(report, indent=2, sort_keys=True) + "\n",
        Path(args.output_markdown): render_markdown(report),
        Path(args.output_header): render_header(report),
    }
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    print(json.dumps({
        "runs": report["runs"],
        "research_ready": report["research_ready"],
        "promotion_ready": report["promotion_ready"],
        "candidate_min_net_edge": report["candidate_policy"]["min_net_edge"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
