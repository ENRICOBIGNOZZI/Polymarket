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
    value.restart_maximum = 5
    value.stopping = False
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


def test_git_head_uses_exact_sha_env_or_runtime_identity(tmp_path: Path, monkeypatch) -> None:
    env_sha = "e" * 40
    monkeypatch.setenv("PM_V7_MODEL_SHA", env_sha)
    assert supervisor._git_head(tmp_path) == env_sha

    monkeypatch.delenv("PM_V7_MODEL_SHA")
    identity = tmp_path / "deploy" / "london"
    identity.mkdir(parents=True)
    runtime_sha = "f" * 40
    (identity / "runtime_sha").write_text(runtime_sha + "\n", encoding="utf-8")
    assert supervisor._git_head(tmp_path) == runtime_sha


def _write_external_state(root: Path, sha: str, *, full: bool = True, books: int = 2) -> None:
    external = root / "external_fair"
    external.mkdir(parents=True, exist_ok=True)
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
        "schema": "polymarket_v7_crypto_settlement_engine_status_v1",
        "code_sha": sha,
        "state": "RUNNING",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "OPPORTUNITY_PROPOSAL_ONLY",
        "capital_authority": False,
        "oms_authority": False,
        "inventory_authority": False,
        "ledger_writer_authority": False,
        "order_submission_enabled": False,
        "counterfactual_collection_enabled": True,
        "simulated_paper_account_authority": "V7_CANONICAL_LEDGER_AND_SINGLE_WRITER_SPOOL",
        "paper_exploration_accounting_active": True,
        "canonical_order_reconciliation": {
            "schema": "polymarket_v7_paper_exploration_order_reconciliation_v1",
            "model_sha": sha, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "complete": True, "unresolved_orders": [],
            "invalid_spool_records": [], "conflicts": [],
        },
        "canonical_final_reconciliation": {
            "schema": "polymarket_v7_paper_exploration_final_reconciliation_v1",
            "model_sha": sha, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "complete": True, "missing_canonical_fills": [],
            "invalid_virtual_finals": [],
        },
        "paper_exploration_account": {
            "schema": "polymarket_v7_paper_exploration_account_v1",
            "model_sha": sha, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "real_capital_at_risk": False,
            "accounting_owner": "V7_CANONICAL_LEDGER_AND_SINGLE_WRITER_SPOOL",
            "execution_authority": "SIMULATED_PAPER_EXPLORATION_ONLY",
            "complete": True, "issues": [], "invalid_spool_records": [],
            "starting_capital": 4000.0, "cash": 4000.0, "equity": 4000.0,
            "realized_pnl": 0.0, "entry_debit": 0.0,
            "settlement_payout": 0.0, "orders_submitted": 0,
            "fills": 0, "terminal_positions": 0, "open_positions": 0,
        },
        "orders_submitted": 0, "fills": 0, "open_positions": 0,
        "cash": 4000.0, "equity": 4000.0, "realized_pnl": 0.0,
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
    _write_external_state(tmp_path, sha)
    router_path = tmp_path / "external_fair" / "paper_router_status.json"
    router_status = json.loads(router_path.read_text())
    router_status["canonical_final_reconciliation"]["complete"] = False
    router_path.write_text(json.dumps(router_status))
    assert not supervisor.external_fair_ready(tmp_path, sha, now=1_001)
    _write_external_state(tmp_path, sha)
    router_status = json.loads(router_path.read_text())
    router_status["canonical_order_reconciliation"]["complete"] = False
    router_path.write_text(json.dumps(router_status))
    assert not supervisor.external_fair_ready(tmp_path, sha, now=1_001)
    _write_external_state(tmp_path, sha)
    router_status = json.loads(router_path.read_text())
    router_status["cash"] = 3999.0
    router_path.write_text(json.dumps(router_status))
    assert not supervisor.external_fair_ready(tmp_path, sha, now=1_001)
    _write_external_state(tmp_path, sha, full=False)
    assert not supervisor.external_fair_ready(tmp_path, sha, now=1_001)


def test_exhausted_restart_budget_has_bounded_exact_sha_cooldown(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "supervisor_restarts.json"
    current = 10_000
    monkeypatch.setattr(supervisor.time, "time", lambda: current)
    path.write_text(json.dumps({
        "schema": "polymarket_v7_supervisor_restarts_v1",
        "expected_sha": "a" * 40,
        "timestamps": [current - 899, current - 10, current - 5, current - 2, current],
    }))
    value = instance(path, "a" * 40)
    times = value._restart_times()
    assert len(times) == 5
    assert value.restart_cooldown_seconds(times) == 2
    monkeypatch.setattr(supervisor.time, "time", lambda: current + 2)
    assert value._restart_times() == [current - 10, current - 5, current - 2, current]
    assert value.restart_cooldown_seconds() == 0


def test_recoverable_health_requires_sustained_grace(tmp_path: Path) -> None:
    value = instance(tmp_path / "restarts.json", "a" * 40)
    value.recoverable_grace = 30.0
    assert not value.recoverable_grace_expired(None, 100.0)
    assert not value.recoverable_grace_expired(100.0, 129.999)
    assert value.recoverable_grace_expired(100.0, 130.0)


def test_supervisor_preserves_health_failure_evidence(tmp_path: Path, monkeypatch) -> None:
    value = instance(tmp_path / "restarts.json", "a" * 40)
    value.recoverable_grace = 30.0
    value.last_health_failure_path = tmp_path / "supervisor_last_health_failure.json"
    monkeypatch.setattr(supervisor.time, "time", lambda: 12345)
    value.record_health_failure(
        supervisor.RECOVERABLE,
        ["native_partition_coverage_incomplete", "runtime_status_stale"],
        elapsed_seconds=450.0,
        sustained_seconds=30.0,
    )
    row = json.loads(value.last_health_failure_path.read_text())
    assert row["schema"] == "polymarket_v7_supervisor_health_failure_v1"
    assert row["classification"] == supervisor.RECOVERABLE
    assert row["paper_only"] is True
    assert row["authenticated_execution"] is False
    assert row["real_order_submission"] is False
    assert row["expected_sha"] == "a" * 40
    assert row["reasons"] == [
        "native_partition_coverage_incomplete",
        "runtime_status_stale",
    ]
    assert row["elapsed_seconds"] == 450.0
    assert row["sustained_seconds"] == 30.0
    assert row["recoverable_grace_seconds"] == 30.0


def test_recoverable_grace_does_not_weaken_unsafe_fail_closed_path() -> None:
    source = (ROOT / "ops/v7_runtime_supervisor.py").read_text()
    unsafe = source.index("if health.classification == UNSAFE:")
    recoverable = source.index("if health.classification == RECOVERABLE:", unsafe)
    assert unsafe < recoverable
    unsafe_block = source[unsafe:recoverable]
    assert "self.stop_child()" in unsafe_block
    assert "return 78" in unsafe_block
    assert "recoverable_grace_expired" not in unsafe_block


def test_service_entrypoint_auto_recovers_budget_but_not_safety_quarantine() -> None:
    entrypoint = (ROOT / "ops/v7_service_entrypoint.sh").read_text()
    assert 'quarantined) exit 0' in entrypoint
    assert 'quarantined|restart_budget_exhausted) exit 0' not in entrypoint
    assert 'restart_budget_cooldown' in (ROOT / "ops/v7_runtime_supervisor.py").read_text()


def test_service_entrypoint_preserves_housekeeping_affinity() -> None:
    entrypoint = (ROOT / "ops/v7_service_entrypoint.sh").read_text()
    assert "taskset -pc" not in entrypoint
    planner = (ROOT / "scripts/v7_runtime_resource_plan.py").read_text()
    assert "cpuset.cpus.effective" in planner
    assert "_cgroup_effective_cpus" in planner
