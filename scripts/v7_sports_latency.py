#!/usr/bin/env python3
"""Causal, fail-closed sports-latency research and forward-shadow pipeline.

Provider transport and decoding live in the canonical V7 sports collector. A
source becomes usable only after an operator supplies a verified source spec and
an exact decoder for that schema. The adapter never submits orders; decisions
and race observations are PAPER shadow evidence only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


class SportsError(ValueError):
    pass


class Sport(str, Enum):
    SOCCER = "SOCCER"
    TENNIS = "TENNIS"
    BASKETBALL = "BASKETBALL"
    BASEBALL = "BASEBALL"


class ConnectionState(str, Enum):
    COLD = "COLD"
    LIVE = "LIVE"
    DISCONNECTED = "DISCONNECTED"
    RESUMING = "RESUMING"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class SportsSourceSpec:
    source_id: str
    authority: str
    official_uri: str
    sport: Sport
    competition: str
    schema_version: str
    schema_hash: str
    sequence_semantics: str
    correction_semantics: str
    verified_at_ms: int
    verified: bool
    state_semantics: str = ""

    def validate(self) -> None:
        required = (
            self.source_id, self.authority, self.official_uri, self.competition,
            self.schema_version, self.schema_hash, self.sequence_semantics,
            self.correction_semantics, self.state_semantics,
        )
        if not all(required) or not self.official_uri.startswith(("https://", "wss://")):
            raise SportsError("incomplete_official_source_spec")
        if self.sequence_semantics not in {
            "STRICT_MONOTONIC_PER_GAME", "PROVIDER_EVENT_ID_AND_TIMELINE_ORDER",
        }:
            raise SportsError("unsupported_sequence_semantics")
        if self.correction_semantics not in {
            "NEW_SEQUENCE_REFERENCES_EVENT", "TIMELINE_EVENT_REPLACEMENT_OR_REVERSAL",
        }:
            raise SportsError("unsupported_correction_semantics")
        if self.state_semantics != "FULL_STATE_AFTER_EVENT":
            raise SportsError("unsupported_game_state_semantics")
        if self.verified_at_ms <= 0 or not self.verified:
            raise SportsError("unverified_official_source")


@dataclass(frozen=True)
class SportsFeedEvent:
    event_id: str
    game_id: str
    sequence: int
    event_type: str
    source: str
    official: bool
    source_ts_ms: int
    receive_ts_ms: int
    state: Mapping[str, Any]
    correction_of: str = ""
    schema_version: str = ""

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()


class SportsWireDecoder(Protocol):
    def __call__(self, payload: bytes, receive_ts_ms: int, spec: SportsSourceSpec) -> SportsFeedEvent: ...


class VerifiedSportsStreamAdapter:
    """Authority boundary around an exact, externally supplied wire decoder."""

    def __init__(self, spec: SportsSourceSpec, decoder: SportsWireDecoder):
        spec.validate()
        if not callable(decoder):
            raise SportsError("sports_decoder_required")
        self.spec = spec
        self._decoder = decoder

    def decode(self, payload: bytes, receive_ts_ms: int) -> SportsFeedEvent:
        if not payload or receive_ts_ms <= 0:
            raise SportsError("invalid_wire_envelope")
        event = self._decoder(payload, receive_ts_ms, self.spec)
        if not isinstance(event, SportsFeedEvent):
            raise SportsError("decoder_returned_wrong_type")
        if (
            event.source != self.spec.source_id
            or not event.official
            or event.schema_version != self.spec.schema_version
            or event.receive_ts_ms != receive_ts_ms
        ):
            raise SportsError("decoder_source_attestation_mismatch")
        return event


@dataclass(frozen=True)
class ContractMapping:
    market_id: str
    game_id: str
    sport: Sport
    contract_type: str
    selection: str
    settlement_source: str
    rules_hash: str
    verified: bool
    cutoff_ms: int = 0
    timezone: str = ""
    cancellation_rules: str = ""
    outcome_semantics: str = ""
    verification_uri: str = ""
    verification_hash: str = ""
    verified_at_ms: int = 0

    def validate(self) -> None:
        if not all((
            self.market_id, self.game_id, self.selection, self.settlement_source,
            self.rules_hash, self.timezone, self.cancellation_rules,
            self.outcome_semantics, self.verification_uri, self.verification_hash,
        )) or self.cutoff_ms <= 0 or self.verified_at_ms <= 0:
            raise SportsError("incomplete_contract_mapping")
        if not self.verification_uri.startswith("https://"):
            raise SportsError("invalid_contract_mapping_evidence")
        # Other contract types stay blocked until their exact settlement
        # transform and model are separately implemented and verified.
        if self.contract_type != "MATCH_WINNER":
            raise SportsError("unsupported_contract_semantics")
        allowed = {
            Sport.SOCCER: {"HOME", "AWAY"},
            Sport.BASKETBALL: {"HOME", "AWAY"},
            Sport.BASEBALL: {"HOME", "AWAY"},
            Sport.TENNIS: {"PLAYER_A", "PLAYER_B"},
        }
        if self.selection not in allowed[self.sport]:
            raise SportsError("unsupported_selection")
        if not self.verified:
            raise SportsError("unverified_contract_mapping")

    @property
    def semantic_hash(self) -> str:
        payload = asdict(self)
        payload.pop("verified")
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SportsTapeRecord:
    ordinal: int
    event: SportsFeedEvent
    accepted: bool
    reason: str
    connection_epoch: int
    previous_event_id: str
    effective_state_hash: str


class SportsCausalTape:
    """Append-only event lineage, including rejected events and corrections."""

    def __init__(self, game_id: str):
        self.game_id = game_id
        self._records: list[SportsTapeRecord] = []
        self._accepted: dict[str, SportsFeedEvent] = {}

    @property
    def records(self) -> tuple[SportsTapeRecord, ...]:
        return tuple(self._records)

    def event(self, event_id: str) -> SportsFeedEvent | None:
        return self._accepted.get(event_id)

    def append(
        self, event: SportsFeedEvent, *, accepted: bool, reason: str,
        connection_epoch: int, previous_event_id: str,
    ) -> SportsTapeRecord:
        if event.game_id != self.game_id:
            raise SportsError("tape_game_mismatch")
        state_hash = hashlib.sha256(json.dumps(
            dict(event.state), sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
        record = SportsTapeRecord(
            len(self._records) + 1, event, accepted, reason, connection_epoch,
            previous_event_id, state_hash,
        )
        self._records.append(record)
        if accepted:
            self._accepted[event.event_id] = event
        return record


class FeedGuard:
    def __init__(
        self, game_id: str, max_age_ms: int, *, source_spec: SportsSourceSpec | None = None,
        tape: SportsCausalTape | None = None,
    ):
        if not game_id or int(max_age_ms) <= 0:
            raise SportsError("invalid_feed_guard")
        if source_spec is not None:
            source_spec.validate()
        self.game_id = game_id
        self.max_age_ms = int(max_age_ms)
        self.source_spec = source_spec
        self.tape = tape or SportsCausalTape(game_id)
        self.last_sequence = 0
        self.last_event_id = ""
        self.last_receive_ts_ms = 0
        self.last_state: Mapping[str, Any] | None = None
        self.valid = False
        self.blocker = "cold_start"
        self.connection_state = ConnectionState.COLD
        self.connection_epoch = 0
        self.gap_count = 0
        self.correction_count = 0

    def _reject(self, event: SportsFeedEvent, blocker: str) -> None:
        self.valid = False
        self.blocker = blocker
        self.connection_state = ConnectionState.QUARANTINED
        self.tape.append(
            event, accepted=False, reason=blocker, connection_epoch=self.connection_epoch,
            previous_event_id=self.last_event_id,
        )

    def disconnect(self, reason: str = "transport_disconnect") -> None:
        if not reason:
            raise SportsError("disconnect_reason_required")
        self.valid = False
        self.blocker = reason
        self.connection_state = ConnectionState.DISCONNECTED

    def begin_resume(self, *, resume_after_sequence: int) -> None:
        if self.connection_state is not ConnectionState.DISCONNECTED:
            raise SportsError("resume_requires_disconnect")
        if resume_after_sequence != self.last_sequence:
            self.valid = False
            self.blocker = "resume_cursor_mismatch"
            self.connection_state = ConnectionState.QUARANTINED
            return
        self.connection_epoch += 1
        self.valid = False
        self.blocker = "resume_pending_contiguous_event"
        self.connection_state = ConnectionState.RESUMING

    def apply(self, event: SportsFeedEvent, now_ms: int) -> None:
        previous_event_id = self.last_event_id
        if self.connection_state is ConnectionState.DISCONNECTED:
            self._reject(event, "resume_not_started"); return
        if self.connection_state is ConnectionState.QUARANTINED:
            self._reject(event, "feed_quarantined_requires_reconnect"); return
        if event.game_id != self.game_id or not event.official:
            self._reject(event, "wrong_game_or_unverified_feed"); return
        if self.source_spec is not None and (
            event.source != self.source_spec.source_id
            or event.schema_version != self.source_spec.schema_version
        ):
            self._reject(event, "source_schema_mismatch"); return
        if not event.event_id or event.sequence <= 0 or not event.event_type or not event.state:
            self._reject(event, "invalid_feed_envelope"); return
        if event.receive_ts_ms < event.source_ts_ms or event.receive_ts_ms > now_ms:
            self._reject(event, "invalid_feed_clocks"); return
        if now_ms - event.receive_ts_ms > self.max_age_ms:
            self._reject(event, "stale_feed"); return
        previous = self.tape.event(event.event_id)
        if previous is not None:
            if previous.payload_hash == event.payload_hash:
                self.tape.append(
                    event, accepted=False, reason="idempotent_duplicate",
                    connection_epoch=self.connection_epoch, previous_event_id=previous_event_id,
                )
                return
            self._reject(event, "conflicting_duplicate_event_id"); return
        if event.sequence != self.last_sequence + 1:
            self.gap_count += 1
            blocker = "feed_gap" if event.sequence > self.last_sequence else "out_of_order_event"
            self._reject(event, blocker); return
        if event.correction_of:
            corrected = self.tape.event(event.correction_of)
            if corrected is None:
                self._reject(event, "correction_target_unknown"); return
            if corrected.game_id != event.game_id:
                self._reject(event, "correction_target_wrong_game"); return
            self.correction_count += 1
        self.last_sequence = event.sequence
        self.last_event_id = event.event_id
        self.last_receive_ts_ms = event.receive_ts_ms
        self.last_state = dict(event.state)
        self.valid = True
        self.blocker = ""
        self.connection_state = ConnectionState.LIVE
        self.tape.append(
            event, accepted=True, reason="correction" if event.correction_of else "event",
            connection_epoch=self.connection_epoch, previous_event_id=previous_event_id,
        )

    def usable(self, now_ms: int) -> bool:
        if (
            not self.valid or self.connection_state is not ConnectionState.LIVE
            or self.last_state is None or now_ms < self.last_receive_ts_ms
        ):
            return False
        if now_ms - self.last_receive_ts_ms > self.max_age_ms:
            self.valid = False
            self.blocker = "stale_feed"
            return False
        return True


def _logistic(x: float) -> float:
    if x >= 0:
        z = math.exp(-x); return 1.0 / (1.0 + z)
    z = math.exp(x); return z / (1.0 + z)


def _num(state: Mapping[str, Any], key: str) -> float:
    value = state.get(key)
    if value is None or isinstance(value, bool):
        raise SportsError(f"missing_state:{key}")
    try: out = float(value)
    except (TypeError, ValueError, OverflowError) as exc: raise SportsError(f"invalid_state:{key}") from exc
    if not math.isfinite(out): raise SportsError(f"invalid_state:{key}")
    return out


def baseline_home_probability(sport: Sport, state: Mapping[str, Any]) -> float:
    """Transparent benchmark only; fitted challengers must beat it OOS."""
    strength = _num(state, "pregame_logit")
    if sport is Sport.SOCCER:
        margin = _num(state, "score_home") - _num(state, "score_away")
        remaining = _num(state, "remaining_seconds")
        red_diff = _num(state, "red_cards_away") - _num(state, "red_cards_home")
        if not 0.0 <= remaining <= 7_200.0: raise SportsError("invalid_soccer_clock")
        scale = 1.0 + 4.0 * (1.0 - remaining / 5_400.0)
        return _logistic(strength + scale * margin + 0.9 * red_diff)
    if sport is Sport.BASKETBALL:
        margin = _num(state, "score_home") - _num(state, "score_away")
        remaining = _num(state, "remaining_seconds")
        possession = _num(state, "home_possession")
        if not 0.0 <= remaining <= 4_800.0 or possession not in {0.0, 1.0}: raise SportsError("invalid_basketball_state")
        scale = max(1.5, math.sqrt(remaining / 60.0 + 1.0) * 1.8)
        return _logistic(strength + margin / scale + 0.08 * (2.0 * possession - 1.0))
    if sport is Sport.BASEBALL:
        margin = _num(state, "score_home") - _num(state, "score_away")
        inning = _num(state, "inning")
        outs = _num(state, "outs")
        batting_home = _num(state, "batting_home")
        base_state = _num(state, "occupied_bases")
        if not 1.0 <= inning <= 20.0 or outs not in {0.0, 1.0, 2.0} or batting_home not in {0.0, 1.0} or not 0.0 <= base_state <= 7.0:
            raise SportsError("invalid_baseball_state")
        leverage = min(4.0, 0.45 + inning / 4.0)
        batting = (2.0 * batting_home - 1.0) * (0.10 + 0.025 * base_state - 0.025 * outs)
        return _logistic(strength + leverage * margin + batting)
    if sport is Sport.TENNIS:
        sets = _num(state, "sets_a") - _num(state, "sets_b")
        games = _num(state, "games_a") - _num(state, "games_b")
        points = _num(state, "points_a") - _num(state, "points_b")
        server_a = _num(state, "server_a")
        serve_strength = _num(state, "serve_logit_a_minus_b")
        if server_a not in {0.0, 1.0}: raise SportsError("invalid_tennis_server")
        return _logistic(strength + 1.7 * sets + 0.25 * games + 0.08 * points + (2.0 * server_a - 1.0) * 0.12 * serve_strength)
    raise SportsError("unsupported_sport")


@dataclass(frozen=True)
class SportsDecision:
    market_id: str
    probability: float | None
    lower: float | None
    upper: float | None
    action: str
    side: str
    shadow_only: bool
    blocker: str


def decide(
    mapping: ContractMapping, guard: FeedGuard, *, now_ms: int, pm_bid: float,
    pm_ask: float, uncertainty: float, executable_cost: float, minimum_edge: float,
) -> SportsDecision:
    try:
        mapping.validate()
        if mapping.game_id != guard.game_id or not guard.usable(now_ms):
            raise SportsError(guard.blocker or "invalid_feed")
        if not (0.0 <= pm_bid <= pm_ask <= 1.0): raise SportsError("invalid_pm_book")
        if executable_cost < 0.0 or minimum_edge < 0.0:
            raise SportsError("invalid_execution_cost")
        p_home = baseline_home_probability(mapping.sport, guard.last_state or {})
        p = p_home if mapping.selection in {"HOME", "PLAYER_A"} else 1.0 - p_home
        if uncertainty < 0.0 or not math.isfinite(uncertainty): raise SportsError("invalid_uncertainty")
        lower, upper = max(0.0, p - uncertainty), min(1.0, p + uncertainty)
        if lower - pm_ask - executable_cost > minimum_edge: action, side = "TAKE", "YES"
        elif pm_bid - upper - executable_cost > minimum_edge: action, side = "TAKE", "NO"
        elif not (lower <= pm_ask and pm_bid <= upper): action, side = "CANCEL", "BOTH"
        else: action, side = "NOTHING", ""
        return SportsDecision(mapping.market_id, p, lower, upper, action, side, True, "")
    except SportsError as exc:
        return SportsDecision(mapping.market_id, None, None, None, "NOTHING", "", True, str(exc))


@dataclass(frozen=True)
class SportsRaceObservation:
    game_id: str
    event_id: str
    source_ts_ms: int
    receive_ts_ms: int
    decision_ts_ms: int
    market_move_ts_ms: int | None

    def validate(self) -> None:
        if not self.game_id or not self.event_id:
            raise SportsError("incomplete_race_observation")
        if not (0 < self.source_ts_ms <= self.receive_ts_ms <= self.decision_ts_ms):
            raise SportsError("invalid_race_timestamps")
        if self.market_move_ts_ms is not None and self.market_move_ts_ms < self.source_ts_ms:
            raise SportsError("invalid_market_move_timestamp")

    @property
    def receive_latency_ms(self) -> int:
        self.validate(); return self.receive_ts_ms - self.source_ts_ms

    @property
    def decision_latency_ms(self) -> int:
        self.validate(); return self.decision_ts_ms - self.receive_ts_ms

    @property
    def lead_ms(self) -> int | None:
        self.validate()
        return None if self.market_move_ts_ms is None else self.market_move_ts_ms - self.decision_ts_ms


@dataclass(frozen=True)
class CalibrationPoint:
    game_id: str
    prediction_ts_ms: int
    probability: float
    outcome: int
    model_fit_cutoff_ms: int = 0


@dataclass(frozen=True)
class CalibrationMetrics:
    observations: int
    brier: float
    log_loss: float


def chronological_calibration(points: Sequence[CalibrationPoint], *, cutoff_ms: int) -> CalibrationMetrics:
    """Score only predictions strictly before the declared OOS cutoff."""
    rows = [x for x in points if x.prediction_ts_ms < cutoff_ms]
    if not rows or len({x.game_id for x in rows}) != len(rows):
        raise SportsError("calibration_requires_independent_games")
    if any(
        x.outcome not in {0, 1} or not 0.0 <= x.probability <= 1.0
        or x.model_fit_cutoff_ms <= 0 or x.model_fit_cutoff_ms >= x.prediction_ts_ms
        for x in rows
    ):
        raise SportsError("invalid_calibration_point")
    epsilon = 1e-12
    brier = sum((x.probability - x.outcome) ** 2 for x in rows) / len(rows)
    loss = -sum(
        x.outcome * math.log(max(epsilon, x.probability))
        + (1 - x.outcome) * math.log(max(epsilon, 1.0 - x.probability))
        for x in rows
    ) / len(rows)
    return CalibrationMetrics(len(rows), brier, loss)


class SportsForwardShadow:
    """Append-only PAPER evidence sink. It has no execution method by design."""

    def __init__(self, path: Path, *, run_id: str):
        if not run_id:
            raise SportsError("shadow_run_id_required")
        self.path = Path(path)
        self.run_id = run_id

    def record(
        self, *, mapping: ContractMapping, guard: FeedGuard, decision: SportsDecision,
        observed_at_ms: int, race: SportsRaceObservation | None = None,
    ) -> str:
        if observed_at_ms <= 0 or not decision.shadow_only:
            raise SportsError("only_timestamped_shadow_decisions_can_be_recorded")
        if guard.last_event_id == "":
            raise SportsError("shadow_record_requires_causal_event")
        if race is not None:
            race.validate()
            if race.event_id != guard.last_event_id:
                raise SportsError("race_event_lineage_mismatch")
        payload = {
            "schema": "v7_sports_forward_shadow_v1", "run_id": self.run_id,
            "observed_at_ms": observed_at_ms, "paper_only": True,
            "real_order_submission": False, "market_id": mapping.market_id,
            "mapping_semantic_hash": mapping.semantic_hash,
            "event_id": guard.last_event_id, "event_sequence": guard.last_sequence,
            "connection_epoch": guard.connection_epoch, "decision": asdict(decision),
            "race": asdict(race) if race else None,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        record_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        envelope = json.dumps({**payload, "record_hash": record_hash}, sort_keys=True, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(envelope + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record_hash


__all__ = [
    "CalibrationMetrics", "CalibrationPoint", "ConnectionState", "ContractMapping",
    "FeedGuard", "Sport", "SportsCausalTape", "SportsDecision", "SportsError",
    "SportsFeedEvent", "SportsForwardShadow", "SportsRaceObservation", "SportsSourceSpec",
    "SportsTapeRecord", "SportsWireDecoder", "VerifiedSportsStreamAdapter",
    "baseline_home_probability", "chronological_calibration", "decide",
]
