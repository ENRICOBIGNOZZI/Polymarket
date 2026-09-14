#!/usr/bin/env python3
"""Evaluate one preregistered PAPER Maker forward window without changing policy.

The evaluator is intended to be copied and hashed into an experiment directory
before the window begins. Before `window_end_ms` it returns only an in-progress
state and does not expose economic endpoints. After the window, it evaluates
canonical PAPER orders/fills/finals plus fill-conditioned markout evidence.

No output grants execution authority. No automatic or real-money promotion exists.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import pathlib
import random
import statistics
import time
from collections import defaultdict
from typing import Any, Iterable

SCHEMA = "polymarket_v7_maker_forward_window_report_v1"
MANIFEST_SCHEMA = "polymarket_v7_maker_forward_window_v1"
STRATEGY = "CRYPTO_SETTLEMENT_ENGINE"
COMPONENT = "professional_maker"
REQUIRED_BASIS = "FRESH_OPPOSITE_FLOW"
DEFAULT_HORIZONS = ("100ms", "250ms", "500ms", "1s", "5s", "10s", "30s")


def number(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_hash(value: dict[str, Any], self_key: str | None = None) -> str:
    payload = dict(value)
    if self_key:
        payload.pop(self_key, None)
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("forward_manifest_schema")
    sha = str(manifest.get("code_sha") or "")
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise ValueError("forward_manifest_code_sha")
    if (
        manifest.get("paper_only") is not True
        or manifest.get("authenticated_execution") is not False
        or manifest.get("real_order_submission") is not False
        or manifest.get("real_capital_at_risk") is not False
        or manifest.get("automatic_promotion") is not False
    ):
        raise ValueError("forward_manifest_authority")
    start = int(number(manifest.get("window_start_ms"), 0))
    end = int(number(manifest.get("window_end_ms"), 0))
    if start <= 0 or end <= start or end - start != 8 * 60 * 60 * 1000:
        raise ValueError("forward_manifest_window")
    evidence = manifest.get("evidence_sufficiency")
    if not isinstance(evidence, dict):
        raise ValueError("forward_manifest_evidence_gate")
    if int(number(evidence.get("minimum_independent_fill_clusters"), 0)) < 1:
        raise ValueError("forward_manifest_cluster_gate")
    if number(evidence.get("minimum_filled_shares"), 0) <= 0:
        raise ValueError("forward_manifest_share_gate")
    if str(manifest.get("required_authority_basis") or "") != REQUIRED_BASIS:
        raise ValueError("forward_manifest_authority_basis")
    horizons = manifest.get("markout_horizons")
    if not isinstance(horizons, list) or "250ms" not in {str(x) for x in horizons}:
        raise ValueError("forward_manifest_markout_horizons")
    recorded = str(manifest.get("manifest_sha256") or "")
    if recorded and recorded != canonical_hash(manifest, "manifest_sha256"):
        raise ValueError("forward_manifest_hash")
    return manifest


def iter_jsonl(path: pathlib.Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def ledger_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in iter_jsonl(path):
        identity = str(row.get("record_id") or canonical_hash(row))
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    return rows


def metadata(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("metadata") if isinstance(row.get("metadata"), dict) else {}


def canonical_maker(row: dict[str, Any], code_sha: str) -> bool:
    meta = metadata(row)
    receipt = meta.get("coordinator_receipt") if isinstance(meta.get("coordinator_receipt"), dict) else {}
    return bool(
        str(row.get("strategy") or "").upper() == STRATEGY
        and row.get("model_sha") == code_sha
        and row.get("paper_only") is True
        and row.get("authenticated_execution") is False
        and meta.get("component") == COMPONENT
        and meta.get("paper_exploration") is True
        and meta.get("economic_authority") == "PAPER_EXPLORATION"
        and meta.get("counterfactual") is not True
        and meta.get("excluded_from_portfolio_equity") is not True
        and receipt.get("owner") == "V7_GLOBAL_PORTFOLIO_COORDINATOR"
        and receipt.get("action") == "MAKE"
        and receipt.get("paper_only") is True
        and receipt.get("paper_exploration_authorized") is True
        and receipt.get("authenticated_execution") is False
        and receipt.get("real_order_submission") is False
    )


def order_time(row: dict[str, Any]) -> int:
    return int(number(row.get("recorded_ts_ms") or row.get("receive_ts_ms"), 0))


def order_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("market_id") or ""), str(row.get("order_id") or "")


def fill_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("market_id") or ""), str(row.get("fill_id") or "")


def authority_provenance(order: dict[str, Any]) -> dict[str, Any]:
    alpha = metadata(order).get("execution_alpha")
    if not isinstance(alpha, dict):
        return {}
    provenance = alpha.get("flow_provenance")
    return provenance if isinstance(provenance, dict) else {}


def entry_features(order: dict[str, Any]) -> dict[str, float]:
    meta = metadata(order)
    placement = meta.get("placement_features") if isinstance(meta.get("placement_features"), dict) else {}
    alpha = meta.get("execution_alpha") if isinstance(meta.get("execution_alpha"), dict) else {}
    alpha_features = alpha.get("features") if isinstance(alpha.get("features"), dict) else {}
    fill = alpha.get("fill_probability") if isinstance(alpha.get("fill_probability"), dict) else {}
    return {
        "microstructure_shadow_delta_250ms": number(placement.get("microstructure_shadow_delta_250ms")),
        "imbalance": number(placement.get("imbalance")),
        "ofi": number(placement.get("ofi")),
        "cancel_intensity": number(placement.get("cancel_intensity")),
        "trade_intensity": number(placement.get("trade_intensity")),
        "aggressive_sell_prints_per_second": number(placement.get("aggressive_sell_prints_per_second")),
        "short_return_ticks": number(placement.get("short_return_ticks")),
        "spread_ticks": number(placement.get("spread_ticks")),
        "distance_from_touch_ticks": number(placement.get("distance_from_touch_ticks")),
        "local_latency_ms": number(placement.get("local_latency_ms")),
        "queue_ahead": number(alpha_features.get("queue_ahead")),
        "fill_probability": number(fill.get("point")),
    }


def markout_files(root: pathlib.Path) -> list[pathlib.Path]:
    if not root.exists():
        return []
    patterns = ("*.json", "*.jsonl", "*.jsonl.gz")
    out: set[pathlib.Path] = set()
    for pattern in patterns:
        out.update(path for path in root.rglob(pattern) if path.is_file())
    return sorted(out)


def load_markouts(root: pathlib.Path, code_sha: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    seen: set[str] = set()
    for path in markout_files(root):
        values: list[dict[str, Any]] = []
        try:
            if path.suffix == ".json":
                raw = json.loads(path.read_text(encoding="utf-8"))
                values = raw if isinstance(raw, list) else [raw]
            else:
                values = list(iter_jsonl(path))
        except (OSError, json.JSONDecodeError):
            continue
        for row in values:
            if not isinstance(row, dict):
                continue
            identity = str(row.get("record_id") or canonical_hash(row))
            if identity in seen:
                continue
            seen.add(identity)
            if (
                row.get("event_type") != "MARKOUT"
                or row.get("model_sha") != code_sha
                or row.get("paper_only") is not True
                or row.get("authenticated_execution") is not False
            ):
                continue
            meta = metadata(row)
            if meta.get("fill_conditioned") is not True:
                continue
            fill_id = str(row.get("fill_id") or "")
            markouts = row.get("markouts") if isinstance(row.get("markouts"), dict) else {}
            if not fill_id:
                continue
            bucket = result.setdefault(fill_id, {})
            for horizon, value in markouts.items():
                numeric = number(value)
                if math.isfinite(numeric):
                    bucket[str(horizon)] = numeric
    return result


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def cluster_bootstrap(
    rows: list[dict[str, Any]], value_key: str, *,
    draws: int, seed: int,
) -> dict[str, Any]:
    groups: defaultdict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        value = number(row.get(value_key))
        weight = max(0.0, number(row.get("filled_shares"), 0.0))
        cluster = str(row.get("event_cluster") or "")
        if cluster and math.isfinite(value) and weight > 0:
            groups[cluster].append((value, weight))
    cluster_means = {
        name: sum(value * weight for value, weight in values) / sum(weight for _, weight in values)
        for name, values in groups.items() if sum(weight for _, weight in values) > 0
    }
    names = sorted(cluster_means)
    if not names:
        return {"clusters": 0, "bootstrap_samples": 0, "ci95": [None, None], "cluster_equal_weight_mean": None}
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(max(0, draws)):
        sample = [cluster_means[rng.choice(names)] for _ in names]
        estimates.append(sum(sample) / len(sample))
    return {
        "clusters": len(names),
        "bootstrap_samples": len(estimates),
        "cluster_equal_weight_mean": sum(cluster_means.values()) / len(cluster_means),
        "ci95": [percentile(estimates, 0.025), percentile(estimates, 0.975)],
    }


def weighted_metric(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    usable = [
        row for row in rows
        if math.isfinite(number(row.get(key))) and number(row.get("filled_shares"), 0) > 0
    ]
    shares = sum(number(row["filled_shares"], 0) for row in usable)
    value = (
        sum(number(row[key]) * number(row["filled_shares"], 0) for row in usable) / shares
        if shares > 0 else None
    )
    return {"fill_observations": len(usable), "filled_shares": shares, "value": value}


def finite_summary(values: list[float]) -> dict[str, Any]:
    usable = [value for value in values if math.isfinite(value)]
    if not usable:
        return {"count": 0, "mean": None, "median": None, "p10": None, "p90": None}
    return {
        "count": len(usable),
        "mean": sum(usable) / len(usable),
        "median": statistics.median(usable),
        "p10": percentile(usable, 0.10),
        "p90": percentile(usable, 0.90),
    }


def evaluate(
    manifest: dict[str, Any], ledger: list[dict[str, Any]],
    markouts: dict[str, dict[str, float]], *, bootstrap_draws: int, seed: int,
) -> dict[str, Any]:
    validate_manifest(manifest)
    code_sha = str(manifest["code_sha"])
    start, end = int(manifest["window_start_ms"]), int(manifest["window_end_ms"])
    maximum_tail = int(number(manifest.get("maximum_post_window_fill_ms"), 60_000))
    horizons = [str(item) for item in manifest.get("markout_horizons", DEFAULT_HORIZONS)]

    orders: dict[tuple[str, str], dict[str, Any]] = {}
    for row in ledger:
        if not canonical_maker(row, code_sha) or row.get("event_type") != "ORDER_SUBMITTED":
            continue
        timestamp = order_time(row)
        if start <= timestamp < end:
            key = order_key(row)
            if key[0] and key[1]:
                orders[key] = row

    authority_missing: list[str] = []
    authority_violations: list[str] = []
    bootstrap_probe_orders: list[str] = []
    for key, order in orders.items():
        meta = metadata(order)
        if meta.get("paper_bootstrap_probe") is True:
            bootstrap_probe_orders.append(key[1])
        provenance = authority_provenance(order)
        if not provenance:
            authority_missing.append(key[1])
            continue
        if (
            str(provenance.get("authority_basis") or "") != REQUIRED_BASIS
            or provenance.get("opposite_flow_is_fresh") is not True
        ):
            authority_violations.append(key[1])

    fills: list[dict[str, Any]] = []
    fill_ids: set[tuple[str, str]] = set()
    order_filled: set[tuple[str, str]] = set()
    for row in ledger:
        if not canonical_maker(row, code_sha) or row.get("event_type") != "FILL":
            continue
        key = order_key(row)
        if key not in orders:
            continue
        timestamp = order_time(row)
        if timestamp <= 0 or timestamp > end + maximum_tail:
            continue
        shares = number(row.get("filled_size"), 0)
        if shares <= 0:
            continue
        fid = fill_key(row)
        if not fid[1] or fid in fill_ids:
            continue
        fill_ids.add(fid)
        order_filled.add(key)
        fill_ms = timestamp
        order_ms = order_time(orders[key])
        cluster = str(row.get("event_id") or row.get("market_id") or "UNKNOWN")
        item = {
            "market_id": str(row.get("market_id") or ""),
            "event_cluster": cluster,
            "order_id": str(row.get("order_id") or ""),
            "fill_id": str(row.get("fill_id") or ""),
            "filled_shares": shares,
            "fill_price": number(row.get("fill_price")),
            "fill_delay_ms": fill_ms - order_ms,
        }
        for horizon in horizons:
            item[f"markout_{horizon}"] = number(markouts.get(item["fill_id"], {}).get(horizon))
        fills.append(item)

    finals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in ledger:
        if not canonical_maker(row, code_sha) or row.get("event_type") != "FINAL":
            continue
        key = fill_key(row)
        if key in fill_ids:
            finals[key] = row

    filled_shares = sum(row["filled_shares"] for row in fills)
    clusters = {row["event_cluster"] for row in fills}
    final_rows: list[dict[str, Any]] = []
    final_shares = 0.0
    final_pnl = 0.0
    for fill in fills:
        final = finals.get((fill["market_id"], fill["fill_id"]))
        if final is None:
            continue
        pnl = number(final.get("final_pnl"))
        if not math.isfinite(pnl):
            continue
        shares = float(fill["filled_shares"])
        final_shares += shares
        final_pnl += pnl
        final_rows.append({
            "event_cluster": fill["event_cluster"],
            "filled_shares": shares,
            "final_pnl_per_share": pnl / shares,
        })
    final_pnl_per_share = final_pnl / final_shares if final_shares > 0 else None
    final_coverage_complete = abs(final_shares - filled_shares) <= 1e-8

    markout_metrics: dict[str, Any] = {}
    markout_bootstrap: dict[str, Any] = {}
    for index, horizon in enumerate(horizons):
        key = f"markout_{horizon}"
        markout_metrics[horizon] = weighted_metric(fills, key)
        markout_bootstrap[horizon] = cluster_bootstrap(
            fills, key, draws=bootstrap_draws, seed=seed + index)

    entry = [entry_features(order) for order in orders.values()]
    entry_summary = {
        name: finite_summary([features[name] for features in entry])
        for name in entry[0] if entry
    }
    fill_delays = finite_summary([number(row.get("fill_delay_ms")) for row in fills])
    evidence = manifest["evidence_sufficiency"]
    sufficient = (
        len(clusters) >= int(evidence["minimum_independent_fill_clusters"])
        and filled_shares + 1e-12 >= float(evidence["minimum_filled_shares"])
    )
    primary = markout_metrics.get("250ms", {})
    primary_bootstrap = markout_bootstrap.get("250ms", {})
    ci = primary_bootstrap.get("ci95") or [None, None]
    lower = ci[0]
    authority_ok = not authority_missing and not authority_violations and not bootstrap_probe_orders
    markout_complete = bool(
        fills
        and primary.get("fill_observations") == len(fills)
        and abs(float(primary.get("filled_shares") or 0.0) - filled_shares) <= 1e-8
    )

    if not authority_ok:
        state = "HARD_CORRECTNESS_FAILURE"
    elif not sufficient:
        state = "INSUFFICIENT_EVIDENCE"
    elif not markout_complete:
        state = "MARKOUT_EVIDENCE_INCOMPLETE"
    elif not final_coverage_complete:
        state = "AWAITING_CANONICAL_FINAL_SETTLEMENTS"
    elif (
        primary.get("value") is not None and float(primary["value"]) > 0.0
        and lower is not None and float(lower) > 0.0
        and final_pnl_per_share is not None and float(final_pnl_per_share) > 0.0
    ):
        state = "PRIMARY_ENDPOINTS_POSITIVE_NO_AUTOMATIC_PROMOTION"
    else:
        state = "PRIMARY_ENDPOINTS_NOT_POSITIVE"

    final_bootstrap = cluster_bootstrap(
        final_rows, "final_pnl_per_share", draws=bootstrap_draws, seed=seed + 100)
    return {
        "schema": SCHEMA,
        "state": state,
        "experiment_id": str(manifest.get("experiment_id") or ""),
        "code_sha": code_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "automatic_promotion": False,
        "window_start_ms": start,
        "window_end_ms": end,
        "manifest_sha256": str(manifest.get("manifest_sha256") or canonical_hash(manifest, "manifest_sha256")),
        "metrics": {
            "submitted_orders": len(orders),
            "filled_orders": len(order_filled),
            "fill_observations": len(fills),
            "filled_shares": filled_shares,
            "independent_fill_clusters": len(clusters),
            "fill_rate_per_submitted_order": len(order_filled) / len(orders) if orders else None,
            "evidence_sufficient": sufficient,
            "authority_provenance_complete": not authority_missing,
            "authority_provenance_missing_order_ids": sorted(authority_missing),
            "authority_basis_violations": len(authority_violations),
            "authority_violation_order_ids": sorted(authority_violations),
            "bootstrap_probe_orders": len(bootstrap_probe_orders),
            "bootstrap_probe_order_ids": sorted(bootstrap_probe_orders),
            "markout_per_share": markout_metrics,
            "markout_cluster_bootstrap": markout_bootstrap,
            "fill_delay_ms": fill_delays,
            "entry_feature_summary": entry_summary,
            "canonical_final_coverage_complete": final_coverage_complete,
            "canonical_final_coverage_shares": final_shares,
            "canonical_final_pnl_total": final_pnl,
            "canonical_final_pnl_per_filled_share": final_pnl_per_share,
            "canonical_final_cluster_bootstrap": final_bootstrap,
        },
        "real_money_gate": {
            "ready": False,
            "automatic_promotion": False,
            "minimum_cumulative_independent_fill_clusters": 100,
            "minimum_positive_forward_windows": 3,
            "note": "A single window can never authorize real capital. Cumulative replicated PAPER evidence is required.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--ledger", type=pathlib.Path)
    parser.add_argument("--markout-root", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--now-ms", type=int)
    parser.add_argument("--bootstrap-draws", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=140926)
    args = parser.parse_args()
    manifest = validate_manifest(read_json(args.manifest))
    if args.validate_only:
        result = {
            "schema": SCHEMA,
            "state": "VALIDATED_PRE_FORWARD_EVALUATOR",
            "experiment_id": manifest.get("experiment_id"),
            "code_sha": manifest["code_sha"],
            "manifest_sha256": manifest.get("manifest_sha256") or canonical_hash(manifest, "manifest_sha256"),
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "automatic_promotion": False,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    now_ms = int(args.now_ms if args.now_ms is not None else time.time_ns() // 1_000_000)
    if now_ms < int(manifest["window_end_ms"]):
        result = {
            "schema": SCHEMA,
            "state": "IN_PROGRESS_NO_INTERIM_ENDPOINT_LOOK",
            "experiment_id": manifest.get("experiment_id"),
            "code_sha": manifest["code_sha"],
            "window_end_ms": manifest["window_end_ms"],
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "automatic_promotion": False,
        }
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 3
    if args.ledger is None or args.markout_root is None:
        raise SystemExit("--ledger and --markout-root are required after the window closes")
    result = evaluate(
        manifest,
        ledger_rows(args.ledger),
        load_markouts(args.markout_root, str(manifest["code_sha"])),
        bootstrap_draws=args.bootstrap_draws,
        seed=args.seed,
    )
    result["generated_at_ms"] = now_ms
    core = dict(result)
    core.pop("report_sha256", None)
    result["report_sha256"] = canonical_hash(core)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
