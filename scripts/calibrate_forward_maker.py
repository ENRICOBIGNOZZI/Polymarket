#!/usr/bin/env python3
"""Calibrate passive quote policies from repeated forward shadow experiments.

The persistent history contains one compact record per forward session. Raw probe
artifacts remain attached to the workflow run; only sufficient statistics needed
for policy inference are carried across runs. Inference resamples complete,
chronologically adjacent sessions so probes from one run are never treated as
independent and short-lived serial regimes are not ignored.

Promotion is deliberately based on ex-reward PnL. Estimated liquidity rewards
and unverified maker rebates are reported but cannot make a policy eligible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def optional_finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def nonnegative_int(value: Any) -> int:
    return max(0, int(finite(value, 0.0)))


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    p = min(1.0, max(0.0, probability))
    if len(ordered) == 1:
        return ordered[0]
    position = p * (len(ordered) - 1)
    lo = int(math.floor(position))
    hi = int(math.ceil(position))
    if lo == hi:
        return ordered[lo]
    weight = position - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def wilson_interval(
    successes: int,
    trials: int,
    z: float = 1.6448536269514722,
) -> tuple[float, float]:
    """Central 90% Wilson interval, equivalent to one-sided 95% bounds."""
    if trials <= 0:
        return 0.0, 1.0
    n = float(trials)
    p = min(1.0, max(0.0, successes / n))
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    radius = z * math.sqrt(
        max(0.0, p * (1.0 - p) / n + z2 / (4.0 * n * n))
    ) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)


@dataclass(frozen=True)
class GateConfig:
    min_sessions: int = 12
    min_history_span_seconds: int = 18_000
    min_probes: int = 180
    min_any_fills: int = 20
    min_pair_fills: int = 10
    min_positive_active_session_rate: float = 0.55
    max_one_sided_given_fill_upper: float = 0.40
    bootstrap_reps: int = 2000
    bootstrap_alpha: float = 0.05
    bootstrap_block_sessions: int = 4
    seed: int = 20260824


@dataclass(frozen=True)
class SessionPolicy:
    session_id: str
    generated_ts: int
    policy: str
    probes: int
    any_fills: int
    pair_fills: int
    one_sided: int
    pnl_ex_rewards: float
    pnl_with_conditional_rewards: float
    conditional_rewards: float
    matched_shares: float
    maker_rebate_fee_basis: float
    markout_60_weighted_sum: float
    markout_60_weight: float
    markout_300_weighted_sum: float
    markout_300_weight: float


def supported_session(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return isinstance(value.get("results"), list) or isinstance(
        value.get("policy_summaries"), list
    )


def load_history(path: Path) -> tuple[list[dict[str, Any]], int]:
    sessions: list[dict[str, Any]] = []
    malformed = 0
    if not path.exists():
        return sessions, malformed
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not supported_session(value):
                malformed += 1
                continue
            sessions.append(value)
    return sessions, malformed


def session_identifier(session: dict[str, Any], index: int) -> tuple[str, int]:
    generated_ts = int(finite(session.get("generated_ts"), 0.0))
    run_id = str(session.get("github_run_id") or "").strip()
    sha = str(session.get("git_sha") or "").strip()
    if run_id:
        identifier = f"run:{run_id}"
    elif generated_ts > 0 or sha:
        identifier = f"timestamp-sha:{generated_ts}:{sha}"
    else:
        identifier = f"session:{index}"
    return identifier, generated_ts


def deduplicate_sessions(
    raw_sessions: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Drop workflow retries so reruns cannot inflate the evidence base."""
    unique: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    duplicates = 0
    for index, session in enumerate(raw_sessions):
        identifier, _ = session_identifier(session, index)
        if identifier.startswith("session:"):
            anonymous.append(session)
            continue
        if identifier in unique:
            duplicates += 1
        unique[identifier] = session
    return [*unique.values(), *anonymous], duplicates


def session_policy_from_compact(
    value: dict[str, Any],
    session_id: str,
    generated_ts: int,
) -> SessionPolicy | None:
    policy = str(value.get("policy") or "").strip()
    if not policy:
        return None
    return SessionPolicy(
        session_id=session_id,
        generated_ts=generated_ts,
        policy=policy,
        probes=nonnegative_int(value.get("probes")),
        any_fills=nonnegative_int(value.get("any_fills")),
        pair_fills=nonnegative_int(value.get("pair_fills")),
        one_sided=nonnegative_int(value.get("one_sided")),
        pnl_ex_rewards=finite(value.get("pnl_ex_rewards")),
        pnl_with_conditional_rewards=finite(value.get("pnl_with_conditional_rewards")),
        conditional_rewards=finite(value.get("conditional_rewards")),
        matched_shares=max(0.0, finite(value.get("matched_shares"))),
        maker_rebate_fee_basis=max(
            0.0, finite(value.get("maker_rebate_fee_basis"))
        ),
        markout_60_weighted_sum=finite(value.get("markout_60_weighted_sum")),
        markout_60_weight=max(0.0, finite(value.get("markout_60_weight"))),
        markout_300_weighted_sum=finite(value.get("markout_300_weighted_sum")),
        markout_300_weight=max(0.0, finite(value.get("markout_300_weight"))),
    )


def summarize_session(session: dict[str, Any], index: int) -> list[SessionPolicy]:
    session_id, generated_ts = session_identifier(session, index)
    compact = session.get("policy_summaries")
    if isinstance(compact, list):
        summaries: list[SessionPolicy] = []
        for value in compact:
            if not isinstance(value, dict):
                continue
            parsed = session_policy_from_compact(value, session_id, generated_ts)
            if parsed is not None:
                summaries.append(parsed)
        return summaries

    grouped: dict[str, list[dict[str, Any]]] = {}
    for value in session.get("results", []):
        if not isinstance(value, dict):
            continue
        policy = str(value.get("policy") or "").strip()
        if policy:
            grouped.setdefault(policy, []).append(value)

    output: list[SessionPolicy] = []
    for policy, rows in grouped.items():
        any_fills = pair_fills = one_sided = 0
        pnl_ex_rewards = pnl_with_rewards = conditional_rewards = 0.0
        matched_shares = rebate_basis = 0.0
        markout_60_sum = markout_60_weight = 0.0
        markout_300_sum = markout_300_weight = 0.0

        for row in rows:
            any_fills += int(truthy(row.get("any_fill")))
            pair_fills += int(truthy(row.get("pair_fill")))
            one_sided += int(truthy(row.get("one_sided_only")))
            ex_reward = finite(row.get("conservative_pnl_ex_rewards_usd"))
            with_reward = finite(
                row.get("conditional_pnl_including_reward_usd"), ex_reward
            )
            pnl_ex_rewards += ex_reward
            pnl_with_rewards += with_reward
            conditional_rewards += with_reward - ex_reward
            matched_shares += max(0.0, finite(row.get("matched_shares")))
            rebate_basis += max(
                0.0, finite(row.get("maker_rebate_fee_basis_usd_not_revenue"))
            )

            for side in ("yes", "no"):
                leg = row.get(side)
                if not isinstance(leg, dict):
                    continue
                shares = max(0.0, finite(leg.get("filled_shares")))
                if shares <= 0.0:
                    continue
                m60 = optional_finite(leg.get("markout_60_bid_per_share"))
                if m60 is not None:
                    markout_60_sum += shares * m60
                    markout_60_weight += shares
                m300 = optional_finite(leg.get("markout_300_bid_per_share"))
                if m300 is not None:
                    markout_300_sum += shares * m300
                    markout_300_weight += shares

        output.append(
            SessionPolicy(
                session_id=session_id,
                generated_ts=generated_ts,
                policy=policy,
                probes=len(rows),
                any_fills=any_fills,
                pair_fills=pair_fills,
                one_sided=one_sided,
                pnl_ex_rewards=pnl_ex_rewards,
                pnl_with_conditional_rewards=pnl_with_rewards,
                conditional_rewards=conditional_rewards,
                matched_shares=matched_shares,
                maker_rebate_fee_basis=rebate_basis,
                markout_60_weighted_sum=markout_60_sum,
                markout_60_weight=markout_60_weight,
                markout_300_weighted_sum=markout_300_sum,
                markout_300_weight=markout_300_weight,
            )
        )
    return output


def compact_session(session: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw forward run to compact per-policy sufficient statistics."""
    summaries = summarize_session(session, 0)
    return {
        "schema": "polymarket_forward_maker_session_summary_v1",
        "generated_ts": int(finite(session.get("generated_ts"), 0.0)),
        "git_sha": str(session.get("git_sha") or ""),
        "github_run_id": str(session.get("github_run_id") or ""),
        "policy_summaries": [
            {key: value for key, value in asdict(summary).items() if key != "session_id"}
            for summary in summaries
        ],
    }


def stable_policy_seed(base_seed: int, policy: str) -> int:
    digest = hashlib.sha256(policy.encode("utf-8")).digest()
    return base_seed ^ int.from_bytes(digest[:8], "big")


def block_bootstrap_mean_per_probe(
    sessions: list[SessionPolicy],
    reps: int,
    alpha: float,
    seed: int,
    block_sessions: int,
) -> tuple[float, float, float]:
    """Circular moving-block bootstrap over chronologically ordered sessions."""
    if not sessions:
        return 0.0, 0.0, 0.0
    ordered = sorted(sessions, key=lambda x: (x.generated_ts, x.session_id))
    total_probes = sum(max(0, x.probes) for x in ordered)
    point = sum(x.pnl_ex_rewards for x in ordered) / total_probes if total_probes else 0.0
    if reps <= 0:
        return point, point, point

    n = len(ordered)
    block = min(n, max(1, block_sessions))
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(reps):
        sampled: list[SessionPolicy] = []
        while len(sampled) < n:
            start = rng.randrange(n)
            sampled.extend(ordered[(start + offset) % n] for offset in range(block))
        sampled = sampled[:n]
        probes = sum(max(0, x.probes) for x in sampled)
        pnl = sum(x.pnl_ex_rewards for x in sampled)
        draws.append(pnl / probes if probes else 0.0)
    return point, quantile(draws, alpha), quantile(draws, 1.0 - alpha)


def policy_report(
    policy: str,
    sessions: list[SessionPolicy],
    config: GateConfig,
) -> dict[str, Any]:
    probes = sum(x.probes for x in sessions)
    any_fills = sum(x.any_fills for x in sessions)
    pair_fills = sum(x.pair_fills for x in sessions)
    one_sided = sum(x.one_sided for x in sessions)
    total_ex = sum(x.pnl_ex_rewards for x in sessions)
    total_with_rewards = sum(x.pnl_with_conditional_rewards for x in sessions)
    conditional_rewards = sum(x.conditional_rewards for x in sessions)
    matched_shares = sum(x.matched_shares for x in sessions)
    rebate_basis = sum(x.maker_rebate_fee_basis for x in sessions)

    any_l, any_u = wilson_interval(any_fills, probes)
    pair_l, pair_u = wilson_interval(pair_fills, probes)
    one_cond_l, one_cond_u = wilson_interval(one_sided, any_fills)

    point, pnl_lcb, pnl_ucb = block_bootstrap_mean_per_probe(
        sessions,
        config.bootstrap_reps,
        config.bootstrap_alpha,
        stable_policy_seed(config.seed, policy),
        config.bootstrap_block_sessions,
    )

    active_sessions = [x for x in sessions if x.any_fills > 0]
    positive_active = sum(x.pnl_ex_rewards > 0.0 for x in active_sessions)
    positive_active_rate = positive_active / len(active_sessions) if active_sessions else 0.0

    session_means = [x.pnl_ex_rewards / x.probes for x in sessions if x.probes > 0]
    session_mean = statistics.fmean(session_means) if session_means else 0.0
    session_median = statistics.median(session_means) if session_means else 0.0

    generated = sorted(x.generated_ts for x in sessions if x.generated_ts > 0)
    history_span = generated[-1] - generated[0] if len(generated) >= 2 else 0

    markout_60_weight = sum(x.markout_60_weight for x in sessions)
    markout_300_weight = sum(x.markout_300_weight for x in sessions)
    markout_60 = (
        sum(x.markout_60_weighted_sum for x in sessions) / markout_60_weight
        if markout_60_weight > 0.0
        else None
    )
    markout_300 = (
        sum(x.markout_300_weighted_sum for x in sessions) / markout_300_weight
        if markout_300_weight > 0.0
        else None
    )

    failures: list[str] = []
    if len(sessions) < config.min_sessions:
        failures.append("insufficient_sessions")
    if history_span < config.min_history_span_seconds:
        failures.append("insufficient_history_span")
    if probes < config.min_probes:
        failures.append("insufficient_probes")
    if any_fills < config.min_any_fills:
        failures.append("insufficient_any_fills")
    if pair_fills < config.min_pair_fills:
        failures.append("insufficient_pair_fills")
    if active_sessions and positive_active_rate < config.min_positive_active_session_rate:
        failures.append("unstable_active_session_pnl")
    elif not active_sessions:
        failures.append("no_active_fill_sessions")
    if any_fills > 0 and one_cond_u > config.max_one_sided_given_fill_upper:
        failures.append("excessive_one_sided_fill_risk")
    if pnl_lcb <= 0.0:
        failures.append("nonpositive_ex_reward_bootstrap_lcb")

    return {
        "policy": policy,
        "eligible_for_paper_shadow": not failures,
        "gate_failures": failures,
        "sessions": len(sessions),
        "history_span_seconds": history_span,
        "first_generated_ts": min(generated, default=0),
        "last_generated_ts": max(generated, default=0),
        "probes": probes,
        "any_fills": any_fills,
        "pair_fills": pair_fills,
        "one_sided_only": one_sided,
        "any_fill_rate": any_fills / probes if probes else 0.0,
        "any_fill_rate_wilson_lower": any_l,
        "any_fill_rate_wilson_upper": any_u,
        "pair_fill_rate": pair_fills / probes if probes else 0.0,
        "pair_fill_rate_wilson_lower": pair_l,
        "pair_fill_rate_wilson_upper": pair_u,
        "one_sided_given_any_fill_rate": one_sided / any_fills if any_fills else 0.0,
        "one_sided_given_any_fill_wilson_lower": one_cond_l,
        "one_sided_given_any_fill_wilson_upper": one_cond_u,
        "active_sessions": len(active_sessions),
        "positive_active_sessions": positive_active,
        "positive_active_session_rate": positive_active_rate,
        "total_pnl_ex_rewards_usd": total_ex,
        "total_conditional_rewards_usd_not_booked": conditional_rewards,
        "total_pnl_with_conditional_rewards_usd_not_booked": total_with_rewards,
        "mean_pnl_ex_rewards_per_probe_usd": point,
        "block_bootstrap_lcb_mean_pnl_ex_rewards_per_probe_usd": pnl_lcb,
        "block_bootstrap_ucb_mean_pnl_ex_rewards_per_probe_usd": pnl_ucb,
        "cluster_bootstrap_lcb_mean_pnl_ex_rewards_per_probe_usd": pnl_lcb,
        "cluster_bootstrap_ucb_mean_pnl_ex_rewards_per_probe_usd": pnl_ucb,
        "mean_session_pnl_ex_rewards_per_probe_usd": session_mean,
        "median_session_pnl_ex_rewards_per_probe_usd": session_median,
        "matched_shares": matched_shares,
        "maker_rebate_fee_basis_usd_not_revenue": rebate_basis,
        "filled_share_weighted_markout_60_bid_per_share": markout_60,
        "filled_share_weighted_markout_300_bid_per_share": markout_300,
        "markout_60_observed_filled_shares": markout_60_weight,
        "markout_300_observed_filled_shares": markout_300_weight,
    }


def calibrate(
    raw_sessions: Iterable[dict[str, Any]],
    config: GateConfig,
    malformed_lines: int = 0,
) -> dict[str, Any]:
    sessions, duplicate_sessions = deduplicate_sessions(raw_sessions)
    by_policy: dict[str, list[SessionPolicy]] = {}
    for index, session in enumerate(sessions):
        for summary in summarize_session(session, index):
            by_policy.setdefault(summary.policy, []).append(summary)

    reports = {
        policy: policy_report(policy, values, config)
        for policy, values in sorted(by_policy.items())
    }
    eligible = [report for report in reports.values() if report["eligible_for_paper_shadow"]]
    eligible.sort(
        key=lambda x: (
            finite(x.get("block_bootstrap_lcb_mean_pnl_ex_rewards_per_probe_usd")),
            finite(x.get("pair_fill_rate_wilson_lower")),
            -finite(x.get("one_sided_given_any_fill_wilson_upper"), 1.0),
        ),
        reverse=True,
    )
    selected = eligible[0]["policy"] if eligible else None
    generated = [
        int(finite(session.get("generated_ts"), 0.0))
        for session in sessions
        if int(finite(session.get("generated_ts"), 0.0)) > 0
    ]
    history_span = max(generated, default=0) - min(generated, default=0) if generated else 0

    return {
        "schema": "polymarket_forward_maker_calibration_v2",
        "read_only": True,
        "real_money_eligible": False,
        "production_action": "no_change",
        "selection_basis": (
            "moving-block bootstrap over complete forward sessions; promotion excludes "
            "estimated liquidity rewards and unverified maker rebates"
        ),
        "history": {
            "valid_sessions": len(sessions),
            "malformed_lines": malformed_lines,
            "duplicate_sessions_dropped": duplicate_sessions,
            "first_generated_ts": min(generated, default=0),
            "last_generated_ts": max(generated, default=0),
            "history_span_seconds": history_span,
        },
        "gate_config": asdict(config),
        "selected_policy_for_paper_shadow": selected,
        "eligible_for_paper_shadow": selected is not None,
        "by_policy": reports,
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-sessions", type=int, default=12)
    parser.add_argument("--min-history-span-seconds", type=int, default=18_000)
    parser.add_argument("--min-probes", type=int, default=180)
    parser.add_argument("--min-any-fills", type=int, default=20)
    parser.add_argument("--min-pair-fills", type=int, default=10)
    parser.add_argument("--min-positive-active-session-rate", type=float, default=0.55)
    parser.add_argument("--max-one-sided-given-fill-upper", type=float, default=0.40)
    parser.add_argument("--bootstrap-reps", type=int, default=2000)
    parser.add_argument("--bootstrap-alpha", type=float, default=0.05)
    parser.add_argument("--bootstrap-block-sessions", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20260824)
    args = parser.parse_args()

    if (
        args.min_sessions < 1
        or args.min_history_span_seconds < 0
        or args.min_probes < 1
        or args.min_any_fills < 1
        or args.min_pair_fills < 1
        or not 0.0 <= args.min_positive_active_session_rate <= 1.0
        or not 0.0 <= args.max_one_sided_given_fill_upper <= 1.0
        or args.bootstrap_reps < 100
        or not 0.0 < args.bootstrap_alpha < 0.5
        or args.bootstrap_block_sessions < 1
    ):
        raise SystemExit("invalid calibration gate arguments")

    sessions, malformed = load_history(args.history)
    config = GateConfig(
        min_sessions=args.min_sessions,
        min_history_span_seconds=args.min_history_span_seconds,
        min_probes=args.min_probes,
        min_any_fills=args.min_any_fills,
        min_pair_fills=args.min_pair_fills,
        min_positive_active_session_rate=args.min_positive_active_session_rate,
        max_one_sided_given_fill_upper=args.max_one_sided_given_fill_upper,
        bootstrap_reps=args.bootstrap_reps,
        bootstrap_alpha=args.bootstrap_alpha,
        bootstrap_block_sessions=args.bootstrap_block_sessions,
        seed=args.seed,
    )
    payload = calibrate(sessions, config, malformed)
    atomic_json(args.output, payload)
    print(
        "forward_maker_calibration"
        f" sessions={payload['history']['valid_sessions']}"
        f" span_s={payload['history']['history_span_seconds']}"
        f" malformed={payload['history']['malformed_lines']}"
        f" policies={len(payload['by_policy'])}"
        f" selected={payload['selected_policy_for_paper_shadow'] or 'NONE'}"
        f" eligible={int(payload['eligible_for_paper_shadow'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
