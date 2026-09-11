#!/usr/bin/env python3
"""Persist every causal external-cancel signal transition for forward research."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_compressed_journal import CompressedJournal, journal_rows

SIGNAL_SCHEMA = "polymarket_v7_btc_m5_external_cancel_live_signal_v2"
STATUS_SCHEMA = "polymarket_v7_external_cancel_signal_journal_status_v1"


def exact_hex(value: str, length: int) -> bool:
    return len(value) == length and all(c in "0123456789abcdef" for c in value)


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def validate_signal(row: dict[str, Any], model_sha: str) -> dict[str, Any] | None:
    if (
        row.get("schema") != SIGNAL_SCHEMA
        or row.get("code_sha") != model_sha
        or row.get("paper_only") is not True
        or row.get("authenticated_execution") is not False
        or row.get("real_order_submission") is not False
        or row.get("execution_authority") != "ZERO_AUTHORITY_SIGNAL_ONLY"
        or row.get("supported_cancel_side") != "BUY"
    ):
        return None
    try:
        version = int(row.get("signal_version"))
        started = int(row.get("started_monotonic_ns"))
        publish = int(row.get("publish_wall_ns"))
        trigger = int(row.get("trigger_receive_wall_ns"))
    except (TypeError, ValueError, OverflowError):
        return None
    rule = str(row.get("rule_sha256") or "")
    outcome = str(row.get("stale_buy_outcome") or "").upper()
    if version < 0 or started <= 0 or publish <= 0 or trigger <= 0 or publish < trigger:
        return None
    if not isinstance(row.get("valid"), bool):
        return None
    if not exact_hex(rule, 64) or outcome not in {"YES", "NO"}:
        return None
    return row


def signal_key(row: dict[str, Any]) -> str:
    identity = {
        "code_sha": row["code_sha"],
        "started_monotonic_ns": int(row["started_monotonic_ns"]),
        "signal_version": int(row["signal_version"]),
        "valid": row.get("valid") is True,
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Journaler:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.journal = CompressedJournal(args.output, args.maximum_hot_bytes)
        self.seen: set[str] = set()
        for row in journal_rows(args.output):
            key = str(row.get("journal_key") or "")
            if key:
                self.seen.add(key)
        self.appended = self.invalid = 0
        self.last: dict[str, Any] | None = None

    def tick(self) -> None:
        raw = load(self.args.signal)
        row = validate_signal(raw, self.args.model_sha)
        if row is None:
            if raw:
                self.invalid += 1
            return
        key = signal_key(row)
        if key in self.seen:
            return
        record = dict(row)
        record["asset"] = "BTC"
        record["horizon"] = "M5"
        record["journal_key"] = key
        record["journal_observed_wall_ns"] = time.time_ns()
        record["journal_execution_authority"] = "ZERO_AUTHORITY_RESEARCH_ONLY"
        self.journal.append(record)
        self.seen.add(key)
        self.appended += 1
        self.last = record

    def publish(self) -> None:
        atomic_json(self.args.status, {
            "schema": STATUS_SCHEMA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "model_sha": self.args.model_sha,
            "timestamp_ns": time.time_ns(),
            "appended_transitions": self.appended,
            "invalid_reads": self.invalid,
            "dedup_keys": len(self.seen),
            "last_signal_version": self.last.get("signal_version") if self.last else None,
            "last_signal_valid": self.last.get("valid") if self.last else None,
            "state": "COLLECTING" if self.appended else "AWAITING_SIGNAL",
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
    ap.add_argument("--signal", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--interval-ms", type=int, default=10)
    ap.add_argument("--maximum-hot-bytes", type=int, default=64 * 1024**2)
    args = ap.parse_args()
    if not exact_hex(args.model_sha, 40):
        raise SystemExit("invalid --model-sha")
    if not 5 <= args.interval_ms <= 1000:
        raise SystemExit("invalid --interval-ms")
    Journaler(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
