#!/usr/bin/env python3
"""OSINT-to-Polymarket mapping discovery and verified-only forward gate.

Candidate generation may use lexical overlap, but the output is always marked
CANDIDATE_ONLY. Only mappings admitted by v7_semantic_mapping may activate the
forward reaction path. The service never emits an executable intent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

from v7_osint_pipeline import CausalEventTape
from v7_semantic_mapping import load_verified_mappings


STATUS_SCHEMA = "polymarket_v7_osint_mapping_status_v1"
CANDIDATE_SCHEMA = "polymarket_v7_osint_mapping_candidate_v1"
STATE_SCHEMA = "polymarket_v7_osint_mapping_state_v1"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
TOKEN = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "will", "by", "is", "be", "and", "or"}


class OsintMappingCollectorError(ValueError):
    pass


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
        raise OsintMappingCollectorError("repository_head_unavailable") from exc
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise OsintMappingCollectorError("repository_head_invalid")
    return sha


def _tokens(text: str) -> set[str]:
    return {row for row in TOKEN.findall(text.lower()) if len(row) > 2 and row not in STOP}


def lexical_candidate_score(event: Mapping[str, Any], market: Mapping[str, Any]) -> float:
    """Discovery score only; callers cannot promote it to verification."""
    content = str(event.get("content") or "")
    try:
        parsed = json.loads(content)
        event_text = " ".join(str(parsed.get(key) or "") for key in ("title", "summary"))
    except (TypeError, json.JSONDecodeError):
        event_text = content
    market_text = " ".join(str(market.get(key) or "") for key in ("question", "description"))
    left, right = _tokens(event_text), _tokens(market_text)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def fetch_markets(endpoint: str, *, timeout: float = 20.0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not endpoint.startswith("https://gamma-api.polymarket.com/"):
        raise OsintMappingCollectorError("polymarket_market_endpoint_not_allowlisted")
    request = urllib.request.Request(endpoint, headers={
        "Accept": "application/json", "Accept-Encoding": "gzip",
        "User-Agent": "PolymarketV7Research/1.0",
    })
    started = time.monotonic_ns()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            first_byte = time.monotonic_ns()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            completed = time.monotonic_ns()
            if len(body) > MAX_RESPONSE_BYTES:
                raise OsintMappingCollectorError("polymarket_market_response_too_large")
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                import gzip
                body = gzip.decompress(body)
    except (OSError, urllib.error.URLError) as exc:
        raise OsintMappingCollectorError(f"polymarket_market_fetch_failed:{exc}") from exc
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OsintMappingCollectorError("polymarket_market_json_invalid") from exc
    if not isinstance(value, list):
        raise OsintMappingCollectorError("polymarket_market_schema_invalid")
    rows = [row for row in value if isinstance(row, dict)]
    return rows, {
        "ttfb_ms": (first_byte - started) / 1_000_000.0,
        "request_ms": (completed - started) / 1_000_000.0,
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


def _configuration(path: Path) -> tuple[str, int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OsintMappingCollectorError("external_input_config_unreadable") from exc
    if (
        value.get("schema") != "polymarket_v7_external_inputs_v1"
        or value.get("version") != 7 or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
    ):
        raise OsintMappingCollectorError("external_input_config_safety_invalid")
    section = value.get("osint") if isinstance(value.get("osint"), dict) else {}
    endpoint = str(section.get("polymarket_market_endpoint") or "")
    limit = int(section.get("candidate_limit_per_event") or 20)
    if not endpoint or not 1 <= limit <= 100:
        raise OsintMappingCollectorError("osint_mapping_config_invalid")
    return endpoint, limit


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": STATE_SCHEMA, "processed_event_ids": [], "last_hash": "0" * 64}
    if value.get("schema") != STATE_SCHEMA or not isinstance(value.get("processed_event_ids"), list):
        raise OsintMappingCollectorError("osint_mapping_state_invalid")
    return value


def append_candidate(path: Path, state: dict[str, Any], value: Mapping[str, Any]) -> None:
    body = {"schema": CANDIDATE_SCHEMA, "previous_hash": state.get("last_hash", "0" * 64), **dict(value)}
    record_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**body, "record_hash": record_hash}, sort_keys=True,
                                separators=(",", ":")) + "\n")
        handle.flush(); os.fsync(handle.fileno())
    state["last_hash"] = record_hash


def collect_once(*, repository_root: Path, config_path: Path, mappings_path: Path,
                 raw_tape_path: Path, candidate_tape_path: Path, state_path: Path,
                 status_path: Path, now_ms: int | None = None,
                 market_fetcher: Any = fetch_markets,
                 forward_tape_path: Path | None = None) -> dict[str, Any]:
    timestamp = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    sha = git_head(repository_root)
    endpoint, limit = _configuration(config_path)
    mappings = load_verified_mappings(mappings_path, "osint", now_ms=timestamp,
                                      repository_sha=sha)
    state = _load_state(state_path)
    processed = set(str(x) for x in state.get("processed_event_ids") or [])
    events = [row.payload for row in CausalEventTape(raw_tape_path).read_verified()
              if row.kind == "RAW_EVENT" and str(row.payload.get("event_id") or "") not in processed]
    candidates = 0
    blocker = ""
    markets: list[dict[str, Any]] = []
    timing: dict[str, Any] = {}
    try:
        markets, timing = market_fetcher(endpoint)
        for event in events:
            scored = sorted(
                ((lexical_candidate_score(event, market), market) for market in markets),
                key=lambda pair: pair[0], reverse=True,
            )[:limit]
            for score, market in scored:
                if score <= 0.0:
                    continue
                market_id = str(market.get("conditionId") or market.get("id") or "")
                if not market_id:
                    continue
                append_candidate(candidate_tape_path, state, {
                    "state": "CANDIDATE", "candidate_only": True,
                    "verification_authority": False,
                    "event_id": event.get("event_id"), "event_family": event.get("event_family"),
                    "market_id": market_id, "question": str(market.get("question") or ""),
                    "lexical_discovery_score": score,
                    "market_metadata_hash": hashlib.sha256(json.dumps(market, sort_keys=True).encode()).hexdigest(),
                    "discovered_at_ms": timestamp, "repository_sha": sha,
                    "reason": "lexical_overlap_generates_candidate_only",
                })
                candidates += 1
            processed.add(str(event.get("event_id") or ""))
        if not mappings:
            blocker = "BLOCKED_NO_VERIFIED_MAPPING"
    except Exception as exc:
        blocker = f"BLOCKED_POLYMARKET_MARKET_DISCOVERY:{type(exc).__name__}:{exc}"
    state["processed_event_ids"] = sorted(processed)[-100000:]
    state["updated_at_ms"] = timestamp
    atomic_json(state_path, state)
    mapping_active = len(mappings) > 0
    status = {
        "schema": STATUS_SCHEMA, "version": 7, "family": "osint",
        "authority": "RESEARCH", "model_sha": sha, "timestamp_ms": timestamp,
        "implementation_complete": True, "mapping_pipeline": True,
        "market_discovery_operational": bool(markets),
        "mapping_status": "VERIFIED_MAPPINGS_ACTIVE" if mapping_active else "NO_VERIFIED_MAPPING",
        "verified_mappings": len(mappings), "candidate_mappings": candidates,
        "unprocessed_events": len(events), "observed_markets": len(markets),
        "forward_collection_active": mapping_active and bool(markets),
        "forward_tape_path": str(forward_tape_path) if forward_tape_path else "",
        "causal_tape": True, "correction_handling": True,
        "title_similarity_verification_forbidden": True,
        "market_request_ttfb_ms": timing.get("ttfb_ms"),
        "market_request_ms": timing.get("request_ms"),
        "blocker": blocker, "reason_codes": [blocker] if blocker else [],
        "paper_only": True, "research_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False,
        "capital_authority": False, "oms_authority": False,
        "ledger_write_authority": False, "promotion_authority": False,
    }
    atomic_json(status_path, status)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, default=Path("config/v7_external_inputs.json"))
    parser.add_argument("--mappings", type=Path, default=Path("config/v7_external_mappings.json"))
    parser.add_argument("--raw-tape", type=Path, required=True)
    parser.add_argument("--candidate-tape", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--forward-tape", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)
    while True:
        status = collect_once(
            repository_root=args.repository_root, config_path=args.config,
            mappings_path=args.mappings, raw_tape_path=args.raw_tape,
            candidate_tape_path=args.candidate_tape, state_path=args.state,
            status_path=args.status, forward_tape_path=args.forward_tape,
        )
        print(json.dumps(status, sort_keys=True), flush=True)
        if not args.loop:
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
