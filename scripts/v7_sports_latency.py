#!/usr/bin/env python3
"""Sport-specific state transition and PM contract comparison kernel.

The feed guard is the authority boundary: gaps, conflicts, stale events and
unsupported settlement mappings fail closed before a probability is computed.
All outputs remain research/shadow until a verified streaming source and
chronological calibration evidence exist.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class SportsError(ValueError):
    pass


class Sport(str, Enum):
    SOCCER = "SOCCER"
    TENNIS = "TENNIS"
    BASKETBALL = "BASKETBALL"
    BASEBALL = "BASEBALL"


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

    def validate(self) -> None:
        if not all((self.market_id, self.game_id, self.selection, self.settlement_source, self.rules_hash)):
            raise SportsError("incomplete_contract_mapping")
        if self.contract_type != "MATCH_WINNER":
            raise SportsError("unsupported_contract_semantics")
        if self.selection not in {"HOME", "AWAY", "PLAYER_A", "PLAYER_B"}:
            raise SportsError("unsupported_selection")
        if not self.verified:
            raise SportsError("unverified_contract_mapping")


class FeedGuard:
    def __init__(self, game_id: str, max_age_ms: int):
        self.game_id = game_id
        self.max_age_ms = int(max_age_ms)
        self.last_sequence = 0
        self.last_event_id = ""
        self.last_state: Mapping[str, Any] | None = None
        self.valid = True
        self.blocker = "cold_start"

    def apply(self, event: SportsFeedEvent, now_ms: int) -> None:
        if event.game_id != self.game_id or not event.official:
            self.valid = False; self.blocker = "wrong_game_or_unverified_feed"; return
        if event.receive_ts_ms < event.source_ts_ms or event.receive_ts_ms > now_ms:
            self.valid = False; self.blocker = "invalid_feed_clocks"; return
        if now_ms - event.receive_ts_ms > self.max_age_ms:
            self.valid = False; self.blocker = "stale_feed"; return
        if event.sequence <= self.last_sequence:
            if not event.correction_of or event.correction_of != self.last_event_id:
                self.valid = False; self.blocker = "replayed_or_conflicting_event"; return
        elif self.last_sequence and event.sequence != self.last_sequence + 1:
            self.valid = False; self.blocker = "feed_gap"; return
        self.last_sequence = max(self.last_sequence, event.sequence)
        self.last_event_id = event.event_id
        self.last_state = dict(event.state)
        self.valid = True; self.blocker = ""


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
        if not 1.0 <= inning <= 20.0 or outs not in {0.0, 1.0, 2.0} or batting_home not in {0.0, 1.0} or not 0.0 <= base_state <= 3.0:
            raise SportsError("invalid_baseball_state")
        leverage = min(4.0, 0.45 + inning / 4.0)
        batting = (2.0 * batting_home - 1.0) * (0.10 + 0.05 * base_state - 0.025 * outs)
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
        if mapping.game_id != guard.game_id or not guard.valid or guard.last_state is None:
            raise SportsError(guard.blocker or "invalid_feed")
        if not (0.0 <= pm_bid <= pm_ask <= 1.0): raise SportsError("invalid_pm_book")
        p_home = baseline_home_probability(mapping.sport, guard.last_state)
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


__all__ = ["ContractMapping", "FeedGuard", "Sport", "SportsDecision", "SportsError",
           "SportsFeedEvent", "baseline_home_probability", "decide"]
