#!/usr/bin/env python3
"""Immutable champion/challenger registry for V7 settlement fair value."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ROLES = frozenset({"RESEARCH", "CHALLENGER", "CHAMPION", "REJECTED"})


class RegistryError(ValueError):
    pass


def _json_canonical(value: Any) -> Any:
    """Round-trip through JSON so tuples/lists have one storage representation."""
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))


def canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(_json_canonical(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class FairModelArtifact:
    schema_version: int
    family: str
    model_version: str
    model_hash: str
    feature_schema_version: str
    code_sha: str
    policy_version: str
    artifact_role: str
    training_start_ns: int
    training_end_ns: int
    training_contracts: int
    training_days: int
    assets: tuple[str, ...]
    contract_templates: tuple[str, ...]
    rules_hashes: tuple[str, ...]
    parameters: dict[str, Any]
    hyperparameters: dict[str, Any]
    oos_scores: dict[str, Any]
    interval_coverage: dict[str, Any]
    economic_replay: dict[str, Any]
    generated_timestamp_ns: int

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RegistryError("schema_version:unsupported")
        if self.artifact_role not in ROLES:
            raise RegistryError("artifact_role:unsupported")
        if not self.family or not self.model_version or not self.feature_schema_version or not self.policy_version:
            raise RegistryError("identity:missing")
        if not _SHA_RE.fullmatch(self.code_sha):
            raise RegistryError("code_sha:not_exact")
        if not re.fullmatch(r"[0-9a-f]{64}", self.model_hash):
            raise RegistryError("model_hash:invalid")
        if self.training_start_ns <= 0 or self.training_end_ns < self.training_start_ns:
            raise RegistryError("training_window:invalid")
        if self.training_contracts <= 0 or self.training_days <= 0:
            raise RegistryError("training_sample:invalid")
        if not self.assets or not self.contract_templates or not self.rules_hashes:
            raise RegistryError("scope:missing")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.rules_hashes):
            raise RegistryError("rules_hashes:invalid")
        if self.generated_timestamp_ns <= 0:
            raise RegistryError("generated_timestamp:invalid")
        expected = canonical_hash(self.hash_payload())
        if expected != self.model_hash:
            raise RegistryError("model_hash:mismatch")

    def hash_payload(self) -> dict[str, Any]:
        raw = asdict(self)
        raw.pop("model_hash", None)
        # Governance role is not model content. Promotion changes the immutable
        # pointer/role without pretending the statistical artifact changed.
        raw["artifact_role"] = "HASH_NEUTRAL"
        return _json_canonical(raw)

    def with_role(self, role: str) -> "FairModelArtifact":
        if role not in ROLES:
            raise RegistryError("artifact_role:unsupported")
        raw = asdict(self)
        raw["artifact_role"] = role
        artifact = FairModelArtifact(**raw)
        artifact.validate()
        return artifact

    @classmethod
    def build(cls, **kwargs: Any) -> "FairModelArtifact":
        raw = dict(kwargs)
        raw.setdefault("schema_version", SCHEMA_VERSION)
        raw.setdefault("artifact_role", "CHALLENGER")
        raw.setdefault("generated_timestamp_ns", time.time_ns())
        raw.setdefault("model_hash", "0" * 64)
        temp = cls(**raw)
        raw["model_hash"] = canonical_hash(temp.hash_payload())
        artifact = cls(**raw)
        artifact.validate()
        return artifact


@dataclass(frozen=True)
class PromotionPolicy:
    minimum_oos_contracts: int = 100
    minimum_forward_shadow_contracts: int = 50
    maximum_ece: float = 0.05
    minimum_calibration_slope: float = 0.75
    maximum_calibration_slope: float = 1.25
    minimum_interval_coverage: float = 0.85
    maximum_interval_coverage: float = 0.99
    require_positive_net_replay_pnl: bool = True
    require_edge_monotonicity: bool = True
    require_no_causality_failures: bool = True


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    canonical = _json_canonical(payload)
    tmp.write_text(json.dumps(canonical, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class FairValueRegistry:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.models = self.root / "models"
        self.research_log = self.root / "research_registry.jsonl"
        self.challenger_pointer = self.root / "fair_value_challenger.json"
        self.champion_pointer = self.root / "fair_value_champion.json"

    def _artifact_path(self, artifact: FairModelArtifact) -> Path:
        return self.models / f"{artifact.model_hash}.json"

    def store(self, artifact: FairModelArtifact, *, experiment_status: str = "completed") -> Path:
        artifact.validate()
        path = self._artifact_path(artifact)
        payload = _json_canonical(asdict(artifact))
        if path.exists():
            existing = _json_canonical(json.loads(path.read_text(encoding="utf-8")))
            existing["artifact_role"] = "HASH_NEUTRAL"
            candidate = dict(payload)
            candidate["artifact_role"] = "HASH_NEUTRAL"
            if existing != candidate:
                raise RegistryError("artifact_hash_collision_or_mutation")
        else:
            _atomic_json(path, payload)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.research_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "timestamp_ns": time.time_ns(),
                "model_hash": artifact.model_hash,
                "model_version": artifact.model_version,
                "family": artifact.family,
                "status": str(experiment_status),
                "role": artifact.artifact_role,
            }, sort_keys=True) + "\n")
        return path

    def publish_challenger(self, artifact: FairModelArtifact) -> Path:
        challenger = artifact.with_role("CHALLENGER")
        self.store(challenger)
        pointer = {
            "schema_version": SCHEMA_VERSION,
            "role": "CHALLENGER",
            "model_hash": challenger.model_hash,
            "model_version": challenger.model_version,
            "artifact": str(self._artifact_path(challenger)),
            "published_timestamp_ns": time.time_ns(),
        }
        _atomic_json(self.challenger_pointer, pointer)
        return self.challenger_pointer

    def record_rejected(self, artifact: FairModelArtifact, *, reason: str) -> None:
        if not str(reason).strip():
            raise RegistryError("rejection_reason:missing")
        rejected = artifact.with_role("REJECTED")
        self.store(rejected, experiment_status=f"rejected:{reason}")

    def promote(self, artifact: FairModelArtifact, *, evidence: dict[str, Any],
                policy: PromotionPolicy = PromotionPolicy()) -> Path:
        artifact.validate()
        self._validate_promotion_evidence(artifact, evidence, policy)
        champion = artifact.with_role("CHAMPION")
        self.store(champion, experiment_status="promoted")
        pointer = {
            "schema_version": SCHEMA_VERSION,
            "role": "CHAMPION",
            "model_hash": champion.model_hash,
            "model_version": champion.model_version,
            "artifact": str(self._artifact_path(champion)),
            "promotion_evidence_hash": canonical_hash(evidence),
            "promoted_timestamp_ns": time.time_ns(),
        }
        _atomic_json(self.champion_pointer, pointer)
        return self.champion_pointer

    @staticmethod
    def _validate_promotion_evidence(artifact: FairModelArtifact, evidence: dict[str, Any],
                                     policy: PromotionPolicy) -> None:
        if not isinstance(evidence, dict):
            raise RegistryError("promotion:evidence_missing")
        oos_contracts = int(evidence.get("oos_contracts", 0))
        shadow_contracts = int(evidence.get("forward_shadow_contracts", 0))
        if oos_contracts < policy.minimum_oos_contracts:
            raise RegistryError("promotion:insufficient_oos_contracts")
        if shadow_contracts < policy.minimum_forward_shadow_contracts:
            raise RegistryError("promotion:insufficient_forward_shadow")

        ece = float(evidence.get("ece", math.inf))
        slope = float(evidence.get("calibration_slope", math.nan))
        coverage = float(evidence.get("interval_coverage", math.nan))
        if not math.isfinite(ece) or ece > policy.maximum_ece:
            raise RegistryError("promotion:ece")
        if not math.isfinite(slope) or not policy.minimum_calibration_slope <= slope <= policy.maximum_calibration_slope:
            raise RegistryError("promotion:calibration_slope")
        if not math.isfinite(coverage) or not policy.minimum_interval_coverage <= coverage <= policy.maximum_interval_coverage:
            raise RegistryError("promotion:interval_coverage")
        if policy.require_positive_net_replay_pnl and float(evidence.get("net_replay_pnl", 0.0)) <= 0.0:
            raise RegistryError("promotion:nonpositive_net_replay_pnl")
        if policy.require_edge_monotonicity and evidence.get("edge_monotonicity_pass") is not True:
            raise RegistryError("promotion:edge_monotonicity")
        if policy.require_no_causality_failures and int(evidence.get("causality_failures", 1)) != 0:
            raise RegistryError("promotion:causality_failure")
        if evidence.get("forward_shadow_frozen") is not True:
            raise RegistryError("promotion:shadow_not_frozen")
        if evidence.get("exact_code_sha") != artifact.code_sha:
            raise RegistryError("promotion:sha_mismatch")
        if evidence.get("rules_hashes") != list(artifact.rules_hashes):
            raise RegistryError("promotion:rules_scope_mismatch")
