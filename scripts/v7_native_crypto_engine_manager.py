#!/usr/bin/env python3
"""Cold-plane manager for partitioned native V7 multi-crypto PAPER workers.

The manager remains the sole portfolio-level lifecycle owner. It grants one
bounded capital partition to each registered asset/horizon context, launches at
most one native worker per context, reconciles every worker through the single
canonical ledger writer, and never submits a real order.
"""
from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
from decimal import Decimal, InvalidOperation
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from v7_execution_ledger import native_order_id_matches, LedgerEvent, canonical_ledger_path, iter_events
from v7_ledger_spool import spool_event
from v7_native_risk_policy import load_native_limits, unsettled_exposure
from v7_legacy_native_claims import validate_registry as validate_legacy_claim_registry
from v7_native_settlement_projection import context_from_fill
from v7_market_execution_terms import snapshot as execution_terms_snapshot, persist as persist_execution_terms

STATUS_SCHEMA = "polymarket_v7_native_engine_manager_status_v1"


class RetryableLaunchError(RuntimeError):
    """Remote per-market metadata is temporarily unavailable.

    This must never tear down already-running native context workers.
    """


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


def public_json(url: str, timeout: float = 1.5) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-native-paper/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (TimeoutError, ConnectionError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RetryableLaunchError(f"remote_metadata_unavailable:{type(exc).__name__}") from exc


def _close_unix(row: dict[str, Any]) -> int:
    close = int(row.get("close_timestamp_unix") or 0)
    if close > 0:
        return close
    start = int(row.get("window_start_unix") or 0)
    horizon = int(row.get("horizon_seconds") or 0)
    return start + horizon if start > 0 and horizon > 0 else 0


def _context_key(row: dict[str, Any]) -> str:
    return f"{str(row.get('asset') or '')}:{str(row.get('horizon') or '')}"


def select_markets(
    snapshot: dict[str, Any], model_sha: str, *, now_s: int | None = None,
) -> dict[str, dict[str, Any]]:
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
        return {}
    rows = snapshot.get("markets")
    if not isinstance(rows, list):
        return {}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("research_only") is True:
            continue
        if row.get("active") is not True or row.get("closed") is True or row.get("accepting_orders") is not True:
            continue
        asset = str(row.get("asset") or "")
        horizon = str(row.get("horizon") or "")
        if asset not in {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}:
            continue
        if horizon not in {"M5", "M15", "H1", "H4", "D1"}:
            continue
        start = int(row.get("window_start_unix") or 0)
        close = _close_unix(row)
        tokens = row.get("clob_token_ids")
        outcomes = row.get("outcomes")
        events = row.get("event_ids")
        symbols = row.get("external_symbols")
        if start <= 0 or close <= start or not (start <= now < close):
            continue
        if not isinstance(tokens, list) or len(tokens) != 2 or not all(str(x) for x in tokens):
            continue
        if not isinstance(outcomes, list) or len(outcomes) != 2:
            continue
        if not isinstance(events, list) or not events:
            continue
        if not isinstance(symbols, dict) or not str(symbols.get("binance_spot") or ""):
            continue
        grouped.setdefault(_context_key(row), []).append(row)

    selected: dict[str, dict[str, Any]] = {}
    for key, candidates in grouped.items():
        # More than one currently active market for one registered context is an
        # ambiguous execution surface. Never choose one by score or API order.
        current = {str(row.get("market_id") or ""): row for row in candidates}
        if len(current) != 1:
            raise RuntimeError(f"ambiguous_active_context:{key}")
        selected[key] = next(iter(current.values()))
    return selected


def select_market(
    snapshot: dict[str, Any], model_sha: str, *, now_s: int | None = None,
) -> dict[str, Any] | None:
    """Backward-compatible BTC/M5 selector used by legacy contract tests."""
    try:
        return select_markets(snapshot, model_sha, now_s=now_s).get("BTC:M5")
    except RuntimeError:
        return None


@functools.lru_cache(maxsize=512)
def venue_metadata(token_id: str) -> tuple[int, int]:
    """Fetch immutable per-token venue terms once on the cold plane."""
    value = public_json(
        "https://clob.polymarket.com/book?"
        + urllib.parse.urlencode({"token_id": token_id})
    )
    if not isinstance(value, dict):
        raise RuntimeError("venue_metadata_response_invalid")
    try:
        raw_tick = float(value["tick_size"])
        quantity = Decimal(str(value["min_order_size"])) * 1_000_000
    except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
        raise RuntimeError("venue_metadata_missing") from exc
    scaled = int(round(raw_tick * 10_000.0))
    if (
        not math.isfinite(raw_tick)
        or raw_tick <= 0
        or scaled <= 0
        or abs(raw_tick * 10_000.0 - scaled) > 1e-8
        or 10_000 % scaled != 0
    ):
        raise RuntimeError("tick_size_invalid")
    if not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value():
        raise RuntimeError("venue_minimum_invalid")
    return scaled, int(quantity)


def tick_size_e4(token_id: str) -> int:
    return venue_metadata(token_id)[0]


def venue_minimum_microunits(token_id: str) -> int:
    return venue_metadata(token_id)[1]

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


def unsettled_native_markets(run_root: Path, model_sha: str) -> list[str]:
    path = canonical_ledger_path(run_root)
    if not path.is_file():
        return []
    fills: set[str] = set()
    finals: set[str] = set()
    for event in iter_events(path, expected_model_sha=model_sha):
        if event.strategy != "CRYPTO_SETTLEMENT_ENGINE" or not event.market_id:
            continue
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        receipt = metadata.get("native_settlement_receipt")
        if not isinstance(receipt, dict) or receipt.get("owner") != "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE":
            continue
        if event.event_type == "FILL":
            fills.add(event.market_id)
        elif (
            event.event_type == "FINAL"
            and metadata.get("native_market_settlement_id") == f"native-settlement:{event.market_id}"
        ):
            finals.add(event.market_id)
    return sorted(fills - finals)


def _native_receipt(event: LedgerEvent) -> dict[str, Any] | None:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    receipt = metadata.get("native_settlement_receipt")
    if not isinstance(receipt, dict):
        return None
    client = receipt.get("client_order_id")
    if (
        receipt.get("schema") != "polymarket_v7_native_settlement_receipt_v1"
        or receipt.get("owner") != "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"
        or receipt.get("engine_id") != "CRYPTO_SETTLEMENT_ENGINE"
        or receipt.get("model_sha") != event.model_sha
        or receipt.get("paper_only") is not True
        or receipt.get("authenticated_execution") is not False
        or receipt.get("real_order_submission") is not False
        or receipt.get("real_capital_at_risk") is not False
        or receipt.get("execution_mode") != "PAPER_SIMULATED"
        or receipt.get("single_owner") is not True
        or not isinstance(client, int) or isinstance(client, bool) or client <= 0
        or not native_order_id_matches(event)
    ):
        return None
    return receipt


def open_native_orders(run_root: Path, model_sha: str) -> dict[str, LedgerEvent]:
    path = canonical_ledger_path(run_root)
    if not path.is_file():
        return {}
    open_orders: dict[str, LedgerEvent] = {}
    terminal = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "LOST"}
    for event in iter_events(path, expected_model_sha=model_sha):
        if event.strategy != "CRYPTO_SETTLEMENT_ENGINE" or not event.order_id:
            continue
        receipt = _native_receipt(event)
        if receipt is None:
            continue
        if event.event_type == "ORDER_SUBMITTED":
            open_orders[event.order_id] = event
        elif event.event_type == "FILL" and event.complete is True:
            open_orders.pop(event.order_id, None)
        elif event.event_type == "ORDER_STATE" and str(event.order_state or "").upper() in terminal:
            open_orders.pop(event.order_id, None)
    return open_orders


def wait_for_record(run_root: Path, model_sha: str, record_id: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    path = canonical_ledger_path(run_root)
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                if any(event.record_id == record_id for event in iter_events(path, expected_model_sha=model_sha)):
                    return True
            except Exception:
                return False
        time.sleep(0.1)
    return False


def native_commit_barrier(run_root: Path, model_sha: str, run_id: str,
                          market_id: str, timeout: float = 10.0) -> bool:
    """Spool publication is not a canonical commit acknowledgement."""
    deadline = time.monotonic() + timeout
    prefix = f"{run_id}:{market_id}:native:"
    while time.monotonic() < deadline:
        per_market = run_root / "control/native_evidence" / (market_id + ".json")
        status = read_json(per_market if per_market.is_file() else run_root / "control/native_evidence_status.json")
        if (status.get("model_sha") == model_sha and status.get("run_id") == run_id
                and status.get("market_id") == market_id and status.get("healthy") is True
                and status.get("dropped") == 0 and status.get("published") == status.get("written")):
            try:
                events = list(iter_events(canonical_ledger_path(run_root), expected_model_sha=model_sha))
                committed = {event.record_id for event in events if event.record_id.startswith(prefix)}
                pending = list((run_root / "ledger/spool").glob(prefix + "*.json"))
                if len(committed) == int(status["written"]) and not pending:
                    return not any(str(event.market_id) == market_id for event in open_native_orders(run_root, model_sha).values())
            except (OSError, ValueError):
                pass
        time.sleep(0.1)
    return False

class CapitalLeaseUnavailable(RuntimeError):
    pass


@dataclass
class Worker:
    context: str
    market: dict[str, Any]
    budget_microdollars: int
    process: subprocess.Popen[bytes]
    log_handle: Any
    state: str = "RUNNING"
    settlement: subprocess.Popen[bytes] | None = None


@dataclass
class PendingSettlement:
    market_id: str
    process: subprocess.Popen[bytes]
    context: str = "RECOVERY"
    attempts: int = 1


def _execution_budget_microdollars(allocation_path: Path) -> int:
    value = read_json(allocation_path)
    scope = value.get("capital_scope") if isinstance(value.get("capital_scope"), dict) else {}
    if (
        scope.get("engine_id") != "CRYPTO_SETTLEMENT_ENGINE"
        or scope.get("scope_class") != "ENGINE_ENVELOPE"
        or scope.get("independent_capital_authority") is not False
    ):
        raise RuntimeError("canonical_allocation_scope_invalid")
    raw = scope.get("execution_budget")
    try:
        dollars = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("canonical_allocation_budget_invalid") from exc
    if not math.isfinite(dollars) or dollars <= 0:
        raise RuntimeError("canonical_allocation_budget_invalid")
    micros = int(math.floor(dollars * 1_000_000.0 + 1e-9))
    if micros <= 0:
        raise RuntimeError("canonical_allocation_budget_invalid")
    return micros


def _budget_after_legacy_claims(
    gross_microdollars: int, legacy_path: Path, target_sha: str,
) -> tuple[int, int, str]:
    if not isinstance(gross_microdollars, int) or isinstance(gross_microdollars, bool) or gross_microdollars <= 0:
        raise RuntimeError("gross_engine_budget_invalid")
    legacy = validate_legacy_claim_registry(read_json(legacy_path), target_sha=target_sha)
    claim = int(legacy["total_claim_microdollars"])
    if claim >= gross_microdollars:
        raise RuntimeError("legacy_claims_exhaust_engine_budget")
    return gross_microdollars - claim, claim, str(legacy["registry_sha256"])



def _signal_policy(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    value = read_json(path)
    contexts = value.get("contexts")
    expected = {f"{asset}:{horizon}" for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB") for horizon in ("M5", "M15", "H1", "H4", "D1")}
    if (
        value.get("schema") != "polymarket_v7_crypto_signal_policy_v2"
        or value.get("paper_only") is not True
        or value.get("real_order_submission") is not False
        or value.get("authenticated_execution") is not False
        or value.get("require_pm_book_pre_signal") is not True
        or not isinstance(contexts, dict)
        or set(contexts) != expected
    ):
        raise RuntimeError("crypto_signal_policy_invalid")
    normalized: dict[str, dict[str, Any]] = {}
    for context, row in contexts.items():
        if not isinstance(row, dict):
            raise RuntimeError("crypto_signal_policy_invalid")
        trigger = row.get("minimum_binance_return_bp")
        confirm = row.get("minimum_confirmation_return_bp")
        age = row.get("maximum_signal_age_ms")
        venue = row.get("confirmation_venue")
        if (
            not isinstance(trigger, (int, float)) or isinstance(trigger, bool)
            or not math.isfinite(float(trigger)) or not 0 < float(trigger) <= 1000
            or not isinstance(confirm, (int, float)) or isinstance(confirm, bool)
            or not math.isfinite(float(confirm)) or not 0 < float(confirm) <= 1000
            or not isinstance(age, int) or isinstance(age, bool) or not 1 <= age <= 5000
            or venue not in {"COINBASE", "BYBIT"}
        ):
            raise RuntimeError("crypto_signal_policy_invalid")
        normalized[context] = {
            "minimum_binance_return_bp": float(trigger),
            "minimum_confirmation_return_bp": float(confirm),
            "maximum_signal_age_ns": age * 1_000_000,
            "confirmation_venue": venue,
        }
    return normalized


def _enabled_contexts(registry_path: Path) -> set[str]:
    value = read_json(registry_path)
    rows = value.get("contexts") if isinstance(value.get("contexts"), list) else []
    contexts = {
        f"{str(row.get('asset') or '')}:{str(row.get('horizon') or '')}"
        for row in rows if isinstance(row, dict) and row.get("enabled") is True
        and row.get("research_only") is not True
    }
    if len(contexts) != 30:
        raise RuntimeError(f"paper_context_partition_invalid:{len(contexts)}")
    return contexts


def _enabled_context_count(registry_path: Path) -> int:
    return len(_enabled_contexts(registry_path))


def load_native_carryover(
    path: Path, model_sha: str, allowed_contexts: set[str], maximum_microdollars: int,
) -> dict[str, Any]:
    value = read_json(path)
    if not value:
        return {
            "present": False, "total_unsettled_microdollars": 0,
            "context_claims_microdollars": {}, "market_count": 0,
        }
    claims = value.get("context_claims_microdollars")
    markets = value.get("markets")
    if (
        value.get("schema") != "polymarket_v7_native_carryover_exposure_v1"
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("target_model_sha") != model_sha
        or not isinstance(claims, dict)
        or not isinstance(markets, list)
    ):
        raise RuntimeError("native_carryover_invalid")
    normalized: dict[str, int] = {}
    for context, claim in claims.items():
        if (
            context not in allowed_contexts
            or not isinstance(claim, int) or isinstance(claim, bool) or claim < 0
        ):
            raise RuntimeError("native_carryover_invalid")
        normalized[context] = claim
    total = value.get("total_unsettled_microdollars")
    if (
        not isinstance(total, int) or isinstance(total, bool) or total < 0
        or total != sum(normalized.values()) or total > maximum_microdollars
    ):
        raise RuntimeError("native_carryover_invalid")
    for row in markets:
        if not isinstance(row, dict):
            raise RuntimeError("native_carryover_invalid")
        if (
            not exact_sha(str(row.get("model_sha") or ""))
            or not str(row.get("market_id") or "")
            or row.get("context") not in allowed_contexts
            or not isinstance(row.get("claim_microdollars"), int)
            or isinstance(row.get("claim_microdollars"), bool)
            or row.get("claim_microdollars") < 0
        ):
            raise RuntimeError("native_carryover_invalid")
    return {
        "present": True,
        "total_unsettled_microdollars": total,
        "context_claims_microdollars": normalized,
        "market_count": len(markets),
    }


class Manager:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_root = args.run_root.resolve()
        self.status_path = self.run_root / "control" / "native_engine_manager_status.json"
        self.kill_path = self.run_root / "control" / "KILL"
        self.workers: dict[str, Worker] = {}
        self.completed_market_ids: set[str] = set()
        self.stopping = False
        self.pending_settlements: dict[str, PendingSettlement] = {}
        self.launch_retry_attempts: dict[str, int] = {}
        self.launch_retry_after: dict[str, float] = {}
        self.launch_retry_reasons: dict[str, str] = {}
        self.base_risk_receipt = load_native_limits(
            read_json(args.repository_root / "config/v7_native_risk_policy.json"),
            read_json(self.run_root / "control/allocations/manifest.json"))
        self.allocated_execution_budget_microdollars = _execution_budget_microdollars(args.allocation)
        self.gross_global_budget_microdollars = min(
            self.allocated_execution_budget_microdollars,
            self.base_risk_receipt["limits"]["max_total_exposure_microdollars"],
        )
        (
            self.global_budget_microdollars,
            self.legacy_claim_microdollars,
            self.legacy_claim_registry_sha256,
        ) = _budget_after_legacy_claims(
            self.gross_global_budget_microdollars, args.legacy_claims, args.model_sha
        )
        self.enabled_contexts = _enabled_contexts(args.market_registry)
        self.signal_policy = _signal_policy(args.signal_policy)
        self.signal_policy_sha256 = (
            hashlib.sha256(args.signal_policy.read_bytes()).hexdigest()
            if args.signal_policy is not None else ""
        )
        self.partition_count = len(self.enabled_contexts)
        self.native_carryover = load_native_carryover(
            self.run_root / "control/native_carryover_exposure.json",
            args.model_sha, self.enabled_contexts, self.global_budget_microdollars,
        )
        self.partition_microdollars = self.global_budget_microdollars // self.partition_count
        if self.partition_microdollars <= 0:
            raise RuntimeError("paper_partition_budget_zero")
        self.partition_total_microdollars = self.partition_microdollars * self.partition_count
        if self.partition_total_microdollars > self.global_budget_microdollars:
            raise RuntimeError("paper_partition_budget_exceeds_global")
        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)

    def _signal(self, *_: object) -> None:
        self.stopping = True
        self._terminate_all()

    def _terminate_process(self, child: subprocess.Popen[bytes] | None) -> None:
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    def _terminate_all(self) -> None:
        for worker in self.workers.values():
            self._terminate_process(worker.process)
            self._terminate_process(worker.settlement)
        for task in self.pending_settlements.values():
            self._terminate_process(task.process)

    def _worker_rows(self) -> list[dict[str, Any]]:
        rows = []
        for key, worker in sorted(self.workers.items()):
            market = worker.market
            rows.append({
                "context": key,
                "asset": str(market.get("asset") or ""),
                "horizon": str(market.get("horizon") or ""),
                "market_id": str(market.get("market_id") or ""),
                "window_start_unix": int(market.get("window_start_unix") or 0),
                "close_timestamp_unix": _close_unix(market),
                "state": worker.state,
                "engine_pid": worker.process.pid if worker.process.poll() is None else 0,
                "settlement_pid": (
                    worker.settlement.pid
                    if worker.settlement is not None and worker.settlement.poll() is None else 0
                ),
                "budget_microdollars": worker.budget_microdollars,
            })
        return rows

    def _aggregate_settlements(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        directory = self.run_root / "control" / "native_paper_settlement"
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                value = read_json(path)
                if (
                    value.get("schema") == "polymarket_v7_native_paper_settlement_status_v1"
                    and value.get("model_sha") == self.args.model_sha
                    and value.get("paper_only") is True
                    and value.get("authenticated_execution") is False
                    and value.get("real_order_submission") is False
                ):
                    rows.append(value)
        summary = {
            "schema": "polymarket_v7_native_paper_settlement_status_v1",
            "state": "MULTI_MARKET",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": self.args.model_sha,
            "timestamp_ms": time.time_ns() // 1_000_000,
            "markets": {
                str(row.get("market_id") or ""): str(row.get("state") or "")
                for row in rows if row.get("market_id")
            },
            "market_count": len(rows),
            "settled_count": sum(
                str(row.get("state") or "") in {"SETTLED", "ALREADY_SETTLED", "NO_POSITION"}
                for row in rows
            ),
            "blocked_count": sum(
                str(row.get("state") or "") == "LEDGER_APPEND_TIMEOUT" for row in rows
            ),
            "retryable_timeout_count": sum(
                str(row.get("state") or "") == "RESOLUTION_TIMEOUT" for row in rows
            ),
        }
        atomic_json(
            self.run_root / "control" / "native_paper_settlement_status.json", summary
        )
        return summary

    def _aggregate_evidence(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        directory = self.run_root / "control" / "native_evidence"
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                value = read_json(path)
                if (
                    value.get("schema") == "polymarket_v7_native_evidence_status_v1"
                    and value.get("model_sha") == self.args.model_sha
                    and value.get("run_id") == self.args.run_id
                    and value.get("paper_only") is True
                    and value.get("authenticated_execution") is False
                    and value.get("real_order_submission") is False
                ):
                    rows.append(value)
        aggregate = {
            "schema": "polymarket_v7_native_evidence_status_v1",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": self.args.model_sha,
            "run_id": self.args.run_id,
            "healthy": all(row.get("healthy") is True for row in rows),
            "published": sum(int(row.get("published") or 0) for row in rows),
            "written": sum(int(row.get("written") or 0) for row in rows),
            "dropped": sum(int(row.get("dropped") or 0) for row in rows),
            "queue_depth": sum(int(row.get("queue_depth") or 0) for row in rows),
            "observations_published": sum(int(row.get("observations_published") or 0) for row in rows),
            "observations_written": sum(int(row.get("observations_written") or 0) for row in rows),
            "observations_dropped": sum(int(row.get("observations_dropped") or 0) for row in rows),
            "observations_queue_depth": sum(int(row.get("observations_queue_depth") or 0) for row in rows),
            "timestamp_ms": time.time_ns() // 1_000_000,
            "worker_count": len(rows),
            "markets": sorted(str(row.get("market_id") or "") for row in rows if row.get("market_id")),
        }
        atomic_json(self.run_root / "control" / "native_evidence_status.json", aggregate)
        return aggregate

    def status(
        self, state: str, *, blocker: str = "",
        targets: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        workers = self._worker_rows()
        evidence = self._aggregate_evidence()
        settlements = self._aggregate_settlements()
        pids = [int(row["engine_pid"]) for row in workers if int(row["engine_pid"]) > 0]
        target_keys = sorted((targets or {}).keys())
        all_contexts = [
            f"{asset}:{horizon}"
            for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
            for horizon in ("M5", "M15", "H1", "H4", "D1")
        ]
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
            # Backward compatibility: one native executable/authority class,
            # even though the manager now runs bounded context partitions.
            "single_native_hot_path": True,
            "single_native_portfolio_owner": True,
            "partitioned_native_workers": True,
            "asynchronous_settlement": bool(getattr(self.args, "asynchronous_settlement", False)),
            "pending_settlement_markets": sorted(self.pending_settlements),
            "pending_settlement_count": len(self.pending_settlements),
            "pending_settlement_attempts": {
                market_id: task.attempts
                for market_id, task in sorted(self.pending_settlements.items())
            },
            "risk_policy_sha256": self.base_risk_receipt["risk_policy_sha256"],
            "signal_policy_sha256": self.signal_policy_sha256 or None,
            "signal_policy_context_count": len(self.signal_policy),
            "probability_model_configured": self.args.probability_model is not None,
            "probability_evaluation_end_wall_ns": self.args.probability_evaluation_end_wall_ns or None,
            "probability_evaluation_open": (
                self.args.probability_model is not None
                and time.time_ns() <= self.args.probability_evaluation_end_wall_ns
            ),
            "allocated_execution_budget_microdollars": self.allocated_execution_budget_microdollars,
            "engine_pid": min(pids) if pids else 0,
            "engine_pids": pids,
            "market_id": str(workers[0]["market_id"]) if workers else "",
            "window_start_unix": int(workers[0]["window_start_unix"]) if workers else 0,
            "hot_path_executable": str(self.args.engine),
            "hot_cpuset": str(os.environ.get("PM_V7_HOT_CPUSET") or ""),
            "hot_nice": int(os.environ.get("PM_V7_HOT_NICE") or 0),
            "workers": workers,
            "active_worker_count": len(pids),
            "managed_context_count": len(workers),
            "target_context_count": len(target_keys),
            "expected_context_count": self.partition_count,
            "target_contexts": target_keys,
            "missing_contexts": sorted(set(all_contexts) - set(target_keys)),
            "gross_global_budget_microdollars": self.gross_global_budget_microdollars,
            "legacy_claim_microdollars": self.legacy_claim_microdollars,
            "legacy_claim_registry_sha256": self.legacy_claim_registry_sha256,
            "global_budget_microdollars": self.global_budget_microdollars,
            "partition_budget_microdollars": self.partition_microdollars,
            "partition_count": self.partition_count,
            "partition_total_microdollars": self.partition_total_microdollars,
            "native_carryover_present": self.native_carryover["present"],
            "native_carryover_microdollars": self.native_carryover["total_unsettled_microdollars"],
            "native_carryover_market_count": self.native_carryover["market_count"],
            "new_risk_budget_microdollars": max(
                0, self.global_budget_microdollars - self.native_carryover["total_unsettled_microdollars"]
            ),
            "taker_target_quantity_microunits": self.args.target_quantity_microunits,
            "taker_maximum_entry_price_e4": self.args.maximum_entry_price_e4,
            "taker_minimum_tte_ns": self.args.minimum_tte_ns,
            "taker_maximum_tte_ns": self.args.maximum_tte_ns,
            "maker_share_cap_microunits": self.args.maker_share_cap_microunits,
            "launch_retry_contexts": sorted(self.launch_retry_attempts),
            "launch_retry_count": len(self.launch_retry_attempts),
            "launch_retry_attempts": dict(sorted(self.launch_retry_attempts.items())),
            "launch_retry_reasons": dict(sorted(self.launch_retry_reasons.items())),
            "native_capture_mode": (
                "FULL" if getattr(self.args, "capture_native_observations", False)
                else "SCOPED_FULL_REQUESTED" if getattr(self.args, "capture_native_full_context", [])
                else "DECISIONS" if getattr(self.args, "capture_native_decisions", False)
                else "OFF"
            ),
            "native_full_capture_contexts_requested": getattr(self.args, "capture_native_full_context", []),
            "native_full_capture_minimum_free_bytes": 20 * 1024**3,
            "evidence_worker_count": int(evidence.get("worker_count") or 0),
            "evidence_dropped": int(evidence.get("dropped") or 0),
            "evidence_queue_depth": int(evidence.get("queue_depth") or 0),
            "native_observations_published": int(evidence.get("observations_published") or 0),
            "native_observations_written": int(evidence.get("observations_written") or 0),
            "native_observations_dropped": int(evidence.get("observations_dropped") or 0),
            "native_observations_queue_depth": int(evidence.get("observations_queue_depth") or 0),
            "settlement_market_count": int(settlements.get("market_count") or 0),
            "settlement_blocked_count": int(settlements.get("blocked_count") or 0),
            "settlement_retryable_timeout_count": int(
                settlements.get("retryable_timeout_count") or 0
            ),
        })

    def _settlement_command(self, market_id: str) -> list[str]:
        return [
            self.args.python, str(self.args.settler),
            "--run-root", str(self.run_root),
            "--model-sha", self.args.model_sha,
            "--market-id", market_id,
            "--timeout-seconds", str(self.args.settlement_timeout_seconds),
        ]

    def settle_blocking(self, market_id: str) -> bool:
        child = subprocess.Popen(
            self._settlement_command(market_id), cwd=self.args.repository_root)
        while child.poll() is None:
            if self.stopping or self.kill_path.exists():
                self._terminate_process(child)
                return False
            self.status("RECOVERING_SETTLEMENT")
            time.sleep(1.0)
        return child.returncode == 0

    def _launch_command(
        self, market: dict[str, Any], budget_microdollars: int,
    ) -> list[str]:
        tokens = [str(x) for x in market["clob_token_ids"]]
        outcomes = [str(x).strip().upper() for x in market["outcomes"]]
        if len(tokens) != 2 or len(outcomes) != 2:
            raise RuntimeError("outcome_mapping_invalid")
        yes_token, no_token = tokens
        try:
            yes_tick = tick_size_e4(yes_token)
            no_tick = tick_size_e4(no_token)
            if yes_tick != no_tick:
                raise RuntimeError("complement_tick_size_mismatch")
            minimum_order = max(
                self.args.min_order_microunits,
                venue_minimum_microunits(yes_token),
                venue_minimum_microunits(no_token),
            )
        except RetryableLaunchError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            # CLOB terms are market-scoped remote metadata. Quarantine/retry
            # this context without taking down already-running contexts.
            raise RetryableLaunchError(f"clob_market_terms:{exc}") from exc
        if minimum_order > self.args.target_quantity_microunits:
            raise RetryableLaunchError("venue_minimum_exceeds_frozen_target_no_automatic_upsizing")
        fee_rate, fee_exponent, fee_source = fee_parameters(market)
        terms = execution_terms_snapshot(market, public_json)
        terms_path = persist_execution_terms(self.run_root, terms)
        atomic_json(self.run_root / "control/market_execution_terms" / (str(market["market_id"]) + ".json"), {
            "schema": terms["schema"], "state": terms["state"], "reason": terms["reason"],
            "market_id": terms["market_id"], "snapshot_sha256": terms["snapshot_sha256"],
            "observed_at_ns": terms["observed_at_ns"], "paper_only": True,
            "mandatory_taker_delay_ns": terms["mandatory_taker_delay_ns"],
            "immutable_snapshot": str(terms_path), "assumed_transport_delay_ns": 250_000_000,
            "transport_latency_measured": False})
        close_unix = _close_unix(market)
        close_wall_ns = close_unix * 1_000_000_000
        if close_wall_ns <= time.time_ns():
            raise RuntimeError("market_already_closed")
        events = market.get("event_ids")
        if not isinstance(events, list) or not events:
            raise RuntimeError("event_identity_missing")
        symbols = market.get("external_symbols")
        if not isinstance(symbols, dict):
            raise RuntimeError("external_symbols_missing")
        binance_symbol = str(symbols.get("binance_spot") or "")
        coinbase_symbol = str(symbols.get("coinbase_spot") or "NONE")
        bybit_symbol = str(symbols.get("bybit_spot") or "NONE")
        if not binance_symbol:
            raise RuntimeError("binance_spot_symbol_missing")
        signal_policy = self.signal_policy.get(_context_key(market))
        if self.signal_policy and signal_policy is None:
            raise RuntimeError("crypto_signal_policy_context_missing")
        if signal_policy and signal_policy["confirmation_venue"] == "COINBASE" and coinbase_symbol == "NONE":
            raise RetryableLaunchError("signal_confirmation_coinbase_missing")
        if signal_policy and signal_policy["confirmation_venue"] == "BYBIT" and bybit_symbol == "NONE":
            raise RetryableLaunchError("signal_confirmation_bybit_missing")
        max_market = min(budget_microdollars, self.base_risk_receipt["limits"]["max_market_exposure_microdollars"])
        max_order = min(max_market, self.base_risk_receipt["limits"]["max_single_order_microdollars"])
        command = [
            str(self.args.engine),
            "--asset", str(market["asset"]),
            "--horizon", str(market["horizon"]),
            "--binance-symbol", binance_symbol,
            "--coinbase-symbol", coinbase_symbol,
            "--bybit-symbol", bybit_symbol if signal_policy and signal_policy["confirmation_venue"] == "BYBIT" else "NONE",
            "--yes-token", yes_token,
            "--no-token", no_token,
            "--run-root", str(self.run_root),
            "--model-sha", self.args.model_sha,
            "--run-id", self.args.run_id,
            "--server-id", self.args.server_id,
            "--market-id", str(market["market_id"]),
            "--event-id", str(events[0]),
            "--fee-source", fee_source,
            "--paper-venue-delay-ns", str(terms["mandatory_taker_delay_ns"] if terms["state"] == "VERIFIED_SNAPSHOT" else -1),
            "--paper-assumed-transport-delay-ns", "250000000",
            "--paper-terms-sha256", terms["snapshot_sha256"],
            "--close-wall-ns", str(close_wall_ns),
            "--tick-size-e4", str(yes_tick),
            "--min-order-microunits", str(minimum_order),
            "--target-quantity-microunits", str(self.args.target_quantity_microunits),
            "--maximum-entry-price-e4", str(self.args.maximum_entry_price_e4),
            "--minimum-tte-ns", str(self.args.minimum_tte_ns),
            "--maximum-tte-ns", str(self.args.maximum_tte_ns),
            "--maker-share-cap-microunits", str(self.args.maker_share_cap_microunits),
            "--risk-policy-sha256", self.base_risk_receipt["risk_policy_sha256"],
            "--sleeve-budget-microdollars", str(budget_microdollars),
            "--max-total-exposure-microdollars", str(budget_microdollars),
            "--max-market-exposure-microdollars", str(max_market),
            "--max-single-order-microdollars", str(max_order),
            "--taker-fee-rate", repr(fee_rate),
            "--taker-fee-exponent", repr(fee_exponent),
            "--duration-seconds", "0",
        ]
        if signal_policy:
            command.extend([
                "--signal-policy-sha256", self.signal_policy_sha256,
                "--strict-signal-policy",
                "--confirmation-venue", str(signal_policy["confirmation_venue"]),
                "--minimum-absolute-binance-return-bp", repr(signal_policy["minimum_binance_return_bp"]),
                "--minimum-absolute-confirmation-return-bp", repr(signal_policy["minimum_confirmation_return_bp"]),
                "--maximum-signal-age-ns", str(signal_policy["maximum_signal_age_ns"]),
            ])
        probability_model = getattr(self.args, "probability_model", None)
        if probability_model is not None:
            command.extend(["--probability-model", str(probability_model)])
            command.extend(["--probability-evaluation-end-wall-ns",
                str(self.args.probability_evaluation_end_wall_ns)])
        scoped_full = _context_key(market) in getattr(self.args, "capture_native_full_context", [])
        full_requested = getattr(self.args, "capture_native_observations", False) or scoped_full
        capture_headroom = shutil.disk_usage(self.run_root).free >= 20 * 1024**3
        if full_requested and capture_headroom:
            command.append("--capture-native-observations")
        elif getattr(self.args, "capture_execution_windows", False):
            command.extend(["--capture-execution-windows", "--execution-window-ns",
                            str(self.args.execution_window_ns)])
        elif getattr(self.args, "capture_native_decisions", False) or full_requested:
            command.append("--capture-native-decisions")
        return command

    def _wrapped_hot_command(self, command: list[str]) -> list[str]:
        launch = list(command)
        hot_cpuset = str(os.environ.get("PM_V7_HOT_CPUSET") or "").strip()
        try:
            hot_nice = int(str(os.environ.get("PM_V7_HOT_NICE") or "0").strip())
        except ValueError as exc:
            raise RuntimeError("native_hot_nice_invalid") from exc
        if hot_nice < 0 or hot_nice > 19:
            raise RuntimeError("native_hot_nice_invalid")
        if os.name == "posix" and sys.platform.startswith("linux") and hot_cpuset:
            taskset = shutil.which("taskset")
            if not taskset:
                raise RuntimeError("native_hot_taskset_missing")
            launch = [taskset, "-c", hot_cpuset] + launch
        if hot_nice > 0:
            nice = shutil.which("nice")
            if not nice:
                raise RuntimeError("native_hot_nice_binary_missing")
            launch = [nice, "-n", str(hot_nice)] + launch
        return launch

    def _context_lease(self, context: str) -> int:
        ledger = canonical_ledger_path(self.run_root)
        rows = [asdict(event) for event in iter_events(ledger, expected_model_sha=self.args.model_sha)] if ledger.is_file() else []
        exposure = unsettled_exposure(rows, self.args.model_sha)
        contexts = {}
        for row in rows:
            if row.get("event_type") == "FILL":
                label = context_from_fill(row)
                key = str(row["market_id"])
                current = f"{label['asset']}:{label['horizon']}"
                if key in contexts and contexts[key] != current:
                    raise ValueError("market has conflicting capital contexts")
                contexts[key] = current
        current_used = sum(
            claim for market, claim in exposure["unsettled_market_claims_microdollars"].items()
            if contexts.get(market) == context
        )
        carryover_used = int(self.native_carryover["context_claims_microdollars"].get(context, 0))
        used = current_used + carryover_used
        available = self.partition_microdollars - used
        atomic_json(self.run_root / "control/native_risk_leases" / (context.replace(":", "_") + ".json"), {
            "schema": "polymarket_v7_native_context_lease_v1", "context": context,
            "paper_only": True, "model_sha": self.args.model_sha, "run_id": self.args.run_id,
            "timestamp_ms": time.time_ns() // 1_000_000, "risk_policy_sha256": self.base_risk_receipt["risk_policy_sha256"],
            "partition_microdollars": self.partition_microdollars,
            "current_unsettled_microdollars": current_used,
            "carryover_unsettled_microdollars": carryover_used,
            "unsettled_microdollars": used,
            "available_microdollars": max(0, available)})
        if available <= 0:
            raise CapitalLeaseUnavailable("unresolved capital claim exhausts context allowance")
        return available

    def launch_worker(self, context: str, market: dict[str, Any]) -> Worker:
        budget_microdollars = self._context_lease(context)
        command = self._wrapped_hot_command(
            self._launch_command(market, budget_microdollars))
        log_dir = self.args.engine_log.parent
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_context = context.lower().replace(":", "_")
        log_path = log_dir / f"native_crypto_settlement_engine_{safe_context}.log"
        handle = log_path.open("ab", buffering=0)
        try:
            child = subprocess.Popen(
                command,
                cwd=self.args.repository_root,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
            )
        except Exception:
            handle.close()
            raise
        return Worker(
            context=context, market=market,
            budget_microdollars=budget_microdollars,
            process=child, log_handle=handle,
        )

    def _start_pending_settlement(
        self, market_id: str, *, context: str = "RECOVERY", attempts: int = 1,
    ) -> None:
        if market_id in self.pending_settlements:
            return
        child = subprocess.Popen(
            self._settlement_command(market_id), cwd=self.args.repository_root
        )
        self.pending_settlements[market_id] = PendingSettlement(
            market_id=market_id, process=child, context=context, attempts=attempts,
        )

    def _retryable_settlement_timeout(self, market_id: str, rc: int) -> bool:
        if rc != 79:
            return False
        status = read_json(
            self.run_root / "control" / "native_paper_settlement" / f"{market_id}.json"
        )
        return (
            status.get("schema") == "polymarket_v7_native_paper_settlement_status_v1"
            and status.get("model_sha") == self.args.model_sha
            and status.get("market_id") == market_id
            and status.get("paper_only") is True
            and status.get("authenticated_execution") is False
            and status.get("real_order_submission") is False
            and status.get("state") == "RESOLUTION_TIMEOUT"
        )

    def _finish_worker(self, key: str, worker: Worker) -> int | None:
        if worker.state == "RUNNING":
            rc = worker.process.poll()
            if rc is None:
                return None
            worker.log_handle.close()
            if rc != 0:
                return int(rc)
            if not native_commit_barrier(self.run_root, self.args.model_sha, self.args.run_id,
                                         str(worker.market["market_id"])):
                return 75
            worker.state = "SETTLING"
            market_id = str(worker.market["market_id"])
            if getattr(self.args, "asynchronous_settlement", False):
                self._start_pending_settlement(market_id, context=key)
                self.workers.pop(key, None)
            else:
                worker.settlement = subprocess.Popen(
                    self._settlement_command(market_id),
                    cwd=self.args.repository_root,
                )
            return None
        if worker.state == "SETTLING":
            assert worker.settlement is not None
            rc = worker.settlement.poll()
            if rc is None:
                return None
            if rc != 0:
                return int(rc)
            self.completed_market_ids.add(str(worker.market["market_id"]))
            self.workers.pop(key, None)
        return None

    def _reap_pending_settlements(self) -> tuple[str, int] | None:
        for market_id, task in list(self.pending_settlements.items()):
            rc = task.process.poll()
            if rc is None:
                continue
            if rc == 0:
                self.completed_market_ids.add(market_id)
                self.pending_settlements.pop(market_id, None)
                continue
            if self._retryable_settlement_timeout(market_id, int(rc)):
                attempts = task.attempts + 1
                self.pending_settlements.pop(market_id, None)
                self._start_pending_settlement(
                    market_id, context=task.context, attempts=attempts
                )
                continue
            return market_id, int(rc)
        return None

    def recover(self) -> int:
        for order_id, submitted in open_native_orders(
            self.run_root, self.args.model_sha
        ).items():
            receipt = _native_receipt(submitted)
            if receipt is None:
                self.status("RECOVERY_BLOCKED", blocker="NATIVE_OPEN_ORDER_RECEIPT_INVALID")
                return 79
            recovered = LedgerEvent(
                event_type="ORDER_STATE",
                strategy="CRYPTO_SETTLEMENT_ENGINE",
                model_sha=self.args.model_sha,
                model_version="native-paper-engine",
                order_id=order_id,
                market_id=submitted.market_id,
                event_id=submitted.event_id,
                token_id=submitted.token_id,
                side=submitted.side,
                order_state="CANCELLED",
                metadata={
                    **(submitted.metadata if isinstance(submitted.metadata, dict) else {}),
                    "native_recovery_cancel": True,
                    "recovery_reason": "PROCESS_RESTART_CANONICAL_COMMIT_BOUNDARY",
                },
            )
            spool_event(self.run_root, recovered)
            if not wait_for_record(
                self.run_root, self.args.model_sha, recovered.record_id
            ):
                self.status(
                    "RECOVERY_BLOCKED",
                    blocker="NATIVE_RECOVERY_CANCEL_APPEND_TIMEOUT",
                )
                return 75
        for market_id in unsettled_native_markets(
            self.run_root, self.args.model_sha
        ):
            if getattr(self.args, "asynchronous_settlement", False):
                self._start_pending_settlement(market_id)
                continue
            self.status("RECOVERING_SETTLEMENT")
            if not self.settle_blocking(market_id):
                self.status(
                    "SETTLEMENT_BLOCKED",
                    blocker="NATIVE_PAPER_SETTLEMENT_INCOMPLETE",
                )
                return 79
        return 0

    def run(self) -> int:
        lock_path = self.run_root / "control/native_engine_manager.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("native manager already owns this run root") from exc
            try:
                return self._run_owned()
            finally:
                self._terminate_all()

    def _launch_missing_workers(
        self, targets: dict[str, dict[str, Any]],
    ) -> tuple[str, Exception] | None:
        now = time.monotonic()
        for key, market in sorted(targets.items()):
            market_id = str(market["market_id"])
            if market_id in self.completed_market_ids or key in self.workers:
                continue
            if now < self.launch_retry_after.get(key, 0.0):
                continue
            try:
                self.workers[key] = self.launch_worker(key, market)
            except CapitalLeaseUnavailable:
                continue
            except RetryableLaunchError as exc:
                attempts = self.launch_retry_attempts.get(key, 0) + 1
                self.launch_retry_attempts[key] = attempts
                self.launch_retry_reasons[key] = str(exc)[:240]
                self.launch_retry_after[key] = time.monotonic() + min(2.0, 0.25 * (2 ** min(attempts - 1, 3)))
                continue
            except (OSError, RuntimeError, ValueError) as exc:
                return key, exc
            else:
                self.launch_retry_attempts.pop(key, None)
                self.launch_retry_reasons.pop(key, None)
                self.launch_retry_after.pop(key, None)
        return None

    def _run_owned(self) -> int:
        self.status("STARTING")
        recovery = self.recover()
        if recovery != 0:
            return recovery

        while not self.stopping and not self.kill_path.exists():
            try:
                targets = select_markets(
                    read_json(self.args.universe), self.args.model_sha)
            except RuntimeError as exc:
                self.status("DISCOVERY_BLOCKED", blocker=str(exc))
                time.sleep(0.5)
                continue

            # Detached settlements retain their capital claim through the
            # canonical ledger. Resolution timeouts are expected for long
            # horizons and are retried without stopping unrelated contexts.
            settlement_failure = self._reap_pending_settlements()
            if settlement_failure is not None:
                market_id, rc = settlement_failure
                self.status(
                    "SETTLEMENT_BLOCKED",
                    blocker=f"{market_id}_RC_{rc}",
                    targets=targets,
                )
                return rc
            # Reap engines and settlements before launching replacement windows.
            for key, worker in list(self.workers.items()):
                rc = self._finish_worker(key, worker)
                if rc is not None:
                    self.status(
                        "ENGINE_EXITED" if worker.state == "RUNNING"
                        else "SETTLEMENT_BLOCKED",
                        blocker=f"{key}_RC_{rc}", targets=targets,
                    )
                    self._terminate_all()
                    return rc

            fatal_launch = self._launch_missing_workers(targets)
            if fatal_launch is not None:
                key, exc = fatal_launch
                self.status("LAUNCH_BLOCKED", blocker=f"{key}:{exc}", targets=targets)
                self._terminate_all()
                return 78

            if self.workers:
                self.status("RUNNING", targets=targets)
            else:
                self.status(
                    "WAITING_FOR_CANONICAL_MARKETS",
                    blocker="" if targets else "NO_REGISTERED_MARKET_READY",
                    targets=targets,
                )
            time.sleep(0.25)

        self._terminate_all()
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
    parser.add_argument("--allocation", type=Path, required=True)
    parser.add_argument("--market-registry", type=Path, required=True)
    parser.add_argument("--signal-policy", type=Path, default=None,
        help="Explicit per-asset PAPER trigger/confirmation policy")
    parser.add_argument("--legacy-claims", type=Path, required=True)
    parser.add_argument("--python", default="python3")
    parser.add_argument("--probability-model", type=Path, default=None,
        help="Explicit frozen experimental PAPER probability artifact; native loader verifies exact SHA")
    parser.add_argument("--probability-evaluation-end-wall-ns", type=int, default=0,
        help="Shared wall-clock end of the fixed 7200-second probability PAPER cohort")
    parser.add_argument("--settlement-timeout-seconds", type=int, default=600)
    parser.add_argument("--min-order-microunits", type=int, default=5_000_000)
    parser.add_argument("--target-quantity-microunits", type=int, default=5_000_000)
    parser.add_argument("--maximum-entry-price-e4", type=int, default=7_500)
    parser.add_argument("--minimum-tte-ns", type=int, default=105_000_000_000)
    parser.add_argument("--maximum-tte-ns", type=int, default=120_000_000_000)
    parser.add_argument("--maker-share-cap-microunits", type=int, default=1_000_000)
    parser.add_argument("--asynchronous-settlement", action="store_true")
    parser.add_argument("--capture-native-observations", action="store_true",
        help="Full bounded native book/trade + decision research capture")
    parser.add_argument("--capture-native-decisions", action="store_true",
        help="Low-volume native decision/intent capture; no raw book-event duplication")
    parser.add_argument("--capture-execution-windows", action="store_true",
        help="Capture bounded post-decision PM book/trade windows for execution modelling")
    parser.add_argument("--execution-window-ns", type=int, default=2_000_000_000)
    parser.add_argument("--capture-native-full-context", action="append", default=[],
        help="Record full native books only for this ASSET:HORIZON, with a 20 GiB free-space launch gate")
    args = parser.parse_args()
    if not exact_sha(args.model_sha):
        parser.error("--model-sha must be exact lowercase 40-hex SHA")
    if args.settlement_timeout_seconds < 1 or args.settlement_timeout_seconds > 600:
        parser.error("invalid settlement timeout")
    if not 0 < args.maker_share_cap_microunits <= 5_000_000:
        parser.error("invalid maker share cap")
    if args.min_order_microunits <= 0:
        parser.error("invalid minimum order")
    if args.target_quantity_microunits < args.min_order_microunits:
        parser.error("target quantity below configured minimum")
    if not 0 < args.maximum_entry_price_e4 < 10_000:
        parser.error("invalid maximum taker entry price")
    if args.minimum_tte_ns <= 0 or args.maximum_tte_ns < args.minimum_tte_ns:
        parser.error("invalid taker tte window")
    if not 1_000_000 <= args.execution_window_ns <= 10_000_000_000:
        parser.error("invalid execution capture window")
    if args.probability_model is not None and args.probability_evaluation_end_wall_ns <= 0:
        parser.error("probability model requires explicit evaluation end wall ns")
    if args.probability_model is None and args.probability_evaluation_end_wall_ns != 0:
        parser.error("probability evaluation end requires probability model")
    return args


def main() -> int:
    args = parse_args()
    return Manager(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
