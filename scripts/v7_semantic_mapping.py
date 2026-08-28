#!/usr/bin/env python3
"""Shared fail-closed semantic mapping contract for V7 external inputs.

Text or embedding similarity may propose candidates, but only an explicitly
attested mapping whose exact semantic fields and source hashes still match may
be consumed by a research kernel.  This module has no execution capability.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


class SemanticMappingError(ValueError):
    pass


class MappingState(str, Enum):
    DISCOVERED = "DISCOVERED"
    CANDIDATE = "CANDIDATE"
    SEMANTICALLY_MATCHED = "SEMANTICALLY_MATCHED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class Relationship(str, Enum):
    EXACT_EQUIVALENT = "EXACT_EQUIVALENT"
    COMPLEMENT_EQUIVALENT = "COMPLEMENT_EQUIVALENT"
    CONDITIONAL_RELATION = "CONDITIONAL_RELATION"
    PARTIAL_OVERLAP = "PARTIAL_OVERLAP"
    NOT_EQUIVALENT = "NOT_EQUIVALENT"
    UNVERIFIED = "UNVERIFIED"


SEMANTIC_FIELDS = (
    "event_definition", "entities", "event_type", "jurisdiction", "geography",
    "outcomes", "direction", "numerical_threshold", "comparison_operator",
    "measurement_window", "deadline_ms", "timezone", "resolution_source",
    "resolution_rules", "cancellation_rules", "void_rules", "postponement_rules",
    "exception_clauses", "settlement_currency",
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def content_hash(value: Any) -> str:
    payload = value if isinstance(value, bytes) else canonical_json(value)
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ContractFingerprint:
    venue: str
    contract_id: str
    market_id: str
    event_definition: str
    entities: tuple[str, ...]
    event_type: str
    jurisdiction: str
    geography: str
    outcomes: tuple[str, ...]
    direction: str
    numerical_threshold: str
    comparison_operator: str
    measurement_window: str
    deadline_ms: int
    timezone: str
    resolution_source: str
    resolution_rules: str
    cancellation_rules: str
    void_rules: str
    postponement_rules: str
    exception_clauses: str
    settlement_currency: str
    source_uri: str
    observed_at_ms: int
    parser_version: str

    def validate(self) -> None:
        strings = (
            self.venue, self.contract_id, self.market_id, self.event_definition,
            self.event_type, self.direction, self.measurement_window, self.timezone,
            self.resolution_source, self.resolution_rules, self.cancellation_rules,
            self.void_rules, self.postponement_rules, self.settlement_currency,
            self.source_uri, self.parser_version,
        )
        if not all(strings) or not self.source_uri.startswith("https://"):
            raise SemanticMappingError("incomplete_contract_fingerprint")
        if not self.entities or not self.outcomes or self.deadline_ms <= 0 or self.observed_at_ms <= 0:
            raise SemanticMappingError("invalid_contract_fingerprint")
        if len(set(self.outcomes)) != len(self.outcomes):
            raise SemanticMappingError("duplicate_contract_outcomes")

    @property
    def fingerprint_hash(self) -> str:
        self.validate()
        return content_hash(asdict(self))

    def semantics(self, *, include_direction: bool = True) -> dict[str, Any]:
        value = {field: getattr(self, field) for field in SEMANTIC_FIELDS}
        if not include_direction:
            value.pop("direction")
        return value


@dataclass(frozen=True)
class FieldComparison:
    field: str
    left_hash: str
    right_hash: str
    equal: bool
    note: str = ""

    def validate(self) -> None:
        if self.field not in SEMANTIC_FIELDS or not self.left_hash or not self.right_hash:
            raise SemanticMappingError("invalid_field_comparison")


@dataclass(frozen=True)
class VerificationEvidence:
    verifier: str
    verification_method: str
    verified_at_ms: int
    evidence_uris: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    comparisons: tuple[FieldComparison, ...]
    repository_sha: str
    mapping_version: int

    def validate(self) -> None:
        if (
            not self.verifier or self.verification_method.upper() in {"LLM", "TITLE_SIMILARITY", "EMBEDDING"}
            or self.verified_at_ms <= 0 or self.mapping_version <= 0
            or len(self.repository_sha) != 40
            or not self.evidence_uris or len(self.evidence_uris) != len(self.evidence_hashes)
            or any(not uri.startswith("https://") for uri in self.evidence_uris)
            or not self.comparisons
        ):
            raise SemanticMappingError("incomplete_mapping_evidence")
        for comparison in self.comparisons:
            comparison.validate()
        if {row.field for row in self.comparisons} != set(SEMANTIC_FIELDS):
            raise SemanticMappingError("mapping_evidence_missing_semantic_fields")


def compare_fields(left: ContractFingerprint, right: ContractFingerprint) -> tuple[FieldComparison, ...]:
    left.validate(); right.validate()
    return tuple(FieldComparison(
        field, content_hash(getattr(left, field)), content_hash(getattr(right, field)),
        getattr(left, field) == getattr(right, field),
    ) for field in SEMANTIC_FIELDS)


def classify(left: ContractFingerprint, right: ContractFingerprint) -> Relationship:
    comparisons = compare_fields(left, right)
    mismatches = {row.field for row in comparisons if not row.equal}
    if not mismatches:
        return Relationship.EXACT_EQUIVALENT
    if mismatches == {"direction"} and {left.direction.upper(), right.direction.upper()} == {"YES", "NO"}:
        return Relationship.COMPLEMENT_EQUIVALENT
    critical = {
        "event_definition", "entities", "event_type", "numerical_threshold",
        "comparison_operator", "measurement_window", "deadline_ms", "timezone",
        "resolution_source", "resolution_rules", "cancellation_rules", "void_rules",
        "postponement_rules", "exception_clauses",
    }
    if mismatches & critical:
        return Relationship.NOT_EQUIVALENT
    return Relationship.PARTIAL_OVERLAP


@dataclass(frozen=True)
class VerifiedMapping:
    mapping_id: str
    family: str
    left: ContractFingerprint
    right: ContractFingerprint
    relationship: Relationship
    state: MappingState
    evidence: VerificationEvidence
    valid_from_ms: int
    expires_at_ms: int

    def validate(self, *, now_ms: int, repository_sha: str) -> None:
        if not self.mapping_id or self.family not in {"osint", "sports_latency", "cross_platform"}:
            raise SemanticMappingError("invalid_mapping_identity")
        self.left.validate(); self.right.validate(); self.evidence.validate()
        if self.state is not MappingState.VERIFIED:
            raise SemanticMappingError("mapping_not_verified")
        if self.evidence.repository_sha != repository_sha:
            raise SemanticMappingError("mapping_repository_sha_mismatch")
        if not self.valid_from_ms <= now_ms < self.expires_at_ms:
            raise SemanticMappingError("mapping_expired_or_not_yet_valid")
        actual = classify(self.left, self.right)
        if actual is not self.relationship:
            raise SemanticMappingError("mapping_relationship_mismatch")
        if self.relationship not in {Relationship.EXACT_EQUIVALENT, Relationship.COMPLEMENT_EQUIVALENT}:
            raise SemanticMappingError("mapping_relationship_not_actionable")
        expected = compare_fields(self.left, self.right)
        observed = {row.field: row for row in self.evidence.comparisons}
        if any(
            observed[row.field].left_hash != row.left_hash
            or observed[row.field].right_hash != row.right_hash
            or observed[row.field].equal != row.equal
            for row in expected
        ):
            raise SemanticMappingError("mapping_evidence_does_not_match_fingerprints")


def _fingerprint(raw: Mapping[str, Any]) -> ContractFingerprint:
    values = dict(raw)
    values["entities"] = tuple(str(x) for x in values.get("entities") or [])
    values["outcomes"] = tuple(str(x) for x in values.get("outcomes") or [])
    try:
        return ContractFingerprint(**values)
    except TypeError as exc:
        raise SemanticMappingError("invalid_mapping_fingerprint_schema") from exc


def load_verified_mappings(path: Path, family: str, *, now_ms: int,
                           repository_sha: str) -> tuple[VerifiedMapping, ...]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticMappingError("mapping_registry_unreadable") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "polymarket_v7_external_mapping_registry_v1"
        or value.get("version") != 7
        or value.get("paper_only") is not True
        or value.get("automatic_promotion") is not False
    ):
        raise SemanticMappingError("mapping_registry_identity_invalid")
    rows = value.get(family)
    if not isinstance(rows, list):
        raise SemanticMappingError("mapping_registry_family_missing")
    output: list[VerifiedMapping] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise SemanticMappingError("mapping_registry_row_not_object")
        evidence_raw = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
        comparisons = tuple(FieldComparison(**row) for row in evidence_raw.get("comparisons") or [])
        evidence = VerificationEvidence(
            verifier=str(evidence_raw.get("verifier") or ""),
            verification_method=str(evidence_raw.get("verification_method") or ""),
            verified_at_ms=int(evidence_raw.get("verified_at_ms") or 0),
            evidence_uris=tuple(str(x) for x in evidence_raw.get("evidence_uris") or []),
            evidence_hashes=tuple(str(x) for x in evidence_raw.get("evidence_hashes") or []),
            comparisons=comparisons,
            repository_sha=str(evidence_raw.get("repository_sha") or ""),
            mapping_version=int(evidence_raw.get("mapping_version") or 0),
        )
        try:
            mapping = VerifiedMapping(
                mapping_id=str(raw.get("mapping_id") or ""), family=family,
                left=_fingerprint(raw.get("left") or {}), right=_fingerprint(raw.get("right") or {}),
                relationship=Relationship(str(raw.get("relationship") or "UNVERIFIED")),
                state=MappingState(str(raw.get("state") or "DISCOVERED")), evidence=evidence,
                valid_from_ms=int(raw.get("valid_from_ms") or 0),
                expires_at_ms=int(raw.get("expires_at_ms") or 0),
            )
        except (TypeError, ValueError) as exc:
            raise SemanticMappingError("invalid_mapping_registry_row") from exc
        mapping.validate(now_ms=now_ms, repository_sha=repository_sha)
        output.append(mapping)
    if len({row.mapping_id for row in output}) != len(output):
        raise SemanticMappingError("duplicate_mapping_id")
    return tuple(output)


def candidate_score(left: ContractFingerprint, right: ContractFingerprint) -> float:
    """Discovery score only. It can never verify or authorize a mapping."""
    left.validate(); right.validate()
    weights = {
        "entities": 3.0, "event_type": 3.0, "jurisdiction": 1.0,
        "geography": 1.0, "deadline_ms": 2.0, "timezone": 1.0,
        "numerical_threshold": 2.0, "comparison_operator": 2.0,
        "resolution_source": 3.0,
    }
    denominator = sum(weights.values())
    return sum(weight for field, weight in weights.items()
               if getattr(left, field) == getattr(right, field)) / denominator


__all__ = [
    "ContractFingerprint", "FieldComparison", "MappingState", "Relationship",
    "SEMANTIC_FIELDS", "SemanticMappingError", "VerificationEvidence", "VerifiedMapping",
    "candidate_score", "canonical_json", "classify", "compare_fields", "content_hash",
    "load_verified_mappings",
]
