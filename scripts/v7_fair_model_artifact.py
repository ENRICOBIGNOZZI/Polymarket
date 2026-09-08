#!/usr/bin/env python3
"""Immutable research-model artifact for V7 fair-value inference.

No registry, deployment, model-version role, deployment or execution authority
lives here.  This module only defines canonical hashing and the frozen model
artifact consumed by PAPER research inference.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA_VERSION = 2
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ArtifactError(ValueError):
    pass


def _json_canonical(value: Any) -> Any:
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
    probability_interval_diagnostics: dict[str, Any]
    economic_replay: dict[str, Any]
    generated_timestamp_ns: int

    def hash_payload(self) -> dict[str, Any]:
        raw=asdict(self); raw.pop("model_hash",None)
        # Role was historically hash-neutral; retain that property so an old
        # statistically identical frozen artifact can be relabeled RESEARCH once.
        raw["artifact_role"]="HASH_NEUTRAL"
        return _json_canonical(raw)

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ArtifactError("schema_version:unsupported")
        if self.artifact_role != "RESEARCH":
            raise ArtifactError("artifact_role:research_required")
        if not self.family or not self.model_version or not self.feature_schema_version or not self.policy_version:
            raise ArtifactError("identity:missing")
        if not _SHA_RE.fullmatch(self.code_sha):
            raise ArtifactError("code_sha:not_exact")
        if not re.fullmatch(r"[0-9a-f]{64}", self.model_hash):
            raise ArtifactError("model_hash:invalid")
        if self.training_start_ns <= 0 or self.training_end_ns < self.training_start_ns:
            raise ArtifactError("training_window:invalid")
        if self.training_contracts <= 0 or self.training_days <= 0:
            raise ArtifactError("training_sample:invalid")
        if not self.assets or not self.contract_templates or not self.rules_hashes:
            raise ArtifactError("scope:missing")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.rules_hashes):
            raise ArtifactError("rules_hashes:invalid")
        if self.generated_timestamp_ns <= 0:
            raise ArtifactError("generated_timestamp:invalid")
        if canonical_hash(self.hash_payload()) != self.model_hash:
            raise ArtifactError("model_hash:mismatch")

    @classmethod
    def build(cls, **kwargs: Any) -> "FairModelArtifact":
        raw=dict(kwargs)
        raw.setdefault("schema_version",SCHEMA_VERSION)
        raw.setdefault("artifact_role","RESEARCH")
        raw.setdefault("generated_timestamp_ns",time.time_ns())
        raw.setdefault("model_hash","0"*64)
        temp=cls(**raw); raw["model_hash"]=canonical_hash(temp.hash_payload())
        artifact=cls(**raw); artifact.validate(); return artifact
