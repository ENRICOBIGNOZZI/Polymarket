#!/usr/bin/env python3
"""Cold-plane market rollover owner for the single native V7 PAPER engine.

This process never decides, sizes, admits or simulates an order. It only starts
one exact-SHA native engine after universe, settlement-contract and fee evidence
all identify the same executable BTC/M5 market.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any

SHA40 = re.compile(r"^[0-9a-f]{40}$")
STATUS_SCHEMA = "polymarket_v7_native_engine_supervisor_status_v1"
SESSION_SCHEMA = "polymarket_v7_native_market_session_v1"


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def parse_end_ns(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


def exact_safety(value: dict[str, Any], sha: str) -> bool:
    return (
        value.get("model_sha", value.get("code_sha")) == sha
        and value.get("paper_only") is True
        and value.get("authenticated_execution") is False
        and value.get("real_order_submission") is False
    )


def executable_spec(
    universe: dict[str, Any],
    external: dict[str, Any],
    registry: dict[str, Any],
    *,
    sha: str,
    now_ms: int,
) -> tuple[dict[str, Any] | None, str]:
    if universe.get("schema") != "polymarket_v7_crypto_universe_snapshot_v1" or not exact_safety(universe, sha):
        return None, "UNIVERSE_NOT_READY"
    if external.get("schema") != "polymarket_v7_external_fair_status_v1" or not exact_safety(external, sha):
        return None, "SETTLEMENT_STATUS_NOT_READY"
    if registry.get("schema") != "polymarket_v7_fee_reward_registry_v1" or not exact_safety(registry, sha):
        return None, "FEE_REGISTRY_NOT_READY"

    market_status = external.get("market") if isinstance(external.get("market"), dict) else {}
    contract = external.get("contract") if isinstance(external.get("contract"), dict) else {}
    settlement = external.get("settlement_reference") if isinstance(external.get("settlement_reference"), dict) else {}
    oracle = external.get("oracle") if isinstance(external.get("oracle"), dict) else {}
    venue = external.get("external") if isinstance(external.get("external"), dict) else {}
    market_id = str(market_status.get("market_id") or "")
    if (
        not market_id
        or market_status.get("active") is not True
        or market_status.get("closed") is True
        or market_status.get("accepting_orders") is not True
        or contract.get("verified") is not True
        or contract.get("rules_hash_recognized") is not True
        or settlement.get("valid") is not True
        or oracle.get("healthy") is not True
        or venue.get("healthy") is not True
    ):
        return None, "MARKET_SETTLEMENT_OR_FEEDS_NOT_READY"

    candidates = [
        row for row in universe.get("markets", [])
        if isinstance(row, dict)
        and str(row.get("market_id") or "") == market_id
        and row.get("asset") == "BTC"
        and row.get("horizon") == "M5"
        and row.get("active") is True
        and row.get("closed") is False
        and row.get("accepting_orders") is True
    ]
    if len(candidates) != 1:
        return None, "BTC_M5_MARKET_IDENTITY_NOT_UNIQUE"
    market = candidates[0]
    token_ids = [str(x) for x in market.get("clob_token_ids", []) if str(x)]
    event_ids = [str(x) for x in market.get("event_ids", []) if str(x)]
    if len(token_ids) != 2 or not event_ids:
        return None, "BINARY_TOKEN_OR_EVENT_ID_MISSING"
    yes_token = str(market_status.get("yes_token") or "")
    no_token = str(market_status.get("no_token") or "")
    if [yes_token, no_token] != token_ids:
        return None, "TOKEN_ORDER_SEMANTICS_MISMATCH"

    close_wall_ns = parse_end_ns(market.get("end_date"))
    now_ns = now_ms * 1_000_000
    if close_wall_ns <= now_ns + 5_000_000_000:
        return None, "MARKET_TOO_CLOSE_TO_END"

    fees = [
        row for row in registry.get("markets", [])
        if isinstance(row, dict) and str(row.get("market_id") or "") == market_id
    ]
    if len(fees) != 1 or fees[0].get("executable_under_registry") is not True:
        return None, "FEE_NOT_EXECUTABLE"
    fee = fees[0].get("fee") if isinstance(fees[0].get("fee"), dict) else {}
    try:
        observed = int(fee.get("observed_at_ms") or 0)
        expires = int(fee.get("expires_at_ms") or 0)
        rate = float(fee["rate"])
        exponent = float(fee["exponent"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "FEE_SHAPE_INVALID"
    if (
        fee.get("verified") is not True
        or observed <= 0 or not (observed <= now_ms <= expires)
        or rate < 0.0 or exponent < 0.0
    ):
        return None, "FEE_STALE_OR_UNVERIFIED"

    return {
        "market_id": market_id,
        "event_id": event_ids[0],
        "yes_token": yes_token,
        "no_token": no_token,
        "close_wall_ns": close_wall_ns,
        "fee_rate": rate,
        "fee_exponent": exponent,
        "fee_source": str(fee.get("source") or "verified_fee_registry"),
        "rules_hash": str(contract.get("rules_hash") or ""),
        "settlement_reference_version": int(settlement.get("version") or 0),
    }, "READY"


class Supervisor:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.run_root = args.run_root.resolve()
        self.status_path = self.run_root / "control" / "native_engine_supervisor_status.json"
        self.sessions_path = self.run_root / "control" / "native_market_sessions.jsonl"
        self.kill_path = self.run_root / "control" / "KILL"
        self.completed: set[str] = set()
        for market_id, row in self._prior_sessions().items():
            if int(row.get("started_ms") or 0) > 0:
                self.completed.add(market_id)
        self.child: subprocess.Popen[str] | None = None
        self.stopping = False

    def _prior_sessions(self) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        if not self.sessions_path.is_file():
            return rows
        try:
            lines = self.sessions_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return rows
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(row, dict)
                and row.get("schema") == SESSION_SCHEMA
                and row.get("model_sha") == self.args.model_sha
                and row.get("paper_only") is True
                and row.get("authenticated_execution") is False
                and row.get("real_order_submission") is False
            ):
                market_id = str(row.get("market_id") or "")
                if market_id:
                    rows[market_id] = row
        return rows

    def publish(self, state: str, **extra: Any) -> None:
        atomic_json(self.status_path, {
            "schema": STATUS_SCHEMA,
            "timestamp_ms": int(time.time() * 1000),
            "state": state,
            "model_sha": self.args.model_sha,
            "run_id": self.args.run_id,
            "server_id": self.args.server_id,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": False,
            "native_hot_path": True,
            "child_pid": self.child.pid if self.child and self.child.poll() is None else 0,
            **extra,
        })

    def stop_child(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    def run_market(self, spec: dict[str, Any]) -> int:
        env = os.environ.copy()
        env["PM_V7_MODEL_SHA"] = self.args.model_sha
        env["PM_V7_RUN_ROOT"] = str(self.run_root)
        env["PM_V7_MAKER_POLICY"] = str(self.args.maker_policy)
        env["PM_V7_MAKER_EXECUTION_MODEL"] = str(self.run_root / "micro_maker" / "execution_model.json")
        command = [
            str(self.args.engine_binary),
            "--run-root", str(self.run_root),
            "--model-sha", self.args.model_sha,
            "--run-id", self.args.run_id,
            "--server-id", self.args.server_id,
            "--market-id", spec["market_id"],
            "--event-id", spec["event_id"],
            "--yes-token", spec["yes_token"],
            "--no-token", spec["no_token"],
            "--close-wall-ns", str(spec["close_wall_ns"]),
            "--min-order-microunits", str(self.args.min_order_microunits),
            "--taker-fee-rate", str(spec["fee_rate"]),
            "--taker-fee-exponent", str(spec["fee_exponent"]),
            "--fee-source", spec["fee_source"],
            "--duration-seconds", "0",
        ]
        started_ms = int(time.time() * 1000)
        append_jsonl(self.sessions_path, {
            "schema": SESSION_SCHEMA,
            "model_sha": self.args.model_sha,
            "run_id": self.args.run_id,
            "server_id": self.args.server_id,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "market_id": spec["market_id"],
            "event_id": spec["event_id"],
            "yes_token": spec["yes_token"],
            "no_token": spec["no_token"],
            "close_wall_ns": spec["close_wall_ns"],
            "started_ms": started_ms,
            "ended_ms": 0,
            "engine_returncode": None,
            "forced_rollover": False,
            "state": "STARTED",
        })
        self.child = subprocess.Popen(command, env=env, text=True)
        self.publish("RUNNING", market_id=spec["market_id"], market_started_ms=started_ms)
        forced_rollover = False
        while self.child.poll() is None:
            if self.stopping or self.kill_path.exists():
                self.stop_child()
                return 0
            external = load(self.args.external_status)
            market = external.get("market") if isinstance(external.get("market"), dict) else {}
            if (
                exact_safety(external, self.args.model_sha)
                and str(market.get("market_id") or "") == spec["market_id"]
                and (market.get("closed") is True or market.get("accepting_orders") is False)
            ):
                forced_rollover = True
                self.stop_child()
                break
            time.sleep(0.25)
        returncode = int(self.child.returncode or 0)
        ended_ms = int(time.time() * 1000)
        append_jsonl(self.sessions_path, {
            "schema": SESSION_SCHEMA,
            "model_sha": self.args.model_sha,
            "run_id": self.args.run_id,
            "server_id": self.args.server_id,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "market_id": spec["market_id"],
            "event_id": spec["event_id"],
            "yes_token": spec["yes_token"],
            "no_token": spec["no_token"],
            "close_wall_ns": spec["close_wall_ns"],
            "started_ms": started_ms,
            "ended_ms": ended_ms,
            "engine_returncode": returncode,
            "forced_rollover": forced_rollover,
            "state": "ENDED",
        })
        self.child = None
        if returncode != 0 and not forced_rollover:
            self.publish("ENGINE_FAILED", market_id=spec["market_id"], engine_returncode=returncode)
            return returncode
        self.completed.add(spec["market_id"])
        self.publish("ROLLED_OVER", market_id=spec["market_id"], engine_returncode=returncode)
        return 0

    def run(self) -> int:
        if not SHA40.fullmatch(self.args.model_sha):
            raise SystemExit("model_sha:not_exact")
        if not self.args.engine_binary.is_file() or not os.access(self.args.engine_binary, os.X_OK):
            raise SystemExit("native_engine_binary:not_executable")
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.publish("WAITING_FOR_EXECUTABLE_MARKET")
        while not self.stopping and not self.kill_path.exists():
            now_ms = int(time.time() * 1000)
            spec, reason = executable_spec(
                load(self.args.universe),
                load(self.args.external_status),
                load(self.args.fee_registry),
                sha=self.args.model_sha,
                now_ms=now_ms,
            )
            if spec is None or spec["market_id"] in self.completed:
                self.publish("WAITING_FOR_EXECUTABLE_MARKET", blocker=reason)
                time.sleep(max(0.25, self.args.interval))
                continue
            result = self.run_market(spec)
            if result != 0:
                return result
        self.stop_child()
        self.publish("STOPPED")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--engine-binary", type=Path, required=True)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--external-status", type=Path, required=True)
    parser.add_argument("--fee-registry", type=Path, required=True)
    parser.add_argument("--maker-policy", type=Path, required=True)
    parser.add_argument("--min-order-microunits", type=int, default=5_000_000)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    if args.min_order_microunits <= 0:
        raise SystemExit("min_order_microunits:invalid")
    supervisor = Supervisor(args)

    def stop(_signum: int, _frame: object) -> None:
        supervisor.stopping = True
        supervisor.stop_child()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
