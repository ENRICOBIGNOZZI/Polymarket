#!/usr/bin/env python3
"""Single-writer transport and authority firewall for the V7 ledger.

Only this module drains records into ``ledger/execution.jsonl``. Engine
component candidates are diverted to the global opportunity coordinator;
zero-authority research is diverted to its own evidence plane; component
order/fill/PnL events without a coordinator receipt are quarantined.  The spool
therefore cannot be used as a component-to-ledger authority bypass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from v7_fast_forward_ipc import BoundedUnixRequestBridge, request as unix_request

from v7_execution_ledger import (
    CanonicalLedgerWriter,
    EconomicJournalEntry,
    LedgerContractError,
    LedgerEvent,
    canonical_ledger_path,
    iter_records,
)


ENGINE_IDS = {"CRYPTO_SETTLEMENT_ENGINE"}
LEDGER_IPC_REQUEST_SCHEMA = "polymarket_v7_ledger_append_request_v1"
LEDGER_IPC_ACK_SCHEMA = "polymarket_v7_ledger_append_ack_v1"
MULTI_FORWARD_FIELDS = {
    "mode", "experiment_id", "protocol_hash", "feature_schema_hash", "model_hash",
    "fill_model_hash", "cost_model_hash", "settlement_semantic_hash",
    "latency_profile_id", "asset", "horizon", "research_only", "automatic_promotion",
    "one_entry_per_market", "hold_to_settlement", "entry_uses_absolute_fair",
    "probability_source",
}

CANDIDATE_EVENTS = {"CANDIDATE", "OPPORTUNITY"}
RISK_CREATING_EVENTS = {"ORDER_SUBMITTED", "FILL", "INVENTORY_SPLIT"}


def _hash64(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)


def _valid_multi_forward_packet(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != MULTI_FORWARD_FIELDS:
        return False
    return (
        value.get("mode") == "PAPER_MULTI_CRYPTO_FORWARD"
        and isinstance(value.get("experiment_id"), str) and bool(value["experiment_id"])
        and all(_hash64(value.get(name)) for name in (
            "protocol_hash", "feature_schema_hash", "model_hash", "fill_model_hash",
            "cost_model_hash", "settlement_semantic_hash"))
        and isinstance(value.get("latency_profile_id"), str) and bool(value["latency_profile_id"])
        and value.get("asset") in {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}
        and value.get("horizon") in {"M5", "M15"}
        and value.get("research_only") is True and value.get("automatic_promotion") is False
        and value.get("one_entry_per_market") is True and value.get("hold_to_settlement") is True
        and value.get("entry_uses_absolute_fair") is False
        and value.get("probability_source") == "POLYMARKET_PRIOR_ONLY"
    )


def _event_hash(event: LedgerEvent) -> str:
    payload = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def ledger_ipc_request(event: LedgerEvent) -> dict[str, Any]:
    event.validate()
    return {
        "schema": LEDGER_IPC_REQUEST_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "record": event.to_dict(),
    }

LEDGER_EVENT_CAUSAL_PRIORITY = {
    "CAPITAL_RESERVE": 10,
    "INVENTORY_SPLIT_REQUESTED": 20,
    "INVENTORY_SPLIT": 30,
    "ORDER_SUBMITTED": 40,
    "ORDER_STATE": 50,
    "FILL": 60,
    "POSITION_MARK": 70,
    "MARKOUT": 80,
    "EXIT": 90,
    "INVENTORY_MERGE": 100,
    "INVENTORY_LIQUIDATION": 110,
    "FINAL": 120,
    "CAPITAL_RELEASE": 130,
}


def _causal_append_key(
    item: tuple[Path, LedgerEvent | EconomicJournalEntry],
) -> tuple[int, int, str]:
    path, record = item
    if isinstance(record, LedgerEvent):
        priority = LEDGER_EVENT_CAUSAL_PRIORITY.get(record.event_type, 75)
        return (record.recorded_ts_ms, priority, path.name)
    return (record.observed_ts_ms, 75, path.name)


def _atomic_payload(directory: Path, name: str, value: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    temporary = target.with_name(target.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def _coordinator_receipt_valid(event: LedgerEvent, engine_id: str) -> bool:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    receipt = metadata.get("coordinator_receipt")
    if not isinstance(receipt, dict):
        return False
    selected = str(receipt.get("selected_replay_key") or "")
    reservation = metadata.get("reservation_projection")
    if isinstance(reservation, dict):
        request = reservation.get("request")
        if not isinstance(request, dict) or selected != str(request.get("coordinator_replay_key") or ""):
            return False
    else:
        bound = {str(value) for value in (event.opportunity_id, event.candidate_id) if isinstance(value, str) and value}
        if bound and selected not in bound:
            return False
    action = str(event.intended_action or receipt.get("action") or "").upper()
    context = receipt.get("crypto_context") if isinstance(receipt.get("crypto_context"), dict) else {}
    paper_base = (
        engine_id == "CRYPTO_SETTLEMENT_ENGINE"
        and metadata.get("paper_exploration") is True
        and metadata.get("economic_authority") == "PAPER_EXPLORATION"
        and receipt.get("paper_exploration_authorized") is True
        and receipt.get("new_risk_authorized") is False
        and receipt.get("paper_only") is True
        and receipt.get("authenticated_execution") is False
        and receipt.get("real_order_submission") is False
        and receipt.get("real_capital_at_risk") is False
        and context.get("authority") == "PAPER_EXPLORATION"
    )
    legacy_paper = (
        paper_base
        and context.get("asset") == "BTC" and context.get("horizon") == "M5"
        and (
            (metadata.get("paper_bootstrap_probe") is True
             and receipt.get("paper_exploration_probe_authorized") is True)
            or
            (metadata.get("paper_bootstrap_probe") is not True
             and receipt.get("paper_exploration_probe_authorized") is not True)
        )
    )
    packet = metadata.get("multi_crypto_forward")
    receipt_packet = receipt.get("multi_crypto_forward")
    multi_crypto_paper = (
        paper_base
        and metadata.get("paper_multi_crypto_forward") is True
        and receipt.get("paper_multi_crypto_forward_authorized") is True
        and selected in {value for value in (event.opportunity_id, event.candidate_id) if isinstance(value, str) and value}
        and _valid_multi_forward_packet(packet) and packet == receipt_packet
        and context.get("asset") == packet.get("asset")
        and context.get("horizon") == packet.get("horizon")
        and packet.get("mode") == "PAPER_MULTI_CRYPTO_FORWARD"
    )
    return (
        receipt.get("schema") == "polymarket_v7_global_opportunity_decision_v1"
        and receipt.get("owner") == "V7_GLOBAL_PORTFOLIO_COORDINATOR"
        and receipt.get("engine_id") == engine_id
        and isinstance(receipt.get("selected_replay_key"), str)
        and bool(receipt.get("selected_replay_key"))
        and receipt.get("action") == action
        and (
            event.event_type not in RISK_CREATING_EVENTS
            or receipt.get("new_risk_authorized") is True
            or legacy_paper
            or multi_crypto_paper
        )
    )


def _authority_route(run_root: Path, event: LedgerEvent) -> str:
    """Return APPEND after routing every non-canonical authority surface."""
    strategy = event.strategy.upper()
    payload = event.to_dict()
    filename = f"{event.recorded_ts_ms:013d}.{event.record_id}.json"
    if strategy not in ENGINE_IDS:
        _atomic_payload(run_root / "opportunities" / "quarantine", filename, payload)
        return "QUARANTINED"
    engine_id = strategy
    if event.event_type in CANDIDATE_EVENTS:
        # Current producers publish canonical OpportunityEnvelope objects directly.
        # A candidate arriving through the ledger spool is a non-canonical authority path.
        _atomic_payload(run_root / "opportunities" / "quarantine", filename, payload)
        return "QUARANTINED"
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    if _coordinator_receipt_valid(event, engine_id):
        return "APPEND"
    evidence_only = (
        metadata.get("counterfactual") is True
        or metadata.get("economic_authority") == "SHADOW_COUNTERFACTUAL"
        or metadata.get("execution_authority") == "SHADOW_ZERO_AUTHORITY"
        or metadata.get("authority") == "SHADOW_ZERO_AUTHORITY"
    )
    destination = "shadow_evidence" if evidence_only else "quarantine"
    _atomic_payload(run_root / "opportunities" / destination, filename, payload)
    return "SHADOW_EVIDENCE" if evidence_only else "QUARANTINED"


def spool_dir(run_root: Path) -> Path:
    return Path(run_root) / "ledger" / "spool"


def spool_event(run_root: Path, event: LedgerEvent) -> Path:
    event.validate()
    directory = spool_dir(run_root)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{event.recorded_ts_ms:013d}.{event.record_id}.json"
    target = directory / name
    temporary = target.with_name(target.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(event.to_dict(), sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def spool_events(run_root: Path, events: Iterable[LedgerEvent]) -> list[Path]:
    return [spool_event(run_root, event) for event in events]


def spool_journal_entry(run_root: Path, entry: EconomicJournalEntry) -> Path:
    """Queue an unsealed monetary fact for the same canonical writer.

    The router, rather than a data collector, establishes its hash-chain link.
    """
    entry.validate(sealed=False)
    directory = spool_dir(run_root)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"journal.{entry.observed_ts_ms:013d}.{entry.entry_id}.json"
    target = directory / name
    temporary = target.with_name(target.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(entry.to_spool_dict(), sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def _existing_record_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    if not path.exists():
        return ids
    for record in iter_records(path):
        if isinstance(record, LedgerEvent):
            ids.add(record.record_id)
        else:
            ids.add(f"journal:{record.entry_id}")
    return ids


def _existing_event_hashes(path: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not path.exists():
        return hashes
    for record in iter_records(path):
        if isinstance(record, LedgerEvent):
            hashes[record.record_id] = _event_hash(record)
    return hashes


def _ledger_record_hash(path: Path, record_id: str) -> str | None:
    if not path.exists():
        return None
    for record in iter_records(path):
        if isinstance(record, LedgerEvent) and record.record_id == record_id:
            return _event_hash(record)
    return None


def _drain_with_existing(
    run_root: Path,
    *,
    model_sha: str,
    existing: set[str],
    writer_id: str,
) -> dict[str, int]:
    """Drain one atomic batch using an already synchronized record-id cache.

    The cache is safe only for the canonical single-writer process. It removes
    the previous O(total-ledger-size) rescan from every 100ms transport cycle,
    while the ledger ownership lock is still acquired only for the actual append
    batch so an unclean process stop cannot leave a long-lived writer lock by
    design.
    """
    root = Path(run_root)
    directory = spool_dir(root)
    ledger_path = canonical_ledger_path(root)
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    appended = 0
    duplicates = 0
    rejected = 0
    routed_opportunities = 0
    routed_research = 0
    routed_shadow = 0
    quarantined = 0
    events: list[tuple[Path, LedgerEvent | EconomicJournalEntry]] = []
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            event: LedgerEvent | EconomicJournalEntry
            if isinstance(raw, dict) and raw.get("record_kind") == "ECONOMIC_JOURNAL":
                event = EconomicJournalEntry.from_spool_dict(raw)
            else:
                event = LedgerEvent.from_dict(raw)
            if event.model_sha != model_sha:
                raise LedgerContractError("spool:mixed_model_sha")
        except (OSError, json.JSONDecodeError, LedgerContractError):
            rejected += 1
            continue
        record_key = event.record_id if isinstance(event, LedgerEvent) else f"journal:{event.entry_id}"
        if record_key in existing:
            duplicates += 1
            path.unlink(missing_ok=True)
            continue
        if isinstance(event, LedgerEvent):
            route = _authority_route(root, event)
            if route != "APPEND":
                path.unlink()
                existing.add(event.record_id)
                if route == "OPPORTUNITY_INGRESS":
                    routed_opportunities += 1
                elif route == "RESEARCH_EVIDENCE":
                    routed_research += 1
                elif route == "SHADOW_EVIDENCE":
                    routed_shadow += 1
                else:
                    quarantined += 1
                    rejected += 1
                continue
        events.append((path, event))

    if events:
        events.sort(key=_causal_append_key)
        with CanonicalLedgerWriter(ledger_path, writer_id=writer_id, model_sha=model_sha) as writer:
            for path, event in events:
                if isinstance(event, LedgerEvent):
                    writer.append(event)
                    existing.add(event.record_id)
                else:
                    writer.append_journal(event)
                    existing.add(f"journal:{event.entry_id}")
                path.unlink()
                appended += 1
    return {
        "queued": len(files), "appended": appended, "duplicates": duplicates,
        "rejected": rejected, "routed_opportunities": routed_opportunities,
        "routed_research": routed_research, "routed_shadow": routed_shadow,
        "quarantined": quarantined,
    }


def drain_spool(
    run_root: Path,
    *,
    model_sha: str,
    writer_id: str = "v7-canonical-ledger-router",
) -> dict[str, int]:
    root = Path(run_root)
    existing = _existing_record_ids(canonical_ledger_path(root))
    return _drain_with_existing(
        root,
        model_sha=model_sha,
        existing=existing,
        writer_id=writer_id,
    )


def append_ledger_ipc_request(
    run_root: Path, raw: dict[str, Any], *, model_sha: str,
    existing: set[str], existing_hashes: dict[str, str],
    writer_id: str = "v7-canonical-ledger-router",
) -> dict[str, Any]:
    if (not isinstance(raw, dict) or set(raw) != {
            "schema", "paper_only", "authenticated_execution", "real_order_submission", "record"}
            or raw.get("schema") != LEDGER_IPC_REQUEST_SCHEMA
            or raw.get("paper_only") is not True
            or raw.get("authenticated_execution") is not False
            or raw.get("real_order_submission") is not False):
        raise LedgerContractError("ledger_ipc:request_contract_invalid")
    event = LedgerEvent.from_dict(raw["record"])
    if event.model_sha != model_sha:
        raise LedgerContractError("ledger_ipc:mixed_model_sha")
    record_hash = _event_hash(event)
    if event.record_id in existing:
        known = existing_hashes.get(event.record_id)
        if known is None:
            known = _ledger_record_hash(canonical_ledger_path(run_root), event.record_id)
            if known is not None:
                existing_hashes[event.record_id] = known
        if known != record_hash:
            raise LedgerContractError("ledger_ipc:record_id_conflict")
        return {
            "schema": LEDGER_IPC_ACK_SCHEMA, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "durable": True, "duplicate": True, "record_id": event.record_id,
            "record_hash": record_hash,
        }
    route = _authority_route(Path(run_root), event)
    if route != "APPEND":
        raise LedgerContractError(f"ledger_ipc:authority_route:{route}")
    with CanonicalLedgerWriter(canonical_ledger_path(run_root), writer_id=writer_id, model_sha=model_sha) as writer:
        writer.append(event)
    existing.add(event.record_id)
    existing_hashes[event.record_id] = record_hash
    return {
        "schema": LEDGER_IPC_ACK_SCHEMA, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "durable": True, "duplicate": False, "record_id": event.record_id,
        "record_hash": record_hash,
    }


def append_event_via_ledger_ipc(socket_path: Path, event: LedgerEvent, *, timeout_seconds: float = 1.0) -> dict[str, Any]:
    expected_hash = _event_hash(event)
    response = unix_request(socket_path, ledger_ipc_request(event), timeout_seconds=timeout_seconds)
    if (response.get("schema") != LEDGER_IPC_ACK_SCHEMA
            or response.get("durable") is not True
            or response.get("record_id") != event.record_id
            or response.get("record_hash") != expected_hash
            or response.get("paper_only") is not True
            or response.get("authenticated_execution") is not False
            or response.get("real_order_submission") is not False):
        raise LedgerContractError("ledger_ipc:durable_ack_invalid")
    return response


def drain_spool_loop(
    run_root: Path,
    *,
    model_sha: str,
    writer_id: str = "v7-canonical-ledger-router",
    interval: float = 1.0,
    ipc_socket: Path | None = None,
    ipc_interval: float = 0.001,
    ipc_capacity: int = 128,
) -> None:
    """Run one writer process with slow file drain plus optional fast durable IPC."""
    root = Path(run_root)
    ledger_path = canonical_ledger_path(root)
    existing = _existing_record_ids(ledger_path)
    existing_hashes = _existing_event_hashes(ledger_path)
    bridge = BoundedUnixRequestBridge(ipc_socket, capacity=ipc_capacity) if ipc_socket is not None else None
    slow_interval = max(0.1, interval)
    fast_interval = max(0.001, ipc_interval)
    next_spool = time.monotonic()
    next_ipc = next_spool
    try:
        while True:
            now = time.monotonic()
            if bridge is not None and now >= next_ipc:
                bridge.drain(lambda raw: append_ledger_ipc_request(
                    root, raw, model_sha=model_sha, existing=existing,
                    existing_hashes=existing_hashes, writer_id=writer_id,
                ), max_messages=64)
                next_ipc = now + fast_interval
            now = time.monotonic()
            if now >= next_spool:
                result = _drain_with_existing(
                    root, model_sha=model_sha, existing=existing, writer_id=writer_id,
                )
                if bridge is not None:
                    result["ipc"] = bridge.snapshot()
                print(json.dumps(result, sort_keys=True), flush=True)
                next_spool = now + slow_interval
            deadline = min(next_spool, next_ipc) if bridge is not None else next_spool
            time.sleep(max(0.0002, deadline - time.monotonic()))
    finally:
        if bridge is not None:
            bridge.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Drain validated V7 strategy events into the canonical single-writer ledger")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--writer-id", default="v7-canonical-ledger-router")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--ipc-socket", type=Path)
    parser.add_argument("--ipc-interval-ms", type=float, default=1.0)
    parser.add_argument("--ipc-capacity", type=int, default=128)
    args = parser.parse_args()
    if args.loop:
        drain_spool_loop(
            args.run_root,
            model_sha=args.model_sha,
            writer_id=args.writer_id,
            interval=args.interval,
            ipc_socket=args.ipc_socket,
            ipc_interval=max(0.001, args.ipc_interval_ms / 1000.0),
            ipc_capacity=max(1, args.ipc_capacity),
        )
        return 0
    result = drain_spool(args.run_root, model_sha=args.model_sha, writer_id=args.writer_id)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())