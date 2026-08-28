#!/usr/bin/env python3
"""Credential-gated Sportradar push adapter for V7 sports-latency research.

The process remains healthy and publishes CREDENTIALS_REQUIRED when the paid
Realtime key is absent.  With a key it consumes the official HTTP chunked push
stream, preserves provider payloads and causal receive clocks, and never gains
execution or promotion authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from v7_semantic_mapping import load_verified_mappings
from v7_sports_latency import Sport, SportsFeedEvent


STATUS_SCHEMA = "polymarket_v7_external_input_component_status_v1"
TAPE_SCHEMA = "polymarket_v7_sports_causal_feed_tape_v1"
STATE_SCHEMA = "polymarket_v7_sports_collector_state_v1"
MAX_JSON_OBJECT_BYTES = 8 * 1024 * 1024


class SportsCollectorError(ValueError):
    pass


class SportradarSportsFeedAdapter:
    """Canonical provider boundary; strategy code never sees vendor transport."""

    def __init__(self, provider: Mapping[str, Any], state: dict[str, Any]):
        self.provider = dict(provider)
        self.state = state
        self.connected = False
        self.subscribed_games: tuple[str, ...] = ()

    def connect(self, *, secret: str, stream: Iterable[bytes] | None = None) -> Iterable[bytes]:
        if stream is not None:
            self.connected = True
            self.state["connection_epoch"] = int(self.state.get("connection_epoch") or 0) + 1
            return stream
        if not secret:
            raise SportsCollectorError(f"credentials_required:{self.provider.get('secret_env')}")
        raise SportsCollectorError("network_connection_owned_by_run_session")

    def subscribe(self, game_ids: Sequence[str] = ()) -> str:
        if not self.connected:
            raise SportsCollectorError("sports_subscribe_requires_connection")
        if any(not str(game_id) for game_id in game_ids):
            raise SportsCollectorError("sports_subscription_game_id_invalid")
        self.subscribed_games = tuple(str(game_id) for game_id in game_ids)
        endpoint = str(self.provider.get("endpoint") or "")
        if self.subscribed_games:
            import urllib.parse
            endpoint += "&sport_event_id=" + urllib.parse.quote(",".join(self.subscribed_games), safe=":,")
        return endpoint

    def normalize(self, payload: Mapping[str, Any], *, receive_ts_ms: int) -> tuple[SportsFeedEvent, ...]:
        return normalize_sportradar_payload(
            payload, receive_ts_ms=receive_ts_ms,
            next_sequence=self.state.get("sequences") or {},
        )

    def health(self, *, now_ms: int, last_receive_ms: int) -> dict[str, Any]:
        age = now_ms - last_receive_ms if last_receive_ms else None
        maximum = int(self.provider.get("max_feed_age_ms") or 15_000)
        return {"connected": self.connected, "feed_age_ms": age,
                "fresh": self.connected and age is not None and 0 <= age <= maximum}

    def reconnect(self) -> str:
        self.connected = False
        self.state["reconnect_count"] = int(self.state.get("reconnect_count") or 0) + 1
        self.state["gap_count"] = int(self.state.get("gap_count") or 0) + 1
        return "REST_TIMELINE_RECOVERY_REQUIRED_BEFORE_RESUBSCRIBE"

    def sequence_state(self) -> dict[str, int]:
        return {str(key): int(value) for key, value in (self.state.get("sequences") or {}).items()}


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def git_head(repository_root: Path) -> str:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository_root, text=True,
            stderr=subprocess.STDOUT, timeout=10,
        ).strip().lower()
    except (OSError, subprocess.SubprocessError) as exc:
        raise SportsCollectorError("repository_head_unavailable") from exc
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise SportsCollectorError("repository_head_invalid")
    return sha


def iter_concatenated_json(chunks: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """Parse adjacent Sportradar JSON objects without inventing framing."""
    decoder = json.JSONDecoder()
    buffer = ""
    for chunk in chunks:
        try:
            buffer += chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SportsCollectorError("sports_stream_invalid_utf8") from exc
        if len(buffer.encode("utf-8")) > MAX_JSON_OBJECT_BYTES:
            raise SportsCollectorError("sports_stream_object_too_large")
        while True:
            stripped = buffer.lstrip()
            if not stripped:
                buffer = ""; break
            try:
                value, end = decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                buffer = stripped; break
            if not isinstance(value, dict):
                raise SportsCollectorError("sports_stream_object_not_mapping")
            yield value
            buffer = stripped[end:]
    if buffer.strip():
        raise SportsCollectorError("sports_stream_truncated_json")


SOCCER_STATES = {
    "not_started", "delayed", "started", "live", "1st_half", "halftime",
    "2nd_half", "awaiting_extra_time", "extra_time_halftime", "extra_time",
    "awaiting_penalties", "penalties", "ended", "closed", "suspended",
    "interrupted", "cancelled", "postponed", "abandoned",
}


def _time_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_sportradar_payload(value: Mapping[str, Any], *, receive_ts_ms: int,
                                 next_sequence: Mapping[str, int]) -> tuple[SportsFeedEvent, ...]:
    payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
    if not payload:
        if isinstance(value.get("heartbeat"), dict):
            return ()
        raise SportsCollectorError("sportradar_payload_missing")
    sport_event = payload.get("sport_event") if isinstance(payload.get("sport_event"), dict) else {}
    status = payload.get("sport_event_status") if isinstance(payload.get("sport_event_status"), dict) else {}
    game_id = str(sport_event.get("id") or payload.get("sport_event_id") or status.get("sport_event_id") or "")
    if not game_id:
        raise SportsCollectorError("sportradar_game_id_missing")
    match_status = str(status.get("match_status") or status.get("status") or "").lower()
    if match_status and match_status not in SOCCER_STATES:
        raise SportsCollectorError(f"unsupported_soccer_state:{match_status}")
    timeline = payload.get("timeline")
    if isinstance(timeline, dict):
        timeline = timeline.get("events")
    if not isinstance(timeline, list):
        timeline = [payload.get("event")] if isinstance(payload.get("event"), dict) else []
    output: list[SportsFeedEvent] = []
    sequence = int(next_sequence.get(game_id) or 0)
    for raw in timeline:
        if not isinstance(raw, dict):
            continue
        event_id = str(raw.get("id") or "")
        event_type = str(raw.get("type") or "")
        source_ts_ms = _time_ms(raw.get("time") or raw.get("timestamp"))
        if not event_id or not event_type or source_ts_ms <= 0:
            raise SportsCollectorError("sportradar_event_identity_or_clock_missing")
        sequence += 1
        home = status.get("home_score")
        away = status.get("away_score")
        state = {
            "sport": Sport.SOCCER.value, "status": str(status.get("status") or ""),
            "match_status": match_status, "score_home": home, "score_away": away,
            "period": raw.get("period"), "match_clock": raw.get("match_clock"),
            "match_time": raw.get("match_time"), "competitor": raw.get("competitor"),
            "remaining_seconds": None, "red_cards_home": None, "red_cards_away": None,
            "provider_full_state": status,
        }
        correction_of = str(raw.get("correction_of") or raw.get("replaces_event_id") or "")
        output.append(SportsFeedEvent(
            event_id=event_id, game_id=game_id, sequence=sequence,
            event_type=event_type, source="sportradar_soccer_v4_push", official=True,
            source_ts_ms=source_ts_ms, receive_ts_ms=receive_ts_ms, state=state,
            correction_of=correction_of, schema_version="sportradar_soccer_v4_push",
        ))
    return tuple(output)


def _load_configuration(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SportsCollectorError("external_input_config_unreadable") from exc
    if (
        value.get("schema") != "polymarket_v7_external_inputs_v1"
        or value.get("version") != 7 or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
    ):
        raise SportsCollectorError("external_input_config_safety_invalid")
    section = value.get("sports_latency") if isinstance(value.get("sports_latency"), dict) else {}
    providers = section.get("providers") if isinstance(section.get("providers"), list) else []
    primary = [row for row in providers if isinstance(row, dict) and row.get("provider_id") == section.get("primary_provider")]
    if len(primary) != 1 or primary[0].get("transport") != "HTTP_CHUNKED_PUSH":
        raise SportsCollectorError("sports_primary_provider_invalid")
    provider = primary[0]
    if provider.get("secret_env") != "PM_V7_SPORTRADAR_API_KEY":
        raise SportsCollectorError("sports_secret_contract_invalid")
    return provider


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": STATE_SCHEMA, "sequences": {}, "event_hashes": {},
                "last_hash": "0" * 64, "connection_epoch": 0}
    if value.get("schema") != STATE_SCHEMA or not isinstance(value.get("sequences"), dict):
        raise SportsCollectorError("sports_collector_state_invalid")
    return value


def append_event(path: Path, state: dict[str, Any], event: SportsFeedEvent,
                 *, repository_sha: str, receive_monotonic_ns: int) -> bool:
    hashes = state.setdefault("event_hashes", {})
    payload_hash = event.payload_hash
    existing = hashes.get(event.event_id)
    if existing == payload_hash:
        return False
    if existing and not event.correction_of:
        raise SportsCollectorError("conflicting_event_requires_correction_lineage")
    body = {
        "schema": TAPE_SCHEMA, "previous_hash": state.get("last_hash", "0" * 64),
        "event": asdict(event), "payload_hash": payload_hash,
        "receive_monotonic_ns": receive_monotonic_ns,
        "connection_epoch": state.get("connection_epoch", 0),
        "repository_sha": repository_sha,
    }
    record_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**body, "record_hash": record_hash}, sort_keys=True,
                                separators=(",", ":"), default=str) + "\n")
        handle.flush(); os.fsync(handle.fileno())
    state["last_hash"] = record_hash
    hashes[event.event_id] = payload_hash
    state["sequences"][event.game_id] = event.sequence
    return True


def component_status(*, sha: str, timestamp_ms: int, provider: Mapping[str, Any],
                     state: Mapping[str, Any], feed_status: str, blocker: str,
                     verified_mappings: int, events: int, heartbeats: int,
                     parse_failures: int, last_receive_ms: int) -> dict[str, Any]:
    mapping_active = verified_mappings > 0
    feed_operational = feed_status == "OPERATIONAL"
    return {
        "schema": STATUS_SCHEMA, "version": 7, "family": "sports_latency",
        "authority": "RESEARCH", "model_sha": sha, "timestamp_ms": timestamp_ms,
        "timestamp": timestamp_ms // 1000, "process_state": "RUNNING",
        "evidence_state": "ACTIVE" if feed_operational and mapping_active else "BLOCKED_EXTERNAL",
        "implementation_complete": True, "adapter_complete": True,
        "feed_operational": feed_operational, "feed_status": feed_status,
        "provider": provider.get("provider_id"), "transport": provider.get("transport"),
        "missing_secret": provider.get("secret_env") if feed_status == "CREDENTIALS_REQUIRED" else "",
        "mapping_pipeline": True,
        "mapping_status": "VERIFIED_MAPPINGS_ACTIVE" if mapping_active else "NO_VERIFIED_MAPPING",
        "verified_mappings": verified_mappings, "state_machine_active": events > 0,
        "forward_race_tape_active": events > 0 and mapping_active,
        "feed_age_ms": timestamp_ms - last_receive_ms if last_receive_ms else None,
        "last_sequence": max((int(x) for x in (state.get("sequences") or {}).values()), default=0),
        "connection_epoch": int(state.get("connection_epoch") or 0),
        "reconnect_count": int(state.get("reconnect_count") or 0),
        "gap_count": int(state.get("gap_count") or 0),
        "parse_failure_count": parse_failures, "dropped_event_count": int(state.get("dropped_event_count") or 0),
        "events_received": events, "heartbeats_received": heartbeats,
        "source_timestamp_resolution": "PROVIDER_FIELD_DEPENDENT",
        "latency_claim": "UNAVAILABLE_UNTIL_MEASURED",
        "blocker": blocker, "reason_codes": [blocker] if blocker else [],
        "last_attempt_ts": timestamp_ms // 1000,
        "last_success_ts": timestamp_ms // 1000 if feed_operational else 0,
        "paper_only": True, "research_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False,
        "capital_authority": False, "oms_authority": False,
        "ledger_write_authority": False, "promotion_authority": False,
    }


def run_session(*, repository_root: Path, config_path: Path, mappings_path: Path,
                tape_path: Path, state_path: Path, status_path: Path,
                stream: Iterable[bytes] | None = None, now_ms: int | None = None,
                maximum_objects: int | None = None) -> dict[str, Any]:
    timestamp = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    sha = git_head(repository_root)
    provider = _load_configuration(config_path)
    mappings = load_verified_mappings(mappings_path, "sports_latency", now_ms=timestamp,
                                      repository_sha=sha)
    state = _load_state(state_path)
    secret = os.environ.get(str(provider.get("secret_env") or ""), "").strip()
    if stream is None and not secret:
        status = component_status(
            sha=sha, timestamp_ms=timestamp, provider=provider, state=state,
            feed_status="CREDENTIALS_REQUIRED",
            blocker=f"BLOCKED_PROVIDER_CREDENTIALS:{provider['secret_env']}",
            verified_mappings=len(mappings), events=0, heartbeats=0,
            parse_failures=0, last_receive_ms=0,
        )
        atomic_json(state_path, state); atomic_json(status_path, status)
        return status
    response = None
    if stream is None:
        access_env = str(provider.get("access_level_env") or "")
        access_level = os.environ.get(access_env, str(provider.get("default_access_level") or "production"))
        endpoint = str(provider["endpoint"]).replace("{access_level}", access_level)
        request = urllib.request.Request(endpoint, headers={
            "Accept": "application/json", "x-api-key": secret,
            "User-Agent": "PolymarketV7Research/1.0",
        })
        response = urllib.request.urlopen(request, timeout=30.0)
        stream = iter(lambda: response.read(64 * 1024), b"")
    state["connection_epoch"] = int(state.get("connection_epoch") or 0) + 1
    events = heartbeats = parse_failures = objects = 0
    last_receive_ms = 0
    blocker = ""
    try:
        for value in iter_concatenated_json(stream):
            objects += 1
            receive_ms = timestamp if now_ms is not None else time.time_ns() // 1_000_000
            monotonic_ns = time.monotonic_ns()
            if isinstance(value.get("heartbeat"), dict):
                heartbeats += 1; last_receive_ms = receive_ms
            else:
                try:
                    normalized = normalize_sportradar_payload(
                        value, receive_ts_ms=receive_ms,
                        next_sequence=state.get("sequences") or {},
                    )
                    for event in normalized:
                        if append_event(tape_path, state, event, repository_sha=sha,
                                        receive_monotonic_ns=monotonic_ns):
                            events += 1
                    if normalized: last_receive_ms = receive_ms
                except SportsCollectorError:
                    parse_failures += 1
            if maximum_objects is not None and objects >= maximum_objects:
                break
    except (OSError, urllib.error.URLError, SportsCollectorError) as exc:
        blocker = f"BLOCKED_PROVIDER_STREAM:{type(exc).__name__}:{exc}"
        state["reconnect_count"] = int(state.get("reconnect_count") or 0) + 1
    finally:
        if response is not None:
            response.close()
    feed_status = "OPERATIONAL" if (events or heartbeats) and not blocker else "DOWN"
    if feed_status == "OPERATIONAL" and not mappings:
        blocker = "BLOCKED_NO_VERIFIED_MAPPING"
    atomic_json(state_path, state)
    status = component_status(
        sha=sha, timestamp_ms=timestamp, provider=provider, state=state,
        feed_status=feed_status, blocker=blocker, verified_mappings=len(mappings),
        events=events, heartbeats=heartbeats, parse_failures=parse_failures,
        last_receive_ms=last_receive_ms,
    )
    atomic_json(status_path, status)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, default=Path("config/v7_external_inputs.json"))
    parser.add_argument("--mappings", type=Path, default=Path("config/v7_external_mappings.json"))
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)
    failures = 0
    while True:
        status = run_session(
            repository_root=args.repository_root, config_path=args.config,
            mappings_path=args.mappings, tape_path=args.tape, state_path=args.state,
            status_path=args.status,
        )
        print(json.dumps(status, sort_keys=True), flush=True)
        if not args.loop:
            return 0
        failures = failures + 1 if status["feed_status"] == "DOWN" else 0
        delay = min(60.0, max(1.0, args.interval) * (2 ** min(failures, 4)))
        time.sleep(delay * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    raise SystemExit(main())
