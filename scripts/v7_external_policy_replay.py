#!/usr/bin/env python3
"""Execution-aware, lower-bound policy replay for External Fair.

The replay prefers complete OPPORTUNITY_SET records, where it can rebuild the
book at empirical arrival latency and walk full visible depth.  Older tapes are
handled as selected-fill-only evidence and are labelled accordingly; they are
never presented as an unbiased all-opportunity policy evaluation.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
import subprocess
import time
from typing import Any, Iterable

try:
    from v7_execution_latency_distribution import build_latency_report
    from v7_external_economic_common import (
        atomic_json, canonical_sha256, finite, group_trade_lifecycles,
        load_counterfactual_evidence,
    )
except ModuleNotFoundError:
    from scripts.v7_execution_latency_distribution import build_latency_report
    from scripts.v7_external_economic_common import (
        atomic_json, canonical_sha256, finite, group_trade_lifecycles,
        load_counterfactual_evidence,
    )


SCHEMA = "polymarket_v7_external_policy_replay_v1"
DEFAULT_THRESHOLDS = (0.0, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.05)
DEFAULT_COST_MULTIPLIERS = (1.0, 1.5, 2.0)
DEFAULT_QUANTITIES = (1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0)


def fee_per_share(price: float, schedule: dict[str, Any]) -> float | None:
    rate = finite(schedule.get("rate"))
    exponent = finite(schedule.get("exponent"))
    if not 0.0 < price < 1.0 or rate is None or exponent is None or rate < 0.0 or exponent < 0.0:
        return None
    return rate * (price * (1.0 - price)) ** exponent


def walk_buy(
    levels: Iterable[Iterable[Any]], quantity: float, schedule: dict[str, Any],
    cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    remaining = max(0.0, float(quantity))
    cost = fee = filled = 0.0
    for raw in levels:
        values = list(raw)
        if len(values) < 2:
            continue
        price, available = finite(values[0]), finite(values[1])
        if price is None or available is None or not 0.0 < price < 1.0 or available <= 0.0:
            continue
        take = min(remaining, available)
        unit_fee = fee_per_share(price, schedule)
        if unit_fee is None:
            return {"complete": False, "reason": "INVALID_FEE_SCHEDULE"}
        cost += take * price
        fee += take * unit_fee * cost_multiplier
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    return {
        "complete": remaining <= 1e-9,
        "requested_quantity": quantity,
        "filled_quantity": filled,
        "average_price": cost / filled if filled > 0.0 else None,
        "gross_cost": cost,
        "fee": fee,
        "all_in_cost": cost + fee,
        "reason": "" if remaining <= 1e-9 else "INSUFFICIENT_VISIBLE_DEPTH",
    }


def day_block_lcb95(samples: list[dict[str, Any]], seed: int = 7, draws: int = 5000) -> dict[str, Any]:
    by_day: dict[int, list[float]] = defaultdict(list)
    for sample in samples:
        timestamp = int(finite(sample.get("timestamp_ms"), 0.0) or 0)
        pnl = finite(sample.get("pnl"))
        if timestamp > 0 and pnl is not None:
            by_day[timestamp // 86_400_000].append(pnl)
    daily_means = [sum(values) / len(values) for _, values in sorted(by_day.items())]
    if not daily_means:
        return {"day_blocks": 0, "mean_pnl_per_trade": None, "lcb95": None, "method": "UNAVAILABLE"}
    mean_trade = sum(float(sample["pnl"]) for sample in samples) / len(samples)
    if len(daily_means) < 2:
        return {
            "day_blocks": len(daily_means), "mean_pnl_per_trade": mean_trade,
            "lcb95": None, "method": "INSUFFICIENT_DAY_BLOCKS",
        }
    rng = random.Random(seed)
    estimates = sorted(
        sum(rng.choice(daily_means) for _ in daily_means) / len(daily_means)
        for _ in range(max(100, draws))
    )
    return {
        "day_blocks": len(daily_means),
        "mean_pnl_per_trade": mean_trade,
        "lcb95": estimates[max(0, math.ceil(0.05 * len(estimates)) - 1)],
        "method": "DAY_BLOCK_PERCENTILE_BOOTSTRAP_ONE_SIDED_95",
        "bootstrap_draws": max(100, draws),
    }


def _latency_profiles(latency_report: dict[str, Any]) -> dict[str, float | None]:
    stress = latency_report.get("stress_profiles") if isinstance(
        latency_report.get("stress_profiles"), dict) else {}
    return {
        name: finite(value.get("decision_to_arrival_ms"))
        for name, value in stress.items() if isinstance(value, dict)
    }


def _settlements(rows: list[dict[str, Any]]) -> tuple[dict[str, float], int]:
    actuals: dict[str, float] = {}
    conflicts = 0
    fills = {
        str(row.get("fill_id") or ""): row for row in rows
        if row.get("event_type") == "VIRTUAL_FILL" and row.get("fill_id")
    }
    for row in rows:
        if row.get("event_type") == "FORECAST_FINAL":
            market = str(row.get("market_id") or "")
            actual = finite(row.get("actual_yes"))
        elif row.get("event_type") == "VIRTUAL_FINAL":
            market = str(row.get("market_id") or "")
            fill = fills.get(str(row.get("fill_id") or ""), {})
            metadata = fill.get("metadata") if isinstance(fill.get("metadata"), dict) else {}
            final_meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            outcome = str(metadata.get("outcome") or "").upper()
            won = final_meta.get("won")
            actual = float(won == (outcome == "YES")) if isinstance(won, bool) and outcome in {"YES", "NO"} else None
        else:
            continue
        if not market or actual is None:
            continue
        if market in actuals and not math.isclose(actuals[market], actual):
            conflicts += 1
        else:
            actuals[market] = actual
    return actuals, conflicts


def _historical_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for lifecycle in group_trade_lifecycles(rows).values():
        fill = lifecycle.get("fill") if isinstance(lifecycle.get("fill"), dict) else {}
        final = lifecycle.get("final") if isinstance(lifecycle.get("final"), dict) else {}
        if not fill or not final:
            continue
        metadata = fill.get("metadata") if isinstance(fill.get("metadata"), dict) else {}
        size = finite(fill.get("filled_size"))
        price = finite(fill.get("fill_price"))
        fee = finite(fill.get("fee"), 0.0)
        pnl = finite(final.get("counterfactual_pnl"))
        probability = finite(
            metadata.get("arrival_robust_probability"), finite(metadata.get("robust_probability")),
        )
        risk = finite(metadata.get("expected_execution_risk"), 0.0)
        if None in (size, price, fee, pnl, probability, risk) or size <= 0.0:
            continue
        markouts: dict[str, float] = {}
        for markout in lifecycle.get("markouts", []):
            values = markout.get("markouts") if isinstance(markout.get("markouts"), dict) else {}
            for horizon, value in values.items():
                number = finite(value)
                if number is not None:
                    markouts[str(horizon)] = number * size
        samples.append({
            "market_id": str(fill.get("market_id") or ""),
            "fill_id": str(fill.get("fill_id") or ""),
            "timestamp_ms": int(finite(fill.get("receive_ts_ms"), finite(fill.get("timestamp_ms"), 0.0)) or 0),
            "outcome": str(metadata.get("outcome") or "").upper(),
            "tte_bucket_id": str(metadata.get("tte_bucket_id") or "UNKNOWN"),
            "shares": size,
            "price": price,
            "base_fee": fee,
            "slippage": max(0.0, finite(fill.get("slippage"), 0.0) or 0.0),
            "robust_probability": probability,
            "execution_risk_per_share": risk,
            "recorded_robust_ev_per_share": finite(metadata.get("arrival_robust_ev_per_share")),
            "hold_pnl": pnl,
            "markout_pnl": markouts,
            "entry_sha": str(fill.get("model_sha") or ""),
        })
    return samples


def _summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    confidence = day_block_lcb95(samples)
    return {
        "trades": len(samples),
        "independent_markets": len({sample["market_id"] for sample in samples}),
        "wins": sum(float(sample["pnl"]) > 0.0 for sample in samples),
        "losses": sum(float(sample["pnl"]) <= 0.0 for sample in samples),
        "net_pnl": sum(float(sample["pnl"]) for sample in samples),
        "confidence": confidence,
        "positive_lcb95": confidence["lcb95"] is not None and confidence["lcb95"] > 0.0,
    }


def replay_selected_fills(
    samples: list[dict[str, Any]], latency_profiles: dict[str, float | None],
    *, thresholds: Iterable[float] = DEFAULT_THRESHOLDS,
    cost_multipliers: Iterable[float] = DEFAULT_COST_MULTIPLIERS,
    latency_risk_per_second: float = 0.001,
) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for threshold in thresholds:
        for multiplier in cost_multipliers:
            for latency_name, latency_ms in latency_profiles.items():
                selected: list[dict[str, Any]] = []
                if latency_ms is not None:
                    for sample in samples:
                        extra_cost = (
                            (multiplier - 1.0) * (sample["base_fee"] + sample["slippage"])
                            + latency_risk_per_second * latency_ms / 1000.0 * sample["shares"]
                        )
                        lcb_ev = (
                            sample["robust_probability"] - sample["price"]
                            - multiplier * sample["base_fee"] / sample["shares"]
                            - sample["execution_risk_per_share"]
                            - latency_risk_per_second * latency_ms / 1000.0
                        )
                        if lcb_ev > threshold:
                            selected.append({
                                **sample, "pnl": sample["hold_pnl"] - extra_cost,
                                "replay_lcb_ev_per_share": lcb_ev,
                            })
                summary = _summarize_samples(selected)
                scenarios.append({
                    "threshold_per_share": float(threshold),
                    "cost_multiplier": float(multiplier),
                    "latency_profile": latency_name,
                    "latency_ms": latency_ms,
                    **summary,
                })
    return scenarios


def exit_policy_comparison(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    policies = {"HOLD_TO_SETTLEMENT"}
    policies.update(
        f"EXIT_{horizon.upper()}" for sample in samples for horizon in sample["markout_pnl"]
    )
    output: list[dict[str, Any]] = []
    for policy in sorted(policies, key=lambda value: (value != "HOLD_TO_SETTLEMENT", value)):
        selected: list[dict[str, Any]] = []
        horizon = policy.removeprefix("EXIT_").lower()
        for sample in samples:
            if policy == "HOLD_TO_SETTLEMENT":
                pnl = sample["hold_pnl"]
            elif horizon in sample["markout_pnl"]:
                pnl = sample["markout_pnl"][horizon]
            else:
                continue
            selected.append({**sample, "pnl": pnl})
        output.append({"policy": policy, **_summarize_samples(selected)})
    return output


def _opportunity_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [row for row in rows if row.get("event_type") == "OPPORTUNITY_SET"],
        key=lambda row: (int(finite(row.get("decision_ts_ms"), 0.0) or 0), str(row.get("record_id") or "")),
    )


def _policy_eligible_opportunity_rows(
    opportunities: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only snapshots whose non-price safety gates passed.

    Thresholds are intentionally replayed below, but contract identity,
    settlement reference, oracle, external-data, TTE and model-disagreement
    gates must never be optimized away by an offline replay.
    """
    return [
        row for row in opportunities
        if row.get("global_policy_gates_passed") is True
    ]


def capacity_curve(
    opportunities: list[dict[str, Any]], quantities: Iterable[float] = DEFAULT_QUANTITIES,
) -> list[dict[str, Any]]:
    opportunities = _policy_eligible_opportunity_rows(opportunities)
    output: list[dict[str, Any]] = []
    for quantity in quantities:
        positive = complete = 0
        evs: list[float] = []
        for row in opportunities:
            schedule = row.get("fee_schedule") if isinstance(row.get("fee_schedule"), dict) else {}
            books = row.get("books") if isinstance(row.get("books"), dict) else {}
            for action in row.get("actions") if isinstance(row.get("actions"), list) else []:
                if not isinstance(action, dict):
                    continue
                outcome = str(action.get("outcome") or "")
                book = books.get(outcome) if isinstance(books.get(outcome), dict) else {}
                walk = walk_buy(book.get("asks") or [], quantity, schedule)
                if not walk.get("complete"):
                    continue
                complete += 1
                probability = finite(action.get("robust_probability"))
                risk = finite(action.get("execution_risk_per_share"), 0.0)
                if probability is None or risk is None:
                    continue
                ev = probability * quantity - float(walk["all_in_cost"]) - risk * quantity
                evs.append(ev)
                positive += int(ev > 0.0)
        output.append({
            "quantity": quantity,
            "complete_action_books": complete,
            "positive_action_books": positive,
            "mean_net_lcb_ev": sum(evs) / len(evs) if evs else None,
            "minimum_net_lcb_ev": min(evs) if evs else None,
            "maximum_net_lcb_ev": max(evs) if evs else None,
        })
    return output


def replay_opportunity_sets(
    opportunities: list[dict[str, Any]], actuals: dict[str, float],
    latency_profiles: dict[str, float | None],
    *, quantity: float = 5.0,
    thresholds: Iterable[float] = DEFAULT_THRESHOLDS,
    cost_multipliers: Iterable[float] = DEFAULT_COST_MULTIPLIERS,
    latency_risk_per_second: float = 0.001,
) -> list[dict[str, Any]]:
    opportunities = _policy_eligible_opportunity_rows(opportunities)
    """Replay one frozen first-entry policy per market at an arrival snapshot."""
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in opportunities:
        market = str(row.get("market_id") or "")
        if market and market in actuals:
            by_market[market].append(row)
    for values in by_market.values():
        values.sort(key=lambda row: int(finite(row.get("decision_ts_ms"), 0.0) or 0))

    scenarios: list[dict[str, Any]] = []
    for threshold in thresholds:
        for multiplier in cost_multipliers:
            for profile, latency_ms in latency_profiles.items():
                samples: list[dict[str, Any]] = []
                missing_arrival = 0
                if latency_ms is not None:
                    for market, snapshots in sorted(by_market.items()):
                        executed = False
                        for decision in snapshots:
                            decision_ts = int(finite(decision.get("decision_ts_ms"), 0.0) or 0)
                            target = decision_ts + math.ceil(latency_ms)
                            arrival = next((
                                row for row in snapshots
                                if int(finite(row.get("decision_ts_ms"), 0.0) or 0) >= target
                            ), None)
                            if arrival is None:
                                missing_arrival += 1
                                continue
                            arrival_books = arrival.get("books") if isinstance(arrival.get("books"), dict) else {}
                            schedule = arrival.get("fee_schedule") if isinstance(arrival.get("fee_schedule"), dict) else {}
                            action_values: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
                            for action in decision.get("actions") if isinstance(decision.get("actions"), list) else []:
                                if not isinstance(action, dict):
                                    continue
                                outcome = str(action.get("outcome") or "")
                                book = arrival_books.get(outcome) if isinstance(arrival_books.get(outcome), dict) else {}
                                walk = walk_buy(book.get("asks") or [], quantity, schedule, multiplier)
                                probability = finite(action.get("robust_probability"))
                                risk = finite(action.get("execution_risk_per_share"), 0.0)
                                if not walk.get("complete") or probability is None or risk is None:
                                    continue
                                lcb_ev = (
                                    probability * quantity - float(walk["all_in_cost"])
                                    - risk * quantity
                                    - latency_risk_per_second * latency_ms / 1000.0 * quantity
                                )
                                action_values.append((lcb_ev, action, walk))
                            if not action_values:
                                continue
                            lcb_ev, action, walk = max(action_values, key=lambda value: value[0])
                            if lcb_ev / quantity <= threshold:
                                continue
                            outcome = str(action.get("outcome") or "")
                            actual = actuals[market]
                            payout = quantity if (actual == 1.0) == (outcome == "YES") else 0.0
                            latency_cost = latency_risk_per_second * latency_ms / 1000.0 * quantity
                            samples.append({
                                "market_id": market,
                                "timestamp_ms": decision_ts,
                                "pnl": payout - float(walk["all_in_cost"]) - latency_cost,
                                "outcome": outcome,
                                "replay_lcb_ev_per_share": lcb_ev / quantity,
                            })
                            executed = True
                            break
                        if executed:
                            continue
                scenarios.append({
                    "quantity": quantity,
                    "threshold_per_share": float(threshold),
                    "cost_multiplier": float(multiplier),
                    "latency_profile": profile,
                    "latency_ms": latency_ms,
                    "missing_arrival_snapshots": missing_arrival,
                    **_summarize_samples(samples),
                })
    return scenarios


def build_replay(
    rows: list[dict[str, Any]], quality: dict[str, Any], repository_head: str,
    *, latency_risk_per_second: float = 0.001,
) -> dict[str, Any]:
    latency = build_latency_report(rows, quality, repository_head)
    profiles = _latency_profiles(latency)
    historical = _historical_samples(rows)
    opportunities = _opportunity_rows(rows)
    eligible_opportunities = _policy_eligible_opportunity_rows(opportunities)
    actuals, settlement_conflicts = _settlements(rows)
    settled_opportunities = sum(
        str(row.get("market_id") or "") in actuals for row in opportunities
    )
    scenarios = replay_selected_fills(
        historical, profiles, latency_risk_per_second=latency_risk_per_second,
    )
    opportunity_scenarios = replay_opportunity_sets(
        opportunities, actuals, profiles,
        latency_risk_per_second=latency_risk_per_second,
    )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at_unix_ms": int(time.time() * 1000),
        "repository_head": repository_head,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "profitability_proven": False,
        "source_quality": quality,
        "replay_scope": (
            "ALL_RECORDED_OPPORTUNITY_BOOKS_AND_SELECTED_HISTORICAL_FILLS"
            if opportunities else "SELECTED_HISTORICAL_FILLS_ONLY"
        ),
        "opportunity_sets": len(opportunities),
        "policy_gate_eligible_opportunity_sets": len(eligible_opportunities),
        "policy_gate_rejected_opportunity_sets": len(opportunities) - len(eligible_opportunities),
        "settled_opportunity_sets": settled_opportunities,
        "settlement_label_conflicts": settlement_conflicts,
        "historical_terminal_fills": len(historical),
        "latency_distribution": latency,
        "threshold_cost_latency_scenarios": scenarios,
        "all_opportunity_arrival_book_scenarios": opportunity_scenarios,
        "exit_policy_comparison_base_observed_costs": exit_policy_comparison(historical),
        "capacity_curve": capacity_curve(eligible_opportunities),
        "best_scenario_not_selected": True,
        "promotion": {
            "automatic": False,
            "eligible": False,
            "reason": "FORWARD_FROZEN_POLICY_AND_INDEPENDENT_OOS_GATES_REQUIRED",
        },
        "limitations": [
            "Historical fills are policy-selected and cannot identify the value of rejected opportunities.",
            "Latency stress on historical fills is a cost penalty because the historical arrival book cannot be moved in time.",
            "MAKE and hedge actions require separate fill/hedge evidence and are not synthesized.",
            "Exit policies are compared on a common historical cohort; no per-trade ex-post exit selection is performed.",
            "SHADOW fills and virtual PnL are not real execution evidence.",
        ],
    }
    report["content_sha256"] = canonical_sha256(report)
    return report


def _latency_risk(config: Path | None) -> float:
    if config is None:
        return 0.001
    try:
        value = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.001
    taker = value.get("taker") if isinstance(value, dict) and isinstance(value.get("taker"), dict) else {}
    return max(0.0, finite(taker.get("latency_risk_per_second"), 0.001) or 0.001)


def generate(
    inputs: Iterable[Path], repo: Path, output: Path, config: Path | None = None,
) -> dict[str, Any]:
    rows, quality = load_counterfactual_evidence(inputs)
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
    ).strip()
    report = build_replay(
        rows, quality, sha, latency_risk_per_second=_latency_risk(config),
    )
    atomic_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, default=Path("config/v7_external_fair.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = generate(args.input, args.repo.resolve(), args.output, args.config)
    print(json.dumps({
        "replay_scope": report["replay_scope"],
        "opportunity_sets": report["opportunity_sets"],
        "historical_terminal_fills": report["historical_terminal_fills"],
    }, indent=2, sort_keys=True))
    return 2 if report["source_quality"]["fail_closed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
