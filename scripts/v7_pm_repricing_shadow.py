#!/usr/bin/env python3
"""Compatibility owner for fast PM repricing inference plus delayed labels.

The process remains a zero-authority research observer. The inference event is
emitted from origin-time evidence only; +250ms evaluation is an independent
internal stage and never feeds the inference path.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from types import SimpleNamespace
import time

from v7_external_rich_model import FEATURE_SCHEMA
from v7_pm_repricing_common import (
    ARTIFACT_SCHEMA, FROZEN_ARTIFACT_SCHEMA, atomic_json, exact_hex,
    file_sha256, load_model, logistic, score_origin, token_tick,
)
from v7_pm_repricing_fast_shadow import FastShadow
from v7_pm_repricing_labeler import Labeler

SCHEMA = "polymarket_v7_pm_repricing_shadow_v1"
STATUS_SCHEMA = "polymarket_v7_pm_repricing_shadow_status_v1"
CAUSAL_SCHEMA = "polymarket_v7_model_independent_causal_observation_v1"


class Shadow(FastShadow):
    """Backward-compatible API with fast scoring and optional internal labeler."""
    def __init__(self, args: argparse.Namespace) -> None:
        defaults = {
            "latency_gate_ms": 50,
            "max_book_age_ms": 100,
            "max_book_wait_ms": 50,
            "maximum_hot_bytes": 64 * 1024**2,
        }
        for key, value in defaults.items():
            if not hasattr(args, key):
                setattr(args, key, value)
        super().__init__(args)
        self.labeler = None
        if getattr(args, "enable_labeler", False):
            self.labeler = Labeler(SimpleNamespace(
                inference=args.output,
                book_tape=args.book_tape,
                book_status=args.book_status,
                output=args.output.with_name("pm_repricing_labels.jsonl"),
                status=args.status.with_name("pm_repricing_labeler_status.json"),
                model_sha=args.model_sha,
                horizon_ms=args.horizon_ms,
                label_grace_ms=75,
                interval_ms=10,
                maximum_hot_bytes=args.maximum_hot_bytes,
            ))

    def tick(self) -> None:
        self.observe_origin()
        self.score_pending()
        if self.labeler is not None:
            self.labeler.ingest()
            self.labeler.label()

    def publish(self) -> None:
        super().publish()
        if self.labeler is not None:
            self.labeler.publish()

    def run(self) -> None:
        next_status = 0.0
        while True:
            self.tick()
            now = time.monotonic()
            if now >= next_status:
                self.publish()
                next_status = now + 1.0
            time.sleep(max(0.001, self.args.interval_ms / 1000.0))


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
    ap.add_argument("--latency-gate-ms", type=int, default=50)
    ap.add_argument("--max-book-age-ms", type=int, default=100)
    ap.add_argument("--max-book-wait-ms", type=int, default=50)
    args = ap.parse_args()
    if not exact_hex(args.model_sha, 40):
        raise SystemExit("invalid --model-sha")
    if args.horizon_ms != 250 or args.family not in ("PM_MICRO_ONLY", "EXTERNAL_ONLY", "PM_PLUS_EXTERNAL"):
        raise SystemExit("invalid model selection")
    if not math.isfinite(args.threshold_ticks) or args.threshold_ticks <= 0:
        raise SystemExit("invalid --threshold-ticks")
    if not 1 <= args.latency_gate_ms <= 100 or not 1 <= args.max_book_wait_ms <= 100:
        raise SystemExit("invalid fast inference gate")
    # Legacy launcher passed 25ms because inference and labeling were coupled.
    # Fast origin scoring is now explicitly capped at 5ms; labeling remains
    # asynchronous inside the same zero-authority process.
    args.interval_ms = min(max(1, args.interval_ms), 5)
    args.enable_labeler = True
    Shadow(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
