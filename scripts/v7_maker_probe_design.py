#!/usr/bin/env python3
"""Pre-registered randomized maker-probe evidence, with zero execution authority.

The module assigns candidate observations to fixed experimental arms before an
outcome is known, records immutable assignment/outcome rows, and estimates the
two distinct maker probabilities required for calibration:

P(aggressive flow reaches an active quote | X)
P(fill | flow reached, queue and quote survived, X)

It has no exchange client, signer, order constructor, or network calls.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
import argparse
from dataclasses import dataclass, asdict, field, replace
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "polymarket_v7_maker_probe_v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
GENESIS_HASH = "0" * 64
REQUIRED_CONTEXT = ("queue_bucket", "spread_bucket", "tte_bucket", "volatility_bucket", "activity_bucket", "quote_lifetime_bucket")
MODES = {"PAPER", "LIVE_OBSERVED"}


class ProbeError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProbeError(f"{name}:missing")
    return value.strip()


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProbeError(f"{name}:invalid")
    return value


def _context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(REQUIRED_CONTEXT):
        raise ProbeError("context:required_strata_missing")
    out = {name: _text(f"context:{name}", value[name]) for name in REQUIRED_CONTEXT}
    return out


def _wilson_lower(successes: int, trials: int, z: float = 1.96) -> float | None:
    if trials <= 0:
        return None
    p = max(0.0, min(1.0, successes / trials))
    denom = 1.0 + z * z / trials
    centre = p + z * z / (2.0 * trials)
    radius = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * trials)) / trials)
    return max(0.0, min(1.0, (centre - radius) / denom))


@dataclass(frozen=True)
class ProbeAssignment:
    experiment_id: str
    model_sha: str
    candidate_id: str
    assigned_ts_ms: int
    context: dict[str, str]
    arms: tuple[str, ...]
    randomization_seed_commitment: str
    minimum_size_base_units: int
    assignment_id: str = ""
    assigned_arm: str = ""
    propensity: float = 0.0
    record_kind: str = "PROBE_ASSIGNMENT"
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    previous_record_hash: str = GENESIS_HASH
    record_hash: str | None = None

    def _without_hash(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("record_hash", None)
        return value

    def validate(self, *, sealed: bool = True) -> None:
        if self.record_kind != "PROBE_ASSIGNMENT":
            raise ProbeError("assignment:record_kind")
        _text("experiment_id", self.experiment_id)
        _text("candidate_id", self.candidate_id)
        _positive_int("assigned_ts_ms", self.assigned_ts_ms)
        if not SHA_RE.fullmatch(self.model_sha):
            raise ProbeError("model_sha:not_exact_git_sha")
        _context(self.context)
        if not isinstance(self.arms, tuple) or len(self.arms) < 2 or tuple(sorted(set(self.arms))) != self.arms:
            raise ProbeError("arms:must_be_sorted_unique_and_multiple")
        if not HASH_RE.fullmatch(self.randomization_seed_commitment):
            raise ProbeError("randomization_seed_commitment:invalid")
        _positive_int("minimum_size_base_units", self.minimum_size_base_units)
        expected_id = digest({"experiment_id": self.experiment_id, "model_sha": self.model_sha,
                              "candidate_id": self.candidate_id, "context": self.context,
                              "randomization_seed_commitment": self.randomization_seed_commitment})
        if self.assignment_id != expected_id:
            raise ProbeError("assignment_id:mismatch")
        if self.assigned_arm not in self.arms:
            raise ProbeError("assigned_arm:invalid")
        if not math.isfinite(self.propensity) or abs(self.propensity - 1.0 / len(self.arms)) > 1e-12:
            raise ProbeError("propensity:invalid")
        if not _text("record_id", self.record_id) or not HASH_RE.fullmatch(self.previous_record_hash):
            raise ProbeError("assignment:chain_identity")
        if sealed:
            if not isinstance(self.record_hash, str) or self.record_hash != digest(self._without_hash()):
                raise ProbeError("assignment:record_hash")
        elif self.record_hash is not None:
            raise ProbeError("assignment:unsealed_hash")

    def seal(self, previous_hash: str) -> "ProbeAssignment":
        candidate = replace(self, previous_record_hash=previous_hash, record_hash=None)
        candidate.validate(sealed=False)
        sealed = replace(candidate, record_hash=digest(candidate._without_hash()))
        sealed.validate()
        return sealed


def assign(candidate: dict[str, Any], *, experiment_id: str, model_sha: str,
           arms: Iterable[str], randomization_secret: str,
           minimum_size_base_units: int) -> ProbeAssignment:
    """Assign before the outcome, using a committed deterministic random seed."""
    if not SHA_RE.fullmatch(model_sha):
        raise ProbeError("model_sha:not_exact_git_sha")
    normalized_arms = tuple(sorted({_text("arm", arm) for arm in arms}))
    if len(normalized_arms) < 2:
        raise ProbeError("arms:at_least_two_required")
    if not randomization_secret:
        raise ProbeError("randomization_secret:missing")
    candidate_id = _text("candidate_id", candidate.get("candidate_id"))
    context = _context(candidate.get("context"))
    ts = _positive_int("received_ts_ms", candidate.get("received_ts_ms"))
    commitment = hashlib.sha256(randomization_secret.encode("utf-8")).hexdigest()
    assignment_id = digest({"experiment_id": experiment_id, "model_sha": model_sha,
                            "candidate_id": candidate_id, "context": context,
                            "randomization_seed_commitment": commitment})
    draw = int(hashlib.sha256(f"{randomization_secret}|{assignment_id}".encode("utf-8")).hexdigest(), 16)
    assignment = ProbeAssignment(
        experiment_id=experiment_id, model_sha=model_sha, candidate_id=candidate_id,
        assigned_ts_ms=ts, context=context, arms=normalized_arms,
        randomization_seed_commitment=commitment,
        minimum_size_base_units=_positive_int("minimum_size_base_units", minimum_size_base_units),
        assignment_id=assignment_id, assigned_arm=normalized_arms[draw % len(normalized_arms)],
        propensity=1.0 / len(normalized_arms),
    )
    assignment.validate(sealed=False)
    return assignment


def outcome(assignment: ProbeAssignment, *, mode: str, terminal_ts_ms: int,
            flow_reached: bool, filled_base_units: int, cancelled: bool,
            evidence_record_hash: str | None = None) -> dict[str, Any]:
    """Create an outcome row; it records facts and cannot route an order."""
    assignment.validate()
    if mode not in MODES or not isinstance(flow_reached, bool) or not isinstance(cancelled, bool):
        raise ProbeError("outcome:mode_or_boolean")
    if isinstance(filled_base_units, bool) or not isinstance(filled_base_units, int) or filled_base_units < 0:
        raise ProbeError("filled_base_units:invalid")
    filled = filled_base_units
    if filled > assignment.minimum_size_base_units:
        raise ProbeError("outcome:filled_exceeds_requested")
    if not flow_reached and filled:
        raise ProbeError("outcome:fill_without_flow")
    if _positive_int("terminal_ts_ms", terminal_ts_ms) < assignment.assigned_ts_ms:
        raise ProbeError("outcome:terminal_before_assignment")
    if mode == "LIVE_OBSERVED" and (not isinstance(evidence_record_hash, str) or not HASH_RE.fullmatch(evidence_record_hash)):
        raise ProbeError("outcome:live_requires_evidence_hash")
    value = {
        "record_kind": "PROBE_OUTCOME", "schema": SCHEMA,
        "assignment_id": assignment.assignment_id, "model_sha": assignment.model_sha,
        "experiment_id": assignment.experiment_id, "assigned_arm": assignment.assigned_arm,
        "context": assignment.context, "minimum_size_base_units": assignment.minimum_size_base_units,
        "mode": mode, "terminal_ts_ms": terminal_ts_ms, "flow_reached": flow_reached,
        "filled_base_units": filled, "cancelled": cancelled,
        "evidence_record_hash": evidence_record_hash,
    }
    value["outcome_id"] = digest(value)
    return value


def calibrate(outcomes: Iterable[dict[str, Any]], *, minimum_terminal_per_cell: int = 20) -> dict[str, Any]:
    """Return conservative funnel calibration; PAPER never earns live credit."""
    if minimum_terminal_per_cell <= 0:
        raise ProbeError("minimum_terminal_per_cell:invalid")
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    invalid = 0
    modes: set[str] = set()
    for row in outcomes:
        try:
            if not isinstance(row, dict) or row.get("record_kind") != "PROBE_OUTCOME" or row.get("schema") != SCHEMA:
                raise ProbeError("outcome:shape")
            context = _context(row.get("context"))
            if row.get("mode") not in MODES or not isinstance(row.get("flow_reached"), bool):
                raise ProbeError("outcome:invalid")
            requested = _positive_int("minimum_size_base_units", row.get("minimum_size_base_units"))
            filled = row.get("filled_base_units")
            if isinstance(filled, bool) or not isinstance(filled, int) or not 0 <= filled <= requested:
                raise ProbeError("outcome:filled")
            if not row["flow_reached"] and filled:
                raise ProbeError("outcome:fill_without_flow")
            if row["mode"] == "LIVE_OBSERVED" and not HASH_RE.fullmatch(str(row.get("evidence_record_hash") or "")):
                raise ProbeError("outcome:live_requires_evidence_hash")
        except ProbeError:
            invalid += 1
            continue
        key = tuple(context[name] for name in REQUIRED_CONTEXT)
        grouped.setdefault(key, []).append(row)
        modes.add(str(row["mode"]))
    cells = []
    for key, rows in sorted(grouped.items()):
        n = len(rows)
        reaches = sum(row["flow_reached"] for row in rows)
        fills = sum(row["filled_base_units"] > 0 for row in rows if row["flow_reached"])
        reached_size = sum(row["minimum_size_base_units"] for row in rows if row["flow_reached"])
        filled_size = sum(row["filled_base_units"] for row in rows if row["flow_reached"])
        reach_lower = _wilson_lower(reaches, n)
        fill_lower = _wilson_lower(fills, reaches)
        cells.append({
            "context": dict(zip(REQUIRED_CONTEXT, key)), "terminal_probes": n,
            "flow_reached": reaches, "filled_after_reach": fills,
            "p_flow_reaches_quote": reaches / n if n else None,
            "p_flow_reaches_quote_lower_95": reach_lower,
            "p_fill_given_reach": fills / reaches if reaches else None,
            "p_fill_given_reach_lower_95": fill_lower,
            "filled_size_fraction_given_reach": filled_size / reached_size if reached_size else None,
            "conservative_any_fill_probability": (reach_lower * fill_lower if reach_lower is not None and fill_lower is not None else None),
            "mature": n >= minimum_terminal_per_cell and reaches > 0,
        })
    all_live = bool(modes) and modes == {"LIVE_OBSERVED"}
    return {
        "schema": "polymarket_v7_maker_probe_calibration_v1", "cells": cells,
        "terminal_probes": sum(cell["terminal_probes"] for cell in cells),
        "invalid_outcomes": invalid, "modes": sorted(modes),
        "state": "LIVE_CALIBRATION_EVIDENCE" if all_live else "PAPER_DIAGNOSTIC_ONLY",
        "promotion_credit": False,
        "simulation_policy": "use_conservative_any_fill_probability_only_when_cell_mature",
    }


def _outcome_hash_payload(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("record_hash", None)
    return payload


def _validate_sealed_outcome(value: Any, previous_hash: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("record_kind") != "PROBE_OUTCOME":
        raise ProbeError("outcome_tape:record_kind")
    if value.get("previous_record_hash") != previous_hash or not HASH_RE.fullmatch(str(value.get("record_hash") or "")):
        raise ProbeError("outcome_tape:chain_link")
    if value["record_hash"] != digest(_outcome_hash_payload(value)):
        raise ProbeError("outcome_tape:record_hash")
    # Reuse calibration's strict row semantics without allowing an invalid
    # record to be quietly treated as a zero observation.
    checked = calibrate([value], minimum_terminal_per_cell=1)
    if checked["invalid_outcomes"]:
        raise ProbeError("outcome_tape:invalid_outcome")
    return value


def append_outcome(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    """Append a terminal probe outcome to a separate immutable hash chain."""
    if not isinstance(value, dict) or value.get("record_kind") != "PROBE_OUTCOME":
        raise ProbeError("outcome_tape:record_kind")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = GENESIS_HASH
    seen: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProbeError("outcome_tape:invalid_json") from exc
            raw = _validate_sealed_outcome(raw, prior)
            outcome_id = str(raw.get("outcome_id") or "")
            if outcome_id in seen:
                raise ProbeError("outcome_tape:duplicate_outcome")
            seen.add(outcome_id)
            prior = str(raw["record_hash"])
    candidate = dict(value)
    if str(candidate.get("outcome_id") or "") in seen:
        raise ProbeError("outcome_tape:duplicate_outcome")
    candidate["previous_record_hash"] = prior
    candidate["record_hash"] = digest(_outcome_hash_payload(candidate))
    _validate_sealed_outcome(candidate, prior)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(candidate, sort_keys=True) + "\n")
        handle.flush(); os.fsync(handle.fileno())
    return candidate


def append_assignment(path: Path, assignment: ProbeAssignment) -> ProbeAssignment:
    """Atomically append a pre-outcome assignment to an immutable hash chain."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = GENESIS_HASH
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = json.loads(line)
            if raw.get("record_kind") != "PROBE_ASSIGNMENT":
                raise ProbeError("assignment_tape:record_kind")
            value = dict(raw)
            value.pop("record_kind", None)
            if isinstance(value.get("arms"), list):
                value["arms"] = tuple(value["arms"])
            row = ProbeAssignment(**value)
            row.validate()
            if row.previous_record_hash != prior:
                raise ProbeError("assignment_tape:chain_break")
            prior = str(row.record_hash)
    sealed = assignment.seal(prior)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(sealed) | {"record_kind": "PROBE_ASSIGNMENT"}, sort_keys=True) + "\n")
        handle.flush(); os.fsync(handle.fileno())
    return sealed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcomes", type=Path, required=True,
                        help="immutable PROBE_OUTCOME JSONL tape")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-terminal-per-cell", type=int, default=20)
    args = parser.parse_args()
    rows = []
    for line in args.outcomes.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    # Validate the whole chain before producing a calibration artifact.
    prior = GENESIS_HASH
    for row in rows:
        _validate_sealed_outcome(row, prior)
        prior = str(row["record_hash"])
    report = calibrate(rows, minimum_terminal_per_cell=args.minimum_terminal_per_cell)
    report["outcome_tape_head_hash"] = prior if rows else None
    rendered = json.dumps(report, sort_keys=True, indent=2) + "\n"
    temporary = args.output.with_name(args.output.name + f".tmp.{os.getpid()}")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
