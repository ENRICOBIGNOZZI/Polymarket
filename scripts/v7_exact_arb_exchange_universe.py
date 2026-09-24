#!/usr/bin/env python3
"""Public exchange metadata, independent from the frozen crypto champion.

Gamma payloads attest same-condition binary token mappings, not an independent
on-chain payout proof. Event membership alone never proves a NegRisk partition.
Unverified event relations remain visible as candidates, never enabled edges.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable
import urllib.parse
import urllib.error
import urllib.request

SCHEMA = "polymarket_v7_exact_arb_exchange_universe_v1"
STATUS_SCHEMA = "polymarket_v7_exact_arb_exchange_universe_status_v1"
SAFETY = dict(paper_only=True, authenticated_execution=False,
              real_order_submission=False, real_capital_at_risk=False,
              automatic_promotion=False)
SOURCE_DOC = "https://docs.polymarket.com/concepts/negative-risk"


def _array(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _iso_unix(value: Any) -> int:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp())
    except (ValueError, TypeError, OverflowError):
        return 0


def _atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                  allow_nan=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def fetch_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={
        "User-Agent": "polymarket-v7-exact-arb-universe/2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        # Invalid JSON/non-finite numbers must not become an empty success page.
        return json.loads(response.read().decode("utf-8"),
                          parse_constant=lambda value: _invalid_json(value))


def _invalid_json(value: str) -> Any:
    raise ValueError("nonfinite_json:" + value)


def _condition_valid(value: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{64}", value)) and int(value[2:], 16) != 0


def _token_ids(raw: dict[str, Any]) -> list[str]:
    values = _array(raw.get("clobTokenIds"))
    result = []
    for value in values:
        # No filtering of corrupt entries: dropping one changes a partition.
        if type(value) not in (str, int):
            return []
        text = str(value).strip()
        if not re.fullmatch(r"[1-9][0-9]*", text) or not 0 < int(text) < 2**256:
            return []
        result.append(text)
    return result


def _outcomes(raw: dict[str, Any]) -> list[str]:
    values = _array(raw.get("outcomes"))
    return [v.strip() for v in values] if all(isinstance(v, str) for v in values) else []


def _binary_partition(raw: dict[str, Any]) -> tuple[bool, str, list[str], list[str]]:
    condition = str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
    tokens, outcomes = _token_ids(raw), _outcomes(raw)
    valid = (_condition_valid(condition) and len(tokens) == len(outcomes) == 2
             and len(set(tokens)) == 2
             and {v.upper() for v in outcomes} in ({"YES", "NO"}, {"UP", "DOWN"}))
    return valid, condition, tokens, outcomes


def _settlement_hash(condition_id: str) -> str:
    return hashlib.sha256(("CTF_CONDITION_V1:" + condition_id.lower()).encode()).hexdigest()


def _fraction(value: Any) -> Fraction:
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid_numeric_term")
    return Fraction(str(value))


def _fee_terms(raw: dict[str, Any]) -> tuple[dict[str, Any], bool | None, bool, str]:
    flag = raw.get("feesEnabled")
    explicit = type(flag) is bool
    fee = raw.get("feeSchedule")
    if fee is None or fee == {}:
        return {}, flag if explicit else None, explicit, (
            "EXPLICIT_DISABLED" if flag is False else "MISSING_SCHEDULE")
    try:
        if not isinstance(fee, dict):
            raise ValueError("fee_shape")
        rate = _fraction(fee["rate"])
        exponent = _fraction(fee.get("exponent", 1 if rate == 0 else None))
        if not 0 <= rate <= 1 or not 0 <= exponent <= 16 or exponent.denominator != 1:
            raise ValueError("fee_bounds")
        if flag is False and rate != 0:
            raise ValueError("fee_flag_conflict")
        result = {k: fee[k] for k in ("rate", "exponent", "rounding_mode", "rounding_increment") if k in fee}
        result.setdefault("exponent", 1)  # Zero rate only; exponent is immaterial.
        # Preserve the graph's declared rounding convention, not a new fee rule.
        result.setdefault("rounding_mode", "VENUE_5DP")
        result.setdefault("rounding_increment", "0.00001")
        return result, flag if explicit else None, explicit, "SCHEDULE_PRESENT"
    except (ValueError, TypeError, KeyError, ZeroDivisionError):
        # A contradictory/malformed schedule must not fall back to fee-free.
        return {}, None, False, "INVALID_OR_CONFLICTING_SCHEDULE"


def _fee_schedule(raw: dict[str, Any]) -> dict[str, Any]:
    return _fee_terms(raw)[0]


def _market_open(raw: dict[str, Any]) -> bool:
    return (raw.get("active") is True and raw.get("closed") is False
            and raw.get("acceptingOrders") is True and raw.get("enableOrderBook") is True)


def _event_negrisk(event: dict[str, Any]) -> bool:
    return event.get("negRisk") is True or event.get("enableNegRisk") is True


def _event_augmented(event: dict[str, Any]) -> bool:
    return event.get("negRiskAugmented") is True


def _negrisk_attestation(event: dict[str, Any], rows: list[Any]) -> tuple[bool, list[str], str]:
    """A Gamma list is discovery evidence, not independent completeness proof."""
    if not _event_negrisk(event):
        return False, [], "NOT_NEGRISK"
    if _event_augmented(event):
        return False, [], "AUGMENTED_NEGRISK_UNSTABLE_MEMBERSHIP"
    if event.get("negRiskAugmented") is not False:
        return False, [], "AUGMENTATION_METADATA_UNKNOWN"
    if len(rows) < 2 or any(not isinstance(row, dict) for row in rows):
        return False, [], "INCOMPLETE_OR_MALFORMED_MEMBERS"
    if any(not _binary_partition(row)[0] or not str(row.get("id") or "") for row in rows):
        return False, [], "NONCANONICAL_MEMBER"
    ids = [str(row["id"]) for row in rows]
    tokens = [token for row in rows for token in _token_ids(row)]
    conditions = [_binary_partition(row)[1].lower() for row in rows]
    if len(set(ids)) != len(ids) or len(set(tokens)) != len(tokens) or len(set(conditions)) != len(conditions):
        return False, [], "DUPLICATE_MEMBER_OR_CLAIM"
    if any(not _market_open(row) or row.get("negRisk") is not True for row in rows):
        return False, [], "MEMBER_NOT_SIMULTANEOUSLY_ORDERABLE"
    # Never synthesize an equality over the observed subset, even if every
    # member is tradable. A separately verified semantic/membership source is
    # required before the compiler may enable this non-binary family.
    return False, [], "INDEPENDENT_COMPLETE_SET_ATTESTATION_REQUIRED"


def normalize_market(raw: dict[str, Any], event: dict[str, Any], *,
                     negrisk_verified: bool, negrisk_member_ids: list[str],
                     negrisk_reason: str) -> dict[str, Any] | None:
    market_id = str(raw.get("id") or "").strip()
    if not market_id:
        return None
    if negrisk_verified:
        raise ValueError("gamma_payload_cannot_attest_complete_set")
    binary, condition, tokens, outcomes = _binary_partition(raw)
    fee, fee_flag, fee_explicit, fee_state = _fee_terms(raw)
    event_id = str(event.get("id") or "").strip()
    partition = dict(condition_id=condition, tokens=tokens, outcomes=outcomes,
                     source="GAMMA_CANONICAL_CTF_MARKET")
    return dict(
        market_id=market_id, event_id=event_id, condition_id=condition,
        question_id=str(raw.get("questionID") or ""), question=str(raw.get("question") or ""),
        slug=str(raw.get("slug") or ""), event_slug=str(event.get("slug") or ""),
        clob_token_ids=tokens, outcomes=outcomes, active=raw.get("active") is True,
        closed=raw.get("closed") is not False, accepting_orders=_market_open(raw),
        enable_order_book=raw.get("enableOrderBook") is True,
        binary_partition_verified=binary, partition_provenance=partition if binary else None,
        partition_proof_hash=_digest(partition) if binary else None,
        partition_evidence_scope="CANONICAL_API_MAPPING_NOT_INDEPENDENT_ONCHAIN_PROOF",
        settlement_semantic_hash=_settlement_hash(condition) if binary else "",
        normalized_rules_hash="", contract_family="CTF_BINARY_PUBLIC" if binary else "UNVERIFIED_PUBLIC",
        asset="", horizon="", window_start_unix=_iso_unix(raw.get("startDate") or event.get("startDate")),
        close_timestamp_unix=_iso_unix(raw.get("endDate") or event.get("endDate")),
        tick_size=raw.get("orderPriceMinTickSize"), minimum_order_size=raw.get("orderMinSize"),
        fee_schedule=fee, fees_enabled=fee_flag, fees_enabled_explicit=fee_explicit, fee_metadata_state=fee_state,
        liquidity=max(0., _finite(raw.get("liquidityNum"), _finite(raw.get("liquidity")))),
        volume_24h=max(0., _finite(raw.get("volume24hr"), _finite(raw.get("volume24h")))),
        neg_risk=raw.get("negRisk") is True or _event_negrisk(event),
        neg_risk_augmented=event.get("negRiskAugmented") if type(event.get("negRiskAugmented")) is bool else None,
        neg_risk_other=raw.get("negRiskOther") is True,
        neg_risk_group_id=str(event.get("negRiskMarketID") or ""),
        neg_risk_complete_set_verified=False, neg_risk_complete_set_market_ids=[],
        neg_risk_complete_set_provenance=dict(event_id=event_id, reason=negrisk_reason),
        neg_risk_complete_set_proof_hash=None, combo_status=str(raw.get("comboStatus") or ""),
        rfq_enabled=raw.get("rfqEnabled") is True,
    )


def build_snapshot(events: list[dict[str, Any]], model_sha: str, *,
                   generated_at_ms: int | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", model_sha) or not isinstance(events, list):
        raise ValueError("invalid_snapshot_input")
    markets: dict[str, dict[str, Any]] = {}
    blocked: set[str] = set()
    reasons: Counter[str] = Counter()
    candidates = []
    event_count = negrisk_count = augmented_count = 0
    for event in events:
        if not isinstance(event, dict) or not str(event.get("id") or "").strip():
            raise ValueError("malformed_event")
        if event.get("active") is not True or event.get("closed") is not False:
            reasons["EVENT_NOT_EXPLICITLY_OPEN"] += 1
            continue
        rows = event.get("markets")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("malformed_event_members")
        event_count += 1
        verified, members, reason = _negrisk_attestation(event, rows)
        if _event_negrisk(event):
            negrisk_count += 1
            augmented_count += _event_augmented(event)
            reasons[reason] += 1
            candidates.append(dict(event_id=str(event["id"]), relation_family="NEGRISK_COMPLETE_SET",
                                   verification="UNVERIFIED_CANDIDATE", reason=reason,
                                   observed_market_ids=sorted(str(row.get("id") or "") for row in rows)))
        for raw in rows:
            market = normalize_market(raw, event, negrisk_verified=verified,
                                      negrisk_member_ids=members, negrisk_reason=reason)
            if market is None:
                raise ValueError("missing_market_id")
            mid = market["market_id"]
            prior = markets.get(mid)
            # Check conflicts before eligibility; a closed duplicate must not
            # leave an earlier open copy eligible for screening.
            if mid in blocked:
                continue
            if prior is not None and prior != market:
                blocked.add(mid)
                markets.pop(mid)
                reasons["CONFLICTING_MARKET_ID"] += 1
                continue
            markets[mid] = market
    # Conflicting condition/token bindings are ambiguous, not extra liquidity.
    bindings: dict[str, tuple[str, str]] = {}
    for mid, market in markets.items():
        for token in market["clob_token_ids"]:
            identity = (market["condition_id"].lower(), mid)
            prior = bindings.setdefault(token, identity)
            if prior != identity:
                blocked.update((mid, prior[1]))
                reasons["CONFLICTING_TOKEN_BINDING"] += 1
    rows = sorted((m for mid, m in markets.items() if mid not in blocked and m["accepting_orders"]),
                  key=lambda m: m["market_id"])
    membership = [dict(market_id=m["market_id"], condition_id=m["condition_id"],
                       tokens=m["clob_token_ids"], event_id=m["event_id"]) for m in rows]
    return dict(schema=SCHEMA, version=1, **SAFETY, execution_authority=False,
                model_sha=model_sha, timestamp_ms=int(time.time_ns()//1_000_000 if generated_at_ms is None else generated_at_ms),
                membership_sha256=_digest(membership), source="PUBLIC_GAMMA_EVENTS", source_valid=True,
                source_semantics=dict(binary="CANONICAL_SAME_CONDITION_CTF_TWO_OUTCOME_PARTITION",
                                      negrisk="UNVERIFIED_UNTIL_INDEPENDENT_MEMBERSHIP_AND_SEMANTIC_PROOF",
                                      documentation=SOURCE_DOC, cross_condition_equivalence="NEVER_INFERRED_FROM_TEXT"),
                events_total=event_count, negrisk_events=negrisk_count, verified_negrisk_events=0,
                augmented_negrisk_events=augmented_count, negrisk_rejection_reasons=dict(reasons),
                source_rejection_reasons=dict(reasons), quarantined_market_ids=sorted(blocked),
                unverified_candidates=candidates, markets=rows)


def discover_events(base_url: str, timeout: float, page_size: int, max_pages: int,
                    fetcher: Callable[[str, float], Any] = fetch_json) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 500 or not 0 < timeout <= 30:
        raise ValueError("invalid_discovery_bounds")
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("invalid_public_source_url")
    events: dict[str, dict[str, Any]] = {}
    started = time.monotonic_ns()
    exhaustive = False
    for page in range(max_pages):
        query = urllib.parse.urlencode(dict(active="true", closed="false", limit=page_size,
                                           offset=page*page_size, order="id", ascending="true"))
        value = fetcher(base_url.rstrip("/") + "/events?" + query, timeout)
        rows = value if isinstance(value, list) else value.get("events") if isinstance(value, dict) else None
        if not isinstance(rows, list) or len(rows) > page_size:
            raise ValueError("invalid_gamma_event_page")
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("id") or "").strip():
                raise ValueError("malformed_gamma_event")
            key = str(row["id"])
            if key in events:
                raise ValueError("duplicate_event_or_pagination_drift")
            events[key] = row
        if len(rows) < page_size:
            exhaustive = True
            break
    return list(events.values()), dict(requests=page+1, events=len(events),
        discovery_exhaustive=exhaustive, pagination_loop_guard_hit=not exhaustive,
        point_in_time_consistent=False, coverage_scope="BOUNDED_OFFSET_SCAN_NOT_ATOMIC_SNAPSHOT",
        scan_duration_ms=(time.monotonic_ns()-started)/1_000_000.)


def discover_keyset_events(base_url: str, timeout: float, page_size: int, max_pages: int,
                          fetcher: Callable[[str, float], Any] = fetch_json) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Public Gamma /events/keyset; offset is capped at 2000 by the venue.

    Follow after_cursor, not cursor (unknown parameters can be silently ignored).
    A completed pagination is still not an atomic snapshot or settlement proof.
    """
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 500 or not 0 < timeout <= 30:
        raise ValueError("invalid_discovery_bounds")
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("invalid_public_source_url")
    events: dict[str, dict[str, Any]] = {}
    cursors: set[str] = set()
    cursor = None
    exhaustive = False
    started = time.monotonic_ns()
    for page in range(max_pages):
        params = dict(active="true", closed="false", limit=page_size, order="id", ascending="true")
        if cursor is not None: params["after_cursor"] = cursor
        value = fetcher(base_url.rstrip("/") + "/events/keyset?" + urllib.parse.urlencode(params), timeout)
        if not isinstance(value, dict) or "next_cursor" not in value:
            raise ValueError("invalid_gamma_keyset_page")
        rows = value.get("events")
        if not isinstance(rows, list) or len(rows) > page_size:
            raise ValueError("invalid_gamma_keyset_page")
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("id") or "").strip():
                raise ValueError("malformed_gamma_event")
            key = str(row["id"])
            if key in events: raise ValueError("duplicate_event_or_pagination_drift")
            events[key] = row
        cursor = value["next_cursor"]
        if cursor is None or cursor == "":
            exhaustive = True
            break
        if not rows or not isinstance(cursor, str) or len(cursor) > 4096 or cursor in cursors:
            raise ValueError("invalid_or_repeated_gamma_cursor")
        cursors.add(cursor)
    return list(events.values()), dict(requests=page+1, events=len(events),
        discovery_exhaustive=exhaustive, pagination_loop_guard_hit=not exhaustive,
        point_in_time_consistent=False, coverage_scope="BOUNDED_KEYSET_SCAN_NOT_ATOMIC_SNAPSHOT",
        pagination="GAMMA_EVENTS_KEYSET_AFTER_CURSOR",
        scan_duration_ms=(time.monotonic_ns()-started)/1_000_000.)


def run_once(args: argparse.Namespace, fetcher: Callable[[str, float], Any] = fetch_json) -> dict[str, Any]:
    status = dict(schema=STATUS_SCHEMA, **SAFETY, execution_authority=False,
                  model_sha=args.model_sha, timestamp_ms=time.time_ns()//1_000_000)
    try:
        events, diagnostics = discover_keyset_events(args.gamma_url, args.timeout_seconds, args.page_size, args.max_pages, fetcher)
        snapshot = build_snapshot(events, args.model_sha)
        snapshot["discovery"] = diagnostics
        status.update(state="READY" if diagnostics["discovery_exhaustive"] else "BOUNDED_PARTIAL",
                      **diagnostics, markets=len(snapshot["markets"]), negrisk_events=snapshot["negrisk_events"],
                      verified_negrisk_events=0, unverified_candidates=len(snapshot["unverified_candidates"]),
                      membership_sha256=snapshot["membership_sha256"])
    except Exception as exc:
        # Publish a safe empty generation rather than letting the graph consume
        # the last successful event list indefinitely after a failed refresh.
        snapshot = build_snapshot([], args.model_sha)
        snapshot.update(source_valid=False, source_error=type(exc).__name__)
        status.update(state="SOURCE_ERROR", error=type(exc).__name__, markets=0)
        if isinstance(exc, urllib.error.HTTPError): status["http_status"] = exc.code
    _atomic(args.output, snapshot)
    if args.status:
        _atomic(args.status, status)
    return status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path)
    ap.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    ap.add_argument("--timeout-seconds", type=float, default=5.)
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--interval-seconds", type=float, default=60.)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.model_sha) or not 5 <= args.interval_seconds <= 3600:
        ap.error("invalid model sha or interval")
    if not .5 <= args.timeout_seconds <= 30 or not 1 <= args.page_size <= 100 or not 1 <= args.max_pages <= 500:
        ap.error("invalid discovery bounds")
    while True:
        status = run_once(args)
        print(json.dumps(status, sort_keys=True), flush=True)
        if args.once:
            return 0 if status["state"] in {"READY", "BOUNDED_PARTIAL"} else 2
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
