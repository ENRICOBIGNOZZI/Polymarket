#!/usr/bin/env python3
"""Zero-authority forward shadow for the two-feature 250ms PM microstructure model."""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

from v7_causal_book import BookTimeline
from v7_compressed_journal import CompressedJournal, journal_rows
from v7_external_lead_lag_collector import load
from v7_pm_repricing_common import atomic_json, exact_hex, file_sha256

SCHEMA = "polymarket_v7_microstructure_pm_delta_shadow_v1"
STATUS_SCHEMA = "polymarket_v7_microstructure_pm_delta_shadow_status_v1"
ARTIFACT_SCHEMA = "polymarket_v7_microstructure_pm_delta_two_feature_v1"
FEATURE_NAMES = ("depth_imbalance_l1", "book_imbalance_l5")


def stable(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] if lo == hi else ordered[lo] * (hi - position) + ordered[hi] * (position - lo)


def _artifact_model_hash(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("model_hash", None)
    payload.pop("generated_at_ns", None)
    return hashlib.sha256(stable(payload).encode()).hexdigest()


def load_artifact(path: Path, expected_file_sha256: str, expected_model_hash: str) -> dict[str, Any]:
    if not exact_hex(expected_file_sha256, 64) or file_sha256(path) != expected_file_sha256:
        raise ValueError("microstructure_shadow:artifact_file_hash_mismatch")
    if not exact_hex(expected_model_hash, 64):
        raise ValueError("microstructure_shadow:model_hash_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != ARTIFACT_SCHEMA
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"
        or value.get("automatic_promotion") is not False
        or value.get("target") != "PM_YES_DELTA_250MS_CAUSAL_BOOK"
        or int(value.get("horizon_ms") or 0) != 250
        or value.get("feature_names") != list(FEATURE_NAMES)
        or value.get("model_hash") != expected_model_hash
        or _artifact_model_hash(value) != expected_model_hash
    ):
        raise ValueError("microstructure_shadow:artifact_contract")
    coefficients = value.get("coefficients_raw")
    slopes = coefficients.get("slopes") if isinstance(coefficients, dict) else None
    if (
        not isinstance(slopes, dict)
        or set(slopes) != set(FEATURE_NAMES)
        or not all(math.isfinite(float(slopes[name])) for name in FEATURE_NAMES)
        or not math.isfinite(float(coefficients.get("intercept")))
    ):
        raise ValueError("microstructure_shadow:coefficients")
    return value


def _finite(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite")
    return number


def paired_probability(yes_cut: dict[str, Any], no_cut: dict[str, Any]) -> float:
    yes_mid = 0.5 * (_finite(yes_cut["best_bid"]) + _finite(yes_cut["best_ask"]))
    no_mid = 0.5 * (_finite(no_cut["best_bid"]) + _finite(no_cut["best_ask"]))
    tolerance = max(_finite(yes_cut["tick_size"]), _finite(no_cut["tick_size"]))
    if not 0.0 < tolerance < 1.0 or abs(yes_mid + no_mid - 1.0) > 2.0 * tolerance + 1e-9:
        raise ValueError("complement_inconsistent")
    return (yes_mid + 1.0 - no_mid) / 2.0


def score_cuts(
    yes_cut: dict[str, Any],
    no_cut: dict[str, Any],
    artifact: dict[str, Any],
    *,
    runtime_model_sha: str,
    artifact_file_sha256: str,
    scored_wall_ns: int,
) -> dict[str, Any]:
    bid_depth = max(0.0, _finite(yes_cut["bid_depth_l1"]))
    ask_depth = max(0.0, _finite(yes_cut["ask_depth_l1"]))
    total = bid_depth + ask_depth
    if total <= 0.0:
        raise ValueError("touch_depth_missing")
    depth_imbalance = (bid_depth - ask_depth) / total
    placement = yes_cut.get("placement_features")
    if not isinstance(placement, dict):
        raise ValueError("placement_features_missing")
    book_imbalance = _finite(placement["imbalance"])
    p0 = paired_probability(yes_cut, no_cut)
    coefficients = artifact["coefficients_raw"]
    slopes = coefficients["slopes"]
    delta = (
        _finite(coefficients["intercept"])
        + _finite(slopes["depth_imbalance_l1"]) * depth_imbalance
        + _finite(slopes["book_imbalance_l5"]) * book_imbalance
    )
    predicted = min(1.0, max(0.0, p0 + delta))
    receive_wall_ms = max(int(yes_cut["receive_wall_ms"]), int(no_cut["receive_wall_ms"]))
    origin_sequence = max(int(yes_cut["observer_sequence"]), int(no_cut["observer_sequence"]))
    origin_id = hashlib.sha256(
        f"{runtime_model_sha}|{yes_cut['market_id']}|{origin_sequence}|{artifact['model_hash']}".encode()
    ).hexdigest()
    return {
        "schema": SCHEMA,
        "phase": "ORIGIN_SCORE",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_money_authority": False,
        "research_only": True,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "runtime_model_sha": runtime_model_sha,
        "source_research_model_sha": artifact["source_model_sha"],
        "artifact_file_sha256": artifact_file_sha256,
        "microstructure_model_hash": artifact["model_hash"],
        "horizon_ms": 250,
        "market_id": str(yes_cut["market_id"]),
        "yes_token": str(yes_cut["token_id"]),
        "no_token": str(no_cut["token_id"]),
        "origin_id": origin_id,
        "origin_observer_sequence": origin_sequence,
        "origin_receive_wall_ms": receive_wall_ms,
        "origin_pm_yes": p0,
        "predicted_delta_probability": delta,
        "predicted_pm_yes": predicted,
        "features": {
            "depth_imbalance_l1": depth_imbalance,
            "book_imbalance_l5": book_imbalance,
        },
        "scored_wall_ns": scored_wall_ns,
        "inference_age_ns": max(0, scored_wall_ns - receive_wall_ms * 1_000_000),
    }


def asof_sequence(
    book: BookTimeline,
    market_id: str,
    token_id: str,
    timestamp_ms: int,
    maximum_sequence: int,
) -> dict[str, Any] | None:
    for row in reversed(book.history.get((market_id, token_id), ())):
        if int(row.get("observer_sequence") or 0) > maximum_sequence:
            continue
        if int(row.get("receive_wall_ms") or 0) <= timestamp_ms:
            if row.get("valid") is not True or row.get("lineage_continuous") is not True:
                return None
            return row
    return None


class MicrostructureShadow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.artifact = load_artifact(args.artifact, args.artifact_sha256, args.model_hash)
        self.book = BookTimeline(args.book_tape, args.model_sha, retention_ms=5_000)
        self.journal = CompressedJournal(args.output, args.maximum_hot_bytes)
        self.seen: set[str] = set()
        self.pending: dict[str, dict[str, Any]] = {}
        self.last_yes_sequence = 0
        self.scored = self.labels = self.censored = self.invalid = self.late = 0
        self.latencies_ms: deque[float] = deque(maxlen=100_000)
        self.started_ns = time.time_ns()
        self._restore()

    def _restore(self) -> None:
        try:
            for row in journal_rows(self.args.output):
                if not isinstance(row, dict) or row.get("schema") != SCHEMA:
                    continue
                if (
                    row.get("runtime_model_sha") != self.args.model_sha
                    or row.get("microstructure_model_hash") != self.args.model_hash
                    or row.get("paper_only") is not True
                    or row.get("authenticated_execution") is not False
                    or row.get("real_order_submission") is not False
                ):
                    continue
                origin_id = str(row.get("origin_id") or "")
                if not origin_id:
                    continue
                if row.get("phase") == "ORIGIN_SCORE":
                    self.seen.add(origin_id)
                    self.scored += 1
                    age = int(row.get("inference_age_ns") or 0)
                    if age >= 0:
                        self.latencies_ms.append(age / 1_000_000.0)
                elif row.get("phase") == "HORIZON_LABEL":
                    self.labels += 1
                elif row.get("phase") == "HORIZON_CENSORED":
                    self.censored += 1
        except (OSError, ValueError, TypeError):
            pass

    def _active_identity(self) -> tuple[str, str, str] | None:
        fair = load(self.args.fair_status)
        market = fair.get("market") if isinstance(fair.get("market"), dict) else {}
        if (
            fair.get("code_sha") != self.args.model_sha
            or fair.get("paper_only") is not True
            or fair.get("authenticated_execution") is not False
            or fair.get("real_order_submission") is not False
        ):
            return None
        market_id = str(market.get("market_id") or "")
        yes_token = str(market.get("yes_token") or "")
        no_token = str(market.get("no_token") or "")
        return (market_id, yes_token, no_token) if market_id and yes_token and no_token and yes_token != no_token else None

    def score_new(self) -> None:
        self.book.poll()
        identity = self._active_identity()
        if identity is None:
            return
        market_id, yes_token, no_token = identity
        history = list(self.book.history.get((market_id, yes_token), ()))
        for yes_cut in history:
            sequence = int(yes_cut.get("observer_sequence") or 0)
            if sequence <= self.last_yes_sequence:
                continue
            self.last_yes_sequence = sequence
            origin_ms = int(yes_cut.get("receive_wall_ms") or 0)
            no_cut = asof_sequence(self.book, market_id, no_token, origin_ms, sequence)
            if no_cut is None:
                continue
            now_ns = time.time_ns()
            try:
                record = score_cuts(
                    yes_cut,
                    no_cut,
                    self.artifact,
                    runtime_model_sha=self.args.model_sha,
                    artifact_file_sha256=self.args.artifact_sha256,
                    scored_wall_ns=now_ns,
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                self.invalid += 1
                continue
            origin_id = record["origin_id"]
            if origin_id in self.seen:
                continue
            self.seen.add(origin_id)
            age_ms = record["inference_age_ns"] / 1_000_000.0
            timely = age_ms <= self.args.latency_gate_ms
            record["latency_gate_ms"] = self.args.latency_gate_ms
            record["latency_gate_pass"] = timely
            self.journal.append(record)
            self.scored += 1
            self.latencies_ms.append(age_ms)
            if not timely:
                self.late += 1
            self.pending[origin_id] = record

    def label_pending(self) -> None:
        self.book.poll()
        status = load(self.args.book_status)
        now_ms = time.time_ns() / 1_000_000.0
        for origin_id, record in list(self.pending.items()):
            origin_ms = int(record["origin_receive_wall_ms"])
            target_ms = origin_ms + 250
            if now_ms < target_ms:
                continue
            label = self.book.label(
                record["market_id"],
                record["yes_token"],
                record["no_token"],
                origin_ms,
                target_ms,
                status,
            )
            if label is None:
                if now_ms < target_ms + self.args.label_grace_ms:
                    continue
                censored = {
                    **record,
                    "phase": "HORIZON_CENSORED",
                    "label_state": "CAUSAL_BOOK_LABEL_UNAVAILABLE",
                    "labeled_wall_ns": time.time_ns(),
                }
                self.journal.append(censored)
                self.censored += 1
                del self.pending[origin_id]
                continue
            realized = float(label["label_pm_yes"]) - float(record["origin_pm_yes"])
            prediction = float(record["predicted_delta_probability"])
            labeled = {
                **record,
                "phase": "HORIZON_LABEL",
                "label_state": "CAUSAL_BOOK_OBSERVED",
                "label_pm_yes": float(label["label_pm_yes"]),
                "realized_delta_probability": realized,
                "prediction_error": prediction - realized,
                "squared_error": (prediction - realized) ** 2,
                "direction_correct": (
                    prediction == 0.0
                    or realized == 0.0
                    or (prediction > 0.0) == (realized > 0.0)
                ),
                "label_pm_snapshot_id": label.get("label_pm_snapshot_id"),
                "label_available_after_receive_ms": label.get("label_available_after_receive_ms"),
                "labeled_wall_ns": time.time_ns(),
            }
            self.journal.append(labeled)
            self.labels += 1
            del self.pending[origin_id]

    def publish(self) -> None:
        values = list(self.latencies_ms)
        atomic_json(self.args.status, {
            "schema": STATUS_SCHEMA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_money_authority": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "automatic_promotion": False,
            "runtime_model_sha": self.args.model_sha,
            "source_research_model_sha": self.artifact["source_model_sha"],
            "artifact_file_sha256": self.args.artifact_sha256,
            "microstructure_model_hash": self.args.model_hash,
            "horizon_ms": 250,
            "started_ns": self.started_ns,
            "timestamp_ns": time.time_ns(),
            "scored_origins": self.scored,
            "labeled_origins": self.labels,
            "pending_origins": len(self.pending),
            "censored_origins": self.censored,
            "invalid_origins": self.invalid,
            "late_inferences": self.late,
            "book_timeline_gaps": self.book.gaps,
            "inference_age_ms": {
                "p50": quantile(values, 0.50),
                "p90": quantile(values, 0.90),
                "p99": quantile(values, 0.99),
                "max": max(values) if values else None,
            },
            "performance_metrics_exposed_interim": False,
            "state": "COLLECTING" if self.scored else "AWAITING_CAUSAL_BOOK",
        })

    def run(self) -> None:
        next_status = 0.0
        while True:
            self.score_new()
            self.label_pending()
            now = time.monotonic()
            if now >= next_status:
                self.publish()
                next_status = now + 1.0
            time.sleep(max(0.001, self.args.interval_ms / 1000.0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-tape", type=Path, required=True)
    parser.add_argument("--book-status", type=Path, required=True)
    parser.add_argument("--fair-status", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--model-hash", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--interval-ms", type=int, default=5)
    parser.add_argument("--latency-gate-ms", type=float, default=50.0)
    parser.add_argument("--label-grace-ms", type=int, default=2_000)
    parser.add_argument("--maximum-hot-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    if not exact_hex(args.model_sha, 40):
        raise SystemExit("invalid --model-sha")
    if args.interval_ms < 1 or args.label_grace_ms < 250 or args.maximum_hot_bytes < 1:
        raise SystemExit("invalid shadow arguments")
    MicrostructureShadow(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
