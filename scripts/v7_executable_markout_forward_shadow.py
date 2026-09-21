#!/usr/bin/env python3
"""Zero-authority forward shadow for V2 executable-markout models.

Reads live native kind=2 decision observations, emits frozen model predictions
before future kind=6 labels exist, and never writes orders/cancel signals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

SCHEMA = "polymarket_v7_executable_markout_forward_shadow_v1"
STATUS_SCHEMA = "polymarket_v7_executable_markout_forward_shadow_status_v1"
ARTIFACT_SCHEMA = "historical_walk_forward_v2_full_window_repricing_models_v2"
HORIZONS = (500, 1000, 2000)


def exact_hex(value: str, length: int) -> bool:
    return len(value) == length and all(ch in "0123456789abcdef" for ch in value)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    os.replace(tmp, path)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def native_wall_ns(row: dict[str, Any]) -> int:
    if row.get("kind") == 2 and isinstance(row.get("decision_wall_ns"), int):
        return int(row["decision_wall_ns"])
    observed = row.get("observed_monotonic_ns") or row.get("decision_monotonic_ns")
    close_wall, close_mono = row.get("close_wall_ns"), row.get("close_monotonic_ns")
    if all(isinstance(v, int) and v > 0 for v in (observed, close_wall, close_mono)):
        return int(observed) + int(close_wall) - int(close_mono)
    return 0


def numeric_features(row: dict[str, Any]) -> dict[str, float] | None:
    result: dict[str, float] = {}
    external = row.get("external_features")
    if isinstance(external, dict):
        input_receive = external.get("input_receive_ns")
        decision = row.get("decision_monotonic_ns")
        if isinstance(input_receive, int) and isinstance(decision, int) and input_receive > decision:
            return None
        for key, value in external.items():
            if finite(value):
                result["external." + str(key)] = float(value)
    for key in (
        "binance_return_100ms_bp", "coinbase_return_100ms_bp",
        "bybit_return_100ms_bp", "signal_return_bp", "signal_age_ns",
        "tte_ns", "bid_e4", "ask_e4", "ask_quantity",
    ):
        if finite(row.get(key)):
            result[key] = float(row[key])
    return result


def decision(row: dict[str, Any]) -> dict[str, Any] | None:
    if (
        row.get("schema") != "polymarket_v7_native_observation_v1"
        or row.get("kind") != 2
        or row.get("paper_only") is not True
        or row.get("execution_authority") is not False
        or row.get("signal_valid") is not True
        or row.get("confirmed_non_opposing") is not True
        or row.get("book_valid") is not True
    ):
        return None
    if row.get("authenticated_execution") not in (None, False):
        return None
    if row.get("real_order_submission") not in (None, False):
        return None
    features = numeric_features(row)
    if features is None:
        return None
    try:
        decision_mono = int(row["decision_monotonic_ns"])
        decision_wall = native_wall_ns(row)
        trigger = int(row["trigger_monotonic_ns"])
        receive = int(row["receive_monotonic_ns"])
        bid = int(row["bid_e4"]) / 10000
        ask = int(row["ask_e4"]) / 10000
        if (
            decision_mono <= 0 or decision_wall <= 0 or max(trigger, receive) > decision_mono
            or decision_mono - receive > 100_000_000 or not 0 < bid < ask < 1
        ):
            return None
        identity = [
            str(row["server_id"]), str(row["run_id"]), str(row["capture_id"]),
            str(row["market_id"]), str(row["token_id"]),
            int(row.get("signal_version") or 0), decision_mono,
        ]
        return {
            "identity": identity,
            "decision_id": canonical_hash(identity),
            "decision_wall_ns": decision_wall,
            "decision_monotonic_ns": decision_mono,
            "run_id": str(row["run_id"]),
            "server_id": str(row["server_id"]),
            "capture_id": str(row["capture_id"]),
            "market_id": str(row["market_id"]),
            "token_id": str(row["token_id"]),
            "asset": str(row.get("asset") or ""),
            "contract_horizon": str(row.get("horizon") or ""),
            "signal_version": int(row.get("signal_version") or 0),
            "features": features,
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def load_artifact(path: Path, expected_sha256: str) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    if not exact_hex(expected_sha256, 64) or file_sha256(path) != expected_sha256:
        raise ValueError("artifact hash mismatch")
    value = load_json(path)
    if (
        value.get("schema") != ARTIFACT_SCHEMA
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("real_capital_at_risk") is not False
        or value.get("automatic_promotion") is not False
    ):
        raise ValueError("artifact safety contract")
    models = value.get("executable_markout_models")
    if not isinstance(models, dict):
        raise ValueError("executable markout models missing")
    selected: dict[int, dict[str, Any]] = {}
    target = "future_executable_bid_minus_decision_ask_minus_entry_and_exit_taker_fees"
    for horizon in HORIZONS:
        spec = models.get(str(horizon))
        if not isinstance(spec, dict) or spec.get("state") != "READY" or spec.get("target") != target:
            raise ValueError(f"model unavailable:{horizon}")
        names, beta = spec.get("feature_names"), spec.get("beta")
        center, scale = spec.get("center"), spec.get("scale")
        if (
            not isinstance(names, list) or not isinstance(beta, list)
            or len(beta) != 1 + 2 * len(names)
            or not isinstance(center, dict) or not isinstance(scale, dict)
        ):
            raise ValueError(f"invalid model shape:{horizon}")
        selected[horizon] = spec
    return value, selected


def predict(features: dict[str, float], spec: dict[str, Any]) -> float:
    names = spec["feature_names"]
    vector = [1.0]
    for name in names:
        raw = features.get(name)
        missing = not finite(raw)
        center = float(spec["center"][name])
        scale = float(spec["scale"][name])
        value = center if missing else float(raw)
        vector.append((value - center) / scale)
    vector.extend(float(not finite(features.get(name))) for name in names)
    return float(sum(float(a) * float(b) for a, b in zip(spec["beta"], vector)))


class Tailer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.artifact, self.models = load_artifact(args.artifact, args.artifact_sha256)
        self.offsets: dict[str, int] = {}
        self.seen: set[str] = set()
        self.scored = self.timely = self.late = self.invalid = 0
        self.started_ns = time.time_ns()
        self.active_run_id = ""
        self.output = args.output.open("a", encoding="utf-8", buffering=1)
        self._restore_seen()
        self._bootstrapped = False

    def _restore_seen(self) -> None:
        if not self.args.output.exists():
            return
        try:
            for line in self.args.output.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row.get("schema") == SCHEMA and isinstance(row.get("decision_id"), str):
                    self.seen.add(row["decision_id"])
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    def manager_status(self) -> dict[str, Any]:
        path = self.args.run_root / "control/native_engine_manager_status.json"
        try:
            value = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        if (
            value.get("paper_only") is not True
            or value.get("authenticated_execution") is not False
            or value.get("real_order_submission") is not False
        ):
            return {}
        return value

    def files(self) -> list[Path]:
        status = self.manager_status()
        run_id = str(status.get("run_id") or "")
        if not run_id:
            return []
        self.active_run_id = run_id
        root = self.args.run_root / "research/native_observations" / run_id
        return sorted(root.glob("*.jsonl")) if root.is_dir() else []

    def bootstrap_existing_files(self) -> None:
        """Forward-only start: existing native bytes predate this shadow launch."""
        if self._bootstrapped:
            return
        for path in self.files():
            try:
                self.offsets[str(path)] = path.stat().st_size
            except OSError:
                continue
        self._bootstrapped = True

    def process_file(self, path: Path) -> None:
        key = str(path)
        offset = self.offsets.get(key, 0)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < offset:
            offset = 0
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    handle.seek(start)
                    break
                self.offsets[key] = handle.tell()
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    self.invalid += 1
                    continue
                item = decision(row)
                if item is None:
                    continue
                if item["decision_id"] in self.seen:
                    continue
                now_ns = time.time_ns()
                age_ns = max(0, now_ns - item["decision_wall_ns"])
                predictions = {str(h): predict(item["features"], self.models[h]) for h in HORIZONS}
                timely = age_ns <= self.args.maximum_inference_age_ms * 1_000_000
                record = {
                    "schema": SCHEMA,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "real_capital_at_risk": False,
                    "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
                    "automatic_promotion": False,
                    "research_only": True,
                    "artifact_sha256": self.args.artifact_sha256,
                    "source_code_sha": self.args.source_code_sha,
                    "runtime_model_sha": str(row.get("code_sha") or ""),
                    "run_id": item["run_id"],
                    "server_id": item["server_id"],
                    "capture_id": item["capture_id"],
                    "market_id": item["market_id"],
                    "token_id": item["token_id"],
                    "asset": item["asset"],
                    "contract_horizon": item["contract_horizon"],
                    "signal_version": item["signal_version"],
                    "decision_id": item["decision_id"],
                    "decision_wall_ns": item["decision_wall_ns"],
                    "decision_monotonic_ns": item["decision_monotonic_ns"],
                    "scored_wall_ns": now_ns,
                    "inference_age_ns": age_ns,
                    "maximum_inference_age_ms": self.args.maximum_inference_age_ms,
                    "forward_eligible": timely,
                    "prediction_semantics": "ROUNDTRIP_EXECUTABLE_MARKOUT_PER_SHARE",
                    "predictions": predictions,
                }
                self.output.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
                self.output.flush()
                self.seen.add(item["decision_id"])
                self.scored += 1
                self.timely += int(timely)
                self.late += int(not timely)

    def publish_status(self) -> None:
        atomic_json(self.args.status, {
            "schema": STATUS_SCHEMA,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "automatic_promotion": False,
            "artifact_sha256": self.args.artifact_sha256,
            "source_code_sha": self.args.source_code_sha,
            "horizons_ms": list(HORIZONS),
            "maximum_inference_age_ms": self.args.maximum_inference_age_ms,
            "started_ns": self.started_ns,
            "timestamp_ns": time.time_ns(),
            "active_run_id": self.active_run_id,
            "files_seen": len(self.offsets),
            "scored": self.scored,
            "timely": self.timely,
            "late": self.late,
            "invalid_json": self.invalid,
            "timely_fraction": self.timely / self.scored if self.scored else None,
            "state": "COLLECTING" if self.active_run_id else "AWAITING_NATIVE_RUN",
        })

    def run(self) -> None:
        started = time.monotonic()
        next_status = 0.0
        try:
            self.bootstrap_existing_files()
            while True:
                for path in self.files():
                    self.process_file(path)
                now = time.monotonic()
                if now >= next_status:
                    self.publish_status()
                    next_status = now + 1.0
                if self.args.duration_seconds and now - started >= self.args.duration_seconds:
                    break
                time.sleep(max(.001, self.args.poll_ms / 1000.0))
        finally:
            self.publish_status()
            self.output.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-root", type=Path, required=True)
    ap.add_argument("--artifact", type=Path, required=True)
    ap.add_argument("--artifact-sha256", required=True)
    ap.add_argument("--source-code-sha", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--poll-ms", type=int, default=10)
    ap.add_argument("--maximum-inference-age-ms", type=int, default=100)
    ap.add_argument("--duration-seconds", type=int, default=0)
    args = ap.parse_args()
    if not exact_hex(args.source_code_sha, 40):
        raise ValueError("exact source code SHA required")
    if not 1 <= args.poll_ms <= 1000 or not 1 <= args.maximum_inference_age_ms <= 500:
        raise ValueError("invalid timing policy")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.status.parent.mkdir(parents=True, exist_ok=True)
    Tailer(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
