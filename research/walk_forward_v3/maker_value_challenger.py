"""Zero-authority Maker value challenger built from canonical PAPER evidence.

This module does not alter Maker runtime policy.  It asks whether the placement
actions that were actually explored have positive out-of-sample economic value
once no-fills, fill-conditioned executable markouts, censoring, queue state and
verified subsidies are kept explicit.

Signed executable markout is measured from the actual fill price, so spread
capture is already embedded in the label and MUST NOT be added a second time.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import json
import math
from pathlib import Path
import random
from typing import Any, Iterable

from research.walk_forward_v2.core import SAFETY, atomic_json
from scripts import v7_maker_durable_learning as durable


SCHEMA = "polymarket_v7_maker_value_challenger_v1"
TERMINAL_STATES = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "LOST"}
DEFAULT_ACTIONS = ("JOIN", "IMPROVE1", "FADE1", "FADE2", "ONE_SIDED")


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def quantile(values: Iterable[float], probability: float) -> float | None:
    values = sorted(float(value) for value in values if finite(value))
    if not values:
        return None
    index = max(
        0,
        min(
            len(values) - 1,
            int(math.ceil(float(probability) * len(values))) - 1,
        ),
    )
    return values[index]


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("metadata") if isinstance(row.get("metadata"), dict) else {}


def load_current_model_rows(
    roots: Iterable[Path],
    *,
    model_sha: str,
) -> list[dict[str, Any]]:
    """Load only the exact current PAPER Maker generation."""
    paths = durable.evidence_files(list(roots))
    output: dict[str, dict[str, Any]] = {}
    for row in durable.rows(paths):
        if str(row.get("model_sha") or "") != model_sha:
            continue
        if (
            row.get("paper_only") is not True
            or row.get("authenticated_execution") is not False
            or row.get("real_order_submission") is True
        ):
            continue
        key = str(row.get("record_id") or durable.canonical_hash(row))
        output.setdefault(key, row)
    return sorted(
        output.values(),
        key=lambda row: (
            int(row.get("recorded_ts_ms") or 0),
            str(row.get("record_id") or ""),
        ),
    )


def _assigned_lifetime_ms(order: dict[str, Any]) -> float | None:
    meta = _metadata(order)
    candidates = (
        order.get("horizon_ms"),
        meta.get("horizon_ms"),
        meta.get("execution_horizon_ms"),
        meta.get("quote_lifetime_ms"),
        meta.get("maximum_rest_ms"),
    )
    for value in candidates:
        if finite(value) and float(value) > 0:
            return float(value)
    envelope = meta.get("opportunity_envelope")
    if isinstance(envelope, dict):
        plan = envelope.get("execution_plan")
        if isinstance(plan, dict):
            value = plan.get("horizon_ms")
            if finite(value) and float(value) > 0:
                return float(value)
    value = meta.get("exploration_max_rest_ns")
    if finite(value) and float(value) > 0:
        return float(value) / 1e6
    return None


def build_episodes(
    values: list[dict[str, Any]],
    *,
    markout_horizon: str = "1s",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    orders = {
        durable.lifecycle_key(row): row
        for row in values
        if row.get("event_type") == "ORDER_SUBMITTED"
        and row.get("order_id")
    }
    lifecycle: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in values:
        key = durable.lifecycle_key(row)
        if key[1] and row.get("event_type") != "ORDER_SUBMITTED":
            lifecycle[key].append(row)

    markout_by_fill: dict[tuple[str, str], float] = {}
    for row in values:
        if row.get("event_type") != "MARKOUT":
            continue
        raw = row.get("markouts")
        fill_id = str(row.get("fill_id") or "")
        model = str(row.get("model_sha") or "")
        if (
            not fill_id
            or not isinstance(raw, dict)
            or markout_horizon not in raw
            or not finite(raw.get(markout_horizon))
        ):
            continue
        markout_by_fill[(model, fill_id)] = float(raw[markout_horizon])

    episodes: list[dict[str, Any]] = []
    diagnostics = Counter()
    for key, order in orders.items():
        diagnostics["orders_seen"] += 1
        action = durable.placement_action(order)
        features = durable.placement_features(order)
        intended = durable.number(order.get("intended_size"), math.nan)
        start = int(order.get("recorded_ts_ms") or 0)
        if action not in DEFAULT_ACTIONS or features is None:
            diagnostics["orders_missing_supported_features"] += 1
            continue
        if start <= 0 or not finite(intended) or intended <= 0:
            diagnostics["orders_invalid_size_or_time"] += 1
            continue

        rows = sorted(
            lifecycle.get(key, []),
            key=lambda row: int(row.get("recorded_ts_ms") or 0),
        )
        fills = [
            row for row in rows
            if row.get("event_type") == "FILL"
            and finite(row.get("filled_size"))
            and float(row.get("filled_size")) > 0
        ]
        terminal = False
        terminal_ts = None
        for row in rows:
            state = str(row.get("order_state") or "").upper()
            if state in TERMINAL_STATES:
                terminal = True
                timestamp = int(row.get("recorded_ts_ms") or 0)
                if timestamp > 0:
                    terminal_ts = max(terminal_ts or 0, timestamp)

        filled_shares = sum(float(row["filled_size"]) for row in fills)
        filled_fraction = min(1.0, max(0.0, filled_shares / float(intended)))
        signed_markout_dollars = 0.0
        unlabeled_fill_shares = 0.0
        for fill in fills:
            shares = float(fill["filled_size"])
            fill_id = str(fill.get("fill_id") or "")
            value = markout_by_fill.get((key[0], fill_id))
            if value is None:
                unlabeled_fill_shares += shares
                continue
            signed_markout_dollars += value * shares

        if filled_shares > 0:
            economic_observed = unlabeled_fill_shares <= 1e-12
            if not economic_observed:
                diagnostics["filled_orders_missing_markout"] += 1
        else:
            # Terminal no-fill contributes exactly zero. Open orders are right
            # censored and never silently converted to zero.
            economic_observed = terminal
            if not terminal:
                diagnostics["open_censored_orders"] += 1

        spread = max(
            0.0,
            # Prices are decimal venue quantities. Subtract in decimal before
            # converting this diagnostic feature, not in binary floating point.
            float(Decimal(str(durable.number(order.get("ask"))))
                  - Decimal(str(durable.number(order.get("bid"))))),
        )
        queue = max(0.0, durable.number(order.get("queue_ahead")))
        value_per_posted_share = (
            signed_markout_dollars / float(intended)
            if economic_observed else None
        )
        signed_markout_per_filled_share = (
            signed_markout_dollars / filled_shares
            if economic_observed and filled_shares > 0 else None
        )
        meta = _metadata(order)
        episodes.append({
            "order_id": str(order.get("order_id") or ""),
            "market_id": str(order.get("market_id") or ""),
            "event_cluster": str(
                order.get("event_id") or order.get("market_id") or "UNKNOWN"),
            "start_ts_ms": start,
            "terminal_ts_ms": terminal_ts,
            "action": action,
            "side": str(order.get("side") or meta.get("execution_side") or ""),
            "outcome": str(meta.get("outcome") or "UNKNOWN").upper(),
            "intended_shares": float(intended),
            "filled_shares": float(filled_shares),
            "filled_fraction": float(filled_fraction),
            "queue_ahead": float(queue),
            "queue_ahead_per_quote": float(queue / float(intended)),
            "spread": float(spread),
            "assigned_lifetime_ms": _assigned_lifetime_ms(order),
            "features": list(features),
            "economic_observed": bool(economic_observed),
            "censored": not bool(economic_observed),
            "signed_markout_dollars": (
                float(signed_markout_dollars) if economic_observed else None),
            "value_per_posted_share": (
                float(value_per_posted_share)
                if value_per_posted_share is not None else None),
            "signed_markout_per_filled_share": (
                float(signed_markout_per_filled_share)
                if signed_markout_per_filled_share is not None else None),
            "markout_horizon": markout_horizon,
            "label_semantics": (
                "SIGNED_EXECUTABLE_MARKOUT_FROM_ACTUAL_FILL_PRICE;"
                "NO_FILL_TERMINAL_EQUALS_ZERO;"
                "NO_SEPARATE_SPREAD_CAPTURE_ADD"
            ),
        })
        diagnostics["episodes"] += 1
        diagnostics["economic_observed"] += int(economic_observed)
        diagnostics["filled_orders"] += int(filled_shares > 0)

    return episodes, dict(diagnostics)


def chronological_cluster_split(
    episodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    first: dict[str, int] = {}
    for row in episodes:
        cluster = str(row["event_cluster"])
        timestamp = int(row["start_ts_ms"])
        first[cluster] = min(timestamp, first.get(cluster, timestamp))
    clusters = sorted(first, key=lambda name: (first[name], name))
    if len(clusters) < 5:
        return [], [], {
            "state": "INSUFFICIENT_EVENT_CLUSTERS",
            "event_clusters": len(clusters),
        }
    cut = max(1, int(len(clusters) * 0.80))
    cut = min(cut, len(clusters) - 1)
    train_clusters = set(clusters[:cut])
    test_clusters = set(clusters[cut:])
    train = [row for row in episodes if row["event_cluster"] in train_clusters]
    test = [row for row in episodes if row["event_cluster"] in test_clusters]
    return train, test, {
        "state": "READY",
        "partition": "EVENT_CLUSTER_CHRONOLOGICAL_80_20",
        "train_clusters": len(train_clusters),
        "test_clusters": len(test_clusters),
        "train_rows": len(train),
        "test_rows": len(test),
    }


def _dot(coefficients: list[float], values: list[float]) -> float:
    return sum(a * b for a, b in zip(coefficients, values))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(50.0, value))
        return 1.0 / (1.0 + z)
    z = math.exp(max(-50.0, value))
    return z / (1.0 + z)


def fit_predictive_heads(
    train: list[dict[str, Any]],
    test: list[dict[str, Any]],
) -> dict[str, Any]:
    train_features = [row for row in train if row.get("features") is not None]
    test_features = [row for row in test if row.get("features") is not None]
    mark_train = [
        row for row in train_features
        if row["economic_observed"] and row["filled_shares"] > 0
        and finite(row["signed_markout_per_filled_share"])
    ]
    mark_test = [
        row for row in test_features
        if row["economic_observed"] and row["filled_shares"] > 0
        and finite(row["signed_markout_per_filled_share"])
    ]
    if (
        len(train_features) < 40
        or len(test_features) < 10
        or len(mark_train) < 10
        or len(mark_test) < 3
    ):
        return {
            "state": "EVIDENCE_ACCUMULATING",
            "train_orders": len(train_features),
            "test_orders": len(test_features),
            "train_markout_fills": len(mark_train),
            "test_markout_fills": len(mark_test),
        }

    fill_coefficients = durable._fit_logistic(
        [row["features"] for row in train_features],
        [row["filled_fraction"] for row in train_features],
    )
    markout_coefficients = durable._fit_linear(
        [row["features"] for row in mark_train],
        [row["signed_markout_per_filled_share"] for row in mark_train],
    )
    fill_prior = sum(row["filled_fraction"] for row in train_features) / len(train_features)
    markout_prior = (
        sum(row["signed_markout_per_filled_share"] for row in mark_train)
        / len(mark_train)
    )

    observed_test = [row for row in test_features if row["economic_observed"]]
    scored = []
    for row in observed_test:
        p_fill = min(
            1.0,
            max(0.0, _sigmoid(_dot(fill_coefficients, row["features"]))),
        )
        markout = _dot(markout_coefficients, row["features"])
        predicted_value = p_fill * markout
        baseline_value = fill_prior * markout_prior
        scored.append({
            **row,
            "predicted_fill_fraction": p_fill,
            "predicted_signed_markout_per_fill_share": markout,
            "predicted_value_per_posted_share": predicted_value,
            "baseline_value_per_posted_share": baseline_value,
        })

    model_mse = (
        sum(
            (row["predicted_value_per_posted_share"] - row["value_per_posted_share"]) ** 2
            for row in scored
        ) / len(scored)
        if scored else None
    )
    baseline_mse = (
        sum(
            (row["baseline_value_per_posted_share"] - row["value_per_posted_share"]) ** 2
            for row in scored
        ) / len(scored)
        if scored else None
    )
    return {
        "state": "READY" if scored else "NO_OOS_ECONOMIC_LABELS",
        "feature_names": list(durable.PLACEMENT_FEATURE_NAMES),
        "train_orders": len(train_features),
        "test_orders": len(test_features),
        "train_markout_fills": len(mark_train),
        "test_markout_fills": len(mark_test),
        "fill_prior": fill_prior,
        "signed_markout_prior": markout_prior,
        "fill_coefficients": fill_coefficients,
        "signed_markout_coefficients": markout_coefficients,
        "oos_value_mse": model_mse,
        "oos_baseline_value_mse": baseline_mse,
        "oos_value_improvement_fraction": (
            1.0 - model_mse / max(1e-12, baseline_mse)
            if model_mse is not None and baseline_mse is not None else None
        ),
        "oos_rows": scored,
    }


def _cluster_bootstrap_value(
    rows: list[dict[str, Any]],
    *,
    samples: int = 3000,
    seed: int = 260921,
) -> dict[str, Any]:
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("economic_observed") and finite(row.get("signed_markout_dollars")):
            groups[str(row["event_cluster"])].append(row)
    names = sorted(groups)
    if not names:
        return {
            "clusters": 0,
            "mean_value_per_posted_share": None,
            "ci95": [None, None],
            "lcb95": None,
        }

    stats = {}
    for name, group in groups.items():
        dollars = sum(float(row["signed_markout_dollars"]) for row in group)
        shares = sum(float(row["intended_shares"]) for row in group)
        stats[name] = (dollars, shares)

    denominator = sum(value[1] for value in stats.values())
    point = (
        sum(value[0] for value in stats.values()) / denominator
        if denominator > 0 else None
    )
    rng = random.Random(seed)
    draws = []
    for _ in range(max(0, int(samples))):
        picked = [rng.choice(names) for _ in names]
        den = sum(stats[name][1] for name in picked)
        if den <= 0:
            continue
        draws.append(sum(stats[name][0] for name in picked) / den)
    return {
        "clusters": len(names),
        "mean_value_per_posted_share": point,
        "ci95": [quantile(draws, .025), quantile(draws, .975)],
        "lcb95": quantile(draws, .025),
        "bootstrap_samples": len(draws),
    }


def action_cells(
    rows: list[dict[str, Any]],
    *,
    bootstrap_samples: int = 3000,
) -> dict[str, Any]:
    output = {}
    for action in DEFAULT_ACTIONS:
        sample = [row for row in rows if row["action"] == action]
        if not sample:
            continue
        observed = [row for row in sample if row["economic_observed"]]
        filled = [row for row in observed if row["filled_shares"] > 0]
        cell = _cluster_bootstrap_value(
            sample,
            samples=bootstrap_samples,
            seed=260921 + DEFAULT_ACTIONS.index(action),
        )
        cell.update({
            "orders": len(sample),
            "observed_orders": len(observed),
            "censored_orders": len(sample) - len(observed),
            "filled_orders": len(filled),
            "observed_fraction": len(observed) / len(sample),
            "filled_order_fraction_observed": (
                len(filled) / len(observed) if observed else None),
            "mean_filled_fraction_observed": (
                sum(float(row["filled_fraction"]) for row in observed) / len(observed)
                if observed else None),
            "mean_queue_ahead_per_quote": (
                sum(float(row["queue_ahead_per_quote"]) for row in sample) / len(sample)
            ),
            "mean_assigned_lifetime_ms": (
                sum(float(row["assigned_lifetime_ms"]) for row in sample
                    if finite(row.get("assigned_lifetime_ms")))
                / max(1, sum(finite(row.get("assigned_lifetime_ms")) for row in sample))
            ),
            "role": "OBSERVED_ACTION_CELL_ONLY_NO_COUNTERFACTUAL_QUEUE_TRANSFER",
        })
        output[action] = cell
    return output


def verified_subsidies(values: list[dict[str, Any]]) -> dict[str, Any]:
    rebate = reward = 0.0
    verified_rows = unverified_rows = 0
    for row in values:
        if row.get("event_type") not in {"FINAL", "INVENTORY_MERGE"}:
            continue
        meta = _metadata(row)
        decomposition = meta.get("pnl_decomposition")
        if not isinstance(decomposition, dict):
            continue
        if decomposition.get("own_reward_share_verified") is not True:
            unverified_rows += 1
            continue
        verified_rows += 1
        if finite(decomposition.get("maker_rebates")):
            rebate += float(decomposition["maker_rebates"])
        if finite(decomposition.get("liquidity_rewards")):
            reward += float(decomposition["liquidity_rewards"])
    return {
        "state": "VERIFIED" if verified_rows else "NO_VERIFIED_SUBSIDY_CREDIT",
        "verified_rows": verified_rows,
        "unverified_rows": unverified_rows,
        "maker_rebates": rebate,
        "liquidity_rewards": reward,
        "total_verified_subsidy": rebate + reward,
        "decision_credit_semantics": (
            "ZERO_UNLESS_OWN_REWARD_SHARE_IS_VERIFIED"
        ),
    }


def complete_set_summary(paths: Iterable[Path]) -> dict[str, Any]:
    rows = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                value = json.loads(line)
                if (
                    isinstance(value, dict)
                    and value.get("schema")
                    == "polymarket_v7_two_sided_complete_set_cycle_v1"
                    and value.get("paper_only") is True
                    and value.get("authenticated_execution") is False
                    and value.get("real_order_submission") is False
                ):
                    rows.append(value)
        except (OSError, ValueError):
            continue
    usable = [row for row in rows if row.get("state") != "CENSORED"]
    states = Counter(str(row.get("state") or "UNKNOWN") for row in usable)
    pnls = [
        float(row["total_shadow_pnl"])
        for row in usable if finite(row.get("total_shadow_pnl"))
    ]
    legging = [
        float(row["legging_loss"])
        for row in usable if finite(row.get("legging_loss"))
    ]
    return {
        "state": "READY" if usable else "NO_COMPLETE_SET_CYCLES",
        "cycles": len(usable),
        "censored_cycles": len(rows) - len(usable),
        "states": dict(states),
        "paired_full_probability": (
            states["BOTH_FULL"] / len(usable) if usable else None),
        "one_leg_probability": (
            (states["YES_ONLY"] + states["NO_ONLY"]) / len(usable)
            if usable else None),
        "mean_total_shadow_pnl": (
            sum(pnls) / len(pnls) if pnls else None),
        "mean_legging_loss": (
            sum(legging) / len(legging) if legging else None),
        "joint_probability_semantics": (
            "DIRECT_EMPIRICAL_CYCLE_STATES_NOT_PRODUCT_OF_MARGINALS"
        ),
    }


def run(
    roots: list[Path],
    *,
    model_sha: str,
    markout_horizon: str = "1s",
    bootstrap_samples: int = 3000,
    complete_set_paths: list[Path] | None = None,
) -> dict[str, Any]:
    values = load_current_model_rows(roots, model_sha=model_sha)
    episodes, diagnostics = build_episodes(
        values, markout_horizon=markout_horizon)
    train, test, split = chronological_cluster_split(episodes)
    predictive = (
        fit_predictive_heads(train, test)
        if split.get("state") == "READY"
        else {"state": "NOT_FIT", "reason": split.get("state")}
    )
    oos_rows = predictive.get("oos_rows") or []
    predictive_public = dict(predictive)
    predictive_public.pop("oos_rows", None)

    cells = action_cells(
        oos_rows if oos_rows else test,
        bootstrap_samples=bootstrap_samples,
    )
    positive_lcb = sorted(
        action for action, cell in cells.items()
        if finite(cell.get("lcb95")) and float(cell["lcb95"]) > 0
    )
    return {
        "schema": SCHEMA,
        **SAFETY,
        "state": "READY" if episodes else "NO_MAKER_EPISODES",
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "model_sha": model_sha,
        "markout_horizon": markout_horizon,
        "no_make_baseline_value_per_posted_share": 0.0,
        "label_semantics": (
            "SIGNED_EXECUTABLE_MARKOUT_FROM_ACTUAL_FILL_PRICE;"
            "SPREAD_CAPTURE_ALREADY_EMBEDDED_DO_NOT_ADD_AGAIN"
        ),
        "counterfactual_scope": (
            "OBSERVED_PLACEMENT_ACTION_CELLS_ONLY;"
            "NO_COUNTERFACTUAL_QUEUE_TRANSFER"
        ),
        "diagnostics": diagnostics,
        "split": split,
        "predictive_heads": predictive_public,
        "oos_action_cells": cells,
        "positive_lcb_actions_diagnostic_only": positive_lcb,
        "subsidies": verified_subsidies(values),
        "complete_set": complete_set_summary(complete_set_paths or []),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, action="append", required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--markout-horizon", default="1s")
    parser.add_argument("--bootstrap-samples", type=int, default=3000)
    parser.add_argument("--complete-set-shadow", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if (
        len(args.model_sha) != 40
        or any(ch not in "0123456789abcdef" for ch in args.model_sha)
    ):
        raise SystemExit("exact 40-hex model SHA required")
    result = run(
        args.source_root,
        model_sha=args.model_sha,
        markout_horizon=args.markout_horizon,
        bootstrap_samples=args.bootstrap_samples,
        complete_set_paths=args.complete_set_shadow,
    )
    atomic_json(args.output, result)
    return 0 if result["state"] in {"READY", "NO_MAKER_EPISODES"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
