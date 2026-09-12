#!/usr/bin/env python3
"""Low-latency zero-authority PM repricing inference.

Inference never waits for a +horizon label or a status watermark covering that
label. It only consumes causal book cuts already at or before the frozen origin.
Late inference is recorded but cannot create a cancel-veto event.
"""
from __future__ import annotations

import argparse
from collections import deque
import math
from pathlib import Path
import time
from typing import Any

from v7_causal_book import BookTimeline
from v7_compressed_journal import CompressedJournal
from v7_external_lead_lag_collector import load, valid_origin, valid_router_live
from v7_pm_repricing_common import load_model, score_origin, atomic_json

SCHEMA = "polymarket_v7_pm_repricing_shadow_v1"
STATUS_SCHEMA = "polymarket_v7_pm_repricing_shadow_status_v1"


def causal_origin_evidence(book: BookTimeline, origin: dict[str, Any], *, max_book_age_ms: int) -> dict[str, Any] | None:
    origin_ms = int(origin["origin_observed_wall_ns"]) / 1_000_000.0
    cuts = [book.asof(origin["market_id"], token, origin_ms) for token in (origin["yes_token"], origin["no_token"])]
    if any(cut is None for cut in cuts):
        return None
    newest = max(int(cut["receive_wall_ms"]) for cut in cuts)
    oldest = min(int(cut["receive_wall_ms"]) for cut in cuts)
    if origin_ms - oldest > max_book_age_ms + 1e-9:
        return None
    try:
        yes_mid = 0.5 * (float(cuts[0]["best_bid"]) + float(cuts[0]["best_ask"]))
        no_mid = 0.5 * (float(cuts[1]["best_bid"]) + float(cuts[1]["best_ask"]))
        tolerance = max(float(cut["tick_size"]) for cut in cuts)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not (0.0 < tolerance < 1.0) or abs(yes_mid + no_mid - 1.0) > 2.0 * tolerance + 1e-9:
        return None
    return {
        "origin_pm_yes": float(origin["origin_pm_yes"]),
        "origin_pm_snapshot_id": origin.get("origin_pm_snapshot_id"),
        "origin_book_cuts": cuts,
        "book_cut_newest_receive_ms": newest,
        "book_cut_oldest_receive_ms": oldest,
    }


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] if lo == hi else ordered[lo] * (hi - position) + ordered[hi] * (position - lo)


class FastShadow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.artifact, self.spec = load_model(args.artifact, args.artifact_sha256, args.horizon_ms, args.family)
        self.book = BookTimeline(args.book_tape, args.model_sha, retention_ms=10_000)
        self.journal = CompressedJournal(args.output, args.maximum_hot_bytes)
        self.last_origin_id = ""
        self.pending: dict[str, tuple[dict[str, Any], int]] = {}
        self.origins = self.scored = self.late = self.no_book = self.invalid = 0
        self.raw_vetoes = self.timely_vetoes = 0
        self.latencies_ms: deque[float] = deque(maxlen=100_000)
        self.started_ns = time.time_ns()
        self.last_record: dict[str, Any] | None = None

    def observe_origin(self) -> None:
        router = load(self.args.router_status)
        live = valid_router_live(router, self.args.model_sha)
        fair = load(self.args.fair_status)
        origin = valid_origin(fair, live, self.args.model_sha) if live else None
        if origin is None or origin["origin_id"] == self.last_origin_id:
            return
        self.last_origin_id = origin["origin_id"]
        self.origins += 1
        self.pending[origin["origin_id"]] = (origin, time.time_ns())

    def score_pending(self) -> None:
        self.book.poll()
        now_ns = time.time_ns()
        for origin_id, (origin, discovered_ns) in list(self.pending.items()):
            evidence = causal_origin_evidence(self.book, origin, max_book_age_ms=self.args.max_book_age_ms)
            age_ns = max(0, now_ns - int(origin["origin_observed_wall_ns"]))
            if evidence is None:
                if now_ns - discovered_ns >= self.args.max_book_wait_ms * 1_000_000:
                    self.no_book += 1
                    del self.pending[origin_id]
                continue
            try:
                record = score_origin(
                    origin, evidence, self.spec,
                    artifact_sha256=self.args.artifact_sha256,
                    artifact_code_sha=str(self.artifact.get("code_sha") or self.artifact.get("source_code_sha") or ""),
                    runtime_sha=self.args.model_sha,
                    horizon_ms=self.args.horizon_ms,
                    family=self.args.family,
                    threshold_ticks=self.args.threshold_ticks,
                    scored_wall_ns=now_ns,
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                self.invalid += 1
                del self.pending[origin_id]
                continue
            raw_yes = record.get("would_veto_yes_buy") is True
            raw_no = record.get("would_veto_no_buy") is True
            timely = age_ns <= self.args.latency_gate_ms * 1_000_000
            if raw_yes or raw_no:
                self.raw_vetoes += 1
            if not timely:
                self.late += 1
                record["would_veto_yes_buy"] = False
                record["would_veto_no_buy"] = False
            elif raw_yes or raw_no:
                self.timely_vetoes += 1
            record.update({
                "inference_phase": "ORIGIN_ONLY_NO_FUTURE_LABEL",
                "origin_discovered_wall_ns": discovered_ns,
                "origin_to_discovery_ns": max(0, discovered_ns - int(origin["origin_observed_wall_ns"])),
                "latency_gate_ms": self.args.latency_gate_ms,
                "latency_gate_pass": timely,
                "raw_would_veto_yes_buy": raw_yes,
                "raw_would_veto_no_buy": raw_no,
                "yes_token": origin["yes_token"],
                "no_token": origin["no_token"],
                "book_cut_newest_receive_ms": evidence["book_cut_newest_receive_ms"],
                "book_cut_oldest_receive_ms": evidence["book_cut_oldest_receive_ms"],
            })
            self.journal.append(record)
            self.last_record = record
            self.scored += 1
            self.latencies_ms.append(age_ns / 1_000_000.0)
            del self.pending[origin_id]

    def publish(self) -> None:
        values = list(self.latencies_ms)
        atomic_json(self.args.status, {
            "schema": STATUS_SCHEMA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "automatic_promotion": False,
            "runtime_model_sha": self.args.model_sha,
            "artifact_sha256": self.args.artifact_sha256,
            "artifact_code_sha": self.artifact.get("code_sha") or self.artifact.get("source_code_sha"),
            "family": self.args.family,
            "horizon_ms": self.args.horizon_ms,
            "threshold_ticks": self.args.threshold_ticks,
            "latency_gate_ms": self.args.latency_gate_ms,
            "started_ns": self.started_ns,
            "timestamp_ns": time.time_ns(),
            "origins_seen": self.origins,
            "scored_origins": self.scored,
            "pending_origins": len(self.pending),
            "book_unavailable_origins": self.no_book,
            "invalid_origins": self.invalid,
            "late_inferences": self.late,
            "raw_veto_origins": self.raw_vetoes,
            "timely_veto_origins": self.timely_vetoes,
            "score_coverage": self.scored / self.origins if self.origins else None,
            "timely_score_coverage": (self.scored - self.late) / self.origins if self.origins else None,
            "inference_age_ms": {
                "p50": quantile(values, 0.50),
                "p90": quantile(values, 0.90),
                "p99": quantile(values, 0.99),
                "max": max(values) if values else None,
            },
            "latency_target": {"p50_lt_ms": 25, "p99_lt_ms": 50, "coverage_gt": 0.90},
            "inference_semantics": "ORIGIN_ONLY_LABELING_SEPARATE",
            "last_record": self.last_record,
            "state": "COLLECTING" if self.scored else "AWAITING_CAUSAL_ORIGIN",
        })

    def run(self) -> None:
        next_status = 0.0
        while True:
            self.observe_origin()
            self.score_pending()
            now = time.monotonic()
            if now >= next_status:
                self.publish()
                next_status = now + 1.0
            time.sleep(max(0.001, self.args.interval_ms / 1000.0))
