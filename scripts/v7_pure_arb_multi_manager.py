#!/usr/bin/env python3
"""Cold-plane owner for one native multi-market PureArb PAPER runtime.

This replaces the historical one-process-per-context fanout.  It owns no
pricing/economic decisions: it resolves verified venue terms, freezes one
config and supervises exactly one native hot-path process.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from v7_execution_ledger import canonical_ledger_path, iter_events
from v7_native_risk_policy import load_native_limits, remaining_capital_lease, unsettled_exposure

STATUS_SCHEMA = "polymarket_v7_native_engine_manager_status_v1"
CONFIG_SCHEMA = "polymarket_v7_pure_arb_multi_runtime_v1"
SELECTION_SCHEMA = "polymarket_v7_multi_crypto_book_selection_v1"
EXPECTED_CONTEXTS = {
    f"{asset}:{horizon}"
    for asset in ("BTC","ETH","SOL","XRP","DOGE","BNB")
    for horizon in ("M5","M15","H1","H4","D1")
}


def exact_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def public_json(url: str, timeout: float = 2.0) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": "polymarket-v7-pure-arb-multi-paper/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def venue_metadata(token_id: str) -> tuple[int, int]:
    value = public_json(
        "https://clob.polymarket.com/book?"
        + urllib.parse.urlencode({"token_id": token_id}))
    if not isinstance(value, dict):
        raise RuntimeError("venue_metadata_response_invalid")
    try:
        raw_tick = float(value["tick_size"])
        quantity = Decimal(str(value["min_order_size"])) * 1_000_000
    except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
        raise RuntimeError("venue_metadata_missing") from exc
    scaled = int(round(raw_tick * 10_000.0))
    if (not math.isfinite(raw_tick) or raw_tick <= 0 or scaled <= 0
            or abs(raw_tick * 10_000.0 - scaled) > 1e-8
            or 10_000 % scaled != 0):
        raise RuntimeError("tick_size_invalid")
    if (not quantity.is_finite() or quantity <= 0
            or quantity != quantity.to_integral_value()):
        raise RuntimeError("venue_minimum_invalid")
    return scaled, int(quantity)


def fee_parameters(row: dict[str, Any]) -> tuple[float, float]:
    explicit = row.get("fees_enabled_explicit") is True
    enabled = row.get("fees_enabled") is True
    if explicit and not enabled:
        return 0.0, 1.0
    schedule = row.get("fee_schedule")
    if not enabled or not isinstance(schedule, dict):
        raise RuntimeError("fee_schedule_not_authoritative")
    try:
        rate = float(schedule["rate"])
        exponent = float(schedule["exponent"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("fee_schedule_invalid") from exc
    if (not math.isfinite(rate) or rate < 0
            or not math.isfinite(exponent) or exponent < 0):
        raise RuntimeError("fee_schedule_invalid")
    return rate, exponent


def handle(namespace: str, value: str) -> int:
    digest = hashlib.sha256((namespace + "\0" + value).encode()).digest()
    out = int.from_bytes(digest[:8], "big")
    return out if out else 1


def load_risk(risk_policy: Path, allocation: Path) -> dict[str, Any]:
    policy = read_json(risk_policy)
    alloc = read_json(allocation)
    return load_native_limits(policy, alloc)


def ledger_state(
    run_root: Path, model_sha: str,
) -> tuple[dict[str, Any], dict[tuple[str, str], tuple[int, int]], set[str]]:
    ledger = canonical_ledger_path(run_root)
    if not ledger.is_file() or ledger.stat().st_size == 0:
        exposure = {
            "unsettled_market_claims_microdollars": {},
            "total_unsettled_microdollars": 0,
            "settled_markets": 0,
            "paper_only": True,
            "expected_model_sha": model_sha,
        }
        return exposure, {}, set()
    events = list(iter_events(ledger, expected_model_sha=model_sha))
    rows = [asdict(event) for event in events]
    exposure = unsettled_exposure(rows, model_sha)
    unsettled = set(exposure["unsettled_market_claims_microdollars"])
    inventory: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for event in events:
        if (event.event_type != "FILL" or event.market_id not in unsettled
                or not event.token_id or not event.side
                or event.filled_size is None or event.fill_price is None):
            continue
        qty = Decimal(str(event.filled_size))
        price = Decimal(str(event.fill_price))
        key = (event.market_id, event.token_id)
        held, basis = inventory.get(key, (Decimal(0), Decimal(0)))
        if event.side == "BUY":
            held += qty
            basis += qty * price
        elif event.side == "SELL":
            if held < qty:
                raise RuntimeError("ledger_inventory_unbacked_sell")
            basis = basis * (held - qty) / held if held else Decimal(0)
            held -= qty
        inventory[key] = (held, basis)
    converted: dict[tuple[str, str], tuple[int, int]] = {}
    for key, (held, basis) in inventory.items():
        quantity = int((held * 1_000_000).to_integral_value())
        collateral = int((basis * 1_000_000).to_integral_value())
        if quantity < 0 or collateral < 0:
            raise RuntimeError("ledger_inventory_negative")
        converted[key] = (quantity, collateral)
    return exposure, converted, unsettled



def build_config(
    selection: dict[str, Any], *, model_sha: str, risk: dict[str, Any],
    latency_tape: Path,
    inventory: dict[tuple[str, str], tuple[int, int]],
) -> tuple[dict[str, Any], list[str]]:
    if (selection.get("schema") != SELECTION_SCHEMA
            or selection.get("version") != 2
            or selection.get("model_sha") != model_sha
            or selection.get("paper_only") is not True
            or selection.get("authenticated_execution") is not False
            or selection.get("real_order_submission") is not False
            or selection.get("execution_authority") is not False
            or selection.get("selection_only") is not True):
        raise RuntimeError("book_selection_contract_invalid")
    rows = selection.get("markets")
    if not isinstance(rows, list):
        raise RuntimeError("book_selection_markets_invalid")

    current = [row for row in rows
               if isinstance(row, dict) and row.get("role") == "CURRENT"]
    contexts = {f"{row.get('asset')}:{row.get('horizon')}" for row in current}
    if len(current) != 30 or contexts != EXPECTED_CONTEXTS:
        raise RuntimeError("book_selection_current_30_contexts_required")

    # Include safe preload rows too. They share one socket worker and remain
    # gated by each market's wall-clock interval in the native detector.
    included = [row for row in rows if isinstance(row, dict)
                and row.get("role") in {"CURRENT", "NEXT"}]
    if not included or len(included) > 64:
        raise RuntimeError("multi_market_capacity_invalid")

    used: dict[int, str] = {}
    def unique(namespace: str, value: str) -> int:
        result = handle(namespace, value)
        prior = used.get(result)
        identity = namespace + ":" + value
        if prior is not None and prior != identity:
            raise RuntimeError("deterministic_handle_collision")
        used[result] = identity
        return result

    markets: list[dict[str, Any]] = []
    for row in included:
        market_id = str(row.get("market_id") or "")
        event_id = str(row.get("event_id") or "")
        yes_token = str(row.get("yes_token") or "")
        no_token = str(row.get("no_token") or "")
        start_ms = int(row.get("start_timestamp_ms") or 0)
        end_ms = int(row.get("end_timestamp_ms") or 0)
        if not all((market_id, event_id, yes_token, no_token)) or end_ms <= start_ms:
            raise RuntimeError("multi_market_identity_invalid")
        yes_tick, yes_min = venue_metadata(yes_token)
        no_tick, no_min = venue_metadata(no_token)
        rate, exponent = fee_parameters(row)
        minimum = max(yes_min, no_min)
        markets.append({
            "market_handle": unique("market", market_id),
            "event_handle": unique("event", event_id),
            "market_id": market_id,
            "event_id": event_id,
            "asset": str(row.get("asset") or ""),
            "horizon": str(row.get("horizon") or ""),
            "fee_source": ("GAMMA_FEES_DISABLED_EXPLICIT"
                           if row.get("fees_enabled_explicit") is True
                           and row.get("fees_enabled") is not True
                           else "GAMMA_FEE_SCHEDULE"),
            "yes_instrument_handle": unique("token", yes_token),
            "no_instrument_handle": unique("token", no_token),
            "market_start_wall_ms": start_ms,
            "market_end_wall_ms": end_ms,
            "maximum_leg_skew_ns": 100_000_000,
            "minimum_order_microunits": minimum,
            "fee_rate": rate,
            "fee_exponent": exponent,
            "reserve_per_share": 0.0005,
            "fee_verified": True,
            "yes_token": yes_token,
            "no_token": no_token,
            "yes_tick_size_e4": yes_tick,
            "no_tick_size_e4": no_tick,
            "yes_inventory_microunits":
                inventory.get((market_id, yes_token), (0, 0))[0],
            "no_inventory_microunits":
                inventory.get((market_id, no_token), (0, 0))[0],
            "yes_collateral_basis_microdollars":
                inventory.get((market_id, yes_token), (0, 0))[1],
            "no_collateral_basis_microdollars":
                inventory.get((market_id, no_token), (0, 0))[1],
        })
    limits = dict(risk["limits"])
    return ({
        "schema": CONFIG_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "pm_ws_url": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        "latency_tape": str(latency_tape),
        "run_root": str(latency_tape.parent.parent),
        "model_sha": model_sha,
        "run_id": "__RUNTIME_RUN_ID__",
        "server_id": "__RUNTIME_SERVER_ID__",
        "risk_policy_sha256": str(risk["risk_policy_sha256"]),
        "capital_limits": {
            "sleeve_budget_microdollars": int(limits["sleeve_budget_microdollars"]),
            "max_total_exposure_microdollars": int(limits["max_total_exposure_microdollars"]),
            "max_market_exposure_microdollars": int(limits["max_market_exposure_microdollars"]),
            "max_single_order_microdollars": int(limits["max_single_order_microdollars"]),
        },
        "markets": markets,
    }, sorted(contexts))


class Manager:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_root = args.run_root.resolve()
        self.status_path = self.run_root / "control/native_engine_manager_status.json"
        self.config_path = self.run_root / "control/pure_arb_multi_runtime.json"
        self.log_path = self.run_root / "native_pure_arb_multi_runtime.log"
        self.latency_tape = self.run_root / "control/pure_arb_latency.bin"
        self.kill_path = self.run_root / "control/KILL"
        self.child: subprocess.Popen[bytes] | None = None
        self.log_handle: Any = None
        self.stopping = False
        self.generation = ""
        self.contexts: list[str] = []
        self.base_risk = load_risk(args.risk_policy, args.allocation)
        self.current_risk = self.base_risk
        self.global_budget = int(self.base_risk["limits"]["sleeve_budget_microdollars"])
        self.unsettled_microdollars = 0
        self.settlements: dict[str, subprocess.Popen[bytes]] = {}
        self.settlement_retry_after: dict[str, float] = {}
        self.settlement_failures = 0

    def status(self, state: str, blocker: str = "") -> None:
        alive = self.child is not None and self.child.poll() is None
        pid = int(self.child.pid) if alive and self.child else 0
        count = 30 if alive and state == "RUNNING" else 0
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
            "single_native_portfolio_owner": True,
            "partitioned_native_workers": False,
            "worker_process_count": 1 if alive else 0,
            "asynchronous_settlement": False,
            "pending_settlement_markets": sorted(self.settlements),
            "pending_settlement_count": len(self.settlements),
            "risk_policy_sha256": self.base_risk["risk_policy_sha256"],
            "allocated_execution_budget_microdollars":
                int(self.base_risk["canonical_engine_budget_microdollars"]),
            "engine_pid": pid,
            "engine_pids": [pid] if pid else [],
            "hot_path_executable": str(self.args.engine),
            "hot_cpuset": str(os.environ.get("PM_V7_HOT_CPUSET") or ""),
            "hot_nice": int(os.environ.get("PM_V7_HOT_NICE") or 0),
            "workers": [],
            # Logical contexts, not operating-system workers.
            "active_worker_count": count,
            "managed_context_count": count,
            "target_context_count": 30,
            "expected_context_count": 30,
            "target_contexts": self.contexts,
            "missing_contexts": sorted(EXPECTED_CONTEXTS - set(self.contexts)),
            "global_budget_microdollars": self.global_budget,
            "partition_budget_microdollars": 0,
            "partition_count": 1,
            "partition_total_microdollars": self.global_budget,
            "native_carryover_present": self.unsettled_microdollars > 0,
            "native_carryover_microdollars": self.unsettled_microdollars,
            "native_carryover_market_count": len(self.settlements),
            "new_risk_budget_microdollars": max(0, self.global_budget - self.unsettled_microdollars),
            "settlement_blocked_count": self.settlement_failures,
            "pure_arb_native_shadow": False,
            "pure_arb_reserve_per_share": 0.0005,
            "pure_arb_max_leg_skew_ns": 100_000_000,
            "pure_arb_execution_authority": True,
            "launch_retry_contexts": [],
            "launch_retry_count": 0,
            "native_observations_published": 0,
            "native_observations_written": 0,
            "native_observations_dropped": 0,
            "native_observations_queue_depth": 0,
            "slow_context_publications": 0,
            "slow_context_failures": 0,
            "slow_context_error": "",
            "evidence_worker_count": 1 if alive else 0,
            "configuration_generation_sha256": self.generation or None,
        })

    def stop_child(self) -> None:
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=3)
        self.child = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None

    def launch(self, selection: dict[str, Any]) -> None:
        exposure, inventory, _ = ledger_state(self.run_root, self.args.model_sha)
        self.unsettled_microdollars = int(exposure["total_unsettled_microdollars"])
        lease = remaining_capital_lease(self.base_risk, exposure)
        if not lease.get("lease_available") or not isinstance(lease.get("limits"), dict):
            raise RuntimeError("native_capital_fully_reserved_by_unsettled_exposure")
        self.current_risk = lease
        config, contexts = build_config(
            selection, model_sha=self.args.model_sha, risk=lease,
            latency_tape=self.latency_tape, inventory=inventory)
        self.contexts = contexts
        self.generation = str(selection.get("generation_sha256") or "")
        config["run_id"] = self.args.run_id
        config["server_id"] = self.args.server_id
        atomic_json(self.config_path, config)
        command = [str(self.args.engine), "--config", str(self.config_path)]
        hot = str(os.environ.get("PM_V7_HOT_CPUSET") or "").strip()
        if hot:
            taskset = __import__("shutil").which("taskset")
            if not taskset:
                raise RuntimeError("taskset_missing_for_declared_hot_cpuset")
            command = [taskset, "-c", hot, *command]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("ab", buffering=0)
        self.child = subprocess.Popen(
            command, cwd=self.args.repository_root,
            stdout=self.log_handle, stderr=subprocess.STDOUT,
            env=os.environ.copy())
        self.status("RUNNING")

    def reap_settlements(self) -> None:
        for market_id, child in list(self.settlements.items()):
            rc = child.poll()
            if rc is None:
                continue
            del self.settlements[market_id]
            if rc == 0:
                self.settlement_retry_after.pop(market_id, None)
            elif rc == 79:
                self.settlement_retry_after[market_id] = time.monotonic() + 60.0
            else:
                self.settlement_failures += 1
                self.settlement_retry_after[market_id] = time.monotonic() + 60.0

    def start_settlements(self, selection: dict[str, Any]) -> None:
        self.reap_settlements()
        _, _, unsettled = ledger_state(self.run_root, self.args.model_sha)
        rows = selection.get("markets") if isinstance(selection, dict) else []
        current = {
            str(row.get("market_id") or "") for row in (rows or [])
            if isinstance(row, dict) and row.get("role") == "CURRENT"
        }
        now = time.monotonic()
        for market_id in sorted(unsettled - current):
            if len(self.settlements) >= 8:
                break
            if market_id in self.settlements or now < self.settlement_retry_after.get(market_id, 0.0):
                continue
            status = self.run_root / "control/native_paper_settlement" / f"{market_id}.json"
            command = [
                __import__("sys").executable, str(self.args.settler),
                "--run-root", str(self.run_root),
                "--model-sha", self.args.model_sha,
                "--market-id", market_id,
                "--timeout-seconds", "15",
                "--status-path", str(status),
            ]
            self.settlements[market_id] = subprocess.Popen(
                command, cwd=self.args.repository_root,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=os.environ.copy())

    def run(self) -> int:
        lock_path = self.run_root / "control/native_engine_manager.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("native manager already owns this run root") from exc

            def stop(_sig: int, _frame: Any) -> None:
                self.stopping = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)

            self.status("STARTING")
            while not self.stopping and not self.kill_path.exists():
                selection = read_json(self.args.selection)
                try:
                    if not selection:
                        raise RuntimeError("book_selection_unavailable")
                    generation = str(selection.get("generation_sha256") or "")
                    if not generation:
                        raise RuntimeError("book_selection_generation_missing")
                    self.start_settlements(selection)
                    if self.child is None:
                        self.launch(selection)
                    elif self.child.poll() is not None:
                        rc = int(self.child.returncode or 0)
                        self.status("ENGINE_EXITED", f"PURE_ARB_MULTI_RC_{rc}")
                        return rc if rc != 0 else 75
                    elif generation != self.generation:
                        # Whole-process rollover is cold-plane and preserves the
                        # one-worker hot-path invariant.  Never roll with an
                        # unresolved PAPER capital claim.
                        self.stop_child()
                        self.status("WAITING_FOR_ROLLOVER")
                        self.launch(selection)
                    else:
                        self.status("RUNNING")
                except (OSError, RuntimeError, ValueError,
                        urllib.error.URLError, json.JSONDecodeError) as exc:
                    self.status("WAITING_FOR_CANONICAL_MARKET", str(exc)[:240])
                    if self.child is None:
                        time.sleep(0.5)
                        continue
                    # Never kill a healthy single engine because remote cold
                    # metadata refresh is temporarily unavailable.
                time.sleep(0.5)
            self.stop_child()
            for child in self.settlements.values():
                if child.poll() is None:
                    child.terminate()
            for child in self.settlements.values():
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired: child.kill()
            self.settlements.clear()
            self.status("STOPPED")
            return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repository-root", type=Path, required=True)
    p.add_argument("--run-root", type=Path, required=True)
    p.add_argument("--model-sha", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--server-id", required=True)
    p.add_argument("--selection", type=Path, required=True)
    p.add_argument("--allocation", type=Path, required=True)
    p.add_argument("--risk-policy", type=Path, required=True)
    p.add_argument("--engine", type=Path, required=True)
    p.add_argument("--settler", type=Path, required=True)
    args = p.parse_args()
    if not exact_sha(args.model_sha):
        p.error("--model-sha must be exact lowercase 40-hex SHA")
    if not args.engine.is_file():
        p.error("--engine must exist")
    if not args.settler.is_file():
        p.error("--settler must exist")
    return args


def main() -> int:
    args = parse_args()
    try:
        return Manager(args).run()
    except Exception as exc:
        print(f"v7_pure_arb_multi_manager: {type(exc).__name__}: {exc}",
              file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
