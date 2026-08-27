#!/usr/bin/env python3
"""Research-only selector for persistent passive binary complete-set maker leads.

This module converts one completed Fast Shadow window into a *future-window* handoff.
It never treats same-window quoted edge as fill/PnL evidence and never submits orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

BP = 1e-4


@dataclass(frozen=True)
class Observation:
    observed_ts_ms: int
    opportunity_id: str
    event_id: str
    net_edge: float
    raw_edge: float
    feed_latency_ms: float
    price_sum: float


class EvidenceError(RuntimeError):
    pass


def _finite_float(value: str, field: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"invalid_{field}") from exc
    if not math.isfinite(x):
        raise EvidenceError(f"nonfinite_{field}")
    return x


def _int(value: str, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"invalid_{field}") from exc


def _parse_binary_post_only_price_sum(legs: str) -> float:
    pieces = [p for p in (legs or "").split("|") if p]
    if len(pieces) != 2:
        raise EvidenceError("maker_complete_set_requires_two_legs")
    sides = set()
    market_ids = set()
    prices: List[float] = []
    for piece in pieces:
        fields = piece.split(":")
        if len(fields) < 7:
            raise EvidenceError("malformed_leg")
        market_ids.add(fields[0])
        side = fields[2]
        if side not in {"YES_POST_ONLY", "NO_POST_ONLY"}:
            raise EvidenceError("non_post_only_binary_leg")
        sides.add(side)
        price = _finite_float(fields[4], "leg_price")
        if not 0.0 <= price <= 1.0:
            raise EvidenceError("leg_price_out_of_range")
        prices.append(price)
    if len(market_ids) != 1 or sides != {"YES_POST_ONLY", "NO_POST_ONLY"}:
        raise EvidenceError("not_same_market_yes_no_complete_set")
    return sum(prices)


def validate_status(status: Dict[str, object]) -> None:
    if status.get("mode") != "shadow":
        raise EvidenceError("status_not_shadow")
    if bool(status.get("real_order_submission", True)):
        raise EvidenceError("real_order_submission_not_false")
    tokens = int(status.get("tokens", 0) or 0)
    ready = int(status.get("freshness_ready_tokens", 0) or 0)
    if tokens <= 0 or ready != tokens:
        raise EvidenceError("incomplete_strict_freshness_coverage")
    if int(status.get("ws_errors", 0) or 0) != 0:
        raise EvidenceError("ws_errors_nonzero")
    if int(status.get("current_stale_opportunities", 0) or 0) != 0:
        raise EvidenceError("stale_opportunities_present")


def load_authorized_min_edge(directives_path: Path) -> float:
    doc = json.loads(directives_path.read_text())
    auth = doc.get("paper_v7_authorization") or {}
    if auth.get("paper_only") is not True or auth.get("authenticated_execution") is not False:
        raise EvidenceError("unsafe_operator_directive")
    edge = float(auth.get("min_net_edge", float("nan")))
    if not math.isfinite(edge) or edge <= 0:
        raise EvidenceError("invalid_authorized_min_net_edge")
    return edge


def load_observations(csv_path: Path) -> Tuple[List[Observation], int]:
    observations: List[Observation] = []
    max_ts = 0
    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        required = {
            "observed_ts_ms", "kind", "id", "event_id", "hard_arbitrage", "executable",
            "reject_reason", "raw_edge_per_share", "net_edge_per_share", "feed_latency_ms", "legs",
        }
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise EvidenceError("missing_required_columns")
        for row in reader:
            ts = _int(row["observed_ts_ms"], "observed_ts_ms")
            max_ts = max(max_ts, ts)
            if row["kind"] != "MAKER_COMPLETE_SET_SHADOW":
                continue
            if row["hard_arbitrage"] not in {"0", "0.0", "False", "false"}:
                raise EvidenceError("maker_shadow_marked_hard_arbitrage")
            if row["executable"] not in {"1", "1.0", "True", "true"}:
                continue
            if (row.get("reject_reason") or "").strip():
                raise EvidenceError("executable_row_has_reject_reason")
            raw_edge = _finite_float(row["raw_edge_per_share"], "raw_edge_per_share")
            net_edge = _finite_float(row["net_edge_per_share"], "net_edge_per_share")
            if raw_edge + 1e-12 < net_edge:
                raise EvidenceError("net_edge_exceeds_raw_edge")
            latency = _finite_float(row["feed_latency_ms"], "feed_latency_ms")
            if latency < 0:
                raise EvidenceError("negative_feed_latency")
            price_sum = _parse_binary_post_only_price_sum(row["legs"])
            opportunity_id = row["id"].strip()
            event_id = row["event_id"].strip()
            if not opportunity_id.startswith("maker-binary:") or not event_id:
                raise EvidenceError("invalid_maker_identity")
            observations.append(Observation(ts, opportunity_id, event_id, net_edge, raw_edge, latency, price_sum))
    if max_ts <= 0:
        raise EvidenceError("empty_observation_window")
    return observations, max_ts


def build_handoff(
    observations: Iterable[Observation],
    *,
    authorized_min_edge: float,
    stress_bps: float,
    min_observations: int,
    min_span_ms: int,
    source_head_sha: str,
    source_run_id: str,
    source_artifact_id: str,
    source_window_end_ms: int,
) -> Dict[str, object]:
    if min_observations < 2 or min_span_ms <= 0 or stress_bps < 0:
        raise EvidenceError("invalid_research_frontier")
    if len(source_head_sha) != 40:
        raise EvidenceError("invalid_source_head_sha")
    groups: Dict[Tuple[str, str], List[Observation]] = {}
    for obs in observations:
        groups.setdefault((obs.opportunity_id, obs.event_id), []).append(obs)

    stress = stress_bps * BP
    candidates: List[Dict[str, object]] = []
    for (opportunity_id, event_id), rows in groups.items():
        rows.sort(key=lambda x: x.observed_ts_ms)
        first_ts = rows[0].observed_ts_ms
        last_ts = rows[-1].observed_ts_ms
        span_ms = last_ts - first_ts
        net_edges = [r.net_edge for r in rows]
        stressed_edges = [x - stress for x in net_edges]
        if len(rows) < min_observations:
            continue
        if span_ms < min_span_ms:
            continue
        if min(stressed_edges) + 1e-12 < authorized_min_edge:
            continue
        market_id = opportunity_id.split(":", 1)[1]
        candidates.append({
            "opportunity_id": opportunity_id,
            "market_id": market_id,
            "event_id": event_id,
            "observations": len(rows),
            "first_observed_ts_ms": first_ts,
            "last_observed_ts_ms": last_ts,
            "span_ms": span_ms,
            "raw_edge_min": min(r.raw_edge for r in rows),
            "net_edge_min": min(net_edges),
            "net_edge_median": statistics.median(net_edges),
            "net_edge_max": max(net_edges),
            "stressed_net_edge_min": min(stressed_edges),
            "price_sum_min": min(r.price_sum for r in rows),
            "price_sum_max": max(r.price_sum for r in rows),
            "feed_latency_ms_median": statistics.median(r.feed_latency_ms for r in rows),
            "feed_latency_ms_max": max(r.feed_latency_ms for r in rows),
        })
    candidates.sort(key=lambda x: (x["stressed_net_edge_min"], x["observations"], x["span_ms"]), reverse=True)

    return {
        "schema_version": 1,
        "state": "PERSISTENT_MAKER_COMPLETE_SET_LEADS_PRE_REGISTERED_MORE_EVIDENCE_REQUIRED",
        "research_only": True,
        "promotion_allowed": False,
        "same_window_fill_or_pnl_credit": False,
        "source": {
            "head_sha": source_head_sha,
            "run_id": str(source_run_id),
            "artifact_id": str(source_artifact_id),
            "window_end_ms": source_window_end_ms,
        },
        "frontier": {
            "authorized_min_net_edge": authorized_min_edge,
            "additional_cost_stress_bps": stress_bps,
            "min_observations": min_observations,
            "min_span_ms": min_span_ms,
            "rule": "every observed executable row must remain above the authorized edge floor after frozen extra cost stress",
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
        "prospective_not_before_ms": source_window_end_ms + 1,
        "next_test": {
            "window": "independent chronological PAPER window only",
            "actions": ["at_touch_two_sided", "selective_improve_only_if_incremental_fill_conditioned_ev_positive"],
            "required_metrics": [
                "unique_fifo_entry_fills", "paired_completion", "one_sided_partial_states",
                "cancel_latency_ms", "capital_hours", "45s_markout", "60s_markout", "300s_markout",
                "realized_post_cost_fill_conditioned_pnl", "frozen_trade_cost_stress_1x_1p5x_2x",
            ],
            "decision": "no alpha claim unless realized audited PAPER economics are positive",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--opportunities", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--operator-directives", type=Path, default=Path("config/operator_directives.json"))
    parser.add_argument("--source-head-sha", required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--source-artifact-id", required=True)
    parser.add_argument("--stress-bps", type=float, default=10.0)
    parser.add_argument("--min-observations", type=int, default=10)
    parser.add_argument("--min-span-ms", type=int, default=20_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    status = json.loads(args.status.read_text())
    validate_status(status)
    min_edge = load_authorized_min_edge(args.operator_directives)
    observations, max_ts = load_observations(args.opportunities)
    report = build_handoff(
        observations,
        authorized_min_edge=min_edge,
        stress_bps=args.stress_bps,
        min_observations=args.min_observations,
        min_span_ms=args.min_span_ms,
        source_head_sha=args.source_head_sha,
        source_run_id=args.source_run_id,
        source_artifact_id=args.source_artifact_id,
        source_window_end_ms=max(max_ts, int(status.get("timestamp_ms", 0) or 0)),
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
