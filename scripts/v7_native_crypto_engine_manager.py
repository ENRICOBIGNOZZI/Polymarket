#!/usr/bin/env python3
"""Cold-plane market rollover manager for the sole native V7 PAPER engine.

This process owns no economic decision, capital, OMS, inventory, ledger or order
submission authority. It selects the already-registered BTC/M5 market from the
canonical universe, resolves public CLOB metadata, launches exactly one native
engine, waits for clean terminal drain, settles PAPER evidence, then rotates.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

STATUS_SCHEMA = "polymarket_v7_native_engine_manager_status_v1"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def exact_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)


def public_json(url: str, timeout: float = 4.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-native-paper/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def select_market(snapshot: dict[str, Any], model_sha: str, *, now_s: int | None = None) -> dict[str, Any] | None:
    now = int(time.time() if now_s is None else now_s)
    if (
        snapshot.get("schema") != "polymarket_v7_crypto_universe_snapshot_v1"
        or snapshot.get("paper_only") is not True
        or snapshot.get("authenticated_execution") is not False
        or snapshot.get("real_order_submission") is not False
        or snapshot.get("execution_authority") is not False
        or snapshot.get("model_sha") != model_sha
        or snapshot.get("discovery_exhaustive") is not True
    ):
        return None
    rows = snapshot.get("markets")
    if not isinstance(rows, list):
        return None
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("asset") != "BTC" or row.get("horizon") != "M5":
            continue
        if row.get("research_only") is True:
            continue
        if row.get("active") is not True or row.get("closed") is True or row.get("accepting_orders") is not True:
            continue
        start = int(row.get("window_start_unix") or 0)
        horizon = int(row.get("horizon_seconds") or 0)
        tokens = row.get("clob_token_ids")
        outcomes = row.get("outcomes")
        events = row.get("event_ids")
        if start <= 0 or horizon != 300 or not (start <= now < start + horizon):
            continue
        if not isinstance(tokens, list) or len(tokens) != 2 or not all(str(x) for x in tokens):
            continue
        if not isinstance(outcomes, list) or len(outcomes) != 2:
            continue
        if not isinstance(events, list) or not events:
            continue
        candidates.append(row)
    if len(candidates) != 1:
        return None
    return candidates[0]


def tick_size_e4(token_id: str) -> int:
    query = urllib.parse.urlencode({"token_id": token_id})
    value = public_json("https://clob.polymarket.com/tick-size?" + query)
    if not isinstance(value, dict):
        raise RuntimeError("tick_size_response_invalid")
    raw = float(value.get("minimum_tick_size"))
    scaled = int(round(raw * 10_000.0))
    if not math.isfinite(raw) or raw <= 0 or scaled <= 0 or abs(raw * 10_000.0 - scaled) > 1e-8:
        raise RuntimeError("tick_size_invalid")
    if 10_000 % scaled != 0:
        raise RuntimeError("tick_size_not_canonical")
    return scaled


def fee_parameters(row: dict[str, Any]) -> tuple[float, float, str]:
    explicit = row.get("fees_enabled_explicit") is True
    enabled = row.get("fees_enabled") is True
    if explicit and not enabled:
        return 0.0, 1.0, "GAMMA_FEES_DISABLED_EXPLICIT"
    schedule = row.get("fee_schedule")
    if not enabled or not isinstance(schedule, dict):
        raise RuntimeError("fee_schedule_not_authoritative")
    try:
        rate = float(schedule["rate"])
        exponent = float(schedule["exponent"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("fee_schedule_invalid") from exc
    if not math.isfinite(rate) or rate < 0 or not math.isfinite(exponent) or exponent < 0:
        raise RuntimeError("fee_schedule_invalid")
    return rate, exponent, "GAMMA_FEE_SCHEDULE"


class Manager:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_root = args.run_root.resolve()
        self.status_path = self.run_root / "control" / "native_engine_manager_status.json"
        self.kill_path = self.run_root / "control" / "KILL"
        self.engine: subprocess.Popen[bytes] | None = None
        self.stopping = False
        self.last_market_id = ""
        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)

    def _signal(self, *_: object) -> None:
        self.stopping = True
        self._terminate_engine()

    def _terminate_engine(self) -> None:
        child = self.engine
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    def status(self, state: str, *, blocker: str = "", market: dict[str, Any] | None = None) -> None:
        atomic_json(self.status_path, {
            "schema": STATUS_SCHEMA,
            "timestamp_ms": time.time_ns() // 1_000_000,
            "state": state,
            "blocker": blocker,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "model_sha": self.args.model_sha,
            "run_id": self.args.run_id,
            "server_id": self.args.server_id,
            "single_native_hot_path": True,
            "engine_pid": self.engine.pid if self.engine and self.engine.poll() is None else 0,
            "market_id": str((market or {}).get("market_id") or ""),
            "window_start_unix": int((market or {}).get("window_start_unix") or 0),
            "hot_path_executable": str(self.args.engine),
        })

    def settle(self, market_id: str) -> bool:
        cmd = [
            self.args.python, str(self.args.settler),
            "--run-root", str(self.run_root),
            "--model-sha", self.args.model_sha,
            "--market-id", market_id,
            "--timeout-seconds", str(self.args.settlement_timeout_seconds),
        ]
        result = subprocess.run(cmd, cwd=self.args.repository_root, check=False)
        return result.returncode == 0

    def run_market(self, market: dict[str, Any]) -> int:
        tokens = [str(x) for x in market["clob_token_ids"]]
        outcomes = [str(x).strip().upper() for x in market["outcomes"]]
        if outcomes not in (["UP", "DOWN"], ["YES", "NO"]):
            # Gamma UP/DOWN contracts still map token index 0/1 canonically.
            if len(outcomes) != 2:
                raise RuntimeError("outcome_mapping_invalid")
        yes_token, no_token = tokens
        yes_tick = tick_size_e4(yes_token)
        no_tick = tick_size_e4(no_token)
        if yes_tick != no_tick:
            raise RuntimeError("complement_tick_size_mismatch")
        fee_rate, fee_exponent, fee_source = fee_parameters(market)
        close_wall_ns = (int(market["window_start_unix"]) + int(market["horizon_seconds"])) * 1_000_000_000
        if close_wall_ns <= time.time_ns():
            return 0
        event_id = str(market["event_ids"][0])
        command = [
            str(self.args.engine),
            "--yes-token", yes_token,
            "--no-token", no_token,
            "--run-root", str(self.run_root),
            "--model-sha", self.args.model_sha,
            "--run-id", self.args.run_id,
            "--server-id", self.args.server_id,
            "--market-id", str(market["market_id"]),
            "--event-id", event_id,
            "--fee-source", fee_source,
            "--close-wall-ns", str(close_wall_ns),
            "--tick-size-e4", str(yes_tick),
            "--min-order-microunits", str(self.args.min_order_microunits),
            "--taker-fee-rate", repr(fee_rate),
            "--taker-fee-exponent", repr(fee_exponent),
            "--duration-seconds", "0",
        ]
        self.args.engine_log.parent.mkdir(parents=True, exist_ok=True)
        with self.args.engine_log.open("ab", buffering=0) as log:
            self.engine = subprocess.Popen(
                command,
                cwd=self.args.repository_root,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
            )
            self.status("RUNNING", market=market)
            while self.engine.poll() is None:
                if self.stopping or self.kill_path.exists():
                    self._terminate_engine()
                    return 0
                self.status("RUNNING", market=market)
                time.sleep(1.0)
            rc = int(self.engine.returncode or 0)
        self.status("ENGINE_EXITED", blocker="" if rc == 0 else f"ENGINE_RC_{rc}", market=market)
        if rc != 0:
            return rc
        if not self.settle(str(market["market_id"])):
            self.status("SETTLEMENT_BLOCKED", blocker="NATIVE_PAPER_SETTLEMENT_INCOMPLETE", market=market)
            return 79
        self.status("ROTATED_CLEAN", market=market)
        return 0

    def run(self) -> int:
        self.status("STARTING")
        while not self.stopping and not self.kill_path.exists():
            snapshot = read_json(self.args.universe)
            market = select_market(snapshot, self.args.model_sha)
            if market is None:
                self.status("WAITING_FOR_CANONICAL_MARKET", blocker="BTC_M5_MARKET_NOT_READY")
                time.sleep(0.5)
                continue
            market_id = str(market["market_id"])
            if market_id == self.last_market_id:
                self.status("WAITING_FOR_ROLLOVER", market=market)
                time.sleep(0.5)
                continue
            rc = self.run_market(market)
            self.engine = None
            if rc != 0:
                return rc
            self.last_market_id = market_id
        self._terminate_engine()
        self.status("STOPPED")
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--settler", type=Path, required=True)
    parser.add_argument("--engine-log", type=Path, required=True)
    parser.add_argument("--python", default="python3")
    parser.add_argument("--settlement-timeout-seconds", type=int, default=120)
    parser.add_argument("--min-order-microunits", type=int, default=5_000_000)
    args = parser.parse_args()
    if not exact_sha(args.model_sha):
        parser.error("--model-sha must be exact lowercase 40-hex SHA")
    if args.settlement_timeout_seconds < 1 or args.settlement_timeout_seconds > 600:
        parser.error("invalid settlement timeout")
    if args.min_order_microunits <= 0:
        parser.error("invalid minimum order")
    return args


def main() -> int:
    args = parse_args()
    return Manager(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
