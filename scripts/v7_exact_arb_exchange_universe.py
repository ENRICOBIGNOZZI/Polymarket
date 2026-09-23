#!/usr/bin/env python3
"""Broad public Polymarket universe for zero-authority exact-arbitrage research.

This collector is deliberately independent from the canonical crypto execution
universe.  It never supplies champion selection or execution authority.  It
pages public Gamma events, preserves exact CTF identities, and emits only
machine-attested same-condition binary partitions plus conservative NegRisk
event-completeness attestations.

NegRisk complete sets are attested only for non-augmented winner-take-all
events whose entire Gamma event market list is simultaneously open, orderable,
binary CTF and internally consistent.  Augmented NegRisk is retained as
metadata but never auto-attested because placeholder/Other semantics can change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import urllib.parse
import urllib.request

SCHEMA = "polymarket_v7_exact_arb_exchange_universe_v1"
STATUS_SCHEMA = "polymarket_v7_exact_arb_exchange_universe_status_v1"
SAFETY = {
    "paper_only": True,
    "authenticated_execution": False,
    "real_order_submission": False,
    "real_capital_at_risk": False,
    "automatic_promotion": False,
}


def _array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _iso_unix(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def fetch_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-exact-arb-universe/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _condition_valid(condition_id: str) -> bool:
    if not (condition_id.startswith("0x") and len(condition_id) == 66):
        return False
    try:
        int(condition_id[2:], 16)
    except ValueError:
        return False
    return True


def _token_ids(raw: dict[str, Any]) -> list[str]:
    values = [str(value).strip() for value in _array(raw.get("clobTokenIds")) if str(value).strip()]
    try:
        if any(not (0 < int(token) < 2**256) for token in values):
            return []
    except ValueError:
        return []
    return values


def _outcomes(raw: dict[str, Any]) -> list[str]:
    return [str(value).strip() for value in _array(raw.get("outcomes"))]


def _binary_partition(raw: dict[str, Any]) -> tuple[bool, str, list[str], list[str]]:
    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
    tokens = _token_ids(raw)
    outcomes = _outcomes(raw)
    names = {value.upper() for value in outcomes}
    valid = (
        _condition_valid(condition_id)
        and len(tokens) == 2
        and len(set(tokens)) == 2
        and len(outcomes) == 2
        and names in ({"YES", "NO"}, {"UP", "DOWN"})
    )
    return valid, condition_id, tokens, outcomes


def _settlement_hash(condition_id: str) -> str:
    # Deliberately condition-specific.  This proves no cross-condition semantic
    # equivalence; it only gives the graph a stable same-condition identity.
    return hashlib.sha256(("CTF_CONDITION_V1:" + condition_id.lower()).encode()).hexdigest()


def _fee_schedule(raw: dict[str, Any]) -> dict[str, Any]:
    fee = raw.get("feeSchedule")
    if not isinstance(fee, dict):
        return {}
    result = {}
    for key in ("rate", "exponent", "rounding_mode", "rounding_increment"):
        if key in fee:
            result[key] = fee[key]
    if "rounding_mode" not in result:
        result["rounding_mode"] = "VENUE_5DP"
    if "rounding_increment" not in result:
        result["rounding_increment"] = "0.00001"
    return result


def _market_open(raw: dict[str, Any]) -> bool:
    return (
        raw.get("active") is True
        and raw.get("closed") is not True
        and raw.get("acceptingOrders") is True
        and raw.get("enableOrderBook") is not False
    )


def _event_negrisk(event: dict[str, Any]) -> bool:
    return event.get("negRisk") is True or event.get("enableNegRisk") is True


def _event_augmented(event: dict[str, Any]) -> bool:
    return event.get("negRiskAugmented") is True


def _negrisk_attestation(event: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[bool, list[str], str]:
    """Conservative event-completeness attestation.

    Polymarket documents non-augmented NegRisk events as winner-take-all
    universes supporting NO->all-other-YES conversion.  We require the complete
    event payload to be simultaneously tradable and canonical.  Augmented events
    are excluded because placeholder/Other membership may change.
    """
    if not _event_negrisk(event):
        return False, [], "NOT_NEGRISK"
    if _event_augmented(event):
        return False, [], "AUGMENTED_NEGRISK_UNSTABLE_MEMBERSHIP"
    if len(rows) < 2:
        return False, [], "INSUFFICIENT_MEMBERS"
    ids = []
    for raw in rows:
        valid, _, _, _ = _binary_partition(raw)
        market_id = str(raw.get("id") or "").strip()
        if not market_id or not valid:
            return False, [], "NONCANONICAL_MEMBER"
        if not _market_open(raw):
            return False, [], "MEMBER_NOT_SIMULTANEOUSLY_ORDERABLE"
        if raw.get("negRisk") is not True:
            return False, [], "MEMBER_NOT_NEGRISK"
        ids.append(market_id)
    if len(ids) != len(set(ids)):
        return False, [], "DUPLICATE_MEMBER"
    return True, sorted(ids), "GAMMA_NON_AUGMENTED_NEGRISK_COMPLETE_EVENT"


def normalize_market(
    raw: dict[str, Any],
    event: dict[str, Any],
    *,
    negrisk_verified: bool,
    negrisk_member_ids: list[str],
    negrisk_reason: str,
) -> dict[str, Any] | None:
    market_id = str(raw.get("id") or "").strip()
    if not market_id:
        return None
    binary, condition_id, token_ids, outcomes = _binary_partition(raw)
    event_id = str(event.get("id") or "").strip()
    fee = _fee_schedule(raw)
    fees_explicit = "feesEnabled" in raw
    event_neg = _event_negrisk(event)
    partition_inputs = {
        "condition_id": condition_id,
        "tokens": token_ids,
        "outcomes": outcomes,
        "source": "GAMMA_CANONICAL_CTF_MARKET",
    }
    event_proof = {
        "event_id": event_id,
        "market_ids": negrisk_member_ids,
        "negRisk": bool(event.get("negRisk", False)),
        "enableNegRisk": bool(event.get("enableNegRisk", False)),
        "negRiskAugmented": bool(event.get("negRiskAugmented", False)),
        "source": negrisk_reason,
    }
    return {
        "market_id": market_id,
        "event_id": event_id,
        "condition_id": condition_id,
        "question_id": str(raw.get("questionID") or ""),
        "question": str(raw.get("question") or ""),
        "slug": str(raw.get("slug") or ""),
        "event_slug": str(event.get("slug") or ""),
        "clob_token_ids": token_ids,
        "outcomes": outcomes,
        "active": bool(raw.get("active", False)),
        "closed": bool(raw.get("closed", False)),
        "accepting_orders": bool(raw.get("acceptingOrders", False)),
        "enable_order_book": bool(raw.get("enableOrderBook", True)),
        "binary_partition_verified": binary,
        "partition_provenance": partition_inputs if binary else None,
        "partition_proof_hash": hashlib.sha256(
            json.dumps(partition_inputs, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest() if binary else None,
        "settlement_semantic_hash": _settlement_hash(condition_id) if binary else "",
        "normalized_rules_hash": "",
        "contract_family": "CTF_BINARY_PUBLIC" if binary else "UNVERIFIED_PUBLIC",
        "asset": "",
        "horizon": "",
        "window_start_unix": _iso_unix(raw.get("startDate") or event.get("startDate")),
        "close_timestamp_unix": _iso_unix(raw.get("endDate") or event.get("endDate")),
        "tick_size": raw.get("orderPriceMinTickSize"),
        "minimum_order_size": raw.get("orderMinSize"),
        "fee_schedule": fee,
        "fees_enabled": bool(raw.get("feesEnabled", False)),
        "fees_enabled_explicit": fees_explicit,
        "liquidity": max(0.0, _finite(raw.get("liquidityNum"), _finite(raw.get("liquidity")))),
        "volume_24h": max(0.0, _finite(raw.get("volume24hr"), _finite(raw.get("volume24h")))),
        "neg_risk": bool(raw.get("negRisk", False) or event_neg),
        "neg_risk_augmented": _event_augmented(event),
        "neg_risk_other": bool(raw.get("negRiskOther", False)),
        "neg_risk_group_id": event_id if event_neg else "",
        "neg_risk_complete_set_verified": bool(negrisk_verified),
        "neg_risk_complete_set_market_ids": negrisk_member_ids if negrisk_verified else [],
        "neg_risk_complete_set_provenance": event_proof if negrisk_verified else {
            "event_id": event_id,
            "reason": negrisk_reason,
        },
        "neg_risk_complete_set_proof_hash": hashlib.sha256(
            json.dumps(event_proof, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest() if negrisk_verified else None,
        "combo_status": str(raw.get("comboStatus") or ""),
        "rfq_enabled": bool(raw.get("rfqEnabled", False)),
    }


def build_snapshot(events: list[dict[str, Any]], model_sha: str, *, generated_at_ms: int | None = None) -> dict[str, Any]:
    if len(model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in model_sha):
        raise ValueError("invalid model sha")
    markets: dict[str, dict[str, Any]] = {}
    event_count = 0
    negrisk_events = 0
    verified_negrisk_events = 0
    augmented_negrisk_events = 0
    rejected_event_reasons: dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("active") is not True or event.get("closed") is True:
            continue
        raw_markets = [row for row in (event.get("markets") or []) if isinstance(row, dict)]
        event_count += 1
        verified, members, reason = _negrisk_attestation(event, raw_markets)
        if _event_negrisk(event):
            negrisk_events += 1
            if _event_augmented(event):
                augmented_negrisk_events += 1
            if verified:
                verified_negrisk_events += 1
            else:
                rejected_event_reasons[reason] = rejected_event_reasons.get(reason, 0) + 1
        for raw in raw_markets:
            normalized = normalize_market(
                raw, event,
                negrisk_verified=verified,
                negrisk_member_ids=members,
                negrisk_reason=reason,
            )
            if normalized is not None and normalized["active"] and not normalized["closed"] and normalized["accepting_orders"]:
                markets[normalized["market_id"]] = normalized
    rows = sorted(markets.values(), key=lambda row: int(row["market_id"]) if row["market_id"].isdigit() else row["market_id"])
    membership = [
        {
            "market_id": row["market_id"],
            "condition_id": row["condition_id"],
            "tokens": row["clob_token_ids"],
            "event_id": row["event_id"],
            "neg_risk_members": row["neg_risk_complete_set_market_ids"],
        }
        for row in rows
    ]
    return {
        "schema": SCHEMA,
        "version": 1,
        **SAFETY,
        "execution_authority": False,
        "model_sha": model_sha,
        "timestamp_ms": int(time.time_ns() // 1_000_000 if generated_at_ms is None else generated_at_ms),
        "membership_sha256": hashlib.sha256(
            json.dumps(membership, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "source": "PUBLIC_GAMMA_EVENTS",
        "source_semantics": {
            "binary": "CANONICAL_SAME_CONDITION_CTF_TWO_OUTCOME_PARTITION",
            "negrisk": "NON_AUGMENTED_WINNER_TAKE_ALL_EVENT_ONLY",
            "augmented_negrisk": "NEVER_AUTO_ATTESTED",
            "cross_condition_equivalence": "NEVER_INFERRED_FROM_TEXT",
        },
        "events_total": event_count,
        "negrisk_events": negrisk_events,
        "verified_negrisk_events": verified_negrisk_events,
        "augmented_negrisk_events": augmented_negrisk_events,
        "negrisk_rejection_reasons": rejected_event_reasons,
        "markets": rows,
    }


def discover_events(
    base_url: str,
    timeout: float,
    page_size: int,
    max_pages: int,
    fetcher: Callable[[str, float], Any] = fetch_json,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    requests = 0
    started = time.monotonic_ns()
    exhaustive = False
    for page in range(max_pages):
        query = urllib.parse.urlencode({
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": page * page_size,
            "order": "id",
            "ascending": "true",
        })
        value = fetcher(base_url.rstrip("/") + "/events?" + query, timeout)
        requests += 1
        rows = value if isinstance(value, list) else value.get("events", []) if isinstance(value, dict) else []
        if not isinstance(rows, list):
            raise ValueError("invalid Gamma event response")
        for event in rows:
            if isinstance(event, dict) and str(event.get("id") or ""):
                events[str(event["id"])] = event
        if len(rows) < page_size:
            exhaustive = True
            break
    return list(events.values()), {
        "requests": requests,
        "events": len(events),
        "discovery_exhaustive": exhaustive,
        "pagination_loop_guard_hit": not exhaustive,
        "scan_duration_ms": (time.monotonic_ns() - started) / 1_000_000.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path)
    ap.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    ap.add_argument("--timeout-seconds", type=float, default=5.0)
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--interval-seconds", type=float, default=60.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("invalid model sha")
    if not (0.5 <= args.timeout_seconds <= 30 and 1 <= args.page_size <= 100
            and 1 <= args.max_pages <= 500 and 5 <= args.interval_seconds <= 3600):
        raise SystemExit("invalid bounds")
    while True:
        status = {
            "schema": STATUS_SCHEMA,
            **SAFETY,
            "execution_authority": False,
            "model_sha": args.model_sha,
            "state": "COLLECTING",
            "timestamp_ms": time.time_ns() // 1_000_000,
        }
        try:
            events, diagnostics = discover_events(
                args.gamma_url, args.timeout_seconds, args.page_size, args.max_pages
            )
            snapshot = build_snapshot(events, args.model_sha)
            _atomic(args.output, snapshot)
            status.update({
                "state": "READY" if diagnostics["discovery_exhaustive"] else "BOUNDED_PARTIAL",
                **diagnostics,
                "markets": len(snapshot["markets"]),
                "negrisk_events": snapshot["negrisk_events"],
                "verified_negrisk_events": snapshot["verified_negrisk_events"],
                "membership_sha256": snapshot["membership_sha256"],
            })
        except Exception as exc:
            status.update({"state": "SOURCE_ERROR", "error": type(exc).__name__})
        if args.status:
            _atomic(args.status, status)
        print(json.dumps(status, sort_keys=True), flush=True)
        if args.once:
            return 0 if status["state"] in {"READY", "BOUNDED_PARTIAL"} else 2
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
