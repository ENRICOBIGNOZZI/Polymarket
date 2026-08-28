#!/usr/bin/env python3
"""Exact-SHA manifest supervisor for the three V7 research sleeves.

The supervisor deliberately performs no market-data connection and owns no
OMS, account, capital, ledger, order, or promotion authority. Sports and
cross-platform component workers publish independently measured evidence which
is validated and aggregated here; wallet intelligence remains BLOCKED_CONFIG.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


MANIFEST_SCHEMA = "polymarket_v7_research_sleeves_manifest_v1"
HEARTBEAT_SCHEMA = "polymarket_v7_research_shadow_heartbeat_v1"
STATUS_SCHEMA = "polymarket_v7_research_shadow_status_v1"
LOCK_SCHEMA = "polymarket_v7_research_shadow_lock_v1"
SUPERVISED_FAMILIES = ("sports_latency", "cross_platform", "wallet_intelligence")
EXCLUDED_LIVE_FAMILIES = ("ranking", "pca", "local_factor")
HEX = frozenset("0123456789abcdef")
BLOCKERS: Mapping[str, tuple[str, ...]] = {
    "sports_latency": (
        "sports_component_status_missing_or_stale",
    ),
    "cross_platform": (
        "cross_platform_component_status_missing_or_stale",
    ),
    "wallet_intelligence": (
        "wallet_chain_indexer_not_configured",
        "verified_wallet_market_mapping_and_outcome_feeds_not_configured",
    ),
}
SAFETY = {
    "paper_only": True,
    "research_only": True,
    "authenticated_execution": False,
    "real_order_submission": False,
    "execution_authority": False,
    "capital_authority": False,
    "oms_authority": False,
    "ledger_write_authority": False,
    "promotion_authority": False,
}


class SupervisorError(ValueError):
    pass


def now_seconds() -> int:
    return int(time.time())


def validate_model_sha(value: str) -> str:
    sha = str(value or "").strip().lower()
    if len(sha) != 40 or any(ch not in HEX for ch in sha):
        raise SupervisorError("model_sha_must_be_exact_40_hex")
    return sha


def verify_repository_sha(repository_root: Path, expected_sha: str) -> None:
    try:
        actual = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository_root, text=True,
            stderr=subprocess.STDOUT, timeout=10,
        ).strip().lower()
    except (OSError, subprocess.SubprocessError) as exc:
        raise SupervisorError("repository_head_unavailable") from exc
    if actual != validate_model_sha(expected_sha):
        raise SupervisorError(f"repository_head_not_exact_model_sha:{actual}")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _pid_alive(value: Any) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return False
    return True


def _string_set(value: Any, field: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise SupervisorError(f"scope_{field}_must_be_nonempty_string_list")
    if len(value) != len(set(value)):
        raise SupervisorError(f"scope_{field}_contains_duplicates")
    return set(value)


def validate_scope(scope_path: Path) -> dict[str, Any]:
    try:
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SupervisorError("live_model_scope_missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SupervisorError("live_model_scope_invalid_json") from exc
    if not isinstance(scope, dict):
        raise SupervisorError("live_model_scope_not_object")
    if (
        scope.get("schema") != "polymarket_v7_live_model_scope_v1"
        or scope.get("version") != 7
        or scope.get("target_live_count") != 12
        or scope.get("paper_only") is not True
        or scope.get("authenticated_execution") is not False
        or scope.get("real_order_submission") is not False
    ):
        raise SupervisorError("live_model_scope_identity_or_safety_invalid")
    supervised = _string_set(
        scope.get("research_shadow_supervised_families"),
        "research_shadow_supervised_families",
    )
    excluded = _string_set(scope.get("excluded_live_families"), "excluded_live_families")
    if supervised != set(SUPERVISED_FAMILIES):
        raise SupervisorError("live_model_scope_supervised_families_mismatch")
    if excluded != set(EXCLUDED_LIVE_FAMILIES):
        raise SupervisorError("live_model_scope_excluded_families_mismatch")
    if supervised & excluded:
        raise SupervisorError("live_model_scope_overlap")
    target = _string_set(scope.get("target_live_families"), "target_live_families")
    if len(target) != 12 or target & excluded or supervised - target:
        raise SupervisorError("live_model_scope_target_partition_invalid")
    governance = scope.get("governance") if isinstance(scope.get("governance"), dict) else {}
    if (
        governance.get("single_execution_owner") is not True
        or governance.get("research_has_capital") is not False
        or governance.get("research_has_oms_authority") is not False
        or governance.get("research_has_ledger_writer_authority") is not False
        or governance.get("automatic_promotion") is not False
    ):
        raise SupervisorError("live_model_scope_governance_invalid")
    return scope


class ResearchShadowSupervisor:
    def __init__(
        self, *, run_root: Path, model_sha: str, scope_path: Path,
        clock: Callable[[], int] = now_seconds,
    ):
        self.run_root = Path(run_root)
        self.model_sha = validate_model_sha(model_sha)
        self.scope_path = Path(scope_path)
        validate_scope(self.scope_path)
        self.clock = clock
        self.started_at = int(clock())
        self.control_root = self.run_root / "control"
        self.output_root = self.run_root / "shadow"
        self.manifest_path = self.control_root / "research_sleeves_manifest.json"
        self.heartbeat_path = self.control_root / "research_shadow_heartbeat.json"
        self.lock_path = self.control_root / "research_shadow_supervisor.lock"
        self.lock_owner_path = self.lock_path / "owner.json"
        self.lock_owned = False
        self._lock_handle: Any = None
        self.statuses: dict[str, dict[str, Any]] = {}
        self.acquire_lock()
        try:
            self._initialize()
        except BaseException:
            self.release_lock()
            raise

    def _lock_owner(self) -> dict[str, Any]:
        return _json_object(self.lock_owner_path)

    def acquire_lock(self) -> None:
        self.control_root.mkdir(parents=True, exist_ok=True)
        self.lock_path.mkdir(exist_ok=True)
        handle = self.lock_owner_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            try:
                owner = json.load(handle)
            except (OSError, json.JSONDecodeError):
                owner = {}
            handle.close()
            owner_pid = owner.get("pid") if isinstance(owner, dict) else None
            state = "live" if _pid_alive(owner_pid) else "locked"
            raise SupervisorError(
                f"research_shadow_supervisor_already_active:{state}:{owner_pid}"
            ) from exc
        owner = {
            "schema": LOCK_SCHEMA,
            "version": 7,
            "model_sha": self.model_sha,
            "pid": os.getpid(),
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "started_at": self.started_at,
        }
        try:
            handle.seek(0)
            handle.truncate()
            json.dump(owner, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise
        self._lock_handle = handle
        self.lock_owned = True

    def release_lock(self) -> None:
        if not self.lock_owned:
            return
        handle = self._lock_handle
        owner = self._lock_owner()
        owns_record = (
            owner.get("schema") == LOCK_SCHEMA
            and owner.get("model_sha") == self.model_sha
            and owner.get("pid") == os.getpid()
        )
        try:
            if owns_record and handle is not None:
                handle.seek(0)
                handle.truncate()
                json.dump({
                    "schema": LOCK_SCHEMA,
                    "version": 7,
                    "model_sha": self.model_sha,
                    "pid": 0,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "started_at": self.started_at,
                    "released_at": int(self.clock()),
                    "state": "STOPPED",
                }, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
            self._lock_handle = None
            self.lock_owned = False

    def _status_path(self, family: str) -> Path:
        return self.output_root / family / "status.json"

    def _output_path(self, family: str) -> Path:
        return self.output_root / family

    def _component_status_path(self, family: str) -> Path:
        return self.output_root / family / "component_status.json"

    def _external_component_status(self, family: str, timestamp: int) -> dict[str, Any] | None:
        if family not in {"sports_latency", "cross_platform"}:
            return None
        component = _json_object(self._component_status_path(family))
        try:
            component_timestamp = int(component.get("timestamp") or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            component.get("schema") != "polymarket_v7_external_input_component_status_v1"
            or component.get("version") != 7
            or component.get("family") != family
            or component.get("model_sha") != self.model_sha
            or component.get("authority") != "RESEARCH"
            or component.get("paper_only") is not True
            or component.get("research_only") is not True
            or component.get("authenticated_execution") is not False
            or component.get("real_order_submission") is not False
            or any(component.get(key) is not False for key in (
                "execution_authority", "capital_authority", "oms_authority",
                "ledger_write_authority", "promotion_authority",
            ))
            or component.get("implementation_complete") is not True
            or not -5 <= timestamp - component_timestamp <= 180
        ):
            return None
        evidence_state = str(component.get("evidence_state") or "")
        if evidence_state not in {"ACTIVE", "BLOCKED_EXTERNAL"}:
            return None
        reason_codes = component.get("reason_codes")
        if not isinstance(reason_codes, list) or any(not isinstance(x, str) for x in reason_codes):
            return None
        return component

    def _status(self, family: str, timestamp: int) -> dict[str, Any]:
        status = {
            "schema": STATUS_SCHEMA,
            "version": 7,
            "family": family,
            "authority": "RESEARCH",
            "model_sha": self.model_sha,
            **SAFETY,
            "supervisor_pid": os.getpid(),
            "process_state": "RUNNING",
            "evidence_state": "BLOCKED_CONFIG",
            "last_attempt_ts": 0,
            "last_success_ts": 0,
            "timestamp": timestamp,
            "status_path": str(self._status_path(family)),
            "output_path": str(self._output_path(family)),
            "reason_codes": list(BLOCKERS[family]),
        }
        component = self._external_component_status(family, timestamp)
        if component is not None:
            status.update({
                "evidence_state": component["evidence_state"],
                "last_attempt_ts": int(component.get("last_attempt_ts") or 0),
                "last_success_ts": int(component.get("last_success_ts") or 0),
                "reason_codes": list(component.get("reason_codes") or []),
                "component_status_path": str(self._component_status_path(family)),
                "implementation_complete": True,
                "feed_status": component.get("feed_status", "UNKNOWN"),
                "feed_operational": component.get("feed_operational") is True,
                "mapping_status": component.get("mapping_status", "UNKNOWN"),
                "verified_mappings": int(component.get("verified_mappings") or 0),
                "forward_collection_active": component.get("forward_collection_active") is True
                    or component.get("forward_race_tape_active") is True,
                "blocker": component.get("blocker", ""),
                "feed_age_ms": component.get("feed_age_ms"),
                "last_sequence": int(component.get("last_sequence") or 0),
                "connection_epoch": int(component.get("connection_epoch") or 0),
                "reconnect_count": int(component.get("reconnect_count") or 0),
                "gap_count": int(component.get("gap_count") or 0),
                "parse_failure_count": int(component.get("parse_failure_count") or 0),
                "dropped_event_count": int(component.get("dropped_event_count") or 0),
            })
        return status

    def _initialize(self) -> None:
        timestamp = int(self.clock())
        for family in SUPERVISED_FAMILIES:
            status = self._status(family, timestamp)
            self.statuses[family] = status
            atomic_json(self._status_path(family), status)
        self.write_heartbeat()

    def write_manifest(self) -> None:
        timestamp = int(self.clock())
        families: dict[str, dict[str, Any]] = {}
        for family in SUPERVISED_FAMILIES:
            status = self.statuses[family]
            families[family] = {
                "authority": "RESEARCH",
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "process_state": status["process_state"],
                "evidence_state": status["evidence_state"],
                "last_attempt_ts": status["last_attempt_ts"],
                "last_success_ts": status["last_success_ts"],
                "status_path": status["status_path"],
                "output_path": status["output_path"],
                "execution_authority": False,
                "capital_authority": False,
                "oms_authority": False,
                "ledger_write_authority": False,
                "promotion_authority": False,
                "implementation_complete": status.get("implementation_complete", False),
                "feed_status": status.get("feed_status", "NOT_CONFIGURED"),
                "feed_operational": status.get("feed_operational", False),
                "mapping_status": status.get("mapping_status", "NOT_CONFIGURED"),
                "verified_mappings": status.get("verified_mappings", 0),
                "forward_collection_active": status.get("forward_collection_active", False),
                "blocker": status.get("blocker", ""),
                "feed_age_ms": status.get("feed_age_ms"),
                "last_sequence": status.get("last_sequence", 0),
                "connection_epoch": status.get("connection_epoch", 0),
                "reconnect_count": status.get("reconnect_count", 0),
                "gap_count": status.get("gap_count", 0),
                "parse_failure_count": status.get("parse_failure_count", 0),
                "dropped_event_count": status.get("dropped_event_count", 0),
            }
        atomic_json(self.manifest_path, {
            "schema": MANIFEST_SCHEMA,
            "version": 7,
            "model_sha": self.model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "supervisor_pid": os.getpid(),
            "timestamp": timestamp,
            "scope_path": str(self.scope_path),
            "families": families,
        })

    def write_heartbeat(self, *, process_state: str = "RUNNING") -> None:
        if process_state not in {"RUNNING", "STOPPED"}:
            raise SupervisorError("invalid_process_state")
        timestamp = int(self.clock())
        for family in SUPERVISED_FAMILIES:
            component = self._external_component_status(family, timestamp)
            status = self._status(family, timestamp) if component is not None else self.statuses[family]
            status.update({
                "timestamp": timestamp,
                "supervisor_pid": os.getpid(),
                "process_state": process_state,
            })
            if family == "wallet_intelligence" or component is None:
                status.update({
                    "evidence_state": "BLOCKED_CONFIG",
                    "last_attempt_ts": 0,
                    "last_success_ts": 0,
                    "reason_codes": list(BLOCKERS[family]),
                })
            self.statuses[family] = status
            atomic_json(self._status_path(family), status)
        atomic_json(self.heartbeat_path, {
            "schema": HEARTBEAT_SCHEMA,
            "version": 7,
            "model_sha": self.model_sha,
            **SAFETY,
            "supervisor_pid": os.getpid(),
            "started_at": self.started_at,
            "timestamp": timestamp,
            "families": {
                family: dict(self.statuses[family])
                for family in SUPERVISED_FAMILIES
            },
        })
        self.write_manifest()

    def run_forever(self, *, heartbeat_seconds: float = 5.0) -> None:
        stopping = False

        def stop(_signum: int, _frame: Any) -> None:
            nonlocal stopping
            stopping = True

        previous = {
            signum: signal.signal(signum, stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            while not stopping:
                self.write_heartbeat()
                time.sleep(max(0.1, float(heartbeat_seconds)))
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            try:
                self.write_heartbeat(process_state="STOPPED")
            finally:
                self.release_lock()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    repository_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--repository-root", type=Path, default=repository_root)
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v7_live"))
    parser.add_argument("--scope", type=Path, default=Path("config/v7_live_model_scope.json"))
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--heartbeat-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)

    expected_sha = validate_model_sha(args.model_sha)
    root = args.repository_root.resolve()
    verify_repository_sha(root, expected_sha)
    run_root = args.run_root if args.run_root.is_absolute() else root / args.run_root
    scope = args.scope if args.scope.is_absolute() else root / args.scope
    daemon = ResearchShadowSupervisor(run_root=run_root, model_sha=expected_sha, scope_path=scope)
    try:
        if args.once:
            daemon.write_heartbeat(process_state="STOPPED")
        else:
            daemon.run_forever(heartbeat_seconds=args.heartbeat_seconds)
    finally:
        daemon.release_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
