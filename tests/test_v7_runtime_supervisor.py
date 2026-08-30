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


def _write_external_state(root: Path, sha: str, *, full: bool = True, books: int = 2) -> None:
    external = root / "external_fair"
    external.mkdir(parents=True)
    (external / "status.json").write_text(json.dumps({
        "schema": "polymarket_v7_external_fair_status_v1",
        "code_sha": sha,
        "state": "FULL_FAIR_SHADOW_OPERATIONAL" if full else "DATA_PLANE_OPERATIONAL",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "blockers": [] if full else ["FAIR_VALUE_INVALID"],
        "external_fair_required_markets": 1 if full else 0,
        "contract": {"verified": full, "rules_hash_recognized": full},
        "settlement_reference": {"valid": full},
        "fair": {"valid": full},
        "oracle": {"healthy": True},
        "external": {"healthy": True},
    }))
    (external / "paper_router_status.json").write_text(json.dumps({
        "schema": "polymarket_v7_external_fair_paper_router_v1",
        "code_sha": sha,
        "state": "RUNNING",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "SHADOW_ZERO_AUTHORITY",
        "order_submission_enabled": False,
        "counterfactual_collection_enabled": True,
        "killed": False,
        "blocker": "",
        "book_requests": 7,
        "last_decision": {"books": books},
        "timestamp": 1_000,
    }))


def test_external_fair_readiness_requires_complete_chain_and_two_books(tmp_path: Path) -> None:
    sha = "d" * 40
    _write_external_state(tmp_path, sha)
    assert supervisor.external_fair_ready(tmp_path, sha, now=1_001)
    _write_external_state(tmp_path, sha, books=0)
    assert not supervisor.external_fair_ready(tmp_path, sha, now=1_001)
    _write_external_state(tmp_path, sha, full=False)
    assert not supervisor.external_fair_ready(tmp_path, sha, now=1_001)
