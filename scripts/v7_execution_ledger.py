#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EVENT_TYPES = frozenset(
    {
        "OPPORTUNITY",
        "CANDIDATE",
        "ORDER_SUBMITTED",
        "ORDER_STATE",
        "FILL",
        "MARKOUT",
        "POSITION_MARK",
        "EXIT",
        "FINAL",
    }
)
MARKOUT_HORIZONS = frozenset({"1s", "10s", "45s", "60s", "300s"})
SIDES = frozenset({"BUY", "SELL"})


class LedgerContractError(ValueError):
    """Raised when a V7 execution-ledger record violates the evidence contract."""


class LedgerOwnershipError(RuntimeError):
    """Raised when another process already owns the canonical ledger writer."""


def _finite_optional(name: str, value: float | None) -> None:
    if value is None:
        return
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LedgerContractError(f"{name}:not_numeric") from exc
    if not math.isfinite(out):
        raise LedgerContractError(f"{name}:not_finite")


def _positive_clock(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LedgerContractError(f"{name}:invalid")


def _nonnegative_int(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerContractError(f"{name}:invalid")


def _nonempty_optional(name: str, value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise LedgerContractError(f"{name}:invalid")


@dataclass(frozen=True)
class LedgerEvent:
    event_type: str
    strategy: str
    model_sha: str
    paper_only: bool = True
    authenticated_execution: bool = False

    schema_version: int = SCHEMA_VERSION
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    recorded_ts_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000)
    model_version: str | None = None

    opportunity_id: str | None = None
    candidate_id: str | None = None
    bundle_id: str | None = None
    order_id: str | None = None
    fill_id: str | None = None
    leg_id: str | None = None
    position_id: str | None = None

    market_id: str | None = None
    event_id: str | None = None
    token_id: str | None = None

    decision_ts_ms: int | None = None
    exchange_ts_ms: int | None = None
    receive_ts_ms: int | None = None
    book_snapshot_id: str | None = None

    side: str | None = None
    bid: float | None = None
    ask: float | None = None
    bid_depth: float | None = None
    ask_depth: float | None = None
    queue_ahead: float | None = None
    limit_price: float | None = None
    fill_price: float | None = None

    predicted_alpha: float | None = None
    predicted_fill_probability: float | None = None
    expected_ev: float | None = None

    intended_action: str | None = None
    intended_size: float | None = None
    order_state: str | None = None
    filled_size: float | None = None
    complete: bool | None = None
    cancel_reason: str | None = None
    timeout_ms: int | None = None

    fee: float | None = None
    fee_rate: float | None = None
    fee_source: str | None = None
    slippage: float | None = None
    unwind_loss: float | None = None
    capital_cost: float | None = None
    latency_cost: float | None = None
    realized_cashflow: float | None = None
    executable_liquidation_value: float | None = None
    unrealized_pnl: float | None = None
    final_pnl: float | None = None
    capital_duration_ms: int | None = None

    markouts: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise LedgerContractError("schema_version:unsupported")
        if self.event_type not in EVENT_TYPES:
            raise LedgerContractError("event_type:unsupported")
        if not isinstance(self.strategy, str) or not self.strategy.strip():
            raise LedgerContractError("strategy:missing")
        if not isinstance(self.model_sha, str) or not _SHA_RE.fullmatch(self.model_sha):
            raise LedgerContractError("model_sha:not_exact_git_sha")
        if self.paper_only is not True or self.authenticated_execution is not False:
            raise LedgerContractError("safety:not_paper_only")
        if not isinstance(self.record_id, str) or not self.record_id.strip():
            raise LedgerContractError("record_id:missing")

        for name, value in (
            ("model_version", self.model_version),
            ("opportunity_id", self.opportunity_id),
            ("candidate_id", self.candidate_id),
            ("bundle_id", self.bundle_id),
            ("order_id", self.order_id),
            ("fill_id", self.fill_id),
            ("leg_id", self.leg_id),
            ("position_id", self.position_id),
            ("market_id", self.market_id),
            ("event_id", self.event_id),
            ("token_id", self.token_id),
            ("book_snapshot_id", self.book_snapshot_id),
            ("intended_action", self.intended_action),
            ("order_state", self.order_state),
            ("cancel_reason", self.cancel_reason),
            ("fee_source", self.fee_source),
        ):
            _nonempty_optional(name, value)

        for name, value in (
            ("recorded_ts_ms", self.recorded_ts_ms),
            ("decision_ts_ms", self.decision_ts_ms),
            ("exchange_ts_ms", self.exchange_ts_ms),
            ("receive_ts_ms", self.receive_ts_ms),
        ):
            _positive_clock(name, value)
        _nonnegative_int("timeout_ms", self.timeout_ms)
        _nonnegative_int("capital_duration_ms", self.capital_duration_ms)

        if self.receive_ts_ms is not None and self.recorded_ts_ms < self.receive_ts_ms:
            raise LedgerContractError("clock:recorded_before_receive")
        if self.decision_ts_ms is not None and self.recorded_ts_ms < self.decision_ts_ms:
            raise LedgerContractError("clock:recorded_before_decision")
        if (
            self.decision_ts_ms is not None
            and self.receive_ts_ms is not None
            and self.decision_ts_ms < self.receive_ts_ms
        ):
            raise LedgerContractError("clock:decision_before_receive")

        if self.side is not None and self.side.upper() not in SIDES:
            raise LedgerContractError("side:unsupported")
        if self.complete is not None and not isinstance(self.complete, bool):
            raise LedgerContractError("complete:not_boolean")

        for name, value in (
            ("bid", self.bid),
            ("ask", self.ask),
            ("bid_depth", self.bid_depth),
            ("ask_depth", self.ask_depth),
            ("queue_ahead", self.queue_ahead),
            ("limit_price", self.limit_price),
            ("fill_price", self.fill_price),
            ("predicted_alpha", self.predicted_alpha),
            ("predicted_fill_probability", self.predicted_fill_probability),
            ("expected_ev", self.expected_ev),
            ("intended_size", self.intended_size),
            ("filled_size", self.filled_size),
            ("fee", self.fee),
            ("fee_rate", self.fee_rate),
            ("slippage", self.slippage),
            ("unwind_loss", self.unwind_loss),
            ("capital_cost", self.capital_cost),
            ("latency_cost", self.latency_cost),
            ("realized_cashflow", self.realized_cashflow),
            ("executable_liquidation_value", self.executable_liquidation_value),
            ("unrealized_pnl", self.unrealized_pnl),
            ("final_pnl", self.final_pnl),
        ):
            _finite_optional(name, value)

        for name, value in (
            ("bid", self.bid),
            ("ask", self.ask),
            ("limit_price", self.limit_price),
            ("fill_price", self.fill_price),
        ):
            if value is not None and not (0.0 <= float(value) <= 1.0):
                raise LedgerContractError(f"{name}:out_of_range")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise LedgerContractError("book:crossed")

        for name, value in (
            ("bid_depth", self.bid_depth),
            ("ask_depth", self.ask_depth),
            ("queue_ahead", self.queue_ahead),
            ("intended_size", self.intended_size),
            ("filled_size", self.filled_size),
            ("fee", self.fee),
            ("fee_rate", self.fee_rate),
            ("slippage", self.slippage),
            ("executable_liquidation_value", self.executable_liquidation_value),
            ("capital_duration_ms", self.capital_duration_ms),
        ):
            if value is not None and value < 0:
                raise LedgerContractError(f"{name}:negative")

        if self.predicted_fill_probability is not None and not (
            0.0 <= self.predicted_fill_probability <= 1.0
        ):
            raise LedgerContractError("predicted_fill_probability:out_of_range")

        if self.event_type in {"CANDIDATE", "ORDER_SUBMITTED"}:
            if self.exchange_ts_ms is None or self.receive_ts_ms is None or self.decision_ts_ms is None:
                raise LedgerContractError("decision:missing_exchange_receive_decision_clock")
            if not self.book_snapshot_id:
                raise LedgerContractError("decision:missing_book_snapshot_id")

        if self.event_type in {"ORDER_SUBMITTED", "ORDER_STATE", "FILL", "MARKOUT"} and not self.order_id:
            raise LedgerContractError("order_id:missing")
        if self.event_type == "ORDER_SUBMITTED":
            if not self.intended_action or self.intended_size is None or self.intended_size <= 0:
                raise LedgerContractError("order_submitted:missing_intent")
        if self.event_type == "FILL":
            if self.exchange_ts_ms is None or self.receive_ts_ms is None:
                raise LedgerContractError("fill:missing_exchange_receive_clock")
            if not self.fill_id:
                raise LedgerContractError("fill:missing_fill_id")
            if not self.token_id or self.side is None:
                raise LedgerContractError("fill:missing_instrument_side")
            if self.fill_price is None:
                raise LedgerContractError("fill:missing_price")
            if self.filled_size is None or self.filled_size <= 0:
                raise LedgerContractError("fill:missing_positive_size")
            if self.fee is None or not self.fee_source:
                raise LedgerContractError("fill:missing_authoritative_fee")
        if self.event_type == "MARKOUT":
            if not self.fill_id:
                raise LedgerContractError("markout:missing_fill_id")
            if self.exchange_ts_ms is None or self.receive_ts_ms is None or not self.book_snapshot_id:
                raise LedgerContractError("markout:missing_causal_book")
            if self.executable_liquidation_value is None:
                raise LedgerContractError("markout:missing_executable_liquidation_value")
            if len(self.markouts) != 1:
                raise LedgerContractError("markout:requires_single_horizon")
        if self.event_type == "POSITION_MARK":
            if not self.position_id:
                raise LedgerContractError("position_mark:missing_position_id")
            if self.executable_liquidation_value is None:
                raise LedgerContractError("position_mark:missing_executable_liquidation_value")
            if self.exchange_ts_ms is None or self.receive_ts_ms is None or not self.book_snapshot_id:
                raise LedgerContractError("position_mark:missing_causal_book")
        if self.event_type == "FINAL" and self.final_pnl is None:
            raise LedgerContractError("final:missing_pnl")

        if not isinstance(self.markouts, dict):
            raise LedgerContractError("markouts:not_object")
        unknown = set(self.markouts) - MARKOUT_HORIZONS
        if unknown:
            raise LedgerContractError(f"markouts:unsupported_horizon:{sorted(unknown)[0]}")
        for horizon, value in self.markouts.items():
            _finite_optional(f"markout_{horizon}", value)
        if self.event_type != "MARKOUT" and self.markouts:
            raise LedgerContractError("markouts:only_markout_events")

        if not isinstance(self.metadata, dict):
            raise LedgerContractError("metadata:not_object")
        try:
            json.dumps(self.metadata, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise LedgerContractError("metadata:not_json_serializable") from exc

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LedgerEvent":
        if not isinstance(raw, dict):
            raise LedgerContractError("record:not_object")
        try:
            event = cls(**raw)
        except TypeError as exc:
            raise LedgerContractError(f"record:shape:{exc}") from exc
        event.validate()
        return event


def canonical_ledger_path(run_root: Path) -> Path:
    return Path(run_root) / "ledger" / "execution.jsonl"


class CanonicalLedgerWriter:
    """Fail-closed single-process owner for the append-only V7 PAPER ledger.

    Historical records from older deployed SHAs may remain in the same logical
    ledger. Every append is bound to this writer's exact SHA, and every research
    read must explicitly select one exact SHA.
    """

    def __init__(self, path: Path, *, writer_id: str, model_sha: str):
        self.path = Path(path)
        self.writer_id = str(writer_id).strip()
        self.model_sha = str(model_sha)
        self.owner_path = self.path.with_suffix(self.path.suffix + ".writer.json")
        self._owned = False
        self._owner_token = uuid.uuid4().hex
        self._append_lock = threading.Lock()

        if not self.writer_id:
            raise LedgerOwnershipError("writer_id:missing")
        if not _SHA_RE.fullmatch(self.model_sha):
            raise LedgerOwnershipError("model_sha:not_exact_git_sha")

    def acquire(self) -> None:
        if self._owned:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        owner = {
            "schema_version": SCHEMA_VERSION,
            "writer_id": self.writer_id,
            "model_sha": self.model_sha,
            "pid": os.getpid(),
            "owner_token": self._owner_token,
            "acquired_ts_ms": time.time_ns() // 1_000_000,
        }
        payload = (json.dumps(owner, sort_keys=True) + "\n").encode("utf-8")
        try:
            fd = os.open(self.owner_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise LedgerOwnershipError(f"ledger_already_owned:{self.owner_path}") from exc
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._owned = True

    def append(self, event: LedgerEvent) -> None:
        if not self._owned:
            raise LedgerOwnershipError("writer:not_acquired")
        event.validate()
        if event.model_sha != self.model_sha:
            raise LedgerContractError("model_sha:mixed_sha_append")
        line = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        with self._append_lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def close(self) -> None:
        if not self._owned:
            return
        try:
            owner = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerOwnershipError("owner_record:unreadable") from exc
        if (
            owner.get("owner_token") != self._owner_token
            or owner.get("writer_id") != self.writer_id
            or owner.get("model_sha") != self.model_sha
        ):
            raise LedgerOwnershipError("owner_record:mismatch")
        self.owner_path.unlink()
        self._owned = False

    def __enter__(self) -> "CanonicalLedgerWriter":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def iter_events(path: Path, *, expected_model_sha: str) -> Iterator[LedgerEvent]:
    """Validate the full ledger and yield evidence for exactly one source SHA."""
    if not _SHA_RE.fullmatch(expected_model_sha):
        raise LedgerContractError("expected_model_sha:not_exact_git_sha")
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerContractError(f"line_{line_number}:invalid_json") from exc
            event = LedgerEvent.from_dict(raw)
            if event.model_sha == expected_model_sha:
                yield event


def load_events(path: Path, *, expected_model_sha: str) -> list[LedgerEvent]:
    return list(iter_events(path, expected_model_sha=expected_model_sha))
