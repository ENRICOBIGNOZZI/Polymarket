#!/usr/bin/env python3
"""Immutable decision-to-settlement evidence tape for real PnL.

The tape keeps hashes of decision inputs and signed-order payloads only; it
never accepts private keys or raw signatures.  CLOB/Data evidence remains in
the separate read-only evidence tape and is referenced by its record hash.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
RECORD_KIND = "EXECUTION_PROVENANCE"
GENESIS_HASH = "0" * 64
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STAGES = ("DECISION", "SIGNED_ORDER", "CLOB_ACCEPTED", "FILL", "SETTLEMENT")
STAGE_INDEX = {stage: index for index, stage in enumerate(STAGES)}
CLOB_STAGES = {"CLOB_ACCEPTED", "FILL"}
SETTLEMENT_STAGES = {"SETTLEMENT"}


class ProvenanceError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvenanceError(f"{name}:missing")
    return value.strip()


def _timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProvenanceError("event_ts_ms:invalid")
    return value


def _next_stage_is_valid(previous_stage: str | None, next_stage: str) -> bool:
    """Permit one or more partial fills after CLOB acceptance."""
    if previous_stage is None:
        return next_stage == "DECISION"
    if next_stage == "FILL":
        return previous_stage in {"CLOB_ACCEPTED", "FILL"}
    return STAGE_INDEX[next_stage] == STAGE_INDEX[previous_stage] + 1


@dataclass(frozen=True)
class ProvenanceRecord:
    model_sha: str
    lineage_id: str
    stage: str
    event_ts_ms: int
    payload: dict[str, str]
    evidence_record_hash: str | None = None
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = SCHEMA_VERSION
    previous_stage_hash: str = GENESIS_HASH
    previous_record_hash: str = GENESIS_HASH
    record_hash: str | None = None

    def _hash_payload(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("record_hash", None)
        value["record_kind"] = RECORD_KIND
        return value

    def computed_hash(self) -> str:
        return sha256(self._hash_payload())

    def validate(self, *, sealed: bool = True) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ProvenanceError("schema_version:unsupported")
        if not isinstance(self.model_sha, str) or not SHA_RE.fullmatch(self.model_sha):
            raise ProvenanceError("model_sha:not_exact_git_sha")
        _text("lineage_id", self.lineage_id)
        _text("record_id", self.record_id)
        _timestamp(self.event_ts_ms)
        if self.stage not in STAGE_INDEX:
            raise ProvenanceError("stage:unsupported")
        if not isinstance(self.payload, dict) or any(
                not isinstance(key, str) or not isinstance(value, str) or not SHA256_RE.fullmatch(value)
                for key, value in self.payload.items()):
            raise ProvenanceError("payload:must_be_sha256_map")
        required_payload = {
            "DECISION": {"decision_hash"},
            "SIGNED_ORDER": {"order_payload_hash", "signature_digest"},
            "CLOB_ACCEPTED": {"acceptance_payload_hash"},
            "FILL": {"fill_payload_hash"},
            "SETTLEMENT": {"settlement_payload_hash"},
        }[self.stage]
        if set(self.payload) != required_payload:
            raise ProvenanceError("payload:stage_shape")
        if not isinstance(self.previous_stage_hash, str) or not SHA256_RE.fullmatch(self.previous_stage_hash):
            raise ProvenanceError("previous_stage_hash:invalid")
        if not isinstance(self.previous_record_hash, str) or not SHA256_RE.fullmatch(self.previous_record_hash):
            raise ProvenanceError("previous_record_hash:invalid")
        evidence_required = self.stage in CLOB_STAGES or self.stage in SETTLEMENT_STAGES
        if evidence_required != (self.evidence_record_hash is not None):
            raise ProvenanceError("evidence_record_hash:stage_requirement")
        if self.evidence_record_hash is not None and (
                not isinstance(self.evidence_record_hash, str) or not SHA256_RE.fullmatch(self.evidence_record_hash)):
            raise ProvenanceError("evidence_record_hash:invalid")
        if sealed:
            if not isinstance(self.record_hash, str) or not SHA256_RE.fullmatch(self.record_hash):
                raise ProvenanceError("record_hash:invalid")
            if self.record_hash != self.computed_hash():
                raise ProvenanceError("record_hash:mismatch")
        elif self.record_hash is not None:
            raise ProvenanceError("unsealed_record_has_hash")

    def seal(self, *, previous_stage_hash: str, previous_record_hash: str) -> "ProvenanceRecord":
        candidate = replace(self, previous_stage_hash=previous_stage_hash,
                            previous_record_hash=previous_record_hash, record_hash=None)
        candidate.validate(sealed=False)
        sealed = replace(candidate, record_hash=candidate.computed_hash())
        sealed.validate()
        return sealed

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"record_kind": RECORD_KIND, **asdict(self)}

    @classmethod
    def from_dict(cls, raw: Any) -> "ProvenanceRecord":
        if not isinstance(raw, dict) or raw.get("record_kind") != RECORD_KIND:
            raise ProvenanceError("record_kind:invalid")
        value = dict(raw)
        value.pop("record_kind", None)
        try:
            record = cls(**value)
        except TypeError as exc:
            raise ProvenanceError(f"record:shape:{exc}") from exc
        record.validate()
        return record


def provenance_path(run_root: Path) -> Path:
    return Path(run_root) / "evidence" / "execution_provenance.jsonl"


def iter_records(path: Path) -> Iterator[ProvenanceRecord]:
    global_tips: dict[str, str] = {}
    stage_tips: dict[tuple[str, str], tuple[str, str]] = {}
    record_ids: set[str] = set()
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = ProvenanceRecord.from_dict(json.loads(line))
            except (json.JSONDecodeError, ProvenanceError) as exc:
                raise ProvenanceError(f"line_{line_number}:{exc}") from exc
            if record.record_id in record_ids:
                raise ProvenanceError(f"line_{line_number}:duplicate_record_id")
            record_ids.add(record.record_id)
            if record.previous_record_hash != global_tips.get(record.model_sha, GENESIS_HASH):
                raise ProvenanceError(f"line_{line_number}:global_chain_break")
            key = (record.model_sha, record.lineage_id)
            prior = stage_tips.get(key)
            if prior is None:
                if not _next_stage_is_valid(None, record.stage) or record.previous_stage_hash != GENESIS_HASH:
                    raise ProvenanceError(f"line_{line_number}:lineage_must_start_with_decision")
            elif (not _next_stage_is_valid(prior[0], record.stage)
                  or record.previous_stage_hash != prior[1]):
                raise ProvenanceError(f"line_{line_number}:lineage_stage_break")
            global_tips[record.model_sha] = str(record.record_hash)
            stage_tips[key] = (record.stage, str(record.record_hash))
            yield record


class ProvenanceTapeWriter:
    def __init__(self, path: Path, *, writer_id: str, model_sha: str):
        self.path = Path(path)
        self.writer_id = _text("writer_id", writer_id)
        if not SHA_RE.fullmatch(model_sha):
            raise ProvenanceError("model_sha:not_exact_git_sha")
        self.model_sha = model_sha
        self.owner_path = self.path.with_suffix(self.path.suffix + ".writer.json")
        self.token = uuid.uuid4().hex
        self._owned = False
        self._global_tip: str | None = None
        self._stage_tips: dict[str, tuple[str, str]] = {}

    def acquire(self) -> None:
        if self._owned:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.owner_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ProvenanceError("provenance_already_owned") from exc
        try:
            os.write(fd, canonical_bytes({"writer_id": self.writer_id, "model_sha": self.model_sha,
                                           "token": self.token}) + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        self._owned = True

    def _load_tips(self) -> None:
        if self._global_tip is not None:
            return
        self._global_tip = GENESIS_HASH
        if self.path.exists():
            for record in iter_records(self.path):
                if record.model_sha == self.model_sha:
                    self._global_tip = str(record.record_hash)
                    self._stage_tips[record.lineage_id] = (record.stage, str(record.record_hash))

    def append(self, record: ProvenanceRecord) -> ProvenanceRecord:
        if not self._owned:
            raise ProvenanceError("writer:not_acquired")
        if record.model_sha != self.model_sha:
            raise ProvenanceError("writer:mixed_model_sha")
        self._load_tips()
        prior = self._stage_tips.get(record.lineage_id)
        if prior is None:
            if not _next_stage_is_valid(None, record.stage):
                raise ProvenanceError("writer:lineage_must_start_with_decision")
            previous_stage_hash = GENESIS_HASH
        else:
            if not _next_stage_is_valid(prior[0], record.stage):
                raise ProvenanceError("writer:lineage_stage_break")
            previous_stage_hash = prior[1]
        sealed = record.seal(previous_stage_hash=previous_stage_hash,
                             previous_record_hash=str(self._global_tip))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sealed.to_dict(), sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._global_tip = str(sealed.record_hash)
        self._stage_tips[sealed.lineage_id] = (sealed.stage, str(sealed.record_hash))
        return sealed

    def close(self) -> None:
        if not self._owned:
            return
        try:
            owner = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProvenanceError("owner:unreadable") from exc
        if owner.get("token") != self.token:
            raise ProvenanceError("owner:mismatch")
        self.owner_path.unlink()
        self._owned = False

    def __enter__(self) -> "ProvenanceTapeWriter":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def manifest(path: Path, *, model_sha: str) -> dict[str, Any]:
    records = [record for record in iter_records(path) if record.model_sha == model_sha]
    terminal = {record.lineage_id: record for record in records}
    return {
        "schema": "polymarket_v7_execution_provenance_manifest_v1",
        "model_sha": model_sha,
        "records": len(records),
        "head_hash": records[-1].record_hash if records else None,
        "complete_lineages": sorted(lineage_id for lineage_id, record in terminal.items()
                                     if record.stage == "SETTLEMENT"),
    }


if __name__ == "__main__":
    raise SystemExit("library_only: runtime adapters write provenance without keys or order submission")
