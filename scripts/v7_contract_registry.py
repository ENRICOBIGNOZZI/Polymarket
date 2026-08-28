#!/usr/bin/env python3
"""Fail-closed V7 contract/settlement registry for external fair value.

This is a slow-plane module. It may parse strings/JSON and persist approved
rule hashes, but it never authorizes an HFT decision by fuzzy text matching.
The C++ hot path consumes numeric handles projected from a fully verified spec.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

SCHEMA_VERSION = 1
PARSER_VERSION = "btc-updown-chainlink-twap-v1"
_CHAINLINK_TWAP_RE = re.compile(
    r"^/streams/(?P<asset>[a-z0-9]+)-(?P<quote>[a-z0-9]+)-twap-(?P<window>[1-9][0-9]*)s-streams/?$",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


class ContractVerificationError(ValueError):
    pass


def _norm(value: Any) -> str:
    return str(value or "").strip()


def normalize_rules(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    return _WS_RE.sub(" ", normalized).strip().lower()


def rules_hash(text: str) -> str:
    return hashlib.sha256(normalize_rules(text).encode("utf-8")).hexdigest()


def hash_handle(value: str) -> int:
    digest = hashlib.sha256(str(value).encode("utf-8")).digest()
    handle = int.from_bytes(digest[:8], "big", signed=False)
    return handle or 1


def _rules_text(raw: dict[str, Any]) -> str:
    for key in ("rules", "description", "resolutionRules", "resolution_rules"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
    event = raw.get("event")
    if isinstance(event, dict):
        for key in ("rules", "description"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _resolution_source(raw: dict[str, Any]) -> str:
    for key in ("resolutionSource", "resolution_source", "resolutionUrl", "resolution_url"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def parse_chainlink_twap_source(url: str) -> tuple[str, str, int, str]:
    parsed = urlparse(str(url).strip())
    host = parsed.netloc.lower()
    if host not in {"data.chain.link", "www.data.chain.link"}:
        raise ContractVerificationError("resolution_source:not_chainlink_data")
    match = _CHAINLINK_TWAP_RE.fullmatch(parsed.path)
    if match is None:
        raise ContractVerificationError("resolution_source:unknown_twap_stream")
    asset = match.group("asset").upper()
    quote = match.group("quote").upper()
    window = int(match.group("window"))
    canonical = f"https://data.chain.link/streams/{asset.lower()}-{quote.lower()}-twap-{window}s-streams"
    return asset, quote, window, canonical


def parse_comparator(normalized_rules: str) -> str:
    text = normalize_rules(normalized_rules)
    if "greater than or equal to" in text:
        return "GREATER_EQUAL"
    if "less than or equal to" in text:
        return "LESS_EQUAL"
    if "greater than" in text:
        return "GREATER"
    if "less than" in text:
        return "LESS"
    raise ContractVerificationError("rules:comparator_unknown")


def _btc_5m_semantics_verified(title: str, normalized: str, source: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    title_n = normalize_rules(title)
    if "btc" not in title_n and "bitcoin" not in title_n:
        reasons.append("title:not_btc")
    if "up or down" not in title_n:
        reasons.append("title:not_updown")
    if "twap" not in normalized:
        reasons.append("rules:twap_missing")
    if "chainlink" not in normalized:
        reasons.append("rules:chainlink_missing")
    if "beginning of that range" not in normalized and "beginning of the range" not in normalized:
        reasons.append("rules:opening_reference_missing")
    if "otherwise" not in normalized or "down" not in normalized:
        reasons.append("rules:down_complement_missing")
    if "spot market" not in normalized and "spot markets" not in normalized:
        reasons.append("rules:external_spot_distinction_missing")
    if "data.chain.link" not in source.lower():
        reasons.append("rules:resolution_source_missing")
    return not reasons, reasons


@dataclass(frozen=True)
class ContractSpec:
    schema_version: int
    parser_version: str
    market_handle: int
    event_handle: int
    yes_instrument_handle: int
    no_instrument_handle: int
    condition_id: str
    market_id: str
    event_id: str
    slug: str
    asset: str
    quote_currency: str
    contract_family: str
    start_timestamp: str
    end_timestamp: str
    settlement_provider: str
    settlement_oracle: str
    oracle_symbol: str
    oracle_feed_id: str
    oracle_window_seconds: int
    comparator: str
    threshold_semantics: str
    resolution_source: str
    normalized_rules: str
    normalized_rules_hash: str
    rules_hash_handle: int
    contract_version: int
    verified_template: bool
    rules_hash_recognized: bool
    reference_semantics_verified: bool
    settlement_source_verified: bool
    verification_reasons: tuple[str, ...]
    fee_schedule_version: str
    authoritative_fee_parameters: dict[str, Any]

    @property
    def informed_trading_authorized(self) -> bool:
        return bool(
            self.verified_template
            and self.rules_hash_recognized
            and self.reference_semantics_verified
            and self.settlement_source_verified
            and self.oracle_feed_id
            and self.oracle_window_seconds > 0
        )

    def hot_projection(self) -> dict[str, Any]:
        comparator_map = {
            "GREATER_EQUAL": 1,
            "GREATER": 2,
            "LESS_EQUAL": 3,
            "LESS": 4,
        }
        return {
            "market_handle": self.market_handle,
            "event_handle": self.event_handle,
            "yes_instrument_handle": self.yes_instrument_handle,
            "no_instrument_handle": self.no_instrument_handle,
            "condition_handle": hash_handle(self.condition_id),
            "rules_hash_handle": self.rules_hash_handle,
            "oracle_feed_handle": hash_handle(self.oracle_feed_id),
            "contract_version": self.contract_version,
            "oracle_window_seconds": self.oracle_window_seconds,
            "comparator": comparator_map[self.comparator],
            "verified_template": int(self.verified_template),
            "rules_hash_recognized": int(self.rules_hash_recognized),
            "reference_semantics_verified": int(self.reference_semantics_verified),
            "settlement_source_verified": int(self.settlement_source_verified),
        }


@dataclass(frozen=True)
class SettlementReferenceSnapshot:
    schema_version: int
    market_handle: int
    contract_version: int
    reference_version: int
    reference_value_exact: str
    reference_value_numeric: str
    reference_observation_timestamp_ns: int
    reference_receive_monotonic_ns: int
    oracle_feed_id: str
    oracle_window_seconds: int
    provenance: str
    provenance_hash: str
    source_state_version: int
    valid: bool

    def hot_projection(self) -> dict[str, Any]:
        return {
            "market_handle": self.market_handle,
            "contract_version": self.contract_version,
            "reference_version": self.reference_version,
            "exact_decimal_handle": hash_handle(self.reference_value_exact),
            "oracle_feed_handle": hash_handle(self.oracle_feed_id),
            "provenance_hash_handle": hash_handle(self.provenance_hash),
            "source_state_version": self.source_state_version,
            "reference_value_numeric": float(Decimal(self.reference_value_numeric)),
            "reference_observation_ns": self.reference_observation_timestamp_ns,
            "reference_receive_monotonic_ns": self.reference_receive_monotonic_ns,
            "oracle_window_seconds": self.oracle_window_seconds,
            "valid": int(self.valid),
        }


class ContractRegistry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._raw = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": SCHEMA_VERSION, "approved_rule_hashes": {}, "contracts": {}}
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise ContractVerificationError("registry:schema_mismatch")
        raw.setdefault("approved_rule_hashes", {})
        raw.setdefault("contracts", {})
        return raw

    @property
    def approved_rule_hashes(self) -> set[str]:
        hashes = self._raw.get("approved_rule_hashes")
        return set(hashes) if isinstance(hashes, dict) else set()

    def approve_rule_hash(self, spec: ContractSpec, *, evidence: str) -> None:
        if not spec.verified_template or not spec.settlement_source_verified:
            raise ContractVerificationError("registry:cannot_approve_unverified_template")
        evidence = str(evidence).strip()
        if not evidence:
            raise ContractVerificationError("registry:approval_evidence_missing")
        self._raw["approved_rule_hashes"][spec.normalized_rules_hash] = {
            "parser_version": spec.parser_version,
            "resolution_source": spec.resolution_source,
            "oracle_feed_id": spec.oracle_feed_id,
            "oracle_window_seconds": spec.oracle_window_seconds,
            "evidence": evidence,
            "approved_ts_ns": time.time_ns(),
        }

    def record_contract(self, spec: ContractSpec) -> None:
        key = spec.market_id or str(spec.market_handle)
        self._raw["contracts"][key] = asdict(spec)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self._raw, sort_keys=True, indent=2) + "\n"
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)


def contract_from_market(raw: dict[str, Any], *, approved_rule_hashes: Iterable[str] = ()) -> ContractSpec:
    if not isinstance(raw, dict):
        raise ContractVerificationError("market:not_object")
    title = _first(raw, "question", "title", "name")
    rules = _rules_text(raw)
    source = _resolution_source(raw)
    normalized = normalize_rules(rules)
    digest = rules_hash(rules)
    reasons: list[str] = []

    try:
        asset, quote, window, canonical_source = parse_chainlink_twap_source(source)
        settlement_source_verified = True
    except ContractVerificationError as exc:
        asset, quote, window, canonical_source = "", "", 0, source
        settlement_source_verified = False
        reasons.append(str(exc))

    try:
        comparator = parse_comparator(normalized)
    except ContractVerificationError as exc:
        comparator = "GREATER_EQUAL"
        reasons.append(str(exc))

    semantic_ok, semantic_reasons = _btc_5m_semantics_verified(title, normalized, source)
    reasons.extend(semantic_reasons)
    verified_template = bool(
        semantic_ok
        and settlement_source_verified
        and asset == "BTC"
        and quote == "USD"
        and window > 0
        and comparator in {"GREATER_EQUAL", "GREATER", "LESS_EQUAL", "LESS"}
    )

    market_id = _first(raw, "id", "market_id", "conditionId", "condition_id")
    event_id = _first(raw, "eventId", "event_id")
    event = raw.get("event")
    if not event_id and isinstance(event, dict):
        event_id = _first(event, "id", "event_id")
    condition_id = _first(raw, "conditionId", "condition_id")
    slug = _first(raw, "slug")
    market_handle = hash_handle(market_id or condition_id or slug or title)
    event_handle = hash_handle(event_id or slug or title)

    tokens = raw.get("tokens") or raw.get("clobTokenIds") or raw.get("clob_token_ids")
    yes_token = ""
    no_token = ""
    if isinstance(tokens, list):
        if tokens and isinstance(tokens[0], dict):
            for token in tokens:
                if not isinstance(token, dict):
                    continue
                outcome = normalize_rules(token.get("outcome"))
                token_id = _first(token, "token_id", "tokenId", "id")
                if outcome in {"yes", "up"}: yes_token = token_id
                elif outcome in {"no", "down"}: no_token = token_id
        elif len(tokens) >= 2:
            yes_token, no_token = str(tokens[0]), str(tokens[1])
    outcomes = raw.get("outcomes")
    if not yes_token or not no_token:
        # Handles remain deterministic but an unknown token mapping fails the
        # trading gate at integration time because the raw IDs are empty.
        yes_token = yes_token or f"unknown-yes:{market_id}"
        no_token = no_token or f"unknown-no:{market_id}"
        reasons.append("tokens:explicit_yes_no_mapping_missing")
        verified_template = False
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = None

    fee_schedule = raw.get("feeSchedule") if isinstance(raw.get("feeSchedule"), dict) else {}
    fee_version = hashlib.sha256(json.dumps(fee_schedule, sort_keys=True).encode()).hexdigest() if fee_schedule else "unknown"
    approved = digest in set(approved_rule_hashes)
    contract_version = hash_handle(f"{digest}:{canonical_source}:{PARSER_VERSION}")
    return ContractSpec(
        schema_version=SCHEMA_VERSION,
        parser_version=PARSER_VERSION,
        market_handle=market_handle,
        event_handle=event_handle,
        yes_instrument_handle=hash_handle(yes_token),
        no_instrument_handle=hash_handle(no_token),
        condition_id=condition_id,
        market_id=market_id,
        event_id=event_id,
        slug=slug,
        asset=asset,
        quote_currency=quote,
        contract_family="BTC_USD_UPDOWN_5M" if verified_template else "UNVERIFIED",
        start_timestamp=_first(raw, "startDate", "start_date", "startTime", "start_time"),
        end_timestamp=_first(raw, "endDate", "end_date", "endTime", "end_time"),
        settlement_provider="Chainlink" if settlement_source_verified else "unknown",
        settlement_oracle="Chainlink Data Streams TWAP" if settlement_source_verified else "unknown",
        oracle_symbol=f"{asset}/{quote}" if asset and quote else "",
        oracle_feed_id=canonical_source if settlement_source_verified else "",
        oracle_window_seconds=window,
        comparator=comparator,
        threshold_semantics="compare contract-window Chainlink TWAP against opening reference",
        resolution_source=canonical_source,
        normalized_rules=normalized,
        normalized_rules_hash=digest,
        rules_hash_handle=hash_handle(digest),
        contract_version=contract_version,
        verified_template=verified_template,
        rules_hash_recognized=approved,
        reference_semantics_verified=verified_template,
        settlement_source_verified=settlement_source_verified,
        verification_reasons=tuple(sorted(set(reasons))),
        fee_schedule_version=fee_version,
        authoritative_fee_parameters=fee_schedule,
    )


def make_settlement_reference(
    spec: ContractSpec,
    *,
    exact_value: str,
    observation_timestamp_ns: int,
    receive_monotonic_ns: int,
    source_state_version: int,
    reference_version: int,
    provenance: str,
) -> SettlementReferenceSnapshot:
    try:
        value = Decimal(str(exact_value))
    except (InvalidOperation, ValueError) as exc:
        raise ContractVerificationError("reference:invalid_decimal") from exc
    valid = bool(
        spec.verified_template
        and spec.rules_hash_recognized
        and spec.settlement_source_verified
        and value.is_finite()
        and value > 0
        and observation_timestamp_ns > 0
        and receive_monotonic_ns > 0
        and source_state_version > 0
        and reference_version > 0
        and provenance.strip()
    )
    provenance_hash = hashlib.sha256(
        f"{spec.market_id}|{spec.contract_version}|{exact_value}|{observation_timestamp_ns}|{provenance}".encode()
    ).hexdigest()
    return SettlementReferenceSnapshot(
        schema_version=SCHEMA_VERSION,
        market_handle=spec.market_handle,
        contract_version=spec.contract_version,
        reference_version=int(reference_version),
        reference_value_exact=str(exact_value),
        reference_value_numeric=format(value, "f"),
        reference_observation_timestamp_ns=int(observation_timestamp_ns),
        reference_receive_monotonic_ns=int(receive_monotonic_ns),
        oracle_feed_id=spec.oracle_feed_id,
        oracle_window_seconds=spec.oracle_window_seconds,
        provenance=str(provenance),
        provenance_hash=provenance_hash,
        source_state_version=int(source_state_version),
        valid=valid,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-json", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("runs/paper_v7_live/external_fair/contract_registry.json"))
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--approval-evidence", default="")
    args = parser.parse_args()

    raw = json.loads(args.market_json.read_text(encoding="utf-8"))
    registry = ContractRegistry(args.registry)
    spec = contract_from_market(raw, approved_rule_hashes=registry.approved_rule_hashes)
    if args.approve:
        registry.approve_rule_hash(spec, evidence=args.approval_evidence)
        spec = contract_from_market(raw, approved_rule_hashes=registry.approved_rule_hashes)
    registry.record_contract(spec)
    registry.save()
    print(json.dumps({**asdict(spec), "informed_trading_authorized": spec.informed_trading_authorized}, indent=2, sort_keys=True))
    return 0 if spec.verified_template else 2


if __name__ == "__main__":
    raise SystemExit(main())
