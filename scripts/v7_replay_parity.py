#!/usr/bin/env python3
"""Fail closed on deterministic replay divergence for a captured V7 interval.

This offline verifier compares canonical stage manifests.  It never fetches
data, mutates a tape, signs, or submits an order.  The manifests deliberately
carry only hashes of the stage payloads, so a redacted report can establish
replay lineage without exposing raw private account data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_replay_parity_manifest_v1"
REPORT_SCHEMA = "polymarket_v7_replay_parity_report_v1"
STAGES = {
    "UNIVERSE", "VALIDATED_BOOK", "FEATURE_SNAPSHOT", "STRATEGY_INTENT",
    "RISK_DECISION", "SIMULATED_OMS_DECISION",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")


class ReplayParityError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReplayParityError(f"{field}:invalid")
    return value


def validate_manifest(value: Any) -> dict[str, Any]:
    """Validate an immutable stage-hash manifest and its causal cut."""
    required = {"schema", "model_sha", "run_id", "source_manifest_sha256", "events", "manifest_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise ReplayParityError("manifest:shape")
    if value.get("schema") != SCHEMA or not SHA_RE.fullmatch(str(value.get("model_sha"))):
        raise ReplayParityError("manifest:identity")
    if not RUN_RE.fullmatch(str(value.get("run_id"))) or not SHA256_RE.fullmatch(str(value.get("source_manifest_sha256"))):
        raise ReplayParityError("manifest:identity")
    events = value.get("events")
    if not isinstance(events, list):
        raise ReplayParityError("manifest:events")
    keys: set[tuple[str, int]] = set()
    event_fields = {"stage", "sequence", "decision_cut_time_ns", "max_input_receive_time_ns",
                    "reason_codes", "deterministic_payload_sha256", "observation_time_ns"}
    for event in events:
        if not isinstance(event, dict) or set(event) != event_fields:
            raise ReplayParityError("event:shape")
        stage, sequence = event.get("stage"), _integer(event.get("sequence"), "event:sequence")
        if stage not in STAGES:
            raise ReplayParityError("event:stage")
        key = (stage, sequence)
        if key in keys:
            raise ReplayParityError("event:duplicate_key")
        keys.add(key)
        cut = _integer(event.get("decision_cut_time_ns"), "event:decision_cut_time_ns")
        receive = _integer(event.get("max_input_receive_time_ns"), "event:max_input_receive_time_ns")
        _integer(event.get("observation_time_ns"), "event:observation_time_ns")
        if receive > cut:
            raise ReplayParityError("event:causal_cut_violation")
        if (not isinstance(event.get("reason_codes"), list)
                or any(not isinstance(code, str) or not code for code in event["reason_codes"])
                or event["reason_codes"] != sorted(set(event["reason_codes"]))):
            raise ReplayParityError("event:reason_codes")
        if not SHA256_RE.fullmatch(str(event.get("deterministic_payload_sha256"))):
            raise ReplayParityError("event:payload_hash")
    if {key[0] for key in keys} != STAGES:
        raise ReplayParityError("event:stage_coverage")
    supplied = value.get("manifest_sha256")
    unhashed = dict(value)
    unhashed.pop("manifest_sha256")
    if not isinstance(supplied, str) or supplied != digest(unhashed):
        raise ReplayParityError("manifest:sha256")
    return value


def build_manifest(*, model_sha: str, run_id: str, source_manifest_sha256: str,
                   events: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a canonical manifest for a captured interval or its replay."""
    value: dict[str, Any] = {
        "schema": SCHEMA, "model_sha": model_sha, "run_id": run_id,
        "source_manifest_sha256": source_manifest_sha256, "events": events,
    }
    value["manifest_sha256"] = digest(value)
    return validate_manifest(value)


def _divergence(classification: str, key: tuple[str, int], detail: str) -> dict[str, Any]:
    return {"classification": classification, "stage": key[0], "sequence": key[1], "detail": detail}


def compare(captured: Any, replay: Any) -> dict[str, Any]:
    """Classify every parity difference; only expected timing is non-blocking."""
    captured = validate_manifest(captured)
    replay = validate_manifest(replay)
    divergences: list[dict[str, Any]] = []
    if captured["model_sha"] != replay["model_sha"]:
        divergences.append(_divergence("SOFTWARE_DEFECT", ("UNIVERSE", 0), "model_sha_mismatch"))
    if captured["source_manifest_sha256"] != replay["source_manifest_sha256"]:
        divergences.append(_divergence("INPUT_MISSING", ("UNIVERSE", 0), "source_manifest_mismatch"))
    captured_events = {(row["stage"], row["sequence"]): row for row in captured["events"]}
    replay_events = {(row["stage"], row["sequence"]): row for row in replay["events"]}
    for key in sorted(captured_events.keys() - replay_events.keys()):
        divergences.append(_divergence("INPUT_MISSING", key, "missing_from_replay"))
    for key in sorted(replay_events.keys() - captured_events.keys()):
        divergences.append(_divergence("INPUT_MISSING", key, "missing_from_capture"))
    for key in sorted(captured_events.keys() & replay_events.keys()):
        original, reproduced = captured_events[key], replay_events[key]
        if (original["decision_cut_time_ns"] != reproduced["decision_cut_time_ns"]
                or original["max_input_receive_time_ns"] != reproduced["max_input_receive_time_ns"]):
            divergences.append(_divergence("CLOCK_DRIFT", key, "causal_time_boundary_mismatch"))
        if original["deterministic_payload_sha256"] != reproduced["deterministic_payload_sha256"]:
            divergences.append(_divergence("SOFTWARE_DEFECT", key, "deterministic_payload_mismatch"))
        if original["reason_codes"] != reproduced["reason_codes"]:
            divergences.append(_divergence("SOFTWARE_DEFECT", key, "reason_codes_mismatch"))
        if (original["observation_time_ns"] != reproduced["observation_time_ns"]
                and original["decision_cut_time_ns"] == reproduced["decision_cut_time_ns"]
                and original["max_input_receive_time_ns"] == reproduced["max_input_receive_time_ns"]
                and original["deterministic_payload_sha256"] == reproduced["deterministic_payload_sha256"]
                and original["reason_codes"] == reproduced["reason_codes"]):
            divergences.append(_divergence("EXPECTED_NONDETERMINISM", key, "observation_time_only"))
    blocking = [row for row in divergences if row["classification"] != "EXPECTED_NONDETERMINISM"]
    return {
        "schema": REPORT_SCHEMA, "model_sha": captured["model_sha"],
        "captured_manifest_sha256": captured["manifest_sha256"],
        "replay_manifest_sha256": replay["manifest_sha256"],
        "captured_run_id": captured["run_id"], "replay_run_id": replay["run_id"],
        "compared_events": len(captured_events.keys() & replay_events.keys()),
        "divergences": divergences, "release_blocked": bool(blocking),
        "status": "RELEASE_BLOCKED" if blocking else "PARITY_OK",
    }


def immutable_write(path: Path, value: dict[str, Any]) -> None:
    path = Path(path)
    if path.is_symlink():
        raise ReplayParityError("output:symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(value) + b"\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o444)
    except FileExistsError:
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise ReplayParityError("output:immutable_path_collision")
        return
    except OSError as exc:
        raise ReplayParityError("output:write_failed") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _load(path: Path) -> dict[str, Any]:
    try:
        return validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayParityError(f"input:invalid:{path}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    compare_cmd = commands.add_parser("compare")
    compare_cmd.add_argument("--captured", type=Path, required=True)
    compare_cmd.add_argument("--replay", type=Path, required=True)
    compare_cmd.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            manifest = _load(args.manifest)
            print(json.dumps({"valid": True, "manifest_sha256": manifest["manifest_sha256"]}, sort_keys=True))
            return 0
        report = compare(_load(args.captured), _load(args.replay))
        if args.output:
            immutable_write(args.output, report)
        print(json.dumps(report, sort_keys=True))
        return 0 if not report["release_blocked"] else 1
    except ReplayParityError as exc:
        print(f"v7_replay_parity: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
