#!/usr/bin/env python3
"""Atomically preserve a prior V7 PAPER run before an exact-SHA cutover."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from v7_native_risk_policy import unsettled_exposure
from v7_native_settlement_projection import context_from_fill
from v7_native_crypto_engine_manager import open_native_orders

SHA40 = re.compile(r"^[0-9a-f]{40}$")
CARRYOVER_SCHEMA = "polymarket_v7_native_carryover_exposure_v1"

class CutoverArchiveError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def legacy_claim_carry_supported(repository_root: Path) -> bool:
    """Prove the target release reserves legacy claims before execution."""
    runtime = read_json(repository_root / "deploy/london/runtime_manifest.json")
    process = read_json(repository_root / "config/v7_process_manifest.json")
    try:
        loop = (repository_root / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    except OSError:
        return False
    entrypoints = runtime.get("python_entrypoints") if isinstance(runtime.get("python_entrypoints"), list) else []
    rows = process.get("processes") if isinstance(process.get("processes"), list) else []
    manager = next((row for row in rows if isinstance(row, dict) and row.get("id") == "native_engine_manager"), {})
    arguments = manager.get("arguments") if isinstance(manager.get("arguments"), list) else []
    inputs = manager.get("inputs") if isinstance(manager.get("inputs"), list) else []
    return (
        "scripts/v7_legacy_native_claims.py" in entrypoints
        and "--legacy-claims" in arguments
        and "control/legacy_native_claims.json" in inputs
        and "v7_legacy_native_claims.py" in loop
        and "--legacy-claims" in loop
    )


def native_unsettled_markets(path: Path) -> list[str]:
    """Return native PAPER fill markets that lack their canonical native FINAL.

    This is a cross-generation cutover guard. Same-SHA recovery is handled
    earlier and remains allowed so the native manager can finish settlement.
    """
    if not path.is_file():
        return []
    fills: set[tuple[str, str]] = set()
    finals: set[tuple[str, str]] = set()
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CutoverArchiveError(f"ledger_invalid_json:{number}") from exc
            if not isinstance(value, dict):
                raise CutoverArchiveError(f"ledger_invalid_record:{number}")
            if value.get("strategy") != "CRYPTO_SETTLEMENT_ENGINE":
                continue
            event_type = str(value.get("event_type") or "")
            if event_type not in {"FILL", "FINAL"}:
                continue
            model_sha = str(value.get("model_sha") or "")
            market_id = str(value.get("market_id") or "")
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            receipt = metadata.get("native_settlement_receipt")
            if not market_id or not isinstance(receipt, dict):
                continue
            if receipt.get("owner") != "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE":
                continue
            key = model_sha, market_id
            if event_type == "FILL":
                fills.add(key)
            elif metadata.get("native_market_settlement_id") == f"native-settlement:{market_id}":
                finals.add(key)
    return [f"{sha}:{market}" for sha, market in sorted(fills - finals)]


def _ledger_economic_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CutoverArchiveError(f"ledger_invalid_json:{number}") from exc
            if not isinstance(value, dict):
                raise CutoverArchiveError(f"ledger_invalid_record:{number}")
            if value.get("event_type") in {"FILL", "FINAL"} and value.get("strategy") == "CRYPTO_SETTLEMENT_ENGINE":
                rows.append(value)
    return rows


def _validate_inherited_carryover(value: dict, previous_sha: str) -> dict:
    if not value:
        return {
            "markets": [], "context_claims_microdollars": {},
            "total_unsettled_microdollars": 0, "source_archives": [],
            "source_model_shas": [],
        }
    if (
        value.get("schema") != CARRYOVER_SCHEMA
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("target_model_sha") != previous_sha
    ):
        raise CutoverArchiveError("prior_native_carryover_invalid")
    claims = value.get("context_claims_microdollars")
    markets = value.get("markets")
    archives = value.get("source_archives")
    shas = value.get("source_model_shas")
    if not isinstance(claims, dict) or not isinstance(markets, list) or not isinstance(archives, list) or not isinstance(shas, list):
        raise CutoverArchiveError("prior_native_carryover_invalid")
    normalized: dict[str, int] = {}
    for context, claim in claims.items():
        if not isinstance(context, str) or not isinstance(claim, int) or isinstance(claim, bool) or claim < 0:
            raise CutoverArchiveError("prior_native_carryover_invalid")
        normalized[context] = claim
    total = value.get("total_unsettled_microdollars")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0 or sum(normalized.values()) != total:
        raise CutoverArchiveError("prior_native_carryover_invalid")
    return {
        "markets": [dict(row) for row in markets if isinstance(row, dict)],
        "context_claims_microdollars": normalized,
        "total_unsettled_microdollars": total,
        "source_archives": [str(x) for x in archives],
        "source_model_shas": [str(x) for x in shas],
    }


def build_native_carryover(
    ledger_path: Path,
    inherited: dict,
    previous_sha: str,
    target_sha: str,
) -> dict:
    """Conservatively carry unresolved native cost basis into the next SHA."""
    prior = _validate_inherited_carryover(inherited, previous_sha)
    economic = _ledger_economic_rows(ledger_path)
    by_sha: dict[str, list[dict]] = {}
    contexts: dict[tuple[str, str], str] = {}
    for row in economic:
        sha = str(row.get("model_sha") or "")
        market = str(row.get("market_id") or "")
        if not SHA40.fullmatch(sha) or not market:
            raise CutoverArchiveError("native_carryover_identity_invalid")
        by_sha.setdefault(sha, []).append(row)
        if row.get("event_type") == "FILL":
            try:
                label = context_from_fill(row)
            except Exception as exc:
                raise CutoverArchiveError("native_carryover_context_invalid") from exc
            context = f"{label['asset']}:{label['horizon']}"
            key = sha, market
            if key in contexts and contexts[key] != context:
                raise CutoverArchiveError("native_carryover_context_conflict")
            contexts[key] = context

    entries = list(prior["markets"])
    context_claims = dict(prior["context_claims_microdollars"])
    source_shas = set(prior["source_model_shas"])
    for sha, rows in sorted(by_sha.items()):
        try:
            exposure = unsettled_exposure(rows, sha)
        except Exception as exc:
            raise CutoverArchiveError("native_carryover_economics_invalid") from exc
        for market, claim in sorted(exposure["unsettled_market_claims_microdollars"].items()):
            context = contexts.get((sha, market))
            if not context or not isinstance(claim, int) or claim < 0:
                raise CutoverArchiveError("native_carryover_claim_invalid")
            entries.append({
                "model_sha": sha,
                "market_id": market,
                "context": context,
                "claim_microdollars": claim,
            })
            context_claims[context] = context_claims.get(context, 0) + claim
            source_shas.add(sha)
    total = sum(context_claims.values())
    return {
        "schema": CARRYOVER_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "target_model_sha": target_sha,
        "source_model_shas": sorted(source_shas),
        "source_archives": list(prior["source_archives"]),
        "markets": entries,
        "context_claims_microdollars": dict(sorted(context_claims.items())),
        "total_unsettled_microdollars": total,
        "lease_credit_policy": "NO_CREDIT_UNTIL_HISTORICAL_FINAL_RECONCILED",
    }


def pid_alive(value: object) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError, ProcessLookupError):
        return False
    return True


def git_is_ancestor(repository_root: Path, older: str, newer: str) -> bool:
    # London cutover runs as root while the immutable repository is owned by
    # the service user. Scope Git's safe-directory exception to this read-only
    # ancestry command instead of mutating global Git configuration.
    return subprocess.run(
        [
            "git", "-c", f"safe.directory={repository_root}",
            "-C", str(repository_root), "merge-base", "--is-ancestor", older, newer,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def validate_ledger(
    path: Path,
    repository_root: Path,
    target_sha: str,
    ancestor_check: Callable[[Path, str, str], bool],
) -> tuple[int, str, dict[str, int], dict[str, dict[str, int]]]:
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return 0, hashlib.sha256(b"").hexdigest(), {}, {}
    digest = hashlib.sha256()
    rows = 0
    model_sha_counts: dict[str, int] = {}
    strategy_counts_by_sha: dict[str, dict[str, int]] = {}
    ancestry: dict[str, bool] = {}
    with handle:
        for number, raw in enumerate(handle, start=1):
            digest.update(raw)
            if not raw.endswith(b"\n"):
                raise CutoverArchiveError("ledger_incomplete_tail")
            if not raw.strip():
                continue
            rows += 1
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CutoverArchiveError(f"ledger_invalid_json:{number}") from exc
            if not isinstance(value, dict):
                raise CutoverArchiveError(f"ledger_invalid_record:{number}")
            if value.get("paper_only") is not True or value.get("authenticated_execution") is not False:
                raise CutoverArchiveError(f"ledger_unsafe_record:{number}")
            model_sha = str(value.get("model_sha") or "")
            if not SHA40.fullmatch(model_sha):
                raise CutoverArchiveError(f"ledger_sha_invalid:{number}")
            if model_sha not in ancestry:
                ancestry[model_sha] = ancestor_check(repository_root, model_sha, target_sha)
            if not ancestry[model_sha]:
                raise CutoverArchiveError(f"ledger_sha_not_ancestor:{number}")
            model_sha_counts[model_sha] = model_sha_counts.get(model_sha, 0) + 1
            if value.get("record_kind") != "ECONOMIC_JOURNAL":
                strategy = str(value.get("strategy") or "").strip().upper()
                if strategy:
                    bucket = strategy_counts_by_sha.setdefault(model_sha, {})
                    bucket[strategy] = bucket.get(strategy, 0) + 1
    normalized_strategies = {
        sha: dict(sorted(counts.items()))
        for sha, counts in sorted(strategy_counts_by_sha.items())
    }
    return rows, digest.hexdigest(), dict(sorted(model_sha_counts.items())), normalized_strategies



def prepare(
    run_root: Path,
    archive_root: Path,
    repository_root: Path,
    target_sha: str,
    *,
    now: int | None = None,
    ancestor_check: Callable[[Path, str, str], bool] = git_is_ancestor,
    allow_native_carryover: bool = False,
    legacy_carry_check: Callable[[Path], bool] = legacy_claim_carry_supported,
) -> dict:
    if not SHA40.fullmatch(target_sha):
        raise CutoverArchiveError("target_sha_invalid")
    run_root = run_root.resolve()
    archive_root = archive_root.resolve()
    repository_root = repository_root.resolve()
    if not run_root.exists():
        run_root.mkdir(parents=True)
        return {"state": "NEW_RUN_ROOT", "target_sha": target_sha, "archived": False}

    runtime = read_json(run_root / "control/runtime_status.json")
    supervisor = read_json(run_root / "control/supervisor_status.json")
    deployed_path = run_root / "control/deployed_sha"
    deployed_sha = deployed_path.read_text(encoding="utf-8").strip() if deployed_path.is_file() else ""
    runtime_sha = str(runtime.get("model_sha") or "")

    if not runtime and not deployed_sha:
        lineage = read_json(run_root / "control/cutover_lineage.json")
        material_paths = sorted(
            path.relative_to(run_root).as_posix()
            for path in run_root.rglob("*")
            if path.is_file() or path.is_symlink()
        )
        if not material_paths:
            return {"state": "NEW_RUN_ROOT", "target_sha": target_sha, "archived": False}
        if material_paths == ["bootstrap_receipt.json"]:
            bootstrap = read_json(run_root / "bootstrap_receipt.json")
            bootstrap_sha = str(bootstrap.get("code_sha") or "")
            if (
                bootstrap.get("schema") == "polymarket_v7_london_bootstrap_receipt_v1"
                and bootstrap.get("paper_only") is True
                and bootstrap.get("authenticated_execution") is False
                and bootstrap.get("real_order_submission") is False
                and bootstrap.get("systemd_installed_but_disabled") is True
                and SHA40.fullmatch(bootstrap_sha)
                and ancestor_check(repository_root, bootstrap_sha, target_sha)
            ):
                return {
                    "state": "BOOTSTRAP_RUN_ROOT",
                    "target_sha": target_sha,
                    "bootstrap_sha": bootstrap_sha,
                    "archived": False,
                }
            raise CutoverArchiveError("bootstrap_receipt_invalid")
        if (
            lineage.get("schema") == "polymarket_v7_cutover_lineage_v1"
            and lineage.get("target_sha") == target_sha
            and lineage.get("paper_only") is True
            and lineage.get("authenticated_execution") is False
            and lineage.get("real_order_submission") is False
            and not (run_root / "ledger/execution.jsonl").exists()
        ):
            return {"state": "PREPARED_RUN_ROOT", "target_sha": target_sha, "archived": False}
        raise CutoverArchiveError("previous_runtime_sha_missing_or_invalid")

    if deployed_sha and not SHA40.fullmatch(deployed_sha):
        raise CutoverArchiveError("previous_deployed_sha_invalid")
    if runtime and not SHA40.fullmatch(runtime_sha):
        raise CutoverArchiveError("previous_runtime_sha_missing_or_invalid")
    if pid_alive(runtime.get("pid")) or pid_alive(supervisor.get("supervisor_pid")):
        raise CutoverArchiveError("prior_runtime_or_supervisor_still_alive")

    previous_sha = deployed_sha or runtime_sha
    if not SHA40.fullmatch(previous_sha):
        raise CutoverArchiveError("previous_runtime_sha_missing_or_invalid")
    runtime_checkout_drift = bool(
        deployed_sha and runtime_sha and runtime_sha == target_sha and runtime_sha != deployed_sha
    )
    if deployed_sha and runtime_sha and runtime_sha != deployed_sha and not runtime_checkout_drift:
        raise CutoverArchiveError("previous_deployed_runtime_sha_mismatch")

    # A first PAPER activation can fail after the immutable deployment SHA is
    # written but before runtime_status/portfolio/ledger state exists. Treat
    # only that narrow, independently safe residue as archival state rather
    # than pretending a trading runtime ever existed.
    failed_activation_residue = False
    if not runtime and deployed_sha:
        bootstrap = read_json(run_root / "bootstrap_receipt.json")
        bootstrap_sha = str(bootstrap.get("code_sha") or "")
        lineage = read_json(run_root / "control/cutover_lineage.json")
        ledger_path_early = run_root / "ledger/execution.jsonl"
        spool_path_early = run_root / "ledger/spool"
        bootstrap_safe = (
            bootstrap.get("schema") == "polymarket_v7_london_bootstrap_receipt_v1"
            and bootstrap.get("paper_only") is True
            and bootstrap.get("authenticated_execution") is False
            and bootstrap.get("real_order_submission") is False
            and bootstrap.get("systemd_installed_but_disabled") is True
            and SHA40.fullmatch(bootstrap_sha) is not None
            and ancestor_check(repository_root, bootstrap_sha, target_sha)
        )
        lineage_open = lineage.get("prior_open_positions")
        lineage_zero = (
            isinstance(lineage_open, dict)
            and all(int(lineage_open.get(key) or 0) == 0 for key in (
                "paper_account", "maker_active_orders", "native_open_orders",
                "native_unsettled_markets", "native_carryover_microdollars"))
        )
        lineage_safe = (
            lineage.get("schema") == "polymarket_v7_cutover_lineage_v1"
            and lineage.get("target_sha") == deployed_sha
            and lineage.get("paper_only") is True
            and lineage.get("authenticated_execution") is False
            and lineage.get("real_order_submission") is False
            and int(lineage.get("ledger_rows") or 0) == 0
            and lineage.get("native_carryover") is None
            and lineage_zero
            and ancestor_check(repository_root, deployed_sha, target_sha)
        )
        failed_activation_residue = (
            supervisor.get("schema") == "polymarket_v7_supervisor_status_v1"
            and supervisor.get("expected_sha") == deployed_sha
            and supervisor.get("state") in {"failed", "restart_budget_cooldown", "stopped"}
            and supervisor.get("paper_only") is True
            and supervisor.get("authenticated_execution") is False
            and supervisor.get("real_order_submission") is False
            and supervisor.get("p0_full_stack_ready") is not True
            and int(supervisor.get("child_pid") or 0) == 0
            and (bootstrap_safe or lineage_safe)
            and not (run_root / "control/KILL").exists()
            and not read_json(run_root / "control/portfolio_state.json")
            and not read_json(run_root / "control/native_engine_manager_status.json")
            and not read_json(run_root / "external_fair/paper_router_status.json")
            and not read_json(run_root / "micro_maker/authorized_make_executor_status.json")
            and (not ledger_path_early.exists() or ledger_path_early.stat().st_size == 0)
            and not (spool_path_early.exists() and any(spool_path_early.glob("*.json")))
        )
        if not failed_activation_residue:
            raise CutoverArchiveError("prior_runtime_safety_contract_invalid")

    if previous_sha == target_sha and not failed_activation_residue:
        _, _, model_sha_counts, _ = validate_ledger(
            run_root / "ledger/execution.jsonl", repository_root, target_sha, ancestor_check,
        )
        if any(model_sha != target_sha for model_sha in model_sha_counts):
            raise CutoverArchiveError("same_sha_ledger_mismatch")
        return {"state": "SAME_SHA_RECOVERY", "target_sha": target_sha, "archived": False}
    if not ancestor_check(repository_root, previous_sha, target_sha):
        raise CutoverArchiveError("previous_runtime_not_ancestor_of_target")
    if (
        not failed_activation_residue
        and (
            runtime.get("paper_only") is not True
            or runtime.get("authenticated_execution") is not False
            or runtime.get("real_order_submission") is not False
        )
    ):
        raise CutoverArchiveError("prior_runtime_safety_contract_invalid")

    portfolio = read_json(run_root / "control/portfolio_state.json")
    if portfolio:
        if (portfolio.get("paper_only") is not True
                or portfolio.get("authenticated_execution") is not False
                or portfolio.get("real_order_submission") is not False
                or portfolio.get("killed") is True):
            raise CutoverArchiveError("prior_portfolio_state_invalid")
        try:
            drawdown = float(portfolio.get("drawdown") or 0.0)
            maximum = float(portfolio.get("max_drawdown") or 0.15)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CutoverArchiveError("prior_portfolio_drawdown_invalid") from exc
        if drawdown >= maximum:
            raise CutoverArchiveError("prior_portfolio_drawdown_limit")

    # Position authority is architecture-specific. Legacy PAPER generations
    # used the external paper router + maker executor. Native generations own
    # inventory/OMS inside the single native engine and canonical ledger.
    account = read_json(run_root / "external_fair/paper_router_status.json")
    executor = read_json(run_root / "micro_maker/authorized_make_executor_status.json")
    native = read_json(run_root / "control/native_engine_manager_status.json")
    native_mode = (
        not failed_activation_residue
        and native.get("schema") == "polymarket_v7_native_engine_manager_status_v1"
        and native.get("model_sha") == previous_sha
        and native.get("paper_only") is True
        and native.get("authenticated_execution") is False
        and native.get("real_order_submission") is False
        and native.get("real_capital_at_risk") is False
        and native.get("single_native_portfolio_owner") is True
        and runtime.get("single_execution_owner") is True
        and runtime.get("global_portfolio_coordinator") == "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"
        and runtime.get("execution_authority") == "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"
        and runtime.get("economic_engines") == ["CRYPTO_SETTLEMENT_ENGINE"]
    )

    account_open = pending_maker = active_maker = 0
    native_open_count = 0
    native_unclean_stop = False
    if failed_activation_residue:
        if account or executor or native:
            raise CutoverArchiveError("failed_activation_position_state_present")
    elif native_mode:
        try:
            active_workers = int(native.get("active_worker_count") or 0)
            engine_pid = int(native.get("engine_pid") or 0)
            engine_pids = [int(pid) for pid in (native.get("engine_pids") or [])]
            worker_pids = [
                int((row or {}).get("pid") or 0)
                for row in (native.get("workers") or [])
                if isinstance(row, dict)
            ]
        except (TypeError, ValueError, OverflowError) as exc:
            raise CutoverArchiveError("prior_native_manager_state_invalid") from exc
        tracked_pids = sorted({
            pid for pid in [engine_pid, *engine_pids, *worker_pids] if pid > 0
        })
        live_tracked_pids = [pid for pid in tracked_pids if pid_alive(pid)]
        if live_tracked_pids:
            raise CutoverArchiveError("prior_native_manager_not_stopped")
        # A nonzero worker count without any recorded process identities cannot
        # be proven quiescent, so remain fail-closed. If process identities are
        # present and all are dead, the manager status may simply be stale after
        # systemd killed the service cgroup; continue as an unclean-but-quiescent
        # stop and still enforce open-order / ledger / carryover guards below.
        if active_workers != 0 and not tracked_pids:
            raise CutoverArchiveError("prior_native_manager_not_stopped")
        manager_state = str(native.get("state") or "")
        if manager_state != "STOPPED":
            if manager_state not in {
                "STARTING", "RUNNING", "LAUNCH_BLOCKED", "SETTLEMENT_BLOCKED",
                "SETTLING", "DRAINING",
            }:
                raise CutoverArchiveError("prior_native_manager_state_invalid")
            native_unclean_stop = True
        native_open = open_native_orders(run_root, previous_sha)
        native_open_count = len(native_open)
        if native_open_count:
            raise CutoverArchiveError(f"prior_native_open_orders:{native_open_count}")
        if bool(account) != bool(executor):
            raise CutoverArchiveError("prior_position_state_incomplete_legacy_surface")
        if account and executor:
            for name, value in (("paper_account", account), ("maker_executor", executor)):
                if (value.get("paper_only") is not True
                        or value.get("authenticated_execution") is not False
                        or value.get("real_order_submission") is not False):
                    raise CutoverArchiveError(f"prior_position_state_unsafe:{name}")
            if account.get("model_sha") not in (None, "", previous_sha):
                raise CutoverArchiveError("prior_paper_account_sha_mismatch")
            if executor.get("model_sha") != previous_sha:
                raise CutoverArchiveError("prior_maker_executor_sha_mismatch")
            try:
                account_open = int(account.get("open_positions") or 0)
                pending_maker = int(account.get("pending_maker_orders") or 0)
                active_maker = int(executor.get("active_orders") or 0)
            except (TypeError, ValueError, OverflowError) as exc:
                raise CutoverArchiveError("prior_open_positions_invalid") from exc
    else:
        for name, value in (("paper_account", account), ("maker_executor", executor)):
            if not value:
                raise CutoverArchiveError(f"prior_position_state_missing:{name}")
            if (value.get("paper_only") is not True
                    or value.get("authenticated_execution") is not False
                    or value.get("real_order_submission") is not False):
                raise CutoverArchiveError(f"prior_position_state_unsafe:{name}")
        if account.get("model_sha") not in (None, "", previous_sha):
            raise CutoverArchiveError("prior_paper_account_sha_mismatch")
        if executor.get("model_sha") != previous_sha:
            raise CutoverArchiveError("prior_maker_executor_sha_mismatch")
        try:
            account_open = int(account.get("open_positions") or 0)
            pending_maker = int(account.get("pending_maker_orders") or 0)
            active_maker = int(executor.get("active_orders") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CutoverArchiveError("prior_open_positions_invalid") from exc
    if account_open < 0 or pending_maker < 0 or active_maker < 0:
        raise CutoverArchiveError("prior_open_positions_invalid")
    if account_open or pending_maker or active_maker:
        raise CutoverArchiveError(
            f"prior_open_positions:account={account_open},pending_maker={pending_maker},active_maker={active_maker}")

    ledger_path = run_root / "ledger/execution.jsonl"
    spool_path = run_root / "ledger/spool"
    ledger_rows, ledger_sha256, ledger_model_sha_counts, ledger_strategy_counts = validate_ledger(
        ledger_path, repository_root, target_sha, ancestor_check,
    )
    native_unsettled = native_unsettled_markets(ledger_path)
    inherited_carryover = read_json(run_root / "control/native_carryover_exposure.json")
    legacy_supported = legacy_carry_check(repository_root)
    legacy_carry_required = bool(native_unsettled or inherited_carryover) and not allow_native_carryover
    if inherited_carryover and not allow_native_carryover and not legacy_supported:
        raise CutoverArchiveError("prior_native_carryover_present")
    if native_unsettled and not allow_native_carryover and not legacy_supported:
        raise CutoverArchiveError(f"prior_native_unsettled_markets:{len(native_unsettled)}")
    carryover = None
    if allow_native_carryover and (native_unsettled or inherited_carryover):
        carryover = build_native_carryover(
            ledger_path, inherited_carryover, previous_sha, target_sha
        )
    if spool_path.exists() and any(spool_path.glob("*.json")):
        raise CutoverArchiveError("prior_ledger_spool_not_empty")
    durable_open = {
        "paper_account": account_open,
        "maker_active_orders": active_maker,
        "native_open_orders": native_open_count,
        "native_unsettled_markets": len(native_unsettled),
        "native_carryover_microdollars": 0 if carryover is None else carryover["total_unsettled_microdollars"],
    }

    archived_at = int(now if now is not None else time.time())
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / f"cutover-{previous_sha}-{archived_at}-{os.getpid()}"
    if destination.exists():
        raise CutoverArchiveError("archive_destination_exists")
    os.replace(run_root, destination)
    run_root.mkdir(parents=True)
    # Research history belongs to the data epoch, not the executable SHA. Move
    # it back intact (including SQLite WAL) while the old generation is stopped.
    permanent = destination / 'research/hft_permanent'
    if permanent.exists():
        if permanent.is_symlink() or not permanent.is_dir():
            raise CutoverArchiveError('unsafe_permanent_research_history')
        parent_info=permanent.parent.stat()
        (run_root / 'research').mkdir(exist_ok=True)
        os.chown(run_root / 'research',parent_info.st_uid,parent_info.st_gid)
        os.replace(permanent, run_root / 'research/hft_permanent')
    control = run_root / "control"
    control.mkdir()
    if carryover is not None:
        carryover["source_archives"] = sorted(set([*carryover["source_archives"], str(destination)]))
        temporary_carry = control / f"native_carryover_exposure.json.tmp.{os.getpid()}"
        temporary_carry.write_text(json.dumps(carryover, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary_carry, control / "native_carryover_exposure.json")

    receipt = {
        "schema": "polymarket_v7_cutover_lineage_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "previous_runtime_sha": previous_sha,
        "target_sha": target_sha,
        "archived_at": archived_at,
        "archive_path": str(destination),
        "ledger_rows": ledger_rows,
        "ledger_sha256": ledger_sha256,
        "ledger_model_sha_counts": ledger_model_sha_counts,
        "ledger_strategy_counts": ledger_strategy_counts,
        "runtime_checkout_drift_detected": runtime_checkout_drift,
        "prior_inventory_contract": (
            "FAILED_ACTIVATION_NO_RUNTIME" if failed_activation_residue
            else "NATIVE_LEDGER_SINGLE_OWNER" if native_mode
            else "LEGACY_PAPER_ACCOUNT_AND_MAKER_EXECUTOR"
        ),
        "prior_failed_activation_residue": failed_activation_residue,
        "prior_native_unclean_stop": native_unclean_stop if native_mode else False,
        "prior_open_positions": durable_open,
        "native_carryover": None if carryover is None else {
            "total_unsettled_microdollars": carryover["total_unsettled_microdollars"],
            "context_claims_microdollars": carryover["context_claims_microdollars"],
            "market_count": len(carryover["markets"]),
        },
        "legacy_claim_carry_required": legacy_carry_required,
        "legacy_native_unsettled": native_unsettled,
    }
    temporary = control / f"cutover_lineage.json.tmp.{os.getpid()}"
    temporary.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, control / "cutover_lineage.json")
    return {"state": "ARCHIVED_PRIOR_SHA", "archived": True, **receipt}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--allow-native-carryover", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(
        args.run_root, args.archive_root, args.repository_root, args.target_sha,
        allow_native_carryover=args.allow_native_carryover,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
