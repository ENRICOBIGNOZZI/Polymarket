#!/usr/bin/env python3
"""Cold-path causal feature tape for six-asset SHADOW research."""
from __future__ import annotations

import argparse
import fcntl
import re
import hashlib
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA = "polymarket_v7_multi_crypto_feature_snapshot_v2"
TAPE_SCHEMA = "polymarket_v7_multi_crypto_feature_tape_v1"
STATUS_SCHEMA = "polymarket_v7_multi_crypto_feature_tape_status_v1"
STOP = False


def stop_handler(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def validate_snapshot(value: dict[str, Any], model_sha: str) -> dict[str, Any]:
    if (value.get("schema") != SNAPSHOT_SCHEMA or value.get("model_sha") != model_sha
            or value.get("paper_only") is not True
            or value.get("authenticated_execution") is not False
            or value.get("real_order_submission") is not False
            or value.get("real_capital_at_risk") is not False
            or value.get("execution_authority") is not False
            or value.get("research_only") is not True
            or value.get("policy_mode") != "SHADOW_COLLECTION_ONLY_UNCALIBRATED"):
        raise ValueError("feature snapshot identity/authority invalid")
    for name in ("policy_hash", "feature_schema_hash"):
        raw = str(value.get(name) or "")
        if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
            raise ValueError(f"{name} invalid")
    rows = value.get("markets")
    if not isinstance(rows, list):
        raise ValueError("feature markets missing")
    for row in rows:
        if not isinstance(row, dict) or row.get("signal_eligible") is not False:
            raise ValueError("uncalibrated tape may not contain executable signal")
        source_hash = str(row.get("source_identity_hash") or "")
        if len(source_hash) != 64 or any(ch not in "0123456789abcdef" for ch in source_hash):
            raise ValueError("market source identity invalid")
        if row.get("feature_schema_hash") != value.get("feature_schema_hash"):
            raise ValueError("market feature schema mismatch")
        available = int(row.get("available_at_ns") or 0)
        decision = int(value.get("timestamp_ns") or 0)
        if available <= 0 or decision <= 0 or available > decision:
            raise ValueError("future_or_unknown_feature_availability")
    return value


def compact_record(snapshot: dict[str, Any], row: dict[str, Any], recorded_wall_ns: int) -> dict[str, Any]:
    external = row.get("external") if isinstance(row.get("external"), dict) else {}
    record = {
        "schema": TAPE_SCHEMA,
        "recorded_wall_ns": recorded_wall_ns,
        "decision_wall_ns": int(snapshot["timestamp_ns"]),
        "available_at_ns": int(row["available_at_ns"]),
        "model_sha": snapshot["model_sha"],
        "policy_hash": snapshot["policy_hash"],
        "feature_schema_hash": snapshot["feature_schema_hash"],
        "feature_schema_version": snapshot.get("feature_schema_version"),
        "source_identity_hash": row["source_identity_hash"],
        "asset": row.get("asset"), "horizon": row.get("horizon"),
        "market_id": row.get("market_id"), "event_id": row.get("event_id"),
        "yes_token": row.get("yes_token"), "no_token": row.get("no_token"),
        "start_timestamp": row.get("start_timestamp"), "end_timestamp": row.get("end_timestamp"),
        "active_now": row.get("active_now") is True,
        "source_versions": row.get("source_versions"),
        "blockers": row.get("blockers"),
        "features": {
            "tte_seconds": row.get("tte_seconds"),
            "pm_book_valid": row.get("pm_book_valid"), "pm_yes_mid": row.get("pm_yes_mid"),
            "pm_no_mid": row.get("pm_no_mid"), "pm_complete_set_gap": row.get("pm_complete_set_gap"),
            "pm_yes_spread": row.get("pm_yes_spread"), "pm_yes_imbalance": row.get("pm_yes_imbalance"),
            "oracle_fresh": row.get("oracle_fresh"), "oracle_price": row.get("oracle_price"),
            "reference_valid": row.get("reference_valid"), "reference_price": row.get("reference_price"),
            "distance_to_reference_bp": row.get("distance_to_reference_bp"),
            "spot_minus_oracle_bp": row.get("spot_minus_oracle_bp"),
            "external": {
                "fresh": external.get("fresh"), "composite_price": external.get("composite_price"),
                "return_50ms_bp": external.get("return_50ms_bp"), "return_100ms_bp": external.get("return_100ms_bp"),
                "return_250ms_bp": external.get("return_250ms_bp"), "return_1s_bp": external.get("return_1s_bp"),
                "dispersion_bps": external.get("dispersion_bps"), "aggregate_ofi": external.get("aggregate_ofi"),
                "aggregate_trade_imbalance": external.get("aggregate_trade_imbalance"),
                "fresh_venue_count": external.get("fresh_venue_count"), "shock": external.get("shock"),
            },
            "leader_features": row.get("leader_features"), "derivatives": row.get("derivatives"),
        },
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False,
    }
    record["record_hash"] = canonical_hash(record)
    return record


class SegmentedWriter:
    """One cold-path writer. Sealed segments are immutable across restarts."""
    def __init__(self, path: Path, segment_bytes: int):
        if type(segment_bytes) is not int or segment_bytes <= 0:
            raise ValueError("SEGMENT_CAPACITY_INVALID")
        self.path = path
        self.segment_bytes = segment_bytes
        self.closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_name(self.path.name + ".writer.lock").open("a")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError("FEATURE_TAPE_WRITER_ALREADY_ACTIVE") from None
        try:
            pattern = re.compile(re.escape(self.path.stem) + r"\.segment-([0-9]+)" + re.escape(self.path.suffix))
            indices = [int(match.group(1)) for p in self.path.parent.iterdir()
                       if (match := pattern.fullmatch(p.name))]
            self.segment = max(indices, default=-1) + 1
            # A crash after link but before unlink must not reopen a sealed inode.
            if self.path.exists() and self.path.stat().st_nlink != 1:
                raise ValueError("FEATURE_TAPE_SEAL_RECOVERY_REQUIRED")
            self.handle = self.path.open("a", encoding="utf-8", buffering=1024 * 1024)
            self.bytes_written = self.path.stat().st_size
        except BaseException:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
            self.lock.close()
            raise

    def _seal(self) -> None:
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        sealed = self.path.with_name(self.path.stem + f".segment-{self.segment:06d}" + self.path.suffix)
        # Atomic no-replace operation: an existing segment can never be overwritten.
        os.link(self.path, sealed)
        digest = hashlib.sha256()
        with sealed.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        manifest = {"schema": "polymarket_v7_feature_segment_manifest_v1",
                    "segment": self.segment, "file": sealed.name,
                    "bytes": sealed.stat().st_size, "sha256": digest.hexdigest()}
        with sealed.with_name(sealed.name + ".manifest.json").open("x", encoding="utf-8") as out:
            out.write(json.dumps(manifest, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
        self.path.unlink()
        self.segment += 1
        self.bytes_written = 0
        self.handle = self.path.open("x", encoding="utf-8", buffering=1024 * 1024)

    def append(self, value: dict[str, Any]) -> None:
        if self.closed:
            raise ValueError("FEATURE_TAPE_WRITER_CLOSED")
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        encoded = payload.encode()
        if len(encoded) > self.segment_bytes:
            raise ValueError("FEATURE_RECORD_EXCEEDS_SEGMENT_CAPACITY")
        if self.bytes_written and self.bytes_written + len(encoded) > self.segment_bytes:
            self._seal()
        self.handle.write(payload)
        self.bytes_written += len(encoded)

    def flush(self, durable: bool = False) -> None:
        self.handle.flush()
        if durable:
            os.fsync(self.handle.fileno())

    def close(self) -> None:
        if self.closed:
            return
        try:
            if not self.handle.closed:
                self.flush(True)
                self.handle.close()
        finally:
            self.closed = True
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
            self.lock.close()


class Collector:
    def __init__(self, *, model_sha: str, minimum_interval_ms: int):
        self.model_sha = model_sha; self.minimum_interval_ns = minimum_interval_ms * 1_000_000
        self.last_emit_ns: dict[str, int] = {}; self.last_hash: dict[str, str] = {}
        self.emitted = 0; self.duplicate_skips = 0; self.interval_skips = 0; self.inactive_skips = 0
        self.invalid_snapshots = 0; self.invalid_reasons: dict[str, int] = {}; self.last_recorded_wall_ns = 0

    def collect(self, snapshot: dict[str, Any], *, now_ns: int) -> list[dict[str, Any]]:
        try:
            value = validate_snapshot(snapshot, self.model_sha)
            decision = value.get("timestamp_ns")
            if type(now_ns) is not int or type(decision) is not int or not 0 < decision <= now_ns:
                raise ValueError("FEATURE_DECISION_AFTER_RECORDING_OR_UNKNOWN_CLOCK")
        except ValueError as exc:
            self.invalid_snapshots += 1
            reason = str(exc) or "UNKNOWN_VALIDATION_ERROR"
            self.invalid_reasons[reason] = self.invalid_reasons.get(reason, 0) + 1
            return []
        output = []
        for row in value["markets"]:
            if row.get("active_now") is not True:
                self.inactive_skips += 1; continue
            market_id = str(row.get("market_id") or "")
            source_hash = str(row["source_identity_hash"])
            if self.last_hash.get(market_id) == source_hash:
                self.duplicate_skips += 1; continue
            previous = self.last_emit_ns.get(market_id, 0)
            if previous and now_ns - previous < self.minimum_interval_ns:
                self.interval_skips += 1; continue
            record = compact_record(value, row, now_ns)
            output.append(record); self.last_hash[market_id] = source_hash; self.last_emit_ns[market_id] = now_ns
            self.emitted += 1; self.last_recorded_wall_ns = now_ns
        return output

    def status(self, *, snapshot_path: Path, output_path: Path, writer: SegmentedWriter) -> dict[str, Any]:
        return {
            "schema": STATUS_SCHEMA, "timestamp_ns": time.time_ns(), "model_sha": self.model_sha,
            "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
            "real_capital_at_risk": False, "execution_authority": False, "research_only": True,
            "snapshot_path": str(snapshot_path), "output_path": str(output_path),
            "minimum_interval_ms": self.minimum_interval_ns // 1_000_000,
            "emitted": self.emitted, "duplicate_skips": self.duplicate_skips,
            "interval_skips": self.interval_skips, "inactive_skips": self.inactive_skips,
            "invalid_snapshots": self.invalid_snapshots, "invalid_reasons": dict(sorted(self.invalid_reasons.items())),
            "last_recorded_wall_ns": self.last_recorded_wall_ns,
            "current_segment_bytes": writer.bytes_written, "segments_sealed": writer.segment,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True); parser.add_argument("--model-sha", required=True)
    parser.add_argument("--poll-ms", type=int, default=25); parser.add_argument("--minimum-interval-ms", type=int, default=100)
    parser.add_argument("--segment-mb", type=int, default=256); parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha): raise ValueError("exact model SHA required")
    if not 10 <= args.poll_ms <= 5000 or not 50 <= args.minimum_interval_ms <= 60_000: raise ValueError("invalid sampling cadence")
    if not 1 <= args.segment_mb <= 4096: raise ValueError("segment size out of range")
    signal.signal(signal.SIGINT, stop_handler); signal.signal(signal.SIGTERM, stop_handler)
    collector=Collector(model_sha=args.model_sha, minimum_interval_ms=args.minimum_interval_ms)
    writer=SegmentedWriter(args.output, args.segment_mb * 1024 * 1024); last_status=0; last_flush=0
    try:
        while True:
            snapshot=load(args.snapshot); now=time.time_ns()
            for record in collector.collect(snapshot, now_ns=now): writer.append(record)
            if now-last_flush >= 1_000_000_000: writer.flush(False); last_flush=now
            if now-last_status >= 1_000_000_000:
                atomic_json(args.status, collector.status(snapshot_path=args.snapshot, output_path=args.output, writer=writer)); last_status=now
            if args.once or STOP: break
            time.sleep(args.poll_ms / 1000.0)
    finally:
        writer.close(); atomic_json(args.status, collector.status(snapshot_path=args.snapshot, output_path=args.output, writer=writer))
    return 0


if __name__ == "__main__": raise SystemExit(main())
