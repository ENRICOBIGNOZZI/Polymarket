#!/usr/bin/env python3
"""Persist Maker evidence across cutovers and fit a censored hazard/joint model.

This is a PAPER-only slow-plane component. It copies immutable canonical ledger
rows into a durable, deduplicated evidence store, excludes incompatible
execution semantics from training, and atomically materializes an exact-code
runtime champion. A cold champion is explicit (`model_state=COLD_START`) rather
than a silent in-process fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import tempfile
import time
from collections import Counter, defaultdict
from typing import Any, Iterable

MODEL_SCHEMA = "polymarket_v7_maker_execution_model_v1"
STORE_SCHEMA = "polymarket_v7_maker_durable_evidence_v1"
STRATEGY = "MICRO_MAKER_PRO"
HORIZONS_SECONDS = (1, 5, 15, 30, 60, 300)


def atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fnv1a64(path: pathlib.Path) -> str:
    value = 1469598103934665603
    for byte in path.read_bytes():
        value ^= byte
        value = (value * 1099511628211) & ((1 << 64) - 1)
    return f"{value:016x}"


def canonical_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return (
        str(row.get("model_sha") or "unknown"),
        str(metadata.get("policy_hash") or "unknown"),
        str(metadata.get("config_hash") or "unknown"),
        str(row.get("record_id") or canonical_hash(row)),
    )


def jsonl_files(roots: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    output: set[pathlib.Path] = set()
    for root in roots:
        if root.is_file() and root.suffix == ".jsonl":
            output.add(root.resolve())
        elif root.exists():
            output.update(item.resolve() for item in root.rglob("*.jsonl"))
    return sorted(output)


def rows(paths: Iterable[pathlib.Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        isinstance(row, dict)
                        and row.get("paper_only") is True
                        and row.get("authenticated_execution") is False
                        and str(row.get("strategy") or "").upper() == STRATEGY
                    ):
                        yield row
        except OSError:
            continue


def load_store(path: pathlib.Path) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    return {identity(row): row for row in rows([path])}


def append_new(path: pathlib.Path, values: Iterable[dict[str, Any]]) -> tuple[int, int]:
    existing = load_store(path)
    additions: list[dict[str, Any]] = []
    for row in values:
        key = identity(row)
        if key in existing:
            continue
        existing[key] = row
        additions.append(row)
    if additions:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                for row in additions:
                    handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            raise
    return len(additions), len(existing)


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def group_key(order: dict[str, Any]) -> str:
    metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
    action = str(order.get("intended_action") or metadata.get("action") or "UNKNOWN").upper()
    outcome = str(metadata.get("outcome") or "UNKNOWN").upper()
    side = str(order.get("side") or metadata.get("execution_side") or "UNKNOWN").upper()
    return f"{action}|{outcome}|{side}"


def order_examples(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    orders = {
        str(row["order_id"]): row for row in values
        if row.get("event_type") == "ORDER_SUBMITTED" and row.get("order_id")
    }
    later: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in values:
        order_id = str(row.get("order_id") or "")
        if order_id and row.get("event_type") != "ORDER_SUBMITTED":
            later[order_id].append(row)
    output: list[dict[str, Any]] = []
    for order_id, order in orders.items():
        start = int(order.get("recorded_ts_ms") or 0)
        intended = max(0.0, number(order.get("intended_size")))
        if start <= 0 or intended <= 0.0:
            continue
        terminal_ts = 0
        first_fill_ts = 0
        filled = 0.0
        terminal_state = "OPEN_CENSORED_AT_OBSERVATION_END"
        for row in sorted(later.get(order_id, []), key=lambda item: int(item.get("recorded_ts_ms") or 0)):
            timestamp = int(row.get("recorded_ts_ms") or 0)
            if row.get("event_type") == "FILL":
                filled += max(0.0, number(row.get("filled_size")))
                if first_fill_ts <= 0:
                    first_fill_ts = timestamp
            state = str(row.get("order_state") or "").upper()
            if state in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "LOST"}:
                terminal_ts = max(terminal_ts, timestamp)
                terminal_state = state
        observation_end = terminal_ts or max(
            (int(row.get("recorded_ts_ms") or 0) for row in values), default=start
        )
        exposure_ms = max(0, observation_end - start)
        output.append({
            "order_id": order_id,
            "group": group_key(order),
            "event_cluster": str(order.get("event_id") or order.get("market_id") or "UNKNOWN"),
            "start_ts_ms": start,
            "exposure_ms": exposure_ms,
            "first_fill_ms": max(0, first_fill_ts - start) if first_fill_ts else None,
            "filled_fraction": min(1.0, filled / intended),
            "terminal_state": terminal_state,
            "censored": terminal_ts <= 0 or (first_fill_ts <= 0 and terminal_state not in {"FILLED"}),
            "queue_ahead": max(0.0, number(order.get("queue_ahead"))),
            "spread": max(0.0, number(order.get("ask")) - number(order.get("bid"))),
            "side": str(order.get("side") or "UNKNOWN"),
        })
    return output


def hazard_model(
    sample: list[dict[str, Any]],
    cold_fill_prior: float,
    fill_prior_strength_orders: float = 20.0,
) -> dict[str, Any]:
    survival = 1.0
    previous = 0
    cumulative: dict[str, float] = {}
    hazards: dict[str, float] = {}
    for horizon in HORIZONS_SECONDS:
        lower_ms, upper_ms = previous * 1000, horizon * 1000
        at_risk = sum(row["exposure_ms"] >= lower_ms for row in sample)
        fills = sum(
            row["first_fill_ms"] is not None
            and lower_ms < int(row["first_fill_ms"]) <= upper_ms
            for row in sample
        )
        # Conservative beta smoothing never raises an empty bucket above the
        # configured cold prior without observed fills.
        hazard = (fills + cold_fill_prior) / (at_risk + 1.0) if at_risk else 0.0
        hazard = min(1.0, max(0.0, hazard))
        survival *= 1.0 - hazard
        hazards[str(horizon)] = hazard
        cumulative[str(horizon)] = 1.0 - survival
        previous = horizon
    filled = [row["filled_fraction"] for row in sample if row["filled_fraction"] > 0.0]
    observed_filled_fraction_mass = sum(row["filled_fraction"] for row in sample)
    prior_strength = max(1e-6, float(fill_prior_strength_orders))
    # A raw zero after a handful of right-censored PAPER orders creates an
    # absorbing state: admission estimates P(fill)=0, exploration stops, and
    # the runtime can never collect the fills needed to revise the estimate.
    # Shrink the expected filled fraction toward the declared cold prior. This
    # affects quote admission only; the pessimistic queue simulator still
    # decides whether a PAPER fill actually occurs from causal public flow.
    posterior_filled_fraction = (
        observed_filled_fraction_mass + cold_fill_prior * prior_strength
    ) / (len(sample) + prior_strength)
    return {
        "orders": len(sample),
        "filled_orders": len(filled),
        "censored_orders": sum(bool(row["censored"]) for row in sample),
        "event_clusters": len({row["event_cluster"] for row in sample}),
        "hazard_by_interval_end_seconds": hazards,
        "p_any_fill_by_seconds": cumulative,
        "observed_filled_fraction_mass": observed_filled_fraction_mass,
        "raw_expected_filled_fraction_60s": (
            observed_filled_fraction_mass / len(sample) if sample else None
        ),
        "expected_filled_fraction_60s": posterior_filled_fraction,
        "fill_prior_mean": cold_fill_prior,
        "fill_prior_strength_orders": prior_strength,
        "expected_filled_fraction_given_fill": sum(filled) / len(filled) if filled else 0.0,
        "expected_time_to_first_fill_seconds": (
            sum(int(row["first_fill_ms"]) for row in sample if row["first_fill_ms"] is not None)
            / 1000.0 / max(1, sum(row["first_fill_ms"] is not None for row in sample))
        ),
        "censoring_semantics": "cancel_or_observation_end_is_right_censored",
    }


def joint_states(values: list[dict[str, Any]]) -> dict[str, Any]:
    cycles: dict[tuple[str, int], dict[str, Any]] = defaultdict(
        lambda: {"YES": 0.0, "NO": 0.0, "BUY": 0.0, "SELL": 0.0, "pnl": 0.0}
    )
    for row in values:
        event = str(row.get("event_type") or "")
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        cluster = str(row.get("event_id") or row.get("market_id") or "UNKNOWN")
        bucket = int(row.get("recorded_ts_ms") or 0) // 60_000
        cycle = cycles[(cluster, bucket)]
        if event == "FILL":
            outcome = str(metadata.get("outcome") or "UNKNOWN").upper()
            side = str(row.get("side") or "UNKNOWN").upper()
            size = max(0.0, number(row.get("filled_size")))
            if outcome in {"YES", "NO"}:
                cycle[outcome] += size
            if side in {"BUY", "SELL"}:
                cycle[side] += size
        elif event in {"FINAL", "INVENTORY_MERGE"}:
            cycle["pnl"] += number(row.get("final_pnl", row.get("realized_cashflow")))
    counts: Counter[str] = Counter()
    pnl: defaultdict[str, float] = defaultdict(float)
    for cycle in cycles.values():
        yes, no = cycle["YES"] > 0.0, cycle["NO"] > 0.0
        buy, sell = cycle["BUY"] > 0.0, cycle["SELL"] > 0.0
        state = (
            "BOTH_YES_NO" if yes and no
            else "YES_ONLY" if yes
            else "NO_ONLY" if no
            else "NO_FILL"
        ) + ("|BID_AND_ASK" if buy and sell else "|BID" if buy else "|ASK" if sell else "|NONE")
        counts[state] += 1
        pnl[state] += cycle["pnl"]
    total = sum(counts.values())
    return {
        "source": "direct_empirical_joint_states_not_product_of_marginals",
        "uses_product_of_marginals": False,
        "cycles": total,
        "states": {
            state: {
                "n": count,
                "probability": count / total if total else 0.0,
                "mean_terminal_pnl": pnl[state] / count if count else 0.0,
            }
            for state, count in sorted(counts.items())
        },
    }


def fit_model(values: list[dict[str, Any]], *, model_sha: str, policy_hash: str,
              config_hash: str, cold_fill_prior: float,
              fill_prior_strength_orders: float = 20.0) -> dict[str, Any]:
    compatible = []
    incompatible = Counter()
    for row in values:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        reason = None
        if str(metadata.get("policy_hash") or "unknown") != policy_hash:
            reason = "policy_hash"
        elif str(metadata.get("config_hash") or "unknown") != config_hash:
            reason = "config_hash"
        elif str(metadata.get("execution_semantics_version") or "") != "maker-paper-v7.2-bilateral-inventory":
            reason = "execution_semantics_version"
        if reason:
            incompatible[reason] += 1
        else:
            compatible.append(row)
    examples = order_examples(compatible)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in examples:
        grouped[row["group"]].append(row)
    grouped["GLOBAL"] = examples
    groups: dict[str, Any] = {}
    for key, sample in sorted(grouped.items()):
        hazard = hazard_model(sample, cold_fill_prior, fill_prior_strength_orders)
        groups[key] = {
            **hazard,
            "fill_probability": hazard["expected_filled_fraction_60s"],
            "fill_probability_semantics": (
                "beta_shrunk_censored_expected_filled_fraction_per_posted_share"
                if sample else "explicit_cold_start_prior"
            ),
            "adverse_markout_per_share": 0.002,
            "mature": len(sample) >= 50
                and len({row["event_cluster"] for row in sample}) >= 12
                and int(hazard["filled_orders"]) >= 20,
            "maturity_requirements": {
                "minimum_orders": 50,
                "minimum_event_clusters": 12,
                "minimum_filled_orders": 20,
            },
        }
    if "GLOBAL" not in groups:
        groups["GLOBAL"] = {
            **hazard_model([], cold_fill_prior, fill_prior_strength_orders),
            "fill_probability": cold_fill_prior,
            "fill_probability_semantics": "explicit_cold_start_prior",
            "adverse_markout_per_share": 0.002,
            "mature": False,
        }
    timestamps = [int(row.get("recorded_ts_ms") or 0) for row in compatible]
    generated = time.time_ns() // 1_000_000
    # Sample size is diagnostic only.  This live accumulator has no untouched
    # chronological OOS window, so it must never confer economic maturity or
    # silently perform the governed challenger -> champion promotion.
    has_evidence = bool(examples)
    return {
        "schema": MODEL_SCHEMA,
        "strategy": STRATEGY,
        "family": "censored_survival_hazard_joint_cycle_v3",
        "version": generated,
        "generated_ts_ms": generated,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": model_sha,
        "code_sha": model_sha,
        "policy_hash": policy_hash,
        "config_hash": config_hash,
        "policy_version": 3,
        "hyperparameters": {
            "cold_fill_prior": cold_fill_prior,
            "fill_prior_strength_orders": fill_prior_strength_orders,
        },
        "feature_schema": {"version": 2, "fill": "censored_hazard", "joint": "direct_cycle_states"},
        "execution_semantics_version": "maker-paper-v7.2-bilateral-inventory",
        "queue_model_version": "pessimistic-public-print-v1",
        "inventory_regime": "seeded_complete_set_bilateral_v1",
        "selection_generation": "bilateral_aggressor_flow_v1",
        "artifact_role": "champion",
        "promotion_state": "PAPER_LEARNING_CHAMPION" if has_evidence else "COLD_START_CHAMPION",
        "model_state": "EVIDENCE_ACCUMULATING" if has_evidence else "COLD_START",
        "eligible_for_live_reload": True,
        "training_window": {
            "start_ts_ms": min(timestamps) if timestamps else None,
            "end_ts_ms": max(timestamps) if timestamps else None,
            "records": len(compatible),
            "orders": len(examples),
            "event_clusters": len({row["event_cluster"] for row in examples}),
        },
        "validation_window": None,
        "economically_mature": False,
        "chronological_oos_required_for_mature_promotion": True,
        "groups": groups,
        "joint_cycle_model": joint_states(compatible),
        "excluded_incompatible_records": dict(incompatible),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", action="append", type=pathlib.Path, default=[])
    parser.add_argument("--store", type=pathlib.Path, required=True)
    parser.add_argument("--store-status", type=pathlib.Path, required=True)
    parser.add_argument("--champion", type=pathlib.Path, required=True)
    parser.add_argument("--policy", type=pathlib.Path, required=True)
    parser.add_argument("--config", type=pathlib.Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--cold-fill-prior", type=float, default=0.02)
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("exact 40-hex model SHA required")
    sources = args.source_root or [pathlib.Path("runs/paper_v7_archives"), pathlib.Path("runs/paper_v7_live")]
    source_files = [path for path in jsonl_files(sources) if path.resolve() != args.store.resolve()]
    added, stored = append_new(args.store, rows(source_files))
    evidence = list(load_store(args.store).values())
    policy_hash, config_hash = fnv1a64(args.policy), fnv1a64(args.config)
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    execution = policy.get("execution_model") if isinstance(
        policy.get("execution_model"), dict
    ) else {}
    fill_prior_strength_orders = max(
        1e-6, number(execution.get("fill_prior_strength_orders"), 20.0)
    )
    model = fit_model(
        evidence, model_sha=args.model_sha, policy_hash=policy_hash,
        config_hash=config_hash, cold_fill_prior=max(1e-6, min(0.5, args.cold_fill_prior)),
        fill_prior_strength_orders=fill_prior_strength_orders,
    )
    atomic_json(args.champion, model)
    status = {
        "schema": STORE_SCHEMA,
        "timestamp_ms": time.time_ns() // 1_000_000,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": args.model_sha,
        "policy_hash": policy_hash,
        "config_hash": config_hash,
        "source_files": [str(path) for path in source_files],
        "new_records": added,
        "stored_records": stored,
        "compatible_training_records": model["training_window"]["records"],
        "model_state": model["model_state"],
        "fill_prior_strength_orders": fill_prior_strength_orders,
        "champion_path": str(args.champion),
        "champion_sha256": hashlib.sha256(args.champion.read_bytes()).hexdigest(),
    }
    atomic_json(args.store_status, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
