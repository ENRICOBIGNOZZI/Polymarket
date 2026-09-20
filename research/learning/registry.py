"""Immutable explicit candidate transitions; this module has no deploy client."""
import fcntl
from pathlib import Path
from .common import SAFETY, canonical, digest, immutable, read_json
from v7_probability_promotion import REQUIRED_GATES

STATES = {"TRAINED": {"VALIDATED_OFFLINE", "REJECTED"},
          "VALIDATED_OFFLINE": {"PAPER_ELIGIBLE", "REJECTED"},
          "PAPER_ELIGIBLE": {"PAPER_FORWARD_TEST", "REJECTED"},
          "PAPER_FORWARD_TEST": {"PROMOTED", "REJECTED"}, "PROMOTED": set(), "REJECTED": set()}
GATES = REQUIRED_GATES


def promotion_gates(evidence):
    failed = [k for k in GATES if evidence.get(k) is not True]
    return {"passed": not failed, "failed_gates": failed}


def transition(root, candidate_sha, state, *, evidence=None, activation=None):
    if len(candidate_sha) != 64 or any(c not in "0123456789abcdef" for c in candidate_sha):
        raise ValueError("INVALID_CANDIDATE_SHA")
    if state not in STATES:
        raise ValueError("INVALID_CANDIDATE_STATE")
    path = Path(root)/"registry"/candidate_sha; path.mkdir(parents=True, exist_ok=True)
    with (path/".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = [read_json(p) for p in sorted(path.glob("*.json"))]
        previous = history[-1] if history else None
        if previous and previous["state"] == state:
            return previous
        if (previous is None and state != "TRAINED") or (previous and state not in STATES[previous["state"]]):
            raise ValueError("INVALID_CANDIDATE_TRANSITION")
        evidence = evidence or {}
        if state in {"VALIDATED_OFFLINE", "PAPER_ELIGIBLE", "PAPER_FORWARD_TEST", "PROMOTED"}:
            if not promotion_gates(evidence)["passed"]:
                raise ValueError("PROMOTION_GATES_FAILED")
        if state in {"PAPER_FORWARD_TEST", "PROMOTED"}:
            if not isinstance(activation, dict) or activation.get("explicit_activation") is not True:
                raise ValueError("EXPLICIT_ACTIVATION_REQUIRED_NO_AUTO_PROMOTION")
            if activation.get("artifact_sha256") != candidate_sha or activation.get("duration_seconds") != 7200:
                raise ValueError("FORWARD_FREEZE_IDENTITY_MISMATCH")
            if state == "PROMOTED" and (activation.get("multiple_forward_cohorts_validated") is not True
                                       or activation.get("cohort_count", 0) < 2):
                raise ValueError("TWO_HOUR_SAMPLE_CANNOT_PROMOTE")
        value = {"schema": "v7_candidate_transition_v1", **SAFETY, "candidate_sha256": candidate_sha,
                 "sequence": len(history), "state": state, "evidence": evidence, "activation": activation,
                 "previous_receipt_sha256": digest(canonical(previous)) if previous else None}
        immutable(path/(f"{len(history):06d}.json"), canonical(value))
        return value
