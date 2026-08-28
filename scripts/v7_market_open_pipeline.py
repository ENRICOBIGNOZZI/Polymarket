#!/usr/bin/env python3
"""Causal FULL-PAPER infrastructure around the V7 market-open kernel."""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from v7_market_open import (
    ColdStartContract,
    FairEstimate,
    FairSource,
    MarketOpenError,
    OpenDecision,
    decide_open,
    edge_decay,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha(value: object) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else _canonical(value)).hexdigest()


def _verified_origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise MarketOpenError("unverified_market_source_url")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    return f"https://{parsed.hostname.lower()}{port}"


@dataclass(frozen=True)
class MarketSourceAdapter:
    source_id: str
    allowed_origins: tuple[str, ...]
    adapter_version: str
    authoritative: bool

    def validate(self) -> None:
        if not self.source_id or not self.adapter_version or not self.authoritative:
            raise MarketOpenError("market_source_not_authoritative")
        if not self.allowed_origins or any(_verified_origin(x) != x.rstrip("/").lower()
                                           for x in self.allowed_origins):
            raise MarketOpenError("invalid_market_source_allowlist")

    def verify_url(self, url: str) -> None:
        self.validate()
        if _verified_origin(url) not in self.allowed_origins:
            raise MarketOpenError("market_source_origin_not_allowed")


@dataclass(frozen=True)
class MarketStreamEvent:
    source_id: str
    source_url: str
    source_event_id: str
    market_id: str
    event_id: str
    event_type: str
    published_ts_ms: int
    received_ts_ms: int
    payload_hash: str
    advertised_open_ts_ms: int | None = None

    def validate(self, adapter: MarketSourceAdapter) -> None:
        adapter.verify_url(self.source_url)
        if adapter.source_id != self.source_id or not all((self.source_event_id, self.market_id,
                                                           self.event_id, self.payload_hash)):
            raise MarketOpenError("invalid_market_stream_identity")
        if self.event_type not in {"CREATED", "ACTIVATED", "BOOK_OPEN"}:
            raise MarketOpenError("unsupported_market_stream_event")
        if self.published_ts_ms <= 0 or self.received_ts_ms < self.published_ts_ms:
            raise MarketOpenError("invalid_market_stream_clocks")
        if self.advertised_open_ts_ms is not None and not (
                self.published_ts_ms <= self.advertised_open_ts_ms <= self.received_ts_ms):
            raise MarketOpenError("noncausal_advertised_open")


def ingest_market_stream_event(
    adapter: MarketSourceAdapter, *, source_url: str, source_event_id: str,
    market_id: str, event_id: str, event_type: str, published_ts_ms: int,
    received_ts_ms: int, payload: bytes, advertised_payload_sha256: str = "",
    advertised_open_ts_ms: int | None = None,
) -> MarketStreamEvent:
    adapter.verify_url(source_url)
    if not payload:
        raise MarketOpenError("empty_market_stream_payload")
    payload_hash = _sha(payload)
    if advertised_payload_sha256 and advertised_payload_sha256.lower() != payload_hash:
        raise MarketOpenError("market_stream_payload_hash_mismatch")
    row = MarketStreamEvent(adapter.source_id, source_url, source_event_id, market_id, event_id,
                            event_type, published_ts_ms, received_ts_ms, payload_hash,
                            advertised_open_ts_ms)
    row.validate(adapter)
    return row


@dataclass(frozen=True)
class MarketOpenRecord:
    market_id: str
    event_id: str
    detected_ts_ms: int
    open_ts_ms: int
    source_id: str
    source_event_id: str
    payload_hash: str
    adapter_version: str


class NewMarketDetector:
    """Stateful first-seen detector; duplicates and time travel fail closed."""

    def __init__(self, adapters: Iterable[MarketSourceAdapter]):
        rows = tuple(adapters)
        if not rows:
            raise MarketOpenError("empty_market_source_registry")
        self._adapters = {}
        for row in rows:
            row.validate()
            if row.source_id in self._adapters:
                raise MarketOpenError("duplicate_market_source")
            self._adapters[row.source_id] = row
        self._seen: set[str] = set()
        self._last_receive_ts_ms = 0

    def observe(self, event: MarketStreamEvent) -> MarketOpenRecord | None:
        adapter = self._adapters.get(event.source_id)
        if adapter is None:
            raise MarketOpenError("market_source_not_registered")
        event.validate(adapter)
        if event.received_ts_ms < self._last_receive_ts_ms:
            raise MarketOpenError("noncausal_market_stream")
        self._last_receive_ts_ms = event.received_ts_ms
        if event.market_id in self._seen:
            return None
        self._seen.add(event.market_id)
        open_ts = event.advertised_open_ts_ms or event.received_ts_ms
        return MarketOpenRecord(event.market_id, event.event_id, event.received_ts_ms, open_ts,
                                event.source_id, event.source_event_id, event.payload_hash,
                                adapter.adapter_version)


@dataclass(frozen=True)
class StructuredSemantics:
    market_id: str
    event_id: str
    rules_text: str
    settlement_source: str
    comparator: str
    cutoff_ms: int
    timezone: str
    source_id: str
    source_url: str
    source_record_id: str
    received_ts_ms: int
    verified: bool
    verification_method: str


def parse_verified_semantics(row: StructuredSemantics, *, decision_ts_ms: int,
                             adapter: MarketSourceAdapter) -> ColdStartContract:
    """Parse structured authoritative fields only; never infer free-form meaning."""
    if not all((row.market_id, row.event_id, row.rules_text, row.settlement_source,
                row.comparator, row.timezone, row.source_id, row.source_url,
                row.source_record_id)):
        raise MarketOpenError("unknown_or_incomplete_semantics")
    if row.source_id != adapter.source_id:
        raise MarketOpenError("semantic_source_adapter_mismatch")
    adapter.verify_url(row.source_url)
    if row.comparator not in {">", ">=", "<", "<=", "==", "IN", "MATCH_WINNER"}:
        raise MarketOpenError("unsupported_semantic_comparator")
    if row.cutoff_ms <= 0 or row.received_ts_ms <= 0 or row.received_ts_ms > decision_ts_ms:
        raise MarketOpenError("noncausal_semantics")
    if not row.verified:
        raise MarketOpenError("unverified_semantics")
    if row.verification_method.upper() == "LLM":
        raise MarketOpenError("llm_cannot_verify_semantics")
    contract = ColdStartContract(row.market_id, row.event_id, _sha(row.rules_text.encode()),
                                 row.settlement_source, row.comparator, row.cutoff_ms,
                                 row.timezone, True)
    contract.validate()
    return contract


@dataclass(frozen=True)
class FairLineage:
    source_id: str
    source_record_id: str
    source_payload_hash: str
    mapping_sha: str
    dataset_manifest_sha: str
    received_ts_ms: int
    verified: bool
    verification_method: str

    def validate(self, decision_ts_ms: int) -> None:
        if not all((self.source_id, self.source_record_id, self.source_payload_hash,
                    self.mapping_sha, self.dataset_manifest_sha)):
            raise MarketOpenError("incomplete_fair_source_lineage")
        if self.received_ts_ms <= 0 or self.received_ts_ms > decision_ts_ms:
            raise MarketOpenError("noncausal_fair_source_lineage")
        if not self.verified or self.verification_method.upper() == "LLM":
            raise MarketOpenError("unverified_fair_source_lineage")


@dataclass(frozen=True)
class LineagedFair:
    estimate: FairEstimate
    lineage: FairLineage

    def validate(self, decision_ts_ms: int) -> None:
        self.estimate.validate(decision_ts_ms)
        self.lineage.validate(decision_ts_ms)
        if self.lineage.received_ts_ms > self.estimate.causal_ts_ms:
            raise MarketOpenError("fair_precedes_its_lineage")


@dataclass(frozen=True)
class RelatedMarket:
    market_id: str
    event_id: str
    relation: str
    probability: float
    uncertainty: float
    mature_since_ms: int
    received_ts_ms: int
    mapping_sha: str
    verified: bool


def related_market_fair(
    contract: ColdStartContract, candidates: Sequence[RelatedMarket], *, decision_ts_ms: int,
    minimum_maturity_ms: int = 3_600_000,
) -> LineagedFair:
    valid = [x for x in candidates if x.verified and x.event_id == contract.event_id
             and x.relation in {"EXACT", "COMPLEMENT"}
             and 0 < x.probability < 1 and 0 <= x.uncertainty <= .5
             and 0 < x.mature_since_ms <= x.received_ts_ms <= decision_ts_ms
             and decision_ts_ms - x.mature_since_ms >= minimum_maturity_ms and x.mapping_sha]
    if not valid:
        raise MarketOpenError("no_verified_related_mature_market")
    row = min(valid, key=lambda x: (x.uncertainty, x.market_id))
    probability = row.probability if row.relation == "EXACT" else 1.0 - row.probability
    payload_hash = _sha(asdict(row))
    estimate = FairEstimate(FairSource.RELATED_MATURE_MARKETS, probability, row.uncertainty,
                            f"related:{row.mapping_sha[:16]}", row.received_ts_ms, True)
    lineage = FairLineage("RELATED_MARKET", row.market_id, payload_hash, row.mapping_sha,
                          payload_hash, row.received_ts_ms, True, "EXACT_MAPPING")
    return LineagedFair(estimate, lineage)


def decide_verified_open(
    contract: ColdStartContract, estimates: Sequence[LineagedFair], *, decision_ts_ms: int,
    open_ts_ms: int, pm_bid: float, pm_ask: float, executable_cost: float,
    minimum_edge: float, base_size_multiplier: float = .25,
) -> OpenDecision:
    try:
        for row in estimates:
            row.validate(decision_ts_ms)
    except MarketOpenError as exc:
        return OpenDecision(contract.market_id, "NOTHING", None, None, None, 0.0, "", True, str(exc))
    return decide_open(contract, [x.estimate for x in estimates], decision_ts_ms=decision_ts_ms,
                       open_ts_ms=open_ts_ms, pm_bid=pm_bid, pm_ask=pm_ask,
                       executable_cost=executable_cost, minimum_edge=minimum_edge,
                       base_size_multiplier=base_size_multiplier)


@dataclass(frozen=True)
class InitialBookSnapshot:
    market_id: str
    sequence: int
    exchange_ts_ms: int
    received_ts_ms: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    def validate(self, open_ts_ms: int) -> None:
        if not self.market_id or self.sequence < 0 or self.exchange_ts_ms <= 0:
            raise MarketOpenError("invalid_initial_book_identity")
        if self.received_ts_ms < max(open_ts_ms, self.exchange_ts_ms):
            raise MarketOpenError("noncausal_initial_book")
        if not 0 <= self.bid <= self.ask <= 1 or self.bid_size < 0 or self.ask_size < 0:
            raise MarketOpenError("invalid_initial_book")


@dataclass(frozen=True)
class OpenRaceObservation:
    market_id: str
    open_ts_ms: int
    first_quote_received_ts_ms: int
    decision_ts_ms: int
    intent_ready_ts_ms: int
    paper_ack_ts_ms: int
    first_bid: float
    first_ask: float
    maker_count: int
    fill_model_version: str
    paper_only: bool = True

    def validate(self) -> None:
        clocks = (self.open_ts_ms, self.first_quote_received_ts_ms, self.decision_ts_ms,
                  self.intent_ready_ts_ms, self.paper_ack_ts_ms)
        if not self.market_id or not self.fill_model_version or any(x <= 0 for x in clocks):
            raise MarketOpenError("incomplete_open_race_observation")
        if tuple(sorted(clocks)) != clocks or not 0 <= self.first_bid <= self.first_ask <= 1:
            raise MarketOpenError("invalid_open_race_observation")
        if self.maker_count < 0 or not self.paper_only:
            raise MarketOpenError("market_open_must_remain_full_paper")

    def metrics(self) -> dict[str, float | int]:
        self.validate()
        return {
            "detection_latency_ms": self.first_quote_received_ts_ms - self.open_ts_ms,
            "decision_latency_ms": self.decision_ts_ms - self.first_quote_received_ts_ms,
            "intent_latency_ms": self.intent_ready_ts_ms - self.decision_ts_ms,
            "paper_ack_latency_ms": self.paper_ack_ts_ms - self.intent_ready_ts_ms,
            "first_spread": self.first_ask - self.first_bid,
            "maker_count": self.maker_count,
        }


class ForwardOpenTape:
    """Append-only hash chain for opens, books, decisions and PAPER race labels."""

    GENESIS = "0" * 64

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read_verified(self) -> tuple[dict[str, object], ...]:
        if not self.path.exists():
            return ()
        result = []
        previous, previous_ts = self.GENESIS, 0
        with self.path.open("rb") as handle:
            for sequence, raw in enumerate(handle, 1):
                if not raw.endswith(b"\n"):
                    raise MarketOpenError("truncated_open_tape")
                try:
                    row = json.loads(raw)
                    record_hash = row.pop("record_hash")
                except (json.JSONDecodeError, KeyError) as exc:
                    raise MarketOpenError("invalid_open_tape_record") from exc
                if (row.get("sequence") != sequence or row.get("previous_hash") != previous
                        or not isinstance(row.get("received_ts_ms"), int)
                        or row["received_ts_ms"] < previous_ts or _sha(row) != record_hash):
                    raise MarketOpenError("broken_open_tape_chain")
                row["record_hash"] = record_hash
                result.append(row)
                previous, previous_ts = record_hash, row["received_ts_ms"]
        return tuple(result)

    def append(self, kind: str, received_ts_ms: int, payload: Mapping[str, object]) -> str:
        if not kind or received_ts_ms <= 0:
            raise MarketOpenError("invalid_open_tape_append")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover
                pass
            rows = self.read_verified()
            if rows and received_ts_ms < rows[-1]["received_ts_ms"]:
                raise MarketOpenError("noncausal_open_tape_append")
            body = {"kind": kind, "payload": dict(payload),
                    "previous_hash": rows[-1]["record_hash"] if rows else self.GENESIS,
                    "received_ts_ms": received_ts_ms, "sequence": len(rows) + 1}
            record_hash = _sha(body)
            os.write(descriptor, _canonical({**body, "record_hash": record_hash}) + b"\n")
            os.fsync(descriptor)
            return record_hash
        finally:
            os.close(descriptor)

    def append_open(self, row: MarketOpenRecord) -> str:
        return self.append("MARKET_OPEN", row.detected_ts_ms, asdict(row))

    def append_book(self, row: InitialBookSnapshot, *, open_ts_ms: int) -> str:
        row.validate(open_ts_ms)
        existing = [x for x in self.read_verified() if x["kind"] == "INITIAL_BOOK"
                    and x["payload"].get("market_id") == row.market_id]
        if existing and row.sequence <= int(existing[-1]["payload"]["sequence"]):
            raise MarketOpenError("nonmonotonic_initial_book_sequence")
        return self.append("INITIAL_BOOK", row.received_ts_ms, asdict(row))

    def append_decision(self, decision: OpenDecision, *, decision_ts_ms: int,
                        code_sha: str, config_sha: str, dataset_manifest_sha: str,
                        fair_lineages: Sequence[FairLineage]) -> str:
        if not decision.shadow_only or not all((code_sha, config_sha, dataset_manifest_sha)):
            raise MarketOpenError("invalid_forward_open_decision")
        if not fair_lineages:
            raise MarketOpenError("missing_forward_open_fair_lineage")
        for lineage in fair_lineages:
            lineage.validate(decision_ts_ms)
        opens = [x for x in self.read_verified() if x["kind"] == "MARKET_OPEN"
                 and x["payload"].get("market_id") == decision.market_id]
        if not opens or decision_ts_ms < int(opens[0]["payload"]["open_ts_ms"]):
            raise MarketOpenError("forward_open_without_causal_open")
        return self.append("FORWARD_OPEN_DECISION", decision_ts_ms,
                           {**asdict(decision), "code_sha": code_sha, "config_sha": config_sha,
                            "dataset_manifest_sha": dataset_manifest_sha, "paper_only": True,
                            "execution_authority": False,
                            "fair_lineage_sha": _sha([asdict(x) for x in fair_lineages])})


@dataclass(frozen=True)
class OpenDatasetManifest:
    dataset_id: str
    dataset_sha: str
    source_tape_sha: str
    row_count: int
    markets: tuple[str, ...]
    events: tuple[str, ...]
    start_ts_ms: int
    end_ts_ms: int
    receive_timestamp_coverage: bool
    data_sources: tuple[str, ...]
    missing_data: tuple[str, ...]
    known_gaps: tuple[str, ...]
    collector_sha: str
    point_in_time: bool


def build_open_dataset_manifest(
    dataset_id: str, tape: ForwardOpenTape, *, collector_sha: str,
    missing_data: Sequence[str] = (), known_gaps: Sequence[str] = (),
) -> OpenDatasetManifest:
    rows = tape.read_verified()
    if not dataset_id or not collector_sha or not rows:
        raise MarketOpenError("incomplete_open_dataset_manifest")
    markets = sorted({str(x["payload"].get("market_id")) for x in rows
                      if x["payload"].get("market_id")})
    events = sorted({str(x["payload"].get("event_id")) for x in rows
                     if x["payload"].get("event_id")})
    sources = sorted({str(x["payload"].get("source_id")) for x in rows
                      if x["payload"].get("source_id")})
    tape_sha = _sha([x["record_hash"] for x in rows])
    identity = {"collector_sha": collector_sha, "dataset_id": dataset_id,
                "source_tape_sha": tape_sha}
    return OpenDatasetManifest(dataset_id, _sha(identity), tape_sha, len(rows), tuple(markets),
                               tuple(events), int(rows[0]["received_ts_ms"]),
                               int(rows[-1]["received_ts_ms"]), True, tuple(sources),
                               tuple(sorted(set(missing_data))), tuple(sorted(set(known_gaps))),
                               collector_sha, True)


def write_open_dataset_manifest(path: str | Path, manifest: OpenDatasetManifest) -> None:
    """Persist once; changing evidence produces a new dataset identity and file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise MarketOpenError("open_dataset_manifest_already_exists") from exc
    try:
        os.write(descriptor, _canonical(asdict(manifest)) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def edge_decay_summary(open_ts_ms: int, observations: Mapping[int, float], *,
                       efficient_edge: float = .005) -> dict[str, object]:
    rows = edge_decay(open_ts_ms, observations)
    if not rows or efficient_edge < 0:
        raise MarketOpenError("insufficient_edge_decay_observations")
    initial = abs(rows[0][1])
    half_life = next((seconds for seconds, edge in rows if abs(edge) <= initial / 2), None)
    efficient = next((seconds for seconds, edge in rows if abs(edge) <= efficient_edge), None)
    return {"observations": rows, "initial_absolute_edge": initial,
            "edge_half_life_seconds": half_life, "time_to_efficient_price_seconds": efficient}


__all__ = [
    "FairLineage", "ForwardOpenTape", "InitialBookSnapshot", "LineagedFair",
    "MarketOpenRecord", "MarketSourceAdapter", "MarketStreamEvent", "NewMarketDetector",
    "OpenDatasetManifest", "OpenRaceObservation", "RelatedMarket", "StructuredSemantics",
    "build_open_dataset_manifest", "decide_verified_open", "edge_decay_summary",
    "ingest_market_stream_event", "parse_verified_semantics", "related_market_fair",
    "write_open_dataset_manifest",
]
