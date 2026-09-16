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

SCHEMA = "polymarket_v7_lead_lag_taker_v1_status"
MANIFEST_SCHEMA = "polymarket_v7_lead_lag_taker_v1_forward_manifest"
EVENT_SCHEMA = "polymarket_v7_lead_lag_taker_v1_event"
MODEL_VERSION = "lead-lag-taker-v1-forward"
STRATEGY = "CRYPTO_SETTLEMENT_ENGINE"
COMPONENT = "crypto_informed_taker"


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
    if (value["schema"] != "polymarket_v7_lead_lag_taker_v1_config" or value["version"] != 1
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
    def __init__(self, root: Path, sha: str, config_path: Path, clob_url: str, gamma_url: str):
        self.root, self.sha = root, sha
        self.config_path = config_path
        self.config = validate_config(load(config_path))
        self.protocol_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
        self.clob = ClobBooksClient(clob_url.rstrip("/"), 0.25)
        self.gamma_url = gamma_url.rstrip("/")
        self.signal_path = root / "external_fair" / "external_cancel_signal.json"
        self.status_source = root / "external_fair" / "status.json"
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
        self._write_manifest_once()
        self.publish("COLLECTING")

    def _write_manifest_once(self) -> None:
        if self.manifest_path.exists():
            existing = load(self.manifest_path)
            if existing.get("code_sha") != self.sha or existing.get("protocol_hash") != self.protocol_hash:
                raise RuntimeError("lead_lag_forward_manifest_identity_mismatch")
            return
        runtime = load(self.root / "control" / "runtime_status.json")
        if (runtime.get("model_sha") != self.sha or runtime.get("paper_only") is not True
                or runtime.get("authenticated_execution") is not False or runtime.get("real_order_submission") is not False):
            raise RuntimeError("lead_lag_runtime_identity_not_ready")
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
        append_jsonl(self.events_path, row); self.state["last_event"] = row

    def skip(self, reason: str) -> None:
        reasons = self.state.setdefault("skip_reasons", {})
        reasons[reason] = int(reasons.get(reason) or 0) + 1

    def books(self, status: dict[str, Any]) -> dict[str, Book]:
        market = status.get("market") if isinstance(status.get("market"), dict) else {}
        tokens = [str(market.get("yes_token") or ""), str(market.get("no_token") or "")]
        if any(not token for token in tokens) or tokens[0] == tokens[1]:
            return {}
        try:
            rows = self.clob.request_books(tokens)
        except Exception:
            return {}
        received = now_ms(); output: dict[str, Book] = {}
        for raw in rows if isinstance(rows, list) else []:
            book = parse_book(raw, received)
            if book is not None and book.token_id in tokens:
                output[book.token_id] = book
        return output

    def wait_receipt(self, replay_key: str) -> dict[str, Any] | None:
        path = self.root / "opportunities" / "receipts" / (replay_key.replace("/", "_") + ".json")
        deadline = time.monotonic() + int(self.config["coordinator_receipt_timeout_ms"]) / 1000.0
        while time.monotonic() < deadline:
            receipt = load(path)
            if (receipt.get("selected_replay_key") == replay_key
                    and receipt.get("action") == "TAKE"
                    and receipt.get("paper_exploration_authorized") is True
                    and receipt.get("paper_forward_test_authorized") is True
                    and receipt.get("new_risk_authorized") is False
                    and receipt.get("paper_only") is True
                    and receipt.get("real_order_submission") is False):
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
        runtime = load(self.root / "control" / "runtime_status.json")
        if runtime.get("model_sha") != self.sha:
            return None
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

    def candidate_step(self) -> None:
        if any((self.root / "control" / name).exists() for name in ("CUTOVER_DRAIN", "KILL")):
            self.skip("RISK_FROZEN"); return
        target = int(self.config["forward_test"]["target_independent_markets"])
        if int(self.state.get("entries") or 0) >= target:
            self.skip("TARGET_ENTRIES_REACHED"); return
        current_ns = time.time_ns(); signal = load(self.signal_path); status = load(self.status_source)
        candidate, reason = signal_candidate(self.config, signal, status, model_sha=self.sha, current_ns=current_ns)
        if candidate is None:
            self.skip(reason); return
        market_id = candidate["market_id"]
        if market_id in set(self.state.get("traded_markets") or []):
            self.skip("MARKET_ALREADY_TRADED"); return
        attempt_key = f"{market_id}:{candidate['signal_version']}"
        if attempt_key in set(self.state.get("attempted_keys") or []):
            self.skip("SIGNAL_ALREADY_ATTEMPTED"); return
        books = self.books(status)
        decision_ns = max(time.time_ns(), max((book.receive_ts_ms for book in books.values()), default=0) * 1_000_000)
        envelope = self.envelope(candidate, status, books, current_ns=decision_ns)
        if envelope is None:
            self.skip("CANDIDATE_BOOK_OR_DEPTH_INVALID"); return
        self.state.setdefault("attempted_keys", []).append(attempt_key)
        self.state["attempted_keys"] = self.state["attempted_keys"][-5000:]
        self.state["candidate_count"] = int(self.state.get("candidate_count") or 0) + 1
        self.event("CANDIDATE", market_id=market_id, signal_version=candidate["signal_version"],
                   tte_seconds=candidate["tte_seconds"], replay_key=envelope["deterministic_replay_key"],
                   candidate_limit_price=envelope["execution_plan"]["legs"][0]["limit_price"])
        inbox = self.root / "opportunities" / "fast_forward_inbox" / (
            f"{current_ns}.lead-lag-taker-v1.{market_id}.{candidate['signal_version']}.json"
        )
        atomic_json(inbox, envelope)
        receipt = self.wait_receipt(envelope["deterministic_replay_key"])
        if receipt is None:
            self.skip("COORDINATOR_RECEIPT_TIMEOUT"); self.event("REJECTED", market_id=market_id, reason="COORDINATOR_RECEIPT_TIMEOUT"); return
        self.state["receipt_count"] = int(self.state.get("receipt_count") or 0) + 1
        arrival_ns = time.time_ns(); arrival_signal = load(self.signal_path); arrival_status = load(self.status_source)
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
        spool_event(self.root, LedgerEvent(event_type="ORDER_SUBMITTED", **common,
            recorded_ts_ms=decision_ms, bid=book.bids[0][0] if book.bids else None, ask=ask,
            bid_depth=sum(q for _, q in book.bids), ask_depth=sum(q for _, q in book.asks),
            limit_price=ask, intended_action="TAKE", intended_size=size, order_state="SUBMITTED_PAPER_FORWARD",
            predicted_fill_probability=1.0, expected_ev=0.0, metadata=metadata))
        spool_event(self.root, LedgerEvent(event_type="FILL", **common,
            recorded_ts_ms=decision_ms + 1, fill_id=fill_id, fill_price=ask, filled_size=size, complete=True,
            fee=fee, fee_rate=float(schedule.get("rate") or 0.0), fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
            slippage=max(0.0, ask - candidate_limit) * size, metadata=metadata))
        self.state.setdefault("traded_markets", []).append(market_id)
        self.state["entries"] = int(self.state.get("entries") or 0) + 1
        self.state.setdefault("positions", {})[position_id] = {
            "position_id": position_id, "fill_id": fill_id, "order_id": order_id, "market_id": market_id,
            "event_id": arrival["event_id"], "token_id": arrival["token_id"], "outcome": arrival["outcome"],
            "shares": size, "entry_price": ask, "entry_fee": fee, "entry_cost": entry_cost,
            "opened_ms": book.receive_ts_ms, "resolution_due_ms": resolution_due_ms, "settled": False,
            "protocol_hash": self.protocol_hash,
        }
        self.event("FILLED", market_id=market_id, outcome=arrival["outcome"], shares=size, price=ask,
                   fee=fee, tte_seconds=arrival["tte_seconds"], signal_age_ms=arrival["signal_age_ms"])
        self.persist()

    def settle_positions(self) -> None:
        current = now_ms()
        for position in list((self.state.get("positions") or {}).values()):
            if position.get("settled") or current < int(position.get("resolution_due_ms") or 0) + 5_000:
                continue
            if current - int(position.get("settlement_attempt_ms") or 0) < 5_000:
                continue
            position["settlement_attempt_ms"] = current
            try:
                raw = request_json(f"{self.gamma_url}/markets/{urllib.parse.quote(str(position['market_id']))}", timeout=4)
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("closed") is not True:
                continue
            outcomes = [str(x) for x in parse_array(raw.get("outcomes"))]
            tokens = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
            prices = [finite(x, math.nan) for x in parse_array(raw.get("outcomePrices"))]
            win_idx = next((i for i, p in enumerate(prices) if math.isfinite(p) and p >= 1.0 - 1e-9), -1)
            if win_idx < 0 or win_idx >= len(tokens):
                continue
            won = tokens[win_idx] == str(position["token_id"]); payout = float(position["shares"]) if won else 0.0
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
            self.event("FINAL", market_id=position["market_id"], won=won, pnl=pnl, settlement_outcome=resolved)
            self.persist()

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
            "skip_reasons": self.state.get("skip_reasons") or {}, "last_event": self.state.get("last_event") or {},
        })

    def run(self) -> None:
        fast = max(0.001, int(self.config["fast_poll_ms"]) / 1000.0)
        settle = max(0.1, int(self.config["settlement_poll_ms"]) / 1000.0)
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-root", type=Path, required=True); ap.add_argument("--model-sha", required=True)
    ap.add_argument("--config", type=Path, default=Path("config/v7_lead_lag_taker_v1.json"))
    ap.add_argument("--clob-url", default="https://clob.polymarket.com")
    ap.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    args = ap.parse_args()
    if not exact_sha(args.model_sha): raise SystemExit("exact model SHA required")
    runtime = LeadLagRuntime(args.run_root.resolve(), args.model_sha, args.config.resolve(), args.clob_url, args.gamma_url)
    runtime.run(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
