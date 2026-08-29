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
