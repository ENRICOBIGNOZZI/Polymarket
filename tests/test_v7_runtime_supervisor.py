from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))

import v7_runtime_supervisor as supervisor


def instance(restart_path: Path, expected_sha: str) -> supervisor.Supervisor:
    value = object.__new__(supervisor.Supervisor)
    value.restart_path = restart_path
    value.expected_sha = expected_sha
    value.restart_window = 900
    return value


def test_restart_budget_is_scoped_to_exact_sha(tmp_path: Path) -> None:
    now = int(time.time())
    path = tmp_path / "supervisor_restarts.json"
    path.write_text(json.dumps({
        "schema": "polymarket_v7_supervisor_restarts_v1",
        "expected_sha": "a" * 40,
        "timestamps": [now - 1, now],
    }))
    assert instance(path, "b" * 40)._restart_times() == []
    assert instance(path, "a" * 40)._restart_times() == [now - 1, now]


def test_unscoped_or_malformed_restart_budget_starts_a_new_exact_sha_counter(tmp_path: Path) -> None:
    path = tmp_path / "supervisor_restarts.json"
    path.write_text(json.dumps({"timestamps": [int(time.time())]}))
    assert instance(path, "c" * 40)._restart_times() == []


def _write_external_state(root: Path, sha: str, *, full: bool = True) -> None:
    external = root / "external_fair"
    control = root / "control"
    external.mkdir(parents=True, exist_ok=True)
    control.mkdir(parents=True, exist_ok=True)
    (external / "status.json").write_text(json.dumps({
        "schema": "polymarket_v7_external_fair_status_v1",
        "code_sha": sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "market": {
            "market_id": "m1", "active": True, "closed": False,
            "accepting_orders": True,
        },
        "contract": {"verified": full, "rules_hash_recognized": full},
        "settlement_reference": {"valid": full},
        "oracle": {"healthy": True},
        "external": {"healthy": True},
    }))
    (control / "native_engine_supervisor_status.json").write_text(json.dumps({
        "schema": "polymarket_v7_native_engine_supervisor_status_v1",
        "timestamp_ms": 1_000_000,
        "state": "RUNNING",
        "model_sha": sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": False,
        "child_pid": 123,
    }))
    (control / "native_evidence_status.json").write_text(json.dumps({
        "schema": "polymarket_v7_native_evidence_status_v1",
        "timestamp_ms": 1_000_000,
        "model_sha": sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "healthy": True,
        "published": 5,
        "written": 5,
        "dropped": 0,
    }))


def test_external_fair_readiness_requires_native_engine_and_evidence_chain(tmp_path: Path) -> None:
    sha = "d" * 40
    _write_external_state(tmp_path, sha)
    assert supervisor.external_fair_ready(tmp_path, sha, now=1_001)

    _write_external_state(tmp_path, sha)
    native_path = tmp_path / "control" / "native_engine_supervisor_status.json"
    native = json.loads(native_path.read_text())
    native["state"] = "ENGINE_FAILED"
    native_path.write_text(json.dumps(native))
    assert not supervisor.external_fair_ready(tmp_path, sha, now=1_001)

    _write_external_state(tmp_path, sha)
    evidence_path = tmp_path / "control" / "native_evidence_status.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["dropped"] = 1
    evidence_path.write_text(json.dumps(evidence))
    assert not supervisor.external_fair_ready(tmp_path, sha, now=1_001)

    _write_external_state(tmp_path, sha, full=False)
    assert not supervisor.external_fair_ready(tmp_path, sha, now=1_001)

    _write_external_state(tmp_path, sha)
    evidence = json.loads(evidence_path.read_text())
    evidence["timestamp_ms"] = 990_000
    evidence_path.write_text(json.dumps(evidence))
    assert not supervisor.external_fair_ready(tmp_path, sha, now=1_001)

