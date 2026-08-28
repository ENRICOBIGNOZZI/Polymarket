#!/usr/bin/env python3
"""Verified, causal infrastructure around the V7 OSINT research kernel.

This module deliberately has no network client and no execution integration.
Callers supply bytes received from a configured adapter; the pipeline verifies
source identity, timestamps and hashes before they may enter an append-only
tape or a point-in-time dataset.  Model output remains forward-shadow only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from v7_osint_engine import (
    EVENT_FAMILIES,
    EventMarketLink,
    LikelihoodModel,
    OsintDecision,
    OsintError,
    RawEvent,
    SourceTier,
    update_probability,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha(value: object) -> str:
    data = value if isinstance(value, bytes) else _canonical(value)
    return hashlib.sha256(data).hexdigest()


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise OsintError("source_url_not_verified_https")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    return f"https://{parsed.hostname.lower()}{port}"


@dataclass(frozen=True)
class AuthoritativeSource:
    source_id: str
    authority: str
    tier: SourceTier
    allowed_origins: tuple[str, ...]
    event_families: tuple[str, ...]
    adapter_version: str
    enabled: bool = True

    def validate(self) -> None:
        if not all((self.source_id, self.authority, self.adapter_version)):
            raise OsintError("incomplete_source_registration")
        if self.tier not in (SourceTier.PRIMARY, SourceTier.VERIFIED_PROVIDER):
            raise OsintError("source_not_authoritative")
        if not self.allowed_origins or any(_origin(x) != x.rstrip("/").lower() for x in self.allowed_origins):
            raise OsintError("invalid_source_origin_allowlist")
        if not self.event_families or any(x not in EVENT_FAMILIES for x in self.event_families):
            raise OsintError("invalid_source_event_family")
        if not self.enabled:
            raise OsintError("source_disabled")


class SourceRegistry:
    """Immutable-by-construction allowlist for authoritative adapters."""

    def __init__(self, sources: Iterable[AuthoritativeSource]):
        rows = tuple(sources)
        if not rows:
            raise OsintError("empty_source_registry")
        by_id: dict[str, AuthoritativeSource] = {}
        for row in rows:
            row.validate()
            if row.source_id in by_id:
                raise OsintError("duplicate_source_id")
            by_id[row.source_id] = row
        self._sources = by_id
        self.registry_sha = _sha([asdict(x) for x in sorted(rows, key=lambda x: x.source_id)])

    def resolve(self, source_id: str, source_url: str, event_family: str) -> AuthoritativeSource:
        row = self._sources.get(source_id)
        if row is None:
            raise OsintError("source_not_registered")
        row.validate()
        if _origin(source_url) not in row.allowed_origins:
            raise OsintError("source_origin_not_allowed")
        if event_family not in row.event_families:
            raise OsintError("source_event_family_not_allowed")
        return row


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    source_url: str
    source_event_id: str
    root_lineage_id: str
    event_family: str
    entity: str
    published_ts_ms: int
    received_ts_ms: int
    payload: bytes
    advertised_payload_sha256: str = ""
    correction_of: str = ""
    extracted_by_llm: bool = False


def ingest_document(registry: SourceRegistry, document: SourceDocument) -> RawEvent:
    """Convert received authoritative bytes into a causal kernel event."""
    source = registry.resolve(document.source_id, document.source_url, document.event_family)
    if not document.payload:
        raise OsintError("empty_source_payload")
    payload_hash = _sha(document.payload)
    if document.advertised_payload_sha256 and document.advertised_payload_sha256.lower() != payload_hash:
        raise OsintError("source_payload_hash_mismatch")
    event_key = {
        "adapter_version": source.adapter_version,
        "payload_hash": payload_hash,
        "received_ts_ms": document.received_ts_ms,
        "source_event_id": document.source_event_id,
        "source_id": source.source_id,
    }
    event = RawEvent(
        event_id=f"osint-{_sha(event_key)[:24]}",
        event_family=document.event_family,
        entity=document.entity,
        source_id=source.source_id,
        source_tier=source.tier,
        source_event_id=document.source_event_id,
        root_lineage_id=document.root_lineage_id,
        published_ts_ms=document.published_ts_ms,
        received_ts_ms=document.received_ts_ms,
        payload_hash=payload_hash,
        correction_of=document.correction_of,
        extracted_by_llm=document.extracted_by_llm,
    )
    event.validate(document.received_ts_ms)
    return event


@dataclass(frozen=True)
class TapeRecord:
    sequence: int
    kind: str
    received_ts_ms: int
    payload: Mapping[str, object]
    previous_hash: str
    record_hash: str


class CausalEventTape:
    """Hash-chained JSONL tape which refuses corrupt or non-causal history."""

    GENESIS = "0" * 64

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @staticmethod
    def _body(sequence: int, kind: str, received_ts_ms: int,
              payload: Mapping[str, object], previous_hash: str) -> dict[str, object]:
        return {"kind": kind, "payload": dict(payload), "previous_hash": previous_hash,
                "received_ts_ms": received_ts_ms, "sequence": sequence}

    def read_verified(self) -> tuple[TapeRecord, ...]:
        if not self.path.exists():
            return ()
        records: list[TapeRecord] = []
        previous = self.GENESIS
        previous_ts = 0
        with self.path.open("rb") as handle:
            for expected, raw in enumerate(handle, start=1):
                if not raw.endswith(b"\n"):
                    raise OsintError("truncated_event_tape")
                try:
                    envelope = json.loads(raw)
                    body = self._body(int(envelope["sequence"]), str(envelope["kind"]),
                                      int(envelope["received_ts_ms"]), envelope["payload"],
                                      str(envelope["previous_hash"]))
                    record_hash = str(envelope["record_hash"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise OsintError("invalid_event_tape_record") from exc
                if body["sequence"] != expected or body["previous_hash"] != previous:
                    raise OsintError("broken_event_tape_chain")
                if body["received_ts_ms"] < previous_ts:
                    raise OsintError("noncausal_event_tape")
                if _sha(body) != record_hash:
                    raise OsintError("event_tape_hash_mismatch")
                records.append(TapeRecord(record_hash=record_hash, **body))
                previous, previous_ts = record_hash, int(body["received_ts_ms"])
        return tuple(records)

    def append(self, kind: str, received_ts_ms: int, payload: Mapping[str, object]) -> TapeRecord:
        if not kind or received_ts_ms <= 0:
            raise OsintError("invalid_event_tape_append")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # An exclusive advisory lock makes verify+append atomic between V7 writers.
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - non-POSIX development host
                pass
            records = self.read_verified()
            previous = records[-1].record_hash if records else self.GENESIS
            if records and received_ts_ms < records[-1].received_ts_ms:
                raise OsintError("noncausal_event_tape_append")
            body = self._body(len(records) + 1, kind, received_ts_ms, payload, previous)
            record = TapeRecord(record_hash=_sha(body), **body)
            wire = _canonical({**body, "record_hash": record.record_hash}) + b"\n"
            os.write(descriptor, wire)
            os.fsync(descriptor)
            return record
        finally:
            os.close(descriptor)

    def append_event(self, event: RawEvent, *, source_registry_sha: str) -> TapeRecord:
        event.validate(event.received_ts_ms)
        payload = asdict(event)
        payload["source_tier"] = int(event.source_tier)
        payload["source_registry_sha"] = source_registry_sha
        return self.append("RAW_EVENT", event.received_ts_ms, payload)

    def append_label(self, event: RawEvent, label: ResolvedLabel) -> TapeRecord:
        label.validate(event)
        if not any(x.kind == "RAW_EVENT" and x.payload.get("event_id") == event.event_id
                   for x in self.read_verified()):
            raise OsintError("label_event_not_on_tape")
        return self.append("RESOLVED_LABEL", label.received_ts_ms, asdict(label))


@dataclass(frozen=True)
class ResolvedLabel:
    label_id: str
    event_id: str
    market_id: str
    market_family: str
    outcome_yes: bool
    resolved_ts_ms: int
    received_ts_ms: int
    source_id: str
    source_event_id: str
    payload_hash: str
    verified: bool
    verification_method: str
    source_registry_sha: str = ""

    def validate(self, event: RawEvent) -> None:
        if not all((self.label_id, self.event_id, self.market_id, self.market_family,
                    self.source_id, self.source_event_id, self.payload_hash,
                    self.source_registry_sha)):
            raise OsintError("incomplete_resolved_label")
        if self.event_id != event.event_id or self.resolved_ts_ms < event.published_ts_ms:
            raise OsintError("label_event_mismatch")
        if self.received_ts_ms < self.resolved_ts_ms:
            raise OsintError("invalid_label_clocks")
        if not self.verified:
            raise OsintError("unverified_resolved_label")
        if self.verification_method.upper() == "LLM":
            raise OsintError("llm_cannot_verify_label")


@dataclass(frozen=True)
class ResolvedLabelDocument:
    market_id: str
    market_family: str
    outcome_yes: bool
    resolved_ts_ms: int
    received_ts_ms: int
    source_id: str
    source_url: str
    source_event_id: str
    payload: bytes
    advertised_payload_sha256: str = ""


def ingest_resolved_label(registry: SourceRegistry, event: RawEvent,
                          document: ResolvedLabelDocument) -> ResolvedLabel:
    """Create a label only from bytes admitted by an authoritative adapter."""
    source = registry.resolve(document.source_id, document.source_url, event.event_family)
    if not document.payload:
        raise OsintError("empty_label_payload")
    payload_hash = _sha(document.payload)
    if document.advertised_payload_sha256 and document.advertised_payload_sha256.lower() != payload_hash:
        raise OsintError("label_payload_hash_mismatch")
    identity = {"event_id": event.event_id, "market_id": document.market_id,
                "outcome_yes": document.outcome_yes, "payload_hash": payload_hash,
                "source_event_id": document.source_event_id}
    label = ResolvedLabel(
        f"label-{_sha(identity)[:24]}", event.event_id, document.market_id,
        document.market_family, document.outcome_yes, document.resolved_ts_ms,
        document.received_ts_ms, source.source_id, document.source_event_id, payload_hash,
        True, f"AUTHORITATIVE_ADAPTER:{source.adapter_version}", registry.registry_sha,
    )
    label.validate(event)
    return label


@dataclass(frozen=True)
class EventFamilyRow:
    event: RawEvent
    label: ResolvedLabel
    link: EventMarketLink

    @property
    def time_to_resolution_bucket(self) -> str:
        seconds = max(0, self.label.resolved_ts_ms - self.event.published_ts_ms) / 1_000
        if seconds <= 3600:
            return "LE_1H"
        if seconds <= 86400:
            return "LE_1D"
        if seconds <= 604800:
            return "LE_7D"
        return "GT_7D"

    def validate(self) -> None:
        self.event.validate(self.label.received_ts_ms)
        self.label.validate(self.event)
        self.link.validate()
        if not self.link.verified or self.link.event_family != self.event.event_family:
            raise OsintError("unverified_or_mismatched_dataset_link")


@dataclass(frozen=True)
class CorroborationResult:
    event_id: str
    independent_events: tuple[RawEvent, ...]
    independent_source_count: int
    rejected: tuple[tuple[str, str], ...]


def corroborate(event: RawEvent, candidates: Sequence[RawEvent], *,
                 decision_ts_ms: int) -> CorroborationResult:
    """Select causally available, independent corroboration without double counting."""
    event.validate(decision_ts_ms)
    accepted: list[RawEvent] = []
    rejected: list[tuple[str, str]] = []
    used_sources = {event.source_id}
    used_lineages = {event.root_lineage_id}
    for candidate in sorted(candidates, key=lambda x: (x.received_ts_ms, x.event_id)):
        try:
            candidate.validate(decision_ts_ms)
            if candidate.event_family != event.event_family or candidate.entity != event.entity:
                raise OsintError("corroboration_subject_mismatch")
            if candidate.source_tier == SourceTier.UNVERIFIED:
                raise OsintError("unverified_corroboration")
            if candidate.source_id in used_sources or candidate.root_lineage_id in used_lineages:
                raise OsintError("nonindependent_corroboration")
            if candidate.correction_of:
                raise OsintError("correction_is_not_corroboration")
        except OsintError as exc:
            rejected.append((candidate.event_id, str(exc)))
            continue
        accepted.append(candidate)
        used_sources.add(candidate.source_id)
        used_lineages.add(candidate.root_lineage_id)
    return CorroborationResult(event.event_id, tuple(accepted), len(used_sources) - 1,
                               tuple(rejected))


def update_verified_probability(
    *, market_id: str, prior: float, event: RawEvent, link: EventMarketLink,
    model: LikelihoodModel, decision_ts_ms: int, candidates: Sequence[RawEvent] = (),
    uncertainty_log_odds: float = 0.0, pm_bid: float | None = None,
    pm_ask: float | None = None, executable_cost: float = 0.0,
    minimum_edge: float = 0.0,
) -> tuple[OsintDecision, CorroborationResult]:
    result = corroborate(event, candidates, decision_ts_ms=decision_ts_ms)
    decision = update_probability(
        market_id=market_id, prior=prior, event=event, link=link, model=model,
        decision_ts_ms=decision_ts_ms, independent_events=result.independent_events,
        uncertainty_log_odds=uncertainty_log_odds, pm_bid=pm_bid, pm_ask=pm_ask,
        executable_cost=executable_cost, minimum_edge=minimum_edge,
    )
    return decision, result


@dataclass(frozen=True)
class DatasetManifest:
    dataset_id: str
    dataset_sha: str
    source_files: tuple[tuple[str, str], ...]
    row_count: int
    markets: tuple[str, ...]
    events: tuple[str, ...]
    start_ts_ms: int
    end_ts_ms: int
    receive_start_ts_ms: int
    receive_end_ts_ms: int
    data_sources: tuple[str, ...]
    missing_data: tuple[str, ...]
    known_gaps: tuple[str, ...]
    collector_sha: str
    point_in_time: bool


def build_dataset_manifest(
    dataset_id: str,
    rows: Sequence[EventFamilyRow],
    *,
    source_files: Mapping[str, bytes],
    collector_sha: str,
    missing_data: Sequence[str] = (),
    known_gaps: Sequence[str] = (),
) -> DatasetManifest:
    if not dataset_id or not collector_sha or not rows or not source_files:
        raise OsintError("incomplete_dataset_manifest")
    for row in rows:
        row.validate()
    ordered = tuple(sorted(rows, key=lambda x: (x.event.received_ts_ms, x.event.event_id)))
    files = tuple(sorted((name, _sha(content)) for name, content in source_files.items()))
    identity = [{
        "event_id": x.event.event_id,
        "event_payload_hash": x.event.payload_hash,
        "label_id": x.label.label_id,
        "label_payload_hash": x.label.payload_hash,
        "label_received_ts_ms": x.label.received_ts_ms,
        "market_family": x.link.market_family,
        "mapping_version": x.link.mapping_version,
        "outcome_yes": x.label.outcome_yes,
        "source_tier": int(x.event.source_tier),
        "time_to_resolution_bucket": x.time_to_resolution_bucket,
    } for x in ordered]
    dataset_sha = _sha({"collector_sha": collector_sha, "rows": identity, "source_files": files})
    return DatasetManifest(
        dataset_id, dataset_sha, files, len(ordered),
        tuple(sorted({x.label.market_id for x in ordered})),
        tuple(sorted({x.event.event_id for x in ordered})),
        min(x.event.published_ts_ms for x in ordered), max(x.label.resolved_ts_ms for x in ordered),
        min(x.event.received_ts_ms for x in ordered), max(x.label.received_ts_ms for x in ordered),
        tuple(sorted({x.event.source_id for x in ordered})), tuple(sorted(set(missing_data))),
        tuple(sorted(set(known_gaps))), collector_sha, True,
    )


def write_dataset_artifact(path: str | Path, manifest: DatasetManifest,
                           rows: Sequence[EventFamilyRow]) -> None:
    """Create an immutable canonical dataset artifact; never overwrite one."""
    if len(rows) != manifest.row_count:
        raise OsintError("dataset_manifest_row_count_mismatch")
    encoded_rows = []
    for row in sorted(rows, key=lambda x: (x.event.received_ts_ms, x.event.event_id)):
        row.validate()
        event = asdict(row.event)
        event["source_tier"] = int(row.event.source_tier)
        encoded_rows.append({"event": event, "label": asdict(row.label), "link": asdict(row.link),
                             "time_to_resolution_bucket": row.time_to_resolution_bucket})
    artifact = {"manifest": asdict(manifest), "rows": encoded_rows}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise OsintError("dataset_artifact_already_exists") from exc
    try:
        os.write(descriptor, _canonical(artifact) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class FittedLikelihood:
    model: LikelihoodModel
    model_id: str
    model_sha: str
    training_dataset_sha: str
    market_family: str
    source_tier: SourceTier
    time_to_resolution_bucket: str
    training_rows: int
    oos_rows: int
    oos_brier: float
    baseline_brier: float


def fit_chronological_oos(
    rows: Sequence[EventFamilyRow],
    manifest: DatasetManifest,
    *,
    training_end_ms: int,
    event_family: str,
    source_tier: SourceTier,
    market_family: str,
    time_to_resolution_bucket: str,
    minimum_training_rows: int = 20,
    minimum_oos_rows: int = 10,
    smoothing: float = 1.0,
    base_probability: float = .5,
) -> FittedLikelihood:
    """Fit a frozen stratum and validate only on later received labels."""
    if manifest.dataset_sha == "" or not manifest.point_in_time:
        raise OsintError("dataset_not_point_in_time")
    if smoothing <= 0 or training_end_ms <= 0 or not 0 < base_probability < 1:
        raise OsintError("invalid_fit_policy")
    selected = []
    for row in rows:
        row.validate()
        if (row.event.event_family == event_family and row.event.source_tier == source_tier
                and row.link.market_family == market_family
                and row.time_to_resolution_bucket == time_to_resolution_bucket):
            selected.append(row)
    training = [x for x in selected if x.label.received_ts_ms <= training_end_ms]
    oos = [x for x in selected if x.event.published_ts_ms > training_end_ms
           and x.event.received_ts_ms > training_end_ms]
    if len(training) < minimum_training_rows:
        raise OsintError("insufficient_training_rows")
    if len(oos) < minimum_oos_rows:
        raise OsintError("insufficient_chronological_oos_rows")
    yes = sum(x.label.outcome_yes for x in training)
    probability = (yes + smoothing) / (len(training) + 2.0 * smoothing)
    event_log_odds = math.log(probability / (1.0 - probability))
    base_log_odds = math.log(base_probability / (1.0 - base_probability))
    llr = event_log_odds - base_log_odds
    oos_brier = sum((probability - float(x.label.outcome_yes)) ** 2 for x in oos) / len(oos)
    baseline_brier = sum((base_probability - float(x.label.outcome_yes)) ** 2 for x in oos) / len(oos)
    # Chronological sample sufficiency establishes OOS mechanics; metrics remain
    # explicit and promotion is deliberately outside this research sleeve.
    model_identity = {
        "dataset_sha": manifest.dataset_sha, "event_family": event_family,
        "llr": llr, "market_family": market_family, "source_tier": int(source_tier),
        "time_bucket": time_to_resolution_bucket, "training_end_ms": training_end_ms,
        "base_probability": base_probability,
        "training_rows": len(training),
    }
    model_sha = _sha(model_identity)
    model = LikelihoodModel(event_family, llr, len({x.event.root_lineage_id for x in training}),
                            training_end_ms, True, True)
    return FittedLikelihood(model, f"osint-{model_sha[:20]}", model_sha, manifest.dataset_sha,
                            market_family, source_tier, time_to_resolution_bucket,
                            len(training), len(oos), oos_brier, baseline_brier)


def record_forward_shadow(
    tape: CausalEventTape,
    *,
    decision: OsintDecision,
    fitted: FittedLikelihood,
    decision_ts_ms: int,
    code_sha: str,
    config_sha: str,
) -> TapeRecord:
    if not decision.shadow_only or not fitted.model.frozen or not fitted.model.oos_validated:
        raise OsintError("forward_shadow_not_admissible")
    if not code_sha or not config_sha or decision_ts_ms <= fitted.model.trained_until_ms:
        raise OsintError("incomplete_forward_shadow_identity")
    payload = asdict(decision)
    payload.update({"code_sha": code_sha, "config_sha": config_sha,
                    "dataset_manifest_sha": fitted.training_dataset_sha,
                    "model_id": fitted.model_id, "model_sha": fitted.model_sha,
                    "paper_only": True, "execution_authority": False})
    return tape.append("FORWARD_SHADOW_DECISION", decision_ts_ms, payload)


def evaluate_forward_shadow(
    tape: CausalEventTape, label: ResolvedLabel, *, decision_record_hash: str
) -> TapeRecord:
    records = tape.read_verified()
    decision = next((x for x in records if x.record_hash == decision_record_hash
                     and x.kind == "FORWARD_SHADOW_DECISION"), None)
    if decision is None:
        raise OsintError("forward_shadow_decision_not_found")
    if label.event_id != decision.payload.get("event_id") or label.received_ts_ms <= decision.received_ts_ms:
        raise OsintError("noncausal_forward_shadow_label")
    if not label.verified or label.verification_method.upper() == "LLM":
        raise OsintError("forward_shadow_label_not_verified")
    return tape.append("FORWARD_SHADOW_LABEL", label.received_ts_ms,
                       {**asdict(label), "decision_record_hash": decision_record_hash})


__all__ = [
    "AuthoritativeSource", "CausalEventTape", "CorroborationResult", "DatasetManifest",
    "EventFamilyRow",
    "FittedLikelihood", "ResolvedLabel", "ResolvedLabelDocument", "SourceDocument",
    "SourceRegistry", "TapeRecord",
    "build_dataset_manifest", "corroborate", "evaluate_forward_shadow", "fit_chronological_oos",
    "ingest_document", "ingest_resolved_label", "record_forward_shadow",
    "update_verified_probability", "write_dataset_artifact",
]
