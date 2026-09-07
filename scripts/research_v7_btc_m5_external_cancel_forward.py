#!/usr/bin/env python3
"""Read-only forward evaluator for the frozen BTC M5 maker external-cancel experiment."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "polymarket_v7_btc_m5_external_cancel_forward_report_v1"
EPISODE_SCHEMA = "polymarket_v7_btc_m5_external_cancel_forward_episode_v1"
DEFAULT_EXPERIMENT_ID = "btc-m5-external-cancel-overlay-forward-v1"
PRIMARY_HORIZON_MS = 500
STRESS_KEY = "queue_3x_cancel_200ms"
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class EvidenceError(ValueError):
    pass


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EvidenceError(f"not_an_object:{path}")
    return value


def finite(value: Any, *, field: str) -> float:
    if value is None or isinstance(value, bool):
        raise EvidenceError(f"nonfinite:{field}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceError(f"nonfinite:{field}") from exc
    if not math.isfinite(number):
        raise EvidenceError(f"nonfinite:{field}")
    return number


def utc_ms(value: str) -> int:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError("freeze_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceError("freeze_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise EvidenceError("freeze_timestamp_invalid")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def canonical_rule_hash(rule: dict[str, Any]) -> str:
    raw = json.dumps(rule, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_experiment(registry_path: Path, experiment_id: str) -> dict[str, Any]:
    registry = load_object(registry_path)
    if (
        registry.get("schema") != "polymarket_v7_maker_fillability_experiment_registry_v1"
        or registry.get("paper_only") is not True
        or registry.get("real_order_submission") is not False
    ):
        raise EvidenceError("unsafe_registry")
    rows = registry.get("experiments")
    if not isinstance(rows, list):
        raise EvidenceError("experiment_registry_missing")
    experiment = next(
        (row for row in rows if isinstance(row, dict) and row.get("experiment_id") == experiment_id),
        None,
    )
    if experiment is None:
        raise EvidenceError("experiment_not_found")
    if (
        experiment.get("execution_authority") != "RESEARCH_ZERO_AUTHORITY"
        or experiment.get("quote_submission") is not False
        or experiment.get("promotion_credit") is not False
        or experiment.get("real_money_authority") is not False
        or experiment.get("automatic_promotion") is not False
        or experiment.get("freeze_boundary_strictly_after") is not True
        or not SHA40.fullmatch(str(experiment.get("freeze_merge_sha") or ""))
    ):
        raise EvidenceError("unsafe_or_unfrozen_experiment")
    rule = experiment.get("frozen_rule")
    if not isinstance(rule, dict) or rule.get("retuning_after_freeze") is not False:
        raise EvidenceError("frozen_rule_invalid")
    return experiment


def iter_jsonl(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EvidenceError(f"invalid_json:{path}:{line_number}") from exc
                if not isinstance(row, dict):
                    raise EvidenceError(f"invalid_row:{path}:{line_number}")
                yield row


def markout(row: dict[str, Any], arm: str, horizon_ms: int) -> float:
    filled = row.get(f"{arm}_fill")
    if filled is not True:
        return 0.0
    values = row.get(f"{arm}_markout_per_share")
    if not isinstance(values, dict):
        raise EvidenceError(f"missing_{arm}_markout")
    key = str(horizon_ms)
    return finite(values.get(key), field=f"{arm}_markout_{key}")


def stress_markout(row: dict[str, Any], arm: str) -> float:
    stress = row.get("stress")
    if not isinstance(stress, dict) or not isinstance(stress.get(STRESS_KEY), dict):
        raise EvidenceError("stress_missing")
    value = stress[STRESS_KEY]
    filled = value.get(f"{arm}_fill")
    if filled is not True:
        return 0.0
    marks = value.get(f"{arm}_markout_per_share")
    if not isinstance(marks, dict):
        raise EvidenceError(f"stress_{arm}_markout_missing")
    return finite(marks.get(str(PRIMARY_HORIZON_MS)), field=f"stress_{arm}_500")


def validate_episode(
    row: dict[str, Any], *, freeze_ms: int, expected_rule_hash: str,
    quote_size: float, book_schema: int,
) -> None:
    if row.get("schema") != EPISODE_SCHEMA:
        raise EvidenceError("episode_schema")
    market_id = str(row.get("market_id") or "")
    quote_id = str(row.get("quote_id") or "")
    if not market_id or not quote_id:
        raise EvidenceError("episode_identity")
    quote_ms = row.get("quote_receive_ms")
    if isinstance(quote_ms, bool) or not isinstance(quote_ms, int) or quote_ms <= freeze_ms:
        raise EvidenceError(f"not_strictly_forward:{market_id}:{quote_id}")
    published_ms = row.get("maker_model_published_ms")
    if (
        isinstance(published_ms, bool) or not isinstance(published_ms, int)
        or published_ms <= 0 or published_ms >= quote_ms
        or not SHA40.fullmatch(str(row.get("maker_model_sha") or ""))
    ):
        raise EvidenceError(f"maker_model_lookahead:{market_id}:{quote_id}")
    if row.get("rule_sha256") != expected_rule_hash:
        raise EvidenceError(f"rule_hash_drift:{market_id}:{quote_id}")
    if row.get("book_tape_schema") != book_schema or row.get("receive_time_causal") is not True:
        raise EvidenceError(f"causality_contract:{market_id}:{quote_id}")
    violations = row.get("causality_violations")
    if violations not in (None, []):
        raise EvidenceError(f"causality_violation:{market_id}:{quote_id}")
    if row.get("trigger_applied") is not True:
        raise EvidenceError(f"nontrigger_episode:{market_id}:{quote_id}")
    if abs(finite(row.get("quote_size_shares"), field="quote_size_shares") - quote_size) > 1e-9:
        raise EvidenceError(f"quote_size_drift:{market_id}:{quote_id}")
    if row.get("overlay_fill") is True and row.get("baseline_fill") is not True:
        raise EvidenceError(f"overlay_created_fill:{market_id}:{quote_id}")
    # Validate all primary horizons now so missing labels can never disappear
    # from one metric while remaining in another.
    for horizon in (250, 500, 1000):
        markout(row, "baseline", horizon)
        markout(row, "overlay", horizon)
    if (
        isinstance(row.get("stress"), dict)
        and isinstance(row["stress"].get(STRESS_KEY), dict)
        and row["stress"][STRESS_KEY].get("overlay_fill") is True
        and row["stress"][STRESS_KEY].get("baseline_fill") is not True
    ):
        raise EvidenceError(f"stress_overlay_created_fill:{market_id}:{quote_id}")
    stress_markout(row, "baseline")
    stress_markout(row, "overlay")


def cluster_bootstrap(values: list[float], *, seed: int, draws: int = 10_000) -> list[float | None]:
    if not values:
        return [None, None]
    if len(values) == 1:
        return [values[0], values[0]]
    rng = random.Random(seed)
    samples = sorted(
        sum(rng.choice(values) for _ in values) / len(values)
        for _ in range(draws)
    )
    return [samples[int(0.025 * draws)], samples[int(0.975 * draws) - 1]]


def evaluate(
    registry_path: Path, episode_paths: list[Path], *,
    experiment_id: str = DEFAULT_EXPERIMENT_ID,
) -> dict[str, Any]:
    experiment = load_experiment(registry_path, experiment_id)
    rule = experiment["frozen_rule"]
    expected_rule_hash = canonical_rule_hash(rule)
    freeze_ms = utc_ms(str(experiment["start_time"]))
    minimum = experiment.get("minimum_evidence")
    if not isinstance(minimum, dict):
        raise EvidenceError("minimum_evidence_missing")
    min_markets = int(minimum.get("independent_markets_with_triggers") or 0)
    min_avoidable = int(minimum.get("avoidable_fill_events") or 0)
    quote_size = finite(minimum.get("quote_size_shares"), field="minimum_quote_size")
    book_schema = int(minimum.get("require_exact_book_tape_schema") or 0)
    if min_markets <= 0 or min_avoidable <= 0 or quote_size <= 0 or book_schema <= 0:
        raise EvidenceError("minimum_evidence_invalid")

    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for row in iter_jsonl(episode_paths):
        validate_episode(
            row, freeze_ms=freeze_ms, expected_rule_hash=expected_rule_hash,
            quote_size=quote_size, book_schema=book_schema,
        )
        identity = (str(row["market_id"]), str(row["quote_id"]))
        if identity in seen:
            raise EvidenceError(f"duplicate_episode:{identity[0]}:{identity[1]}")
        seen.add(identity)
        by_market[identity[0]].append(row)

    market_rows: list[dict[str, Any]] = []
    avoidable = 0
    for market_id, rows in sorted(by_market.items()):
        primary_episode: list[float] = []
        stress_episode: list[float] = []
        market_avoidable = 0
        for row in rows:
            primary_episode.append(
                markout(row, "overlay", PRIMARY_HORIZON_MS)
                - markout(row, "baseline", PRIMARY_HORIZON_MS)
            )
            stress_episode.append(stress_markout(row, "overlay") - stress_markout(row, "baseline"))
            if row.get("baseline_fill") is True and row.get("overlay_fill") is not True:
                market_avoidable += 1
        avoidable += market_avoidable
        market_rows.append({
            "market_id": market_id,
            "episodes": len(rows),
            "avoidable_fill_events": market_avoidable,
            "primary_500ms_improvement_per_share": sum(primary_episode) / len(primary_episode),
            "stress_3x_queue_200ms_cancel_improvement_per_share": sum(stress_episode) / len(stress_episode),
        })

    primary_values = [row["primary_500ms_improvement_per_share"] for row in market_rows]
    stress_values = [row["stress_3x_queue_200ms_cancel_improvement_per_share"] for row in market_rows]
    equal_weight = sum(primary_values) / len(primary_values) if primary_values else None
    stress_equal_weight = sum(stress_values) / len(stress_values) if stress_values else None
    positive_fraction = (
        sum(value > 0.0 for value in primary_values) / len(primary_values)
        if primary_values else None
    )
    if len(primary_values) >= 2:
        best_index = max(range(len(primary_values)), key=primary_values.__getitem__)
        leave_best = sum(
            value for index, value in enumerate(primary_values) if index != best_index
        ) / (len(primary_values) - 1)
        best_market = market_rows[best_index]["market_id"]
    else:
        leave_best = None
        best_market = market_rows[0]["market_id"] if market_rows else None

    reasons: list[str] = []
    if len(market_rows) < min_markets:
        reasons.append("INSUFFICIENT_INDEPENDENT_MARKETS")
    if avoidable < min_avoidable:
        reasons.append("INSUFFICIENT_AVOIDABLE_FILL_EVENTS")
    enough = not reasons
    if enough:
        if equal_weight is None or equal_weight <= 0.0:
            reasons.append("PRIMARY_500MS_IMPROVEMENT_NOT_POSITIVE")
        if leave_best is None or leave_best <= 0.0:
            reasons.append("LEAVE_BEST_MARKET_OUT_NOT_POSITIVE")
        if positive_fraction is None or positive_fraction < 0.70:
            reasons.append("POSITIVE_MARKET_FRACTION_BELOW_70PCT")
        if stress_equal_weight is None or stress_equal_weight <= 0.0:
            reasons.append("STRESS_3X_QUEUE_200MS_CANCEL_NOT_POSITIVE")

    state = (
        "FORWARD_EVIDENCE_INSUFFICIENT" if not enough
        else "PASS" if not reasons
        else "FAIL"
    )
    seed = int(expected_rule_hash[:16], 16)
    return {
        "schema": SCHEMA,
        "experiment_id": experiment_id,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_money_authority": False,
        "automatic_promotion": False,
        "state": state,
        "freeze_merge_sha": experiment["freeze_merge_sha"],
        "freeze_timestamp_utc": experiment["freeze_merge_timestamp_utc"],
        "freeze_boundary_ms": freeze_ms,
        "rule_sha256": expected_rule_hash,
        "market_count": len(market_rows),
        "episode_count": len(seen),
        "avoidable_fill_events": avoidable,
        "minimum_markets": min_markets,
        "minimum_avoidable_fill_events": min_avoidable,
        "equal_weight_500ms_improvement_per_share": equal_weight,
        "leave_best_market_out_500ms_improvement_per_share": leave_best,
        "best_market_id": best_market,
        "positive_market_fraction": positive_fraction,
        "stress_3x_queue_200ms_cancel_improvement_per_share": stress_equal_weight,
        "bootstrap95_market_cluster_500ms_improvement": cluster_bootstrap(primary_values, seed=seed),
        "reason_codes": sorted(set(reasons)),
        "markets": market_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path("config/v7_maker_fillability_experiments.json"))
    parser.add_argument("--episodes", type=Path, nargs="+", required=True)
    parser.add_argument("--experiment-id", default=DEFAULT_EXPERIMENT_ID)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = evaluate(args.registry, args.episodes, experiment_id=args.experiment_id)
    except (OSError, json.JSONDecodeError, EvidenceError) as exc:
        print(f"v7_btc_m5_external_cancel_forward: {exc}", file=__import__("sys").stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "state", "market_count", "episode_count", "avoidable_fill_events",
        "equal_weight_500ms_improvement_per_share",
        "leave_best_market_out_500ms_improvement_per_share",
        "positive_market_fraction",
        "stress_3x_queue_200ms_cancel_improvement_per_share",
        "reason_codes",
    )}, sort_keys=True))
    return 1 if report["state"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
