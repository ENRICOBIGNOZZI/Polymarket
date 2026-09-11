#!/usr/bin/env python3
"""Score the frozen short-horizon PM repricing model on live causal origins.

Research only. This observer has no OMS, cancel, capital, inventory, or ledger
authority. It reuses the exact frozen model transform from the incremental
benchmark and records whether a BUY quote would face an adverse >= N-tick PM
repricing at the requested horizon.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_causal_book import BookTimeline
from v7_compressed_journal import CompressedJournal
from v7_external_lead_lag_collector import load, valid_origin, valid_router_live
from v7_external_rich_model import FEATURE_SCHEMA, logit
from v7_pm_repricing_incremental_benchmark import predict

SCHEMA = "polymarket_v7_pm_repricing_shadow_v1"
STATUS_SCHEMA = "polymarket_v7_pm_repricing_shadow_status_v1"
ARTIFACT_SCHEMA = "polymarket_v7_pm_repricing_incremental_benchmark_v1"
FROZEN_ARTIFACT_SCHEMA = "polymarket_v7_pm_repricing_shadow_artifact_v1"
CAUSAL_SCHEMA = "polymarket_v7_model_independent_causal_observation_v1"
MAX_ORIGIN_WAIT_NS = 500_000_000


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def exact_hex(value: str, length: int) -> bool:
    return len(value) == length and all(ch in "0123456789abcdef" for ch in value)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_model(path: Path, expected_sha256: str, horizon_ms: int, family: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not exact_hex(expected_sha256, 64) or file_sha256(path) != expected_sha256:
        raise ValueError("repricing_shadow:artifact_hash_mismatch")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") not in {ARTIFACT_SCHEMA, FROZEN_ARTIFACT_SCHEMA}
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"
        or value.get("automatic_promotion") is not False
    ):
        raise ValueError("repricing_shadow:artifact_safety_contract")
    code_sha = str(value.get("code_sha") or value.get("source_code_sha") or "")
    if not exact_hex(code_sha, 40):
        raise ValueError("repricing_shadow:artifact_code_sha_missing")
    if value.get("schema") == FROZEN_ARTIFACT_SCHEMA:
        source_sha = str(value.get("source_artifact_sha256") or "")
        if not exact_hex(source_sha, 64):
            raise ValueError("repricing_shadow:source_artifact_hash_missing")
    models = value.get("models") if isinstance(value.get("models"), dict) else {}
    by_horizon = models.get(str(horizon_ms)) if isinstance(models.get(str(horizon_ms)), dict) else {}
    spec = by_horizon.get(family) if isinstance(by_horizon.get(family), dict) else None
    if spec is None or spec.get("family") != family:
        raise ValueError("repricing_shadow:model_missing")
    coefficients = spec.get("coefficients")
    names = spec.get("feature_names")
    if not isinstance(coefficients, list) or not isinstance(names, list) or len(coefficients) != 1 + 2 * len(names):
        raise ValueError("repricing_shadow:model_shape")
    return value, spec


def logistic(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -60.0))
    return z / (1.0 + z)


def token_tick(cuts: list[dict[str, Any]], token_id: str) -> float | None:
    for row in cuts:
        if isinstance(row, dict) and str(row.get("token_id") or "") == token_id:
            try:
                value = float(row.get("tick_size"))
            except (TypeError, ValueError, OverflowError):
                return None
            return value if math.isfinite(value) and 0 < value < 1 else None
    return None


def score_origin(origin: dict[str, Any], evidence: dict[str, Any], spec: dict[str, Any], *,
                 artifact_sha256: str, artifact_code_sha: str, runtime_sha: str,
                 horizon_ms: int, family: str, threshold_ticks: float,
                 scored_wall_ns: int) -> dict[str, Any]:
    cuts = evidence.get("origin_book_cuts")
    if not isinstance(cuts, list) or len(cuts) != 2:
        raise ValueError("repricing_shadow:origin_book_cut_missing")
    row = {**origin, **evidence}
    prediction = predict(row, spec)
    if prediction is None or not math.isfinite(float(prediction)):
        raise ValueError("repricing_shadow:prediction_missing")
    p0 = float(evidence["origin_pm_yes"])
    p1 = logistic(logit(p0) + float(prediction))
    yes_tick = token_tick(cuts, str(origin.get("yes_token") or ""))
    no_tick = token_tick(cuts, str(origin.get("no_token") or ""))
    if yes_tick is None or no_tick is None:
        raise ValueError("repricing_shadow:tick_missing")
    delta = p1 - p0
    adverse_yes_ticks = max(0.0, -delta / yes_tick)
    adverse_no_ticks = max(0.0, delta / no_tick)
    return {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_money_authority": False,
        "research_only": True,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "runtime_model_sha": runtime_sha,
        "artifact_sha256": artifact_sha256,
        "artifact_code_sha": artifact_code_sha,
        "family": family,
        "horizon_ms": horizon_ms,
        "threshold_ticks": threshold_ticks,
        "market_id": origin["market_id"],
        "origin_id": origin["origin_id"],
        "origin_observed_wall_ns": int(origin["origin_observed_wall_ns"]),
        "scored_wall_ns": scored_wall_ns,
        "inference_age_ns": max(0, scored_wall_ns - int(origin["origin_observed_wall_ns"])),
        "origin_pm_yes": p0,
        "predicted_pm_yes": p1,
        "predicted_delta_logit": float(prediction),
        "predicted_delta_probability": delta,
        "yes_tick_size": yes_tick,
        "no_tick_size": no_tick,
        "adverse_yes_ticks": adverse_yes_ticks,
        "adverse_no_ticks": adverse_no_ticks,
        "would_veto_yes_buy": adverse_yes_ticks + 1e-12 >= threshold_ticks,
        "would_veto_no_buy": adverse_no_ticks + 1e-12 >= threshold_ticks,
        "origin_pm_snapshot_id": evidence.get("origin_pm_snapshot_id"),
        "observer_session_id": evidence.get("observer_session_id"),
        "connection_epoch": evidence.get("connection_epoch"),
        "rich_feature_sha256": origin.get("rich_feature_sha256"),
        "feature_schema_version": origin.get("feature_schema_version"),
    }


class Shadow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.artifact, self.spec = load_model(args.artifact, args.artifact_sha256, args.horizon_ms, args.family)
        self.book = BookTimeline(args.book_tape, args.model_sha, retention_ms=10_000)
        self.journal = CompressedJournal(args.output, args.maximum_hot_bytes)
        self.pending: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.last_origin_id = ""
        self.scored = self.censored = self.invalid = 0
        self.last_record: dict[str, Any] | None = None
        self.started_ns = time.time_ns()

    def tick(self) -> None:
        self.book.poll()
        router = load(self.args.router_status)
        live = valid_router_live(router, self.args.model_sha)
        fair = load(self.args.fair_status)
        origin = valid_origin(fair, live, self.args.model_sha) if live else None
        if origin is not None and origin["origin_id"] != self.last_origin_id:
            if (origin.get("causal_observation_schema") != CAUSAL_SCHEMA
                    or origin.get("feature_schema_version") != FEATURE_SCHEMA):
                self.invalid += 1
            else:
                self.pending[origin["origin_id"]] = origin
                if len(self.pending) > 5000:
                    self.pending.popitem(last=False)
            self.last_origin_id = origin["origin_id"]

        book_status = load(self.args.book_status)
        now_ns = time.time_ns()
        for origin_id, row in list(self.pending.items()):
            origin_ms = int(row["origin_observed_wall_ns"]) / 1_000_000.0
            evidence = self.book.label(
                row["market_id"], row["yes_token"], row["no_token"],
                origin_ms, origin_ms, book_status,
            )
            if evidence is None:
                if now_ns - int(row["origin_observed_wall_ns"]) > MAX_ORIGIN_WAIT_NS:
                    self.censored += 1
                    del self.pending[origin_id]
                continue
            try:
                record = score_origin(
                    row, evidence, self.spec,
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
            self.journal.append(record)
            self.last_record = record
            self.scored += 1
            del self.pending[origin_id]

    def publish(self) -> None:
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
            "started_ns": self.started_ns,
            "timestamp_ns": time.time_ns(),
            "scored_origins": self.scored,
            "pending_origins": len(self.pending),
            "censored_origins": self.censored,
            "invalid_origins": self.invalid,
            "book_timeline_gaps": self.book.gaps,
            "last_record": self.last_record,
            "state": "COLLECTING" if self.scored else "AWAITING_CAUSAL_ORIGIN",
        })

    def run(self) -> None:
        next_status = 0.0
        while True:
            self.tick()
            now = time.monotonic()
            if now >= next_status:
                self.publish()
                next_status = now + 1.0
            time.sleep(max(0.005, self.args.interval_ms / 1000.0))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fair-status", type=Path, required=True)
    ap.add_argument("--router-status", type=Path, required=True)
    ap.add_argument("--book-tape", type=Path, required=True)
    ap.add_argument("--book-status", type=Path, required=True)
    ap.add_argument("--artifact", type=Path, required=True)
    ap.add_argument("--artifact-sha256", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--family", default="PM_PLUS_EXTERNAL")
    ap.add_argument("--horizon-ms", type=int, default=250)
    ap.add_argument("--threshold-ticks", type=float, default=1.0)
    ap.add_argument("--interval-ms", type=int, default=25)
    ap.add_argument("--maximum-hot-bytes", type=int, default=64 * 1024**2)
    args = ap.parse_args()
    if not exact_hex(args.model_sha, 40):
        raise SystemExit("invalid --model-sha")
    if args.horizon_ms not in (100, 250, 500, 1000) or args.family not in ("PM_MICRO_ONLY", "EXTERNAL_ONLY", "PM_PLUS_EXTERNAL"):
        raise SystemExit("invalid model selection")
    if not math.isfinite(args.threshold_ticks) or args.threshold_ticks <= 0:
        raise SystemExit("invalid --threshold-ticks")
    Shadow(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
