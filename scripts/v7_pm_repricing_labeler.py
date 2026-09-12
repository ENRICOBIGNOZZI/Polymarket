#!/usr/bin/env python3
"""Attach causal +250ms labels to fast PM repricing inference records.

This process evaluates predictions only. It has zero execution authority and
never feeds future labels back into the inference path.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import json
import os
from pathlib import Path
import time
from typing import Any

from v7_causal_book import BookTimeline
from v7_compressed_journal import CompressedJournal, journal_rows
from v7_external_lead_lag_collector import load
from v7_external_rich_model import logit
from v7_pm_repricing_common import atomic_json

INFERENCE_SCHEMA = "polymarket_v7_pm_repricing_shadow_v1"
SCHEMA = "polymarket_v7_pm_repricing_label_v1"
STATUS_SCHEMA = "polymarket_v7_pm_repricing_labeler_status_v1"


class JsonlTail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None
        self.inode: tuple[int, int] | None = None

    def poll(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.path.exists():
            return out
        if self.handle is None:
            self.handle = self.path.open("rb")
            stat = os.fstat(self.handle.fileno())
            self.inode = (stat.st_dev, stat.st_ino)
        while True:
            offset = self.handle.tell()
            raw = self.handle.readline()
            if not raw or not raw.endswith(b"\n"):
                self.handle.seek(offset)
                break
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                out.append(value)
        try:
            current = self.path.stat()
            identity = (current.st_dev, current.st_ino)
        except OSError:
            return out
        if identity != self.inode:
            self.handle.close()
            self.handle = self.path.open("rb")
            stat = os.fstat(self.handle.fileno())
            self.inode = (stat.st_dev, stat.st_ino)
        return out


def valid_inference(row: dict[str, Any], model_sha: str, horizon_ms: int) -> bool:
    return bool(
        row.get("schema") == INFERENCE_SCHEMA
        and row.get("paper_only") is True
        and row.get("authenticated_execution") is False
        and row.get("real_order_submission") is False
        and row.get("execution_authority") == "ZERO_AUTHORITY_RESEARCH_ONLY"
        and row.get("runtime_model_sha") == model_sha
        and row.get("inference_phase") == "ORIGIN_ONLY_NO_FUTURE_LABEL"
        and int(row.get("horizon_ms") or 0) == horizon_ms
        and str(row.get("origin_id") or "")
        and str(row.get("market_id") or "")
        and str(row.get("yes_token") or "")
        and str(row.get("no_token") or "")
        and int(row.get("origin_observed_wall_ns") or 0) > 0
    )


class Labeler:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.tail = JsonlTail(args.inference)
        self.book = BookTimeline(args.book_tape, args.model_sha, retention_ms=10_000)
        self.pending: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.seen: set[str] = set()
        self.resolved: set[str] = set()
        self.inferences = self.labels = self.censored = self.invalid = 0
        self.timely_labels = self.correct_sign = 0
        self.started_ns = time.time_ns()
        self.last_record: dict[str, Any] | None = None
        self._restore()
        self.journal = CompressedJournal(args.output, args.maximum_hot_bytes)

    def _restore(self) -> None:
        try:
            for row in journal_rows(self.args.output):
                if (
                    not isinstance(row, dict)
                    or row.get("schema") != SCHEMA
                    or row.get("runtime_model_sha") != self.args.model_sha
                    or int(row.get("horizon_ms") or 0) != self.args.horizon_ms
                ):
                    continue
                origin_id = str(row.get("origin_id") or "")
                if not origin_id or origin_id in self.resolved:
                    continue
                self.resolved.add(origin_id)
                self.seen.add(origin_id)
                if row.get("state") == "OBSERVED":
                    self.labels += 1
                    self.correct_sign += int(row.get("prediction_sign_correct") is True)
                    self.timely_labels += int(row.get("latency_gate_pass") is True)
                else:
                    self.censored += 1
                self.last_record = row
        except (OSError, ValueError, TypeError):
            pass
        try:
            for row in journal_rows(self.args.inference):
                if not valid_inference(row, self.args.model_sha, self.args.horizon_ms):
                    continue
                origin_id = str(row["origin_id"])
                if origin_id in self.resolved or origin_id in self.pending:
                    continue
                self.seen.add(origin_id)
                self.pending[origin_id] = row
                if len(self.pending) > 20_000:
                    self.pending.popitem(last=False)
        except (OSError, ValueError, TypeError):
            pass
        self.inferences = len(self.resolved) + len(self.pending)

    def ingest(self) -> None:
        for row in self.tail.poll():
            if not valid_inference(row, self.args.model_sha, self.args.horizon_ms):
                self.invalid += 1
                continue
            origin_id = str(row["origin_id"])
            if origin_id in self.seen or origin_id in self.pending:
                continue
            self.seen.add(origin_id)
            self.inferences += 1
            self.pending[origin_id] = row
            if len(self.pending) > 20_000:
                self.pending.popitem(last=False)

    def label(self) -> None:
        self.book.poll()
        status = load(self.args.book_status)
        now_ms = time.time_ns() / 1_000_000.0
        for origin_id, row in list(self.pending.items()):
            origin_ms = int(row["origin_observed_wall_ns"]) / 1_000_000.0
            target_ms = origin_ms + self.args.horizon_ms
            if now_ms < target_ms:
                continue
            evidence = self.book.label(
                row["market_id"], row["yes_token"], row["no_token"],
                origin_ms, target_ms, status,
            )
            if evidence is None:
                if now_ms < target_ms + self.args.label_grace_ms:
                    continue
                record = {
                    "schema": SCHEMA,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
                    "runtime_model_sha": self.args.model_sha,
                    "origin_id": origin_id,
                    "market_id": row["market_id"],
                    "horizon_ms": self.args.horizon_ms,
                    "state": "BOOK_CONTINUITY_OR_WATERMARK_CENSORED",
                    "inference_scored_wall_ns": row.get("scored_wall_ns"),
                    "inference_age_ns": row.get("inference_age_ns"),
                    "latency_gate_pass": row.get("latency_gate_pass") is True,
                    "recorded_wall_ns": time.time_ns(),
                }
                self.censored += 1
            else:
                p0 = float(row["origin_pm_yes"])
                p1 = float(evidence["label_pm_yes"])
                predicted_delta = float(row.get("predicted_delta_probability") or 0.0)
                realized_delta = p1 - p0
                yes_tick = float(row["yes_tick_size"])
                no_tick = float(row["no_tick_size"])
                sign_correct = (predicted_delta == 0.0 and realized_delta == 0.0) or predicted_delta * realized_delta > 0.0
                record = {
                    "schema": SCHEMA,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
                    "runtime_model_sha": self.args.model_sha,
                    "artifact_sha256": row.get("artifact_sha256"),
                    "origin_id": origin_id,
                    "market_id": row["market_id"],
                    "horizon_ms": self.args.horizon_ms,
                    "state": "OBSERVED",
                    "origin_pm_yes": p0,
                    "label_pm_yes": p1,
                    "predicted_delta_probability": predicted_delta,
                    "realized_delta_probability": realized_delta,
                    "realized_delta_logit": logit(p1) - logit(p0),
                    "prediction_sign_correct": sign_correct,
                    "realized_adverse_yes_ticks": max(0.0, -realized_delta / yes_tick),
                    "realized_adverse_no_ticks": max(0.0, realized_delta / no_tick),
                    "raw_would_veto_yes_buy": row.get("raw_would_veto_yes_buy") is True,
                    "raw_would_veto_no_buy": row.get("raw_would_veto_no_buy") is True,
                    "timely_would_veto_yes_buy": row.get("would_veto_yes_buy") is True,
                    "timely_would_veto_no_buy": row.get("would_veto_no_buy") is True,
                    "inference_scored_wall_ns": row.get("scored_wall_ns"),
                    "inference_age_ns": row.get("inference_age_ns"),
                    "latency_gate_pass": row.get("latency_gate_pass") is True,
                    "label_available_after_receive_ms": evidence.get("label_available_after_receive_ms"),
                    "label_pm_snapshot_id": evidence.get("label_pm_snapshot_id"),
                    "recorded_wall_ns": time.time_ns(),
                }
                self.labels += 1
                self.correct_sign += int(sign_correct)
                self.timely_labels += int(row.get("latency_gate_pass") is True)
            self.journal.append(record)
            self.resolved.add(origin_id)
            self.last_record = record
            del self.pending[origin_id]

    def publish(self) -> None:
        resolved = self.labels + self.censored
        atomic_json(self.args.status, {
            "schema": STATUS_SCHEMA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "runtime_model_sha": self.args.model_sha,
            "horizon_ms": self.args.horizon_ms,
            "started_ns": self.started_ns,
            "timestamp_ns": time.time_ns(),
            "inferences_seen": self.inferences,
            "observed_labels": self.labels,
            "censored_labels": self.censored,
            "pending": len(self.pending),
            "invalid_inference_rows": self.invalid,
            "restart_restored_labels": len(self.resolved),
            "label_coverage": self.labels / resolved if resolved else None,
            "timely_inference_fraction_among_labels": self.timely_labels / self.labels if self.labels else None,
            "sign_accuracy": self.correct_sign / self.labels if self.labels else None,
            "last_record": self.last_record,
            "state": "COLLECTING" if self.inferences else "AWAITING_FAST_INFERENCE",
        })

    def run(self) -> None:
        next_status = 0.0
        while True:
            self.ingest()
            self.label()
            now = time.monotonic()
            if now >= next_status:
                self.publish()
                next_status = now + 1.0
            time.sleep(max(0.005, self.args.interval_ms / 1000.0))
