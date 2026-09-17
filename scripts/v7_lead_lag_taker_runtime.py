#!/usr/bin/env python3
"""Frozen PAPER forward runtime for external-market lead/lag on BTC M5.

The process owns no real-money authority. It proposes exactly one TAKE per
eligible market to the global coordinator, waits for its PAPER receipt,
revalidates the current CLOB book without chasing, simulates a full FAK fill
only against visible top-of-book depth, and holds the PAPER position to
settlement. All economic events enter the canonical ledger through the spool.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import queue
import threading
import hashlib
import json
import math
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

from v7_crypto_settlement import load_registry as load_crypto_registry, require_context
from v7_execution_ledger import LedgerEvent
from v7_external_fair_paper_router import Book, fee_per_share, live_market_yes, parse_book
from v7_ledger_spool import spool_event
from v7_market_common import ClobBooksClient, finite, parse_array, request_json
from v7_opportunity import OpportunityEnvelope
from v7_disk_pressure import disk_pressure_status
from v7_fast_forward_ipc import request as fast_forward_request
from v7_hot_book_cache import HotBookCacheReader
from v7_file_event import AtomicReplaceWatcher, UnixDatagramJsonReceiver

SCHEMA = "polymarket_v7_lead_lag_taker_v1_status"
MANIFEST_SCHEMA = "polymarket_v7_lead_lag_taker_v1_forward_manifest"
EVENT_SCHEMA = "polymarket_v7_lead_lag_taker_v1_event"
MODEL_VERSION = "lead-lag-taker-v1-forward"
STRATEGY = "CRYPTO_SETTLEMENT_ENGINE"
COMPONENT = "crypto_informed_taker"
CAPACITY_BOOK_MAX_LEVELS = 2048
CAPACITY_BOOK_MAX_BYTES = 262_144


def capacity_book_evidence(book: Book, schedule: dict[str, Any], candidate_limit: float) -> tuple[dict[str, Any] | None, str | None]:
    """Bounded observational metadata; never truncate a ladder and never affect execution."""
    if len(book.asks) > CAPACITY_BOOK_MAX_LEVELS:
        return None, "TOO_MANY_ASK_LEVELS"
    value = {
        "schema": "polymarket_v7_lead_lag_capacity_book_v1",
        "book_snapshot_id": book.snapshot_id,
        "receive_ts_ms": book.receive_ts_ms,
        "ask_levels": [{"price": price, "size": quantity} for price, quantity in book.asks],
        "fee_schedule": schedule,
        "candidate_limit_price": candidate_limit,
        "observed_ladder_complete_as_received": True,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > CAPACITY_BOOK_MAX_BYTES:
        return None, "SERIALIZED_CAPACITY_BOOK_TOO_LARGE"
    return value, None


def load(path: Path) -> dict[str, Any]:
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


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload); os.fsync(fd)
    finally:
        os.close(fd)


class AsyncJsonlWriter:
    """Bounded single-writer audit queue used only by the opt-in hot path."""
    def __init__(self, path: Path, capacity: int = 4096) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=capacity)
        self.fallbacks = 0
        self._thread = threading.Thread(target=self._run, name="v7-lead-lag-audit", daemon=True)
        self._thread.start()

    def submit(self, value: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(dict(value))
        except queue.Full:
            self.fallbacks += 1
            append_jsonl(self.path, value)

    def _run(self) -> None:
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            while True:
                value = self.queue.get()
                if value is None:
                    self.queue.task_done()
                    return
                batch = [value]
                for _ in range(63):
                    try:
                        extra = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if extra is None:
                        self.queue.task_done()
                        self.queue.put_nowait(None)
                        break
                    batch.append(extra)
                payload = b"".join(
                    (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    for row in batch
                )
                os.write(fd, payload)
                os.fsync(fd)
                for _ in batch:
                    self.queue.task_done()
        finally:
            os.close(fd)

    def close(self) -> None:
        self.queue.put(None)
        self._thread.join(timeout=5.0)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()


def exact_sha(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 40 and all(ch in "0123456789abcdef" for ch in text)


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "version", "strategy_id", "paper_only", "authenticated_execution",
        "real_order_submission", "real_capital_at_risk", "research_only",
        "automatic_promotion", "asset", "horizon", "source_rule_id",
        "source_rule_sha256", "shock_source", "confirmation_source", "confirmation",
        "minimum_absolute_binance_return_bp", "minimum_tte_seconds",
        "maximum_tte_seconds", "maximum_signal_age_ms", "target_shares",
        "one_entry_per_market", "hold_to_settlement", "entry_side_rule",
        "order_type", "price_rule", "require_full_visible_depth",
        "coordinator_receipt_timeout_ms", "candidate_ttl_ms", "fast_poll_ms",
        "settlement_poll_ms", "forward_test",
    }
    if set(value) != required:
        raise RuntimeError("lead_lag_config_field_partition")
    if (value["schema"] != "polymarket_v7_lead_lag_taker_config_v1" or value["version"] != 1
            or value["strategy_id"] != "LEAD_LAG_TAKER_V1"):
        raise RuntimeError("lead_lag_config_identity")
    if not (value["paper_only"] is True and value["authenticated_execution"] is False
            and value["real_order_submission"] is False and value["real_capital_at_risk"] is False
            and value["research_only"] is True and value["automatic_promotion"] is False):
        raise RuntimeError("lead_lag_config_safety")
    if value["asset"] != "BTC" or value["horizon"] != "M5":
        raise RuntimeError("lead_lag_config_scope")
    if value["entry_side_rule"] != "UP_BUY_YES_DOWN_BUY_NO" or value["price_rule"] != "ARRIVAL_BEST_ASK_NO_CHASE":
        raise RuntimeError("lead_lag_config_rule")
    if value["one_entry_per_market"] is not True or value["hold_to_settlement"] is not True:
        raise RuntimeError("lead_lag_config_lifecycle")
    if value["require_full_visible_depth"] is not True:
        raise RuntimeError("lead_lag_config_depth")
    if not (0 < float(value["minimum_tte_seconds"]) < float(value["maximum_tte_seconds"]) <= 300):
        raise RuntimeError("lead_lag_config_tte")
    if not (0 < int(value["maximum_signal_age_ms"]) <= 10_000 and float(value["target_shares"]) > 0):
        raise RuntimeError("lead_lag_config_signal_or_size")
    forward = value["forward_test"]
    if not isinstance(forward, dict) or set(forward) != {
        "target_independent_markets", "minimum_positive_windows_before_canary",
        "automatic_promotion", "no_runtime_tuning",
    }:
        raise RuntimeError("lead_lag_config_forward")
    if int(forward["target_independent_markets"]) < 1 or forward["automatic_promotion"] is not False or forward["no_runtime_tuning"] is not True:
        raise RuntimeError("lead_lag_config_forward_contract")
    return value


def adjusted_tte(status: dict[str, Any], current_ns: int) -> float | None:
    cut = status.get("causal_observation") if isinstance(status.get("causal_observation"), dict) else {}
    cut = cut.get("cut") if isinstance(cut.get("cut"), dict) else {}
    fair = status.get("fair") if isinstance(status.get("fair"), dict) else {}
    raw = finite(cut.get("observed_tte_seconds"), math.nan)
    observed = int(cut.get("observed_wall_ns") or 0)
    if not math.isfinite(raw):
        raw = finite(fair.get("tte_seconds"), math.nan)
        observed = int(fair.get("calculated_wall_ns") or 0)
    if not math.isfinite(raw) or raw < 0:
        return None
    elapsed = max(0.0, (current_ns - observed) / 1e9) if 0 < observed <= current_ns else 0.0
    return max(0.0, raw - elapsed)


def signal_candidate(config: dict[str, Any], signal: dict[str, Any], status: dict[str, Any], *, model_sha: str, current_ns: int) -> tuple[dict[str, Any] | None, str]:
    if (signal.get("schema") != "polymarket_v7_btc_m5_external_cancel_live_signal_v2"
            or signal.get("code_sha") != model_sha
            or signal.get("rule_id") != config["source_rule_id"]
            or signal.get("rule_sha256") != config["source_rule_sha256"]
            or signal.get("paper_only") is not True
            or signal.get("authenticated_execution") is not False
            or signal.get("real_order_submission") is not False
            or signal.get("real_money_authority") is not False
            or signal.get("research_only") is not True
            or signal.get("shock_source") != config["shock_source"]
            or signal.get("confirmation_source") != config["confirmation_source"]
            or signal.get("confirmation") != config["confirmation"]
            or signal.get("confirmed_non_opposing") is not True):
        return None, "SIGNAL_CONTRACT_INVALID"
    version = int(signal.get("signal_version") or 0)
    trigger_ns = int(signal.get("trigger_receive_wall_ns") or 0)
    if version <= 0 or trigger_ns <= 0 or trigger_ns > current_ns:
        return None, "SIGNAL_CLOCK_INVALID"
    age_ms = (current_ns - trigger_ns) / 1e6
    if age_ms > int(config["maximum_signal_age_ms"]):
        return None, "SIGNAL_TOO_OLD"
    bp = finite(signal.get("binance_return_100ms_bp"), math.nan)
    if not math.isfinite(bp) or abs(bp) + 1e-12 < float(config["minimum_absolute_binance_return_bp"]):
        return None, "SIGNAL_TOO_WEAK"
    direction = str(signal.get("direction") or "")
    if direction not in {"UP", "DOWN"}:
        return None, "SIGNAL_DIRECTION_INVALID"
    if (status.get("code_sha") != model_sha or status.get("paper_only") is not True
            or status.get("authenticated_execution") is not False
            or status.get("real_order_submission") is not False):
        return None, "STATUS_IDENTITY_INVALID"
    market = status.get("market") if isinstance(status.get("market"), dict) else {}
    contract = status.get("contract") if isinstance(status.get("contract"), dict) else {}
    reference = status.get("settlement_reference") if isinstance(status.get("settlement_reference"), dict) else {}
    if (not str(market.get("market_id") or "") or market.get("accepting_orders") is not True
            or market.get("closed") is True or contract.get("verified") is not True
            or contract.get("rules_hash_recognized") is not True or reference.get("valid") is not True):
        return None, "MARKET_OR_SETTLEMENT_NOT_READY"
    tte = adjusted_tte(status, current_ns)
    if tte is None or not (float(config["minimum_tte_seconds"]) <= tte <= float(config["maximum_tte_seconds"])):
        return None, "TTE_OUTSIDE_FROZEN_WINDOW"
    outcome = "YES" if direction == "UP" else "NO"
    token = str(market.get("yes_token") if outcome == "YES" else market.get("no_token") or "")
    if not token:
        return None, "TOKEN_MISSING"
    return {
        "signal_version": version, "trigger_wall_ns": trigger_ns, "signal_age_ms": age_ms,
        "direction": direction, "outcome": outcome, "token_id": token,
        "market_id": str(market["market_id"]), "event_id": str(market.get("event_id") or ""),
        "tte_seconds": tte, "binance_return_100ms_bp": bp,
        "coinbase_return_100ms_bp": finite(signal.get("coinbase_return_100ms_bp"), math.nan),
    }, "ELIGIBLE"


class LeadLagRuntime:
    def __init__(self, root: Path, sha: str, config_path: Path, clob_url: str, gamma_url: str,
                 coordinator_ipc: Path | None = None, hot_book_cache: Path | None = None,
                 event_driven_signal: bool = False, signal_socket: Path | None = None):
        self.root, self.sha = root, sha
        self.config_path = config_path
        self.config = validate_config(load(config_path))
        self.protocol_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
        self.clob = ClobBooksClient(clob_url.rstrip("/"), 0.25)
        self.gamma_url = gamma_url.rstrip("/")
        self.coordinator_ipc = coordinator_ipc
        self.hot_book_cache = HotBookCacheReader(hot_book_cache, sha) if hot_book_cache else None
        self.event_driven_signal = bool(event_driven_signal)
        self.signal_socket_path = signal_socket
        self.signal_watcher: AtomicReplaceWatcher | None = None
        self.signal_receiver: UnixDatagramJsonReceiver | None = None
        self.signal_pump_stop = threading.Event()
        self.signal_pump_ready = threading.Event()
        self.signal_condition = threading.Condition()
        self.signal_generation = 0
        self.latest_signal: dict[str, Any] = {}
        self.signal_pump_thread: threading.Thread | None = None
        self.audit_writer = AsyncJsonlWriter(root / "research" / "lead_lag_taker_v1" / "events.jsonl") if coordinator_ipc else None
        self.settlement_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="v7-lead-lag-settlement")
        self.settlement_futures: dict[str, concurrent.futures.Future[Any]] = {}
        self.signal_path = root / "external_fair" / "external_cancel_signal.json"
        self.status_source = root / "external_fair" / "status.json"
        self.cached_status: dict[str, Any] = load(self.status_source)
        self.status_cache_stop = threading.Event()
        self.status_cache_thread: threading.Thread | None = None
        self.dir = root / "research" / "lead_lag_taker_v1"
        self.status_path = self.dir / "status.json"
        self.state_path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self.manifest_path = self.dir / "forward_manifest.json"
        contexts = load_crypto_registry(Path(__file__).resolve().parents[1] / "config/v7_crypto_settlement_markets.json")
        self.crypto_context = require_context(contexts, "BTC", "M5")
        self.state: dict[str, Any] = {
            "model_sha": sha, "protocol_hash": self.protocol_hash,
            "traded_markets": [], "positions": {}, "attempted_keys": [],
            "entries": 0, "settled": 0, "wins": 0, "realized_pnl": 0.0,
            "skip_reasons": {}, "candidate_count": 0, "receipt_count": 0,
            "arrival_rejections": 0, "last_event": {},
        }
        prior = load(self.state_path)
        if prior.get("model_sha") == sha and prior.get("protocol_hash") == self.protocol_hash:
            self.state.update(prior)
        self.traded_markets_set = set(self.state.get("traded_markets") or [])
        self.attempted_keys_set = set(self.state.get("attempted_keys") or [])
        self.runtime_identity = self._load_runtime_identity()
        self._write_manifest_once()
        self.publish("COLLECTING")

    def _publish_signal_snapshot(self, value: dict[str, Any]) -> None:
        if not isinstance(value, dict) or not value:
            return
        with self.signal_condition:
            self.latest_signal = value
            self.signal_generation += 1
            self.signal_condition.notify_all()

    def _signal_pump_loop(self) -> None:
        watcher = AtomicReplaceWatcher(self.signal_path)
        receiver = UnixDatagramJsonReceiver(self.signal_socket_path) if self.signal_socket_path else None
        self.signal_watcher = watcher
        self.signal_receiver = receiver
        try:
            initial = load(self.signal_path)
            if initial:
                self._publish_signal_snapshot(initial)
            self.signal_pump_ready.set()
            while not self.signal_pump_stop.is_set():
                value = None
                if receiver is not None:
                    value = receiver.wait_json(.100)
                    if value is None and watcher.wait(0.0):
                        value = load(self.signal_path)
                elif watcher.wait(.100):
                    value = load(self.signal_path)
                if isinstance(value, dict) and value:
                    self._publish_signal_snapshot(value)
        finally:
            if receiver is not None:
                receiver.close()
            watcher.close()
            self.signal_receiver = None
            self.signal_watcher = None
            self.signal_pump_ready.set()

    def start_signal_pump(self) -> None:
        if self.signal_pump_thread is not None:
            return
        self.signal_pump_stop.clear(); self.signal_pump_ready.clear()
        self.signal_pump_thread = threading.Thread(
            target=self._signal_pump_loop, name="v7-lead-lag-signal-pump", daemon=True)
        self.signal_pump_thread.start()
        if not self.signal_pump_ready.wait(1.0):
            raise RuntimeError("signal_pump_start_timeout")

    def stop_signal_pump(self) -> None:
        self.signal_pump_stop.set()
        with self.signal_condition:
            self.signal_condition.notify_all()
        thread = self.signal_pump_thread
        if thread is not None:
            thread.join(timeout=.5)
        self.signal_pump_thread = None

    def wait_signal_after(self, generation: int, timeout: float) -> tuple[int, dict[str, Any] | None]:
        with self.signal_condition:
            if self.signal_generation <= generation:
                self.signal_condition.wait_for(
                    lambda: self.signal_generation > generation or self.signal_pump_stop.is_set(),
                    timeout=max(0.0, timeout))
            if self.signal_generation <= generation:
                return generation, None
            return self.signal_generation, self.latest_signal

    def current_signal_snapshot(self) -> dict[str, Any]:
        with self.signal_condition:
            return self.latest_signal

    def _status_cache_loop(self) -> None:
        with AtomicReplaceWatcher(self.status_source) as watcher:
            while not self.status_cache_stop.is_set():
                if not watcher.wait(.100):
                    continue
                value = load(self.status_source)
                if (value.get("code_sha") == self.sha and value.get("paper_only") is True
                        and value.get("authenticated_execution") is False
                        and value.get("real_order_submission") is False):
                    self.cached_status = value

    def start_status_cache(self) -> None:
        if self.status_cache_thread is not None:
            return
        self.status_cache_stop.clear()
        self.status_cache_thread = threading.Thread(
            target=self._status_cache_loop, name="v7-lead-lag-status-cache", daemon=True)
        self.status_cache_thread.start()

    def stop_status_cache(self) -> None:
        self.status_cache_stop.set()
        thread = self.status_cache_thread
        if thread is not None:
            thread.join(timeout=.5)
        self.status_cache_thread = None

    def current_status(self) -> dict[str, Any]:
        if self.event_driven_signal:
            return self.cached_status
        return load(self.status_source)

    def _load_runtime_identity(self) -> dict[str, Any]:
        runtime = load(self.root / "control" / "runtime_status.json")
        if (runtime.get("model_sha") != self.sha or runtime.get("paper_only") is not True
                or runtime.get("authenticated_execution") is not False
                or runtime.get("real_order_submission") is not False):
            raise RuntimeError("lead_lag_runtime_identity_not_ready")
        return {key: runtime.get(key) for key in (
            "model_sha", "config_hash", "policy_hash", "run_id", "paper_only",
            "authenticated_execution", "real_order_submission",
        )}

    def _write_manifest_once(self) -> None:
        if self.manifest_path.exists():
            existing = load(self.manifest_path)
            if existing.get("code_sha") != self.sha or existing.get("protocol_hash") != self.protocol_hash:
                raise RuntimeError("lead_lag_forward_manifest_identity_mismatch")
            return
        runtime = self.runtime_identity
        atomic_json(self.manifest_path, {
            "schema": MANIFEST_SCHEMA, "version": 1, "strategy_id": "LEAD_LAG_TAKER_V1",
            "code_sha": self.sha, "protocol_hash": self.protocol_hash,
            "started_ms": now_ms(), "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "real_capital_at_risk": False,
            "automatic_promotion": False,
            "target_independent_markets": int(self.config["forward_test"]["target_independent_markets"]),
            "minimum_positive_windows_before_canary": int(self.config["forward_test"]["minimum_positive_windows_before_canary"]),
            "frozen_rule": self.config,
        })

    def persist(self) -> None:
        atomic_json(self.state_path, self.state)

    def event(self, kind: str, **extra: Any) -> None:
        row = {"schema": EVENT_SCHEMA, "timestamp_ns": time.time_ns(), "event": kind,
               "model_sha": self.sha, "protocol_hash": self.protocol_hash,
               "paper_only": True, "authenticated_execution": False,
               "real_order_submission": False, **extra}
        if self.audit_writer is not None:
            self.audit_writer.submit(row)
        else:
            append_jsonl(self.events_path, row)
        self.state["last_event"] = row

    def skip(self, reason: str) -> None:
        reasons = self.state.setdefault("skip_reasons", {})
        reasons[reason] = int(reasons.get(reason) or 0) + 1

    def _book_tokens(self, status: dict[str, Any]) -> tuple[str, str] | None:
        market = status.get("market") if isinstance(status.get("market"), dict) else {}
        yes, no = str(market.get("yes_token") or ""), str(market.get("no_token") or "")
        return (yes, no) if yes and no and yes != no else None

    def books(self, status: dict[str, Any]) -> dict[str, Book]:
        tokens = self._book_tokens(status)
        if tokens is None:
            return {}
        if self.hot_book_cache is not None:
            current_ms = now_ms(); output: dict[str, Book] = {}
            market_id = str((status.get("market") or {}).get("market_id") or "")
            for token in tokens:
                hot = self.hot_book_cache.read(token, maximum_age_ms=100, now_ms=current_ms)
                if hot is None or hot.market_id != market_id:
                    return {}
                output[token] = Book(
                    token, hot.bids, hot.asks, hot.tick_size, hot.min_order_size,
                    min(current_ms, hot.exchange_event_ns // 1_000_000),
                    hot.receive_wall_ms, hot.snapshot_id,
                )
            return output
        try:
            rows = self.clob.request_books(list(tokens))
        except Exception:
            return {}
        received = now_ms(); output: dict[str, Book] = {}
        for raw in rows if isinstance(rows, list) else []:
            book = parse_book(raw, received)
            if book is not None and book.token_id in tokens:
                output[book.token_id] = book
        return output

    @staticmethod
    def valid_receipt(receipt: dict[str, Any], replay_key: str) -> bool:
        return (receipt.get("selected_replay_key") == replay_key
                and receipt.get("action") == "TAKE"
                and receipt.get("paper_exploration_authorized") is True
                and receipt.get("paper_forward_test_authorized") is True
                and receipt.get("new_risk_authorized") is False
                and receipt.get("paper_only") is True
                and receipt.get("real_order_submission") is False)

    def direct_receipt(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        if self.coordinator_ipc is None:
            return None
        try:
            receipt = fast_forward_request(
                self.coordinator_ipc, envelope,
                timeout_seconds=max(0.001, int(self.config["coordinator_receipt_timeout_ms"]) / 1000.0),
            )
        except Exception:
            return None
        replay_key = str(envelope.get("deterministic_replay_key") or "")
        return receipt if self.valid_receipt(receipt, replay_key) else None

    def wait_receipt(self, replay_key: str) -> dict[str, Any] | None:
        path = self.root / "opportunities" / "receipts" / (replay_key.replace("/", "_") + ".json")
        deadline = time.monotonic() + int(self.config["coordinator_receipt_timeout_ms"]) / 1000.0
        while time.monotonic() < deadline:
            receipt = load(path)
            if self.valid_receipt(receipt, replay_key):
                path.unlink(missing_ok=True)
                return receipt
            time.sleep(0.005)
        return None

    def envelope(self, candidate: dict[str, Any], status: dict[str, Any], books: dict[str, Book], *, current_ns: int) -> dict[str, Any] | None:
        market = status.get("market") if isinstance(status.get("market"), dict) else {}
        market_yes = live_market_yes(books, market)
        book = books.get(candidate["token_id"])
        if market_yes is None or book is None or not book.asks:
            return None
        ask, visible = book.asks[0]
        size = float(self.config["target_shares"])
        if book.min_order_size > size + 1e-9 or visible + 1e-9 < size:
            return None
        schedule = market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {}
        fee_share = fee_per_share(ask, schedule)
        runtime = self.runtime_identity
        outcome_mid = market_yes if candidate["outcome"] == "YES" else 1.0 - market_yes
        replay = "lead-lag-taker-v1:" + stable_id(
            self.sha, self.protocol_hash, candidate["market_id"], candidate["signal_version"]
        )
        decision_ns = current_ns
        envelope = {
            "schema": "polymarket_v7_opportunity_envelope_v1", "version": 1,
            "model_sha": self.sha, "config_hash": str(runtime.get("config_hash") or ""),
            "policy_hash": str(runtime.get("policy_hash") or ""), "run_id": str(runtime.get("run_id") or ""),
            "source_snapshot_identity": book.snapshot_id,
            "engine_id": STRATEGY, "component_provenance": [COMPONENT],
            "market_id": candidate["market_id"], "event_id": candidate["event_id"],
            "contract_id": candidate["token_id"],
            "mapping_identity": self.crypto_context.settlement_semantic_hash,
            "crypto_context": {"asset": "BTC", "horizon": "M5",
                "contract_family": self.crypto_context.contract_family,
                "settlement_semantic_hash": self.crypto_context.settlement_semantic_hash,
                "authority": "PAPER_EXPLORATION", "research_only": False},
            "action": "TAKE", "side": "BUY",
            "decision_receive_timestamp_ns": decision_ns,
            "source_event_timestamps_ns": [int(candidate["trigger_wall_ns"]), book.receive_ts_ms * 1_000_000],
            "fair_value": {"lower": 0.0, "point": outcome_mid, "upper": 1.0},
            "conservative_expected_wealth_change": 0.0,
            "cost_vector": {"fee": size * fee_share, "slippage": 0.0, "unwind_loss": 0.0,
                "capital_cost": 0.0, "latency_cost": 0.0, "adverse_markout": 0.0, "rebate": 0.0},
            "cost_authority": {"fee": "AUTHORITATIVE", "slippage": "CONSERVATIVE_ZERO",
                "unwind_loss": "CONSERVATIVE_ZERO", "capital_cost": "CONSERVATIVE_ZERO",
                "latency_cost": "CONSERVATIVE_ZERO", "adverse_markout": "CONSERVATIVE_ZERO",
                "rebate": "CONSERVATIVE_ZERO"},
            "uncertainty": {"lower_bound": 0.0, "upper_bound": 1.0, "status": "IMMATURE"},
            "calibration_status": "NOT_APPLICABLE",
            "latency": {"profile_id": "lead-lag-taker-v1-forward", "profile_valid": False,
                "economic_percentile": "p99", "arrival_ns": max(0, decision_ns - int(candidate["trigger_wall_ns"]))},
            "capacity": {"executable_size": size, "depth_provenance": book.snapshot_id},
            "execution_plan": {"atomic_unit_id": replay, "execution_style": "SINGLE_LEG",
                "legs": [{"leg_id": "lead-lag-entry", "market_id": candidate["market_id"],
                    "contract_id": candidate["token_id"], "token_id": candidate["token_id"],
                    "side": "BUY", "target_quantity": size, "limit_price": ask,
                    "fee_authority": "AUTHORITATIVE"}],
                "partial_fill_plan": "CANCEL_REMAINDER", "timeout_ms": int(self.config["candidate_ttl_ms"]),
                "unwind_plan": "NONE"},
            "inventory_delta": size, "portfolio_exposure_delta": size * ask,
            "settlement": {"definition": "registry-verified BTC 5m Chainlink TWAP settlement binding",
                "source": "REGISTRY_VERIFIED_CHAINLINK_TWAP_60S", "verified": True},
            "eligible": True,
            "reasons": ["LEAD_LAG_TAKER_V1_FROZEN_FORWARD_TEST", "POLYMARKET_PRIOR_ONLY_NO_ABSOLUTE_FAIR",
                "ONE_ENTRY_PER_MARKET", "HOLD_TO_SETTLEMENT", "ARRIVAL_BEST_ASK_NO_CHASE"],
            "deterministic_replay_key": replay,
            "expires_at_ns": decision_ns + int(self.config["candidate_ttl_ms"]) * 1_000_000,
            "forward_test": {"mode": "PAPER_FORWARD_TEST", "strategy_id": "LEAD_LAG_TAKER_V1",
                "protocol_hash": self.protocol_hash, "research_only": True, "automatic_promotion": False,
                "one_entry_per_market": True, "hold_to_settlement": True,
                "entry_uses_absolute_fair": False, "probability_source": "POLYMARKET_PRIOR_ONLY"},
        }
        return OpportunityEnvelope.parse(envelope).raw

    def candidate_step(self, signal_override: dict[str, Any] | None = None) -> None:
        disk = disk_pressure_status(self.root)
        if disk["active"]:
            self.skip("DISK_PRESSURE"); return
        if any((self.root / "control" / name).exists() for name in ("CUTOVER_DRAIN", "KILL")):
            self.skip("RISK_FROZEN"); return
        target = int(self.config["forward_test"]["target_independent_markets"])
        if int(self.state.get("entries") or 0) >= target:
            self.skip("TARGET_ENTRIES_REACHED"); return
        current_ns = time.time_ns(); signal = signal_override if isinstance(signal_override, dict) else load(self.signal_path); status = self.current_status()
        candidate, reason = signal_candidate(self.config, signal, status, model_sha=self.sha, current_ns=current_ns)
        if candidate is None:
            self.skip(reason); return
        market_id = candidate["market_id"]
        if market_id in self.traded_markets_set:
            self.skip("MARKET_ALREADY_TRADED"); return
        attempt_key = f"{market_id}:{candidate['signal_version']}"
        if attempt_key in self.attempted_keys_set:
            self.skip("SIGNAL_ALREADY_ATTEMPTED"); return
        books = self.books(status)
        decision_ns = max(time.time_ns(), max((book.receive_ts_ms for book in books.values()), default=0) * 1_000_000)
        envelope = self.envelope(candidate, status, books, current_ns=decision_ns)
        if envelope is None:
            self.skip("CANDIDATE_BOOK_OR_DEPTH_INVALID"); return
        self.state.setdefault("attempted_keys", []).append(attempt_key)
        self.attempted_keys_set.add(attempt_key)
        if len(self.state["attempted_keys"]) > 5000:
            self.state["attempted_keys"] = self.state["attempted_keys"][-5000:]
            self.attempted_keys_set = set(self.state["attempted_keys"])
        self.state["candidate_count"] = int(self.state.get("candidate_count") or 0) + 1
        self.event("CANDIDATE", market_id=market_id, signal_version=candidate["signal_version"],
                   tte_seconds=candidate["tte_seconds"], replay_key=envelope["deterministic_replay_key"],
                   candidate_limit_price=envelope["execution_plan"]["legs"][0]["limit_price"])
        if self.coordinator_ipc is not None:
            receipt = self.direct_receipt(envelope)
        else:
            inbox = self.root / "opportunities" / "fast_forward_inbox" / (
                f"{current_ns}.lead-lag-taker-v1.{market_id}.{candidate['signal_version']}.json"
            )
            atomic_json(inbox, envelope)
            receipt = self.wait_receipt(envelope["deterministic_replay_key"])
        if receipt is None:
            self.skip("COORDINATOR_RECEIPT_TIMEOUT"); self.event("REJECTED", market_id=market_id, reason="COORDINATOR_RECEIPT_TIMEOUT"); return
        self.state["receipt_count"] = int(self.state.get("receipt_count") or 0) + 1
        arrival_ns = time.time_ns()
        arrival_signal = self.current_signal_snapshot() if self.event_driven_signal else load(self.signal_path)
        arrival_status = self.current_status()
        arrival, arrival_reason = signal_candidate(self.config, arrival_signal, arrival_status, model_sha=self.sha, current_ns=arrival_ns)
        if (arrival is None or arrival["market_id"] != market_id
                or arrival["signal_version"] != candidate["signal_version"]
                or arrival["direction"] != candidate["direction"]):
            self.state["arrival_rejections"] = int(self.state.get("arrival_rejections") or 0) + 1
            self.skip("ARRIVAL_" + arrival_reason); self.event("REJECTED", market_id=market_id, reason="ARRIVAL_" + arrival_reason); return
        arrival_books = self.books(arrival_status); market = arrival_status.get("market") or {}
        book = arrival_books.get(arrival["token_id"])
        market_yes = live_market_yes(arrival_books, market)
        if book is None or not book.asks or market_yes is None:
            self.state["arrival_rejections"] += 1; self.skip("ARRIVAL_BOOK_INVALID"); return
        ask, visible = book.asks[0]; candidate_limit = float(envelope["execution_plan"]["legs"][0]["limit_price"])
        size = float(self.config["target_shares"])
        if ask > candidate_limit + 1e-12 or book.min_order_size > size + 1e-9 or visible + 1e-9 < size:
            self.state["arrival_rejections"] += 1; self.skip("ARRIVAL_NO_CHASE_OR_DEPTH");
            self.event("REJECTED", market_id=market_id, reason="ARRIVAL_NO_CHASE_OR_DEPTH", ask=ask, limit=candidate_limit, visible=visible); return
        schedule = market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {}
        fee_share = fee_per_share(ask, schedule); fee = size * fee_share; entry_cost = size * ask
        decision_ms = max(now_ms(), book.receive_ts_ms); order_id = "lead-lag-order-" + stable_id(self.sha, market_id, arrival["signal_version"])
        fill_id = "lead-lag-fill-" + stable_id(order_id, book.snapshot_id); position_id = "lead-lag-position-" + stable_id(self.sha, market_id)
        resolution_due_ms = book.receive_ts_ms + max(0, int(round(arrival["tte_seconds"] * 1000.0)))
        metadata = {
            "component": COMPONENT, "model_family": "lead_lag_taker_v1", "paper_exploration": True,
            "paper_forward_test": True, "paper_bootstrap_probe": False, "economic_authority": "PAPER_EXPLORATION",
            "counterfactual": False, "excluded_from_portfolio_equity": False, "research_evidence_only": False,
            "coordinator_receipt": receipt, "outcome": arrival["outcome"], "protocol_hash": self.protocol_hash,
            "signal_version": arrival["signal_version"], "signal_direction": arrival["direction"],
            "signal_trigger_wall_ns": arrival["trigger_wall_ns"], "signal_age_ms_at_fill": arrival["signal_age_ms"],
            "binance_return_100ms_bp": arrival["binance_return_100ms_bp"],
            "coinbase_return_100ms_bp": arrival["coinbase_return_100ms_bp"],
            "tte_seconds_at_fill": arrival["tte_seconds"], "candidate_limit_price": candidate_limit,
            "arrival_best_ask": ask, "arrival_best_ask_size": visible, "arrival_fee_per_share": fee_share,
            "arrival_pm_yes": market_yes, "resolution_due_ms": resolution_due_ms,
            "hold_to_settlement": True, "entry_uses_absolute_fair": False,
            "probability_source": "POLYMARKET_PRIOR_ONLY", "cost_vector_complete": True,
        }
        common = dict(strategy=STRATEGY, model_sha=self.sha, model_version=MODEL_VERSION,
            opportunity_id=envelope["deterministic_replay_key"], candidate_id=envelope["deterministic_replay_key"],
            order_id=order_id, position_id=position_id, market_id=market_id, event_id=arrival["event_id"],
            token_id=arrival["token_id"], decision_ts_ms=decision_ms, exchange_ts_ms=book.exchange_ts_ms,
            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id, side="BUY")
        order_metadata = dict(metadata)
        capacity_book, capacity_omission = capacity_book_evidence(book, schedule, candidate_limit)
        if capacity_book is not None:
            order_metadata["capacity_book"] = capacity_book
        else:
            order_metadata["capacity_book_omitted_reason"] = capacity_omission
        spool_event(self.root, LedgerEvent(event_type="ORDER_SUBMITTED", **common,
            recorded_ts_ms=decision_ms, bid=book.bids[0][0] if book.bids else None, ask=ask,
            bid_depth=sum(q for _, q in book.bids), ask_depth=sum(q for _, q in book.asks),
            limit_price=ask, intended_action="TAKE", intended_size=size, order_state="SUBMITTED_PAPER_FORWARD",
            predicted_fill_probability=1.0, expected_ev=0.0, metadata=order_metadata))
        spool_event(self.root, LedgerEvent(event_type="FILL", **common,
            recorded_ts_ms=decision_ms + 1, fill_id=fill_id, fill_price=ask, filled_size=size, complete=True,
            fee=fee, fee_rate=float(schedule.get("rate") or 0.0), fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
            slippage=max(0.0, ask - candidate_limit) * size, metadata=metadata))
        self.state.setdefault("traded_markets", []).append(market_id)
        self.traded_markets_set.add(market_id)
        self.state["entries"] = int(self.state.get("entries") or 0) + 1
        self.state.setdefault("positions", {})[position_id] = {
            "position_id": position_id, "fill_id": fill_id, "order_id": order_id, "market_id": market_id,
            "event_id": arrival["event_id"], "token_id": arrival["token_id"], "outcome": arrival["outcome"],
            "shares": size, "entry_price": ask, "entry_fee": fee, "entry_cost": entry_cost,
            "opened_ms": book.receive_ts_ms, "resolution_due_ms": resolution_due_ms, "settled": False,
            "protocol_hash": self.protocol_hash, "coordinator_receipt": receipt,
        }
        self.event("FILLED", market_id=market_id, outcome=arrival["outcome"], shares=size, price=ask,
                   fee=fee, tte_seconds=arrival["tte_seconds"], signal_age_ms=arrival["signal_age_ms"])
        self.persist()

    def _settlement_request(self, market_id: str) -> Any:
        return request_json(
            f"{self.gamma_url}/markets/{urllib.parse.quote(market_id)}", timeout=4)

    def _apply_settlement(self, position: dict[str, Any], raw: Any, current: int) -> bool:
        if not isinstance(raw, dict) or raw.get("closed") is not True:
            return False
        outcomes = [str(x) for x in parse_array(raw.get("outcomes"))]
        tokens = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
        prices = [finite(x, math.nan) for x in parse_array(raw.get("outcomePrices"))]
        win_idx = next((i for i, price in enumerate(prices)
                        if math.isfinite(price) and price >= 1.0 - 1e-9), -1)
        if win_idx < 0 or win_idx >= len(tokens):
            return False
        won = tokens[win_idx] == str(position["token_id"])
        payout = float(position["shares"]) if won else 0.0
        pnl = payout - float(position["entry_cost"]) - float(position["entry_fee"])
        resolved = outcomes[win_idx] if win_idx < len(outcomes) else ""
        metadata = {"component": COMPONENT, "model_family": "lead_lag_taker_v1",
            "paper_exploration": True, "paper_forward_test": True, "paper_bootstrap_probe": False,
            "economic_authority": "PAPER_EXPLORATION", "counterfactual": False,
            "excluded_from_portfolio_equity": False, "research_evidence_only": False,
            "realized": True, "unwind_accounted": True, "cost_vector_complete": True,
            "outcome": position["outcome"], "won": won, "settlement_outcome": resolved,
            "winning_token_id": tokens[win_idx], "hold_to_settlement": True,
            "protocol_hash": self.protocol_hash,
            "coordinator_receipt": position.get("coordinator_receipt"),
            "terminal_id": f"lead-lag:{position['position_id']}:final",
            "pnl_decomposition": {"trading_pnl": pnl, "spread_capture": 0.0, "adverse_markout": 0.0,
                "inventory_pnl": 0.0, "maker_rebates": 0.0, "liquidity_rewards": 0.0,
                "own_reward_share_verified": False}}
        spool_event(self.root, LedgerEvent(event_type="FINAL", strategy=STRATEGY, model_sha=self.sha,
            model_version=MODEL_VERSION, order_id=position["order_id"], fill_id=position["fill_id"],
            position_id=position["position_id"], market_id=position["market_id"], event_id=position["event_id"],
            token_id=position["token_id"], side="BUY", final_pnl=pnl, realized_cashflow=payout,
            fee=0.0, slippage=0.0, unwind_loss=0.0, capital_cost=0.0, latency_cost=0.0,
            capital_duration_ms=max(0, current - int(position["opened_ms"])), metadata=metadata))
        position["settled"] = True; position["won"] = won; position["final_pnl"] = pnl
        self.state["settled"] = int(self.state.get("settled") or 0) + 1
        self.state["wins"] = int(self.state.get("wins") or 0) + int(won)
        self.state["realized_pnl"] = float(self.state.get("realized_pnl") or 0.0) + pnl
        self.event("FINAL", market_id=position["market_id"], won=won, pnl=pnl,
                   settlement_outcome=resolved)
        self.persist()
        return True

    def settle_positions(self) -> None:
        """Settle without blocking the hot cohort; preserve the frozen legacy path."""
        current = now_ms()
        if not self.event_driven_signal and self.coordinator_ipc is None and self.hot_book_cache is None:
            for position in list((self.state.get("positions") or {}).values()):
                if position.get("settled") or current < int(position.get("resolution_due_ms") or 0) + 5_000:
                    continue
                if current - int(position.get("settlement_attempt_ms") or 0) < 5_000:
                    continue
                position["settlement_attempt_ms"] = current
                try:
                    raw = self._settlement_request(str(position["market_id"]))
                except Exception:
                    continue
                self._apply_settlement(position, raw, current)
            return
        # Hot cohort: Gamma network I/O is scheduled off the owner thread.
        positions = self.state.get("positions") or {}
        # Apply completed responses without waiting for network I/O.
        for position_id, future in list(self.settlement_futures.items()):
            if not future.done():
                continue
            self.settlement_futures.pop(position_id, None)
            position = positions.get(position_id)
            if not isinstance(position, dict) or position.get("settled"):
                continue
            try:
                raw = future.result()
            except Exception:
                continue
            self._apply_settlement(position, raw, current)
        # Submit due requests only after draining completions. One worker bounds
        # concurrency and a slow Gamma request can no longer stall candidate_step.
        for position_id, position in list(positions.items()):
            if not isinstance(position, dict) or position.get("settled"):
                continue
            if position_id in self.settlement_futures:
                continue
            if current < int(position.get("resolution_due_ms") or 0) + 5_000:
                continue
            if current - int(position.get("settlement_attempt_ms") or 0) < 5_000:
                continue
            position["settlement_attempt_ms"] = current
            self.settlement_futures[position_id] = self.settlement_pool.submit(
                self._settlement_request, str(position["market_id"]))

    def publish(self, state: str = "COLLECTING") -> None:
        target = int(self.config["forward_test"]["target_independent_markets"])
        settled = int(self.state.get("settled") or 0)
        if settled >= target:
            state = "FORWARD_TARGET_COMPLETE_NO_AUTOMATIC_PROMOTION"
        atomic_json(self.status_path, {
            "schema": SCHEMA, "timestamp_ms": now_ms(), "state": state,
            "strategy_id": "LEAD_LAG_TAKER_V1", "model_sha": self.sha,
            "protocol_hash": self.protocol_hash, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "real_capital_at_risk": False, "automatic_promotion": False,
            "target_independent_markets": target, "entries": int(self.state.get("entries") or 0),
            "settled": settled, "wins": int(self.state.get("wins") or 0),
            "realized_pnl": float(self.state.get("realized_pnl") or 0.0),
            "open_positions": sum(not bool(p.get("settled")) for p in (self.state.get("positions") or {}).values()),
            "candidate_count": int(self.state.get("candidate_count") or 0),
            "receipt_count": int(self.state.get("receipt_count") or 0),
            "arrival_rejections": int(self.state.get("arrival_rejections") or 0),
            "coordinator_transport": "UNIX_STREAM_EVENT_DRIVEN" if self.coordinator_ipc else "FILESYSTEM_LEGACY",
            "audit_queue_depth": self.audit_writer.queue.qsize() if self.audit_writer else 0,
            "audit_queue_sync_fallbacks": self.audit_writer.fallbacks if self.audit_writer else 0,
            "settlement_requests_inflight": len(self.settlement_futures),
            "hot_book_cache_enabled": self.hot_book_cache is not None,
            "hot_book_metadata_source": "CANONICAL_WS_BOOTSTRAP_MMAP" if self.hot_book_cache else "LEGACY_REST",
            "signal_wait_mode": ("UNIX_DGRAM_DIRECT_WITH_FILE_FALLBACK" if self.signal_socket_path else
                                 ("FILE_EVENT_PUMP" if self.event_driven_signal else "POLL_INTERVAL")),
            "signal_datagrams_received": self.signal_receiver.received if self.signal_receiver else 0,
            "signal_datagrams_invalid": self.signal_receiver.invalid if self.signal_receiver else 0,
            "status_cache_enabled": self.event_driven_signal,
            "status_cache_market_id": str((self.cached_status.get("market") or {}).get("market_id") or "") if self.event_driven_signal else "",
            "skip_reasons": self.state.get("skip_reasons") or {}, "last_event": self.state.get("last_event") or {},
        })

    def run(self) -> None:
        fast = max(0.001, int(self.config["fast_poll_ms"]) / 1000.0)
        settle = max(0.1, int(self.config["settlement_poll_ms"]) / 1000.0)
        if not self.event_driven_signal:
            next_fast = time.monotonic(); next_settle = next_fast
            while True:
                now = time.monotonic()
                if now >= next_fast:
                    try: self.candidate_step()
                    except Exception as exc:
                        self.skip(f"FAST_ERROR:{type(exc).__name__}"); self.event("ERROR", error=f"{type(exc).__name__}:{exc}")
                    next_fast = now + fast
                now = time.monotonic()
                if now >= next_settle:
                    try: self.settle_positions()
                    except Exception as exc: self.event("SETTLEMENT_ERROR", error=f"{type(exc).__name__}:{exc}")
                    self.persist(); self.publish(); next_settle = now + settle
                time.sleep(max(0.0005, min(next_fast, next_settle) - time.monotonic()))
        self.start_status_cache()
        self.start_signal_pump()
        next_settle = time.monotonic()
        last_generation = 0
        try:
            while True:
                now = time.monotonic()
                if now >= next_settle:
                    try: self.settle_positions()
                    except Exception as exc: self.event("SETTLEMENT_ERROR", error=f"{type(exc).__name__}:{exc}")
                    self.persist(); self.publish(); next_settle = now + settle
                timeout = max(0.0, next_settle - time.monotonic())
                generation, signal = self.wait_signal_after(last_generation, timeout)
                if signal is not None and generation > last_generation:
                    last_generation = generation
                    try: self.candidate_step(signal)
                    except Exception as exc:
                        self.skip(f"FAST_ERROR:{type(exc).__name__}"); self.event("ERROR", error=f"{type(exc).__name__}:{exc}")
        finally:
            self.stop_signal_pump()
            self.stop_status_cache()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-root", type=Path, required=True); ap.add_argument("--model-sha", required=True)
    ap.add_argument("--config", type=Path, default=Path("config/v7_lead_lag_taker_v1.json"))
    ap.add_argument("--clob-url", default="https://clob.polymarket.com")
    ap.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    ap.add_argument("--coordinator-ipc", type=Path)
    ap.add_argument("--hot-book-cache", type=Path)
    ap.add_argument("--event-driven-signal", action="store_true")
    ap.add_argument("--signal-socket", type=Path)
    args = ap.parse_args()
    if not exact_sha(args.model_sha): raise SystemExit("exact model SHA required")
    runtime = LeadLagRuntime(
        args.run_root.resolve(), args.model_sha, args.config.resolve(), args.clob_url, args.gamma_url,
        args.coordinator_ipc.resolve() if args.coordinator_ipc else None,
        args.hot_book_cache.resolve() if args.hot_book_cache else None,
        args.event_driven_signal,
        args.signal_socket.resolve() if args.signal_socket else None,
    )
    try:
        runtime.run()
    finally:
        runtime.settlement_pool.shutdown(wait=False, cancel_futures=True)
        if runtime.hot_book_cache is not None:
            runtime.hot_book_cache.close()
        if runtime.audit_writer is not None:
            runtime.audit_writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
