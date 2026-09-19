#!/usr/bin/env python3
"""Historical PAPER research/recovery primitives. No execution loop or OMS authority."""
from __future__ import annotations
from v7_external_rich_model import is_paper_learning_fair
from v7_maker_accounting import project_maker, settlement_event as maker_settlement_event


import argparse
import hashlib
import gzip
import json
import math
import os
import statistics
import threading
import time
import urllib.parse
from dataclasses import dataclass
from contextlib import ExitStack,nullcontext
from collections import OrderedDict
from pathlib import Path
from typing import Any

from v7_market_common import ClobBooksClient, finite, parse_array, request_json
from v7_disk_pressure import disk_pressure_status
from v7_evidence_contract import hybrid_identity
from v7_compressed_journal import CompressedJournal,journal_paths
from v7_execution_ledger import (
    LedgerEvent, canonical_ledger_path, iter_records,
)
from v7_ledger_spool import spool_event
from v7_crypto_settlement import load_registry as load_crypto_registry, require_context

STRATEGY = "CRYPTO_SETTLEMENT_ENGINE"
COMPONENT = "crypto_informed_taker"
MODEL_VERSION = "external-fair-structural-v7-paper"
# Evidence produced by the same declared semantics may survive an exact-SHA
# cutover.  Bump this whenever forecast labels, settlement semantics or virtual
# execution economics change incompatibly.  Exact SHA still governs execution;
# this version governs only read-only SHADOW evidence pooling.
EVIDENCE_SEMANTICS_VERSION = "external-fair-settlement-evidence-v2"
HORIZONS = (1, 10, 45, 60, 300)
FORECAST_TTE_BUCKETS = (240, 180, 120, 90, 60, 45, 30, 20, 15, 10, 5)
FORECAST_BUCKET_TOLERANCE_SECONDS = 1.25
MAX_CLOB_CLOCK_SKEW_MS = 250
BOOTSTRAP_PROBABILITY_MODEL_ID = "btc_m5_same_oracle_diffusion_bootstrap_v1"


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:32]


def identity_hash(value: Any) -> bool:
    text = str(value or "")
    return len(text) in {40, 64} and all(ch in "0123456789abcdef" for ch in text)


def canonical_exploration_final_record_id(
    model_sha: str, position_id: str, fill_id: str,
) -> str:
    return stable_id(
        "PAPER_EXPLORATION_CANONICAL_FINAL_V1", model_sha, position_id, fill_id,
    )


def _paper_exploration_evidence_paths(run_root: Path) -> list[Path]:
    family_root = run_root.parent if run_root.name == "paper_v7_live" else run_root
    return [
        family_root / "paper_v7_durable" / "external_fair" / "counterfactuals.jsonl",
        run_root / "external_fair" / "counterfactuals.jsonl",
    ]


def _paper_exploration_recovery_paths(run_root: Path) -> list[Path]:
    """Exact-SHA live recovery surface.

    Every newly emitted counterfactual row is synchronously duplicated to the
    live and durable journals.  Canonical PAPER recovery therefore needs only
    the current live journal family (including its sealed segments).  Durable
    cross-SHA history remains immutable research evidence and is deliberately
    excluded from the latency/health-critical router startup path.
    """
    return [Path(run_root) / "external_fair" / "counterfactuals.jsonl"]


class _CounterfactualIndex:
    """Bounded in-memory row locators; payloads stay in their source journals.

    The execution router deliberately uses no SQL/database engine.  A compact
    Python locator map is refreshed incrementally from immutable/append-only
    evidence.  Only hashes and byte offsets are cached; JSON payloads are
    always re-read and hash-checked from their canonical source before use.
    """
    MAX_LINE_BYTES = 16 * 1024 * 1024
    GUARD_BYTES = 4096
    # Conservative logical charge per locator in addition to variable strings.
    # This is an admission budget, not a claim about CPython allocator bytes.
    LOCATOR_FIXED_BYTES = 160

    def __init__(self, paths, *, maximum_cache_bytes=256*1024**2):
        self.paths = tuple(Path(path).absolute() for path in paths)
        self.physical_paths = ()
        if maximum_cache_bytes < 65536:
            raise ValueError('counterfactual cache budget too small')
        self.maximum_cache_bytes = int(maximum_cache_bytes)
        self.records: dict[str, tuple[bytes,str,str,int,int,int,int,int,bytes]] = {}
        self.locator_bytes = 0
        self.states = {}
        self.invalid = False
        self.metrics = {
            "bytes_read": 0, "records_decoded": 0, "rebuilds": 0,
            "last_bytes_read": 0, "last_records_decoded": 0,
            "last_refresh_seconds": 0.0, 'state': 'UNINITIALIZED',
            'storage_backend': 'BOUNDED_PYTHON_LOCATOR_MAP',
            'maximum_index_bytes': self.maximum_cache_bytes,
            'index_bytes': 0, 'database_bytes': 0, 'disk_cache_bytes': 0,
        }

    def close(self):
        # Interface compatibility with the former SQLite-backed locator.
        return None

    @staticmethod
    def _file_identity(info):
        return (info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    @classmethod
    def _record_cost(cls, identity, event_type, model_sha):
        return (cls.LOCATOR_FIXED_BYTES + len(identity.encode())
                + len(event_type.encode()) + len(model_sha.encode()))

    @classmethod
    def _logical_bytes(cls, records):
        return sum(cls._record_cost(identity, row[1], row[2])
                   for identity, row in records.items())

    def _guards(self, handle, offset):
        n = min(offset, self.GUARD_BYTES)
        handle.seek(0)
        prefix = hashlib.sha256(handle.read(n)).digest()
        handle.seek(offset - n)
        suffix = hashlib.sha256(handle.read(n)).digest()
        return prefix, suffix

    def refresh(self):
        # A compression publication can replace a sealed raw pathname between
        # enumeration and open. Retry the authoritative partition inventory;
        # never publish an index that silently omitted the disappearing source.
        for attempt in range(3):
            try:
                return self._refresh_once()
            except FileNotFoundError:
                self.invalid = True
                if attempt == 2:
                    raise

    def _rotation_states(self, physical, snapshots):
        """Reuse indexed prefixes only after exact-byte verification of a move."""
        pending = {}; moves = {}; force = set(); validated = 0
        new_ranks = {path: i for i, path in enumerate(physical)}
        old_logical = {p.with_name(p.name.removesuffix('.gz')) for p in self.physical_paths}
        for old_rank, path in enumerate(self.physical_paths):
            old = self.states.get(path)
            if old is None:
                continue
            current = snapshots.get(path)
            candidates = []
            if path not in self.paths:
                candidate = path.with_name(path.name + '.gz')
                if candidate in snapshots:
                    candidates = [candidate]
            else:
                candidates = [p for p in physical if p.parent == path.parent
                    and p.name.startswith(path.name + '.segment-')
                    and p.with_name(p.name.removesuffix('.gz')) not in old_logical]
            matched = None
            for candidate in candidates:
                h = hashlib.sha256(); remaining = old['offset']
                with (gzip.open(candidate,'rb') if candidate.suffix=='.gz' else candidate.open('rb')) as stream:
                    while remaining:
                        raw = stream.read(min(1024**2, remaining))
                        if not raw:
                            break
                        h.update(raw); remaining -= len(raw); validated += len(raw)
                if remaining == 0 and h.hexdigest() == old['prefix_sha256']:
                    matched = candidate; break
            if matched is None and current and (current.st_dev,current.st_ino) == old['file_identity'][:2]:
                pending[path] = old; moves[old_rank] = new_ranks[path]; continue
            if matched is None:
                return {}, {}, set(), True, validated
            if matched in pending:
                raise RuntimeError('ambiguous counterfactual source rotation')
            pending[matched] = {**old, 'file_identity': self._file_identity(snapshots[matched])}
            moves[old_rank] = new_ranks[matched]; force.add(matched)
        return pending, moves, force, False, validated

    def _refresh_once(self):
        started = time.monotonic()
        physical = []; root_ranks = {}
        for root_rank, path in enumerate(self.paths):
            for source in journal_paths(path):
                if source not in root_ranks:
                    physical.append(source); root_ranks[source] = root_rank
        physical = tuple(physical)
        snapshots = {}
        reset = self.invalid
        for path in physical:
            try:
                if path.is_symlink():
                    raise RuntimeError(f"paper_exploration_evidence_symlink:{path}")
                info = path.stat()
                if not path.is_file():
                    raise RuntimeError(f"paper_exploration_evidence_not_file:{path}")
            except FileNotFoundError:
                raise
            snapshots[path] = info
        pending, moves, force, rotation_reset, validated = self._rotation_states(physical, snapshots)
        reset = reset or rotation_reset
        for path, info in snapshots.items():
            old = pending.get(path)
            if old is None:
                continue
            sig = self._file_identity(info); previous = old["file_identity"]
            if sig[:2] != previous[:2] or sig[2] < previous[2]:
                reset = True
            elif sig[:4] == previous[:4] and path.suffix == '.gz':
                pending[path] = {**old, 'file_identity': sig}
            elif sig[2] == previous[2] and sig[3:] != previous[3:]:
                reset = True
            elif sig != previous:
                with (gzip.open(path,'rb') if path.suffix=='.gz' else path.open('rb')) as handle:
                    if self._guards(handle, old["offset"]) != old["guards"]:
                        reset = True
        decoded = read_bytes = 0
        if reset:
            pending = {}; force = set(); working = {}
        else:
            working = dict(self.records)
            changes = {old:new for old,new in moves.items() if old != new}
            if changes:
                for identity, row in list(working.items()):
                    if row[4] in changes:
                        working[identity] = (*row[:4], changes[row[4]], *row[5:])
        try:
            for rank, path in enumerate(physical):
                info = snapshots.get(path)
                if info is None:
                    continue
                old = pending.get(path); file_identity = self._file_identity(info)
                if old is not None and old["file_identity"] == file_identity and path not in force:
                    continue
                offset = old["offset"] if old else 0
                lines = old["lines"] if old else 0
                raw_hasher = old['raw_hasher'].copy() if old else hashlib.sha256()
                compressed = path.suffix == '.gz'
                with (gzip.open(path,'rb') if compressed else path.open('rb')) as handle:
                    if self._file_identity(os.fstat(handle.fileno()))[:2] != file_identity[:2]:
                        raise RuntimeError("paper_exploration_evidence_replaced_during_read")
                    handle.seek(offset)
                    while compressed or offset < info.st_size:
                        start = offset
                        raw = handle.readline(self.MAX_LINE_BYTES+1 if compressed else
                                              min(self.MAX_LINE_BYTES+1, info.st_size-offset))
                        read_bytes += len(raw)
                        if not raw:
                            if compressed: break
                            raise RuntimeError("paper_exploration_evidence_truncated_during_read")
                        if len(raw) > self.MAX_LINE_BYTES:
                            raise RuntimeError("paper_exploration_evidence_record_too_large")
                        if not raw.endswith(b"\n"):
                            if compressed or path not in self.paths:
                                raise RuntimeError('paper_exploration_closed_evidence_incomplete_tail')
                            break
                        offset += len(raw); lines += 1; raw_hasher.update(raw)
                        if not raw.strip():
                            continue
                        try:
                            row = json.loads(raw)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise RuntimeError(f"paper_exploration_counterfactual_invalid:{path}:{lines}") from exc
                        if not isinstance(row, dict) or not row.get("record_id"):
                            raise RuntimeError(f"paper_exploration_counterfactual_shape:{path}:{lines}")
                        decoded += 1
                        identity = str(row["record_id"])
                        rendered = json.dumps(row, separators=(",", ":"), sort_keys=True)
                        canonical_digest = hashlib.sha256(rendered.encode()).digest()
                        raw_digest = hashlib.sha256(raw).digest()
                        event_type = str(row.get("event_type") or "")
                        model_sha = str(row.get("model_sha") or "")
                        stamp = int(row.get("timestamp_ms") or 0)
                        prior = working.get(identity)
                        if prior is not None:
                            if prior[0] != canonical_digest:
                                raise RuntimeError(f"paper_exploration_counterfactual_conflict:{identity}")
                            if (rank, start) < (prior[4], prior[5]):
                                working[identity] = (canonical_digest,event_type,model_sha,stamp,
                                                     rank,start,root_ranks[path],len(raw),raw_digest)
                        else:
                            working[identity] = (canonical_digest,event_type,model_sha,stamp,
                                                 rank,start,root_ranks[path],len(raw),raw_digest)
                            if self._logical_bytes(working) > self.maximum_cache_bytes:
                                raise RuntimeError('counterfactual metadata cache budget exhausted; source evidence preserved')
                    after = os.fstat(handle.fileno()); current = path.stat()
                    if ((after.st_dev, after.st_ino) != file_identity[:2]
                            or (current.st_dev, current.st_ino) != file_identity[:2]
                            or after.st_size < info.st_size):
                        raise RuntimeError("paper_exploration_evidence_changed_during_read")
                    if (after.st_size == info.st_size
                            and (after.st_mtime_ns != info.st_mtime_ns
                                 or (not compressed and after.st_ctime_ns != info.st_ctime_ns))):
                        raise RuntimeError("paper_exploration_evidence_rewritten_during_read")
                    pending[path] = {"file_identity": file_identity, "offset": offset,
                                     "lines": lines, "guards": self._guards(handle, offset),
                                     'raw_hasher': raw_hasher, 'prefix_sha256': raw_hasher.hexdigest()}
            current_paths = tuple(dict.fromkeys(source for root in self.paths for source in journal_paths(root)))
            if current_paths != physical:
                raise FileNotFoundError('counterfactual journal rotated during snapshot')
            self.records = working
            self.locator_bytes = self._logical_bytes(working)
            self.states = pending; self.physical_paths = physical; self.invalid = False
            self.metrics['state'] = 'READY'; self.metrics["rebuilds"] += int(reset)
        except Exception as exc:
            self.invalid = True
            if 'cache budget exhausted' in str(exc):
                self.metrics['state'] = 'CACHE_BUDGET_EXHAUSTED_SOURCES_PRESERVED'
            else:
                self.metrics['state'] = 'INVALID_SOURCE'
            raise
        finally:
            self.metrics["bytes_read"] += read_bytes
            self.metrics["records_decoded"] += decoded
            self.metrics["last_bytes_read"] = read_bytes
            self.metrics["last_records_decoded"] = decoded
            self.metrics["last_refresh_seconds"] = time.monotonic() - started
            self.metrics['index_bytes'] = self.locator_bytes
            self.metrics['maximum_index_bytes'] = self.maximum_cache_bytes
            self.metrics['database_bytes'] = 0
            self.metrics['disk_cache_bytes'] = 0
            self.metrics['rotation_validation_bytes'] = validated

    def iter_records(self, *, event_types=None, model_sha=None, chronological=False, root_rank_gt=None):
        self.refresh()
        allowed = set(event_types) if event_types is not None else None
        if allowed is not None and not allowed:
            return
        rows = []
        for identity, row in self.records.items():
            if allowed is not None and row[1] not in allowed:
                continue
            if model_sha is not None and row[2] != model_sha:
                continue
            if root_rank_gt is not None and row[6] <= root_rank_gt:
                continue
            rows.append((identity,row))
        rows.sort(key=(lambda item:(item[1][3],item[0])) if chronological
                  else (lambda item:(item[1][4],item[1][5])))
        with ExitStack() as stack:
            active = {}; sealed = OrderedDict(); paths = self.physical_paths
            for path in self.paths:
                if path not in self.states: continue
                handle = stack.enter_context(path.open('rb'))
                if self._file_identity(os.fstat(handle.fileno()))[:2] != self.states[path]['file_identity'][:2]:
                    self.invalid = True
                    raise RuntimeError('counterfactual source rotated before query')
                active[path] = handle
            try:
                for identity, locator in rows:
                    rank, offset, length, raw_digest = locator[4], locator[5], locator[7], locator[8]
                    path = paths[rank]
                    if path in active:
                        handle = active[path]
                    else:
                        handle = sealed.pop(path, None)
                        if handle is None:
                            source = path
                            try: handle = gzip.open(source,'rb') if source.suffix=='.gz' else source.open('rb')
                            except FileNotFoundError:
                                source = source.with_name(source.name+'.gz'); handle = gzip.open(source,'rb')
                        sealed[path] = handle
                        if len(sealed) > 8:
                            sealed.popitem(last=False)[1].close()
                    handle.seek(offset); raw = handle.read(length)
                    if len(raw) != length or not raw.endswith(b'\n'):
                        raise RuntimeError('counterfactual source record is no longer recoverable')
                    if hashlib.sha256(raw).digest() != raw_digest:
                        raise RuntimeError('counterfactual source record differs from indexed evidence')
                    yield identity, json.loads(raw)
            finally:
                for handle in sealed.values(): handle.close()


_COUNTERFACTUAL_INDEXES = {}


def _counterfactual_index(paths):
    # Only two private page caches; eviction never discards canonical evidence.
    key = (os.getpid(), tuple(str(Path(path).absolute()) for path in paths))
    if key not in _COUNTERFACTUAL_INDEXES:
        if len(_COUNTERFACTUAL_INDEXES) >= 2:
            first = next(iter(_COUNTERFACTUAL_INDEXES))
            _COUNTERFACTUAL_INDEXES.pop(first).close()
        _COUNTERFACTUAL_INDEXES[key] = _CounterfactualIndex(paths)
    return _COUNTERFACTUAL_INDEXES[key]


def _read_complete_counterfactual_records(paths, *, event_types=None, model_sha=None):
    return dict(_counterfactual_index(paths).iter_records(
        event_types=event_types, model_sha=model_sha,
    ))


def reconcile_paper_exploration_finals(
    run_root: Path, model_sha: str,
) -> dict[str, Any]:
    """Crash-safely complete canonical PAPER_EXPLORATION terminal lifecycles.

    The durable VIRTUAL_FINAL is the settlement fact. The matching canonical
    FILL supplies the coordinator receipt and exact execution identity. A
    deterministic FINAL id plus canonical/spool inspection makes retries
    idempotent across crashes before or after the ledger router appends it.
    """
    root = Path(run_root)
    records = _read_complete_counterfactual_records(
        _paper_exploration_recovery_paths(root),
        event_types=("VIRTUAL_FINAL",), model_sha=model_sha,
    )
    virtual_finals: dict[str, dict[str, Any]] = {}
    for row in records.values():
        if (
            row.get("event_type") == "VIRTUAL_FINAL"
            and row.get("model_sha") == model_sha
            and row.get("paper_only") is True
            and row.get("authenticated_execution") is False
            and row.get("real_order_submission") is False
            and row.get("execution_authority") == "SHADOW_ZERO_AUTHORITY"
            and row.get("fill_id")
        ):
            virtual_finals.setdefault(str(row["fill_id"]), row)

    existing_record_ids: set[str] = set()
    terminal_positions: set[str] = set()
    fills: dict[str, LedgerEvent] = {}
    ledger = canonical_ledger_path(root)
    if ledger.is_file():
        for record in iter_records(ledger):
            if not isinstance(record, LedgerEvent) or record.model_sha != model_sha:
                continue
            existing_record_ids.add(record.record_id)
            if (record.strategy.upper() != STRATEGY
                    or (record.metadata or {}).get("component") != COMPONENT
                    or (record.metadata or {}).get("paper_forward_test") is True):
                continue
            if record.event_type == "FILL" and record.fill_id:
                fills.setdefault(record.fill_id, record)
            elif (
                record.event_type == "FINAL"
                and record.position_id
                and record.metadata.get("paper_exploration") is True
                and record.metadata.get("economic_authority") == "PAPER_EXPLORATION"
                and record.metadata.get("counterfactual") is False
                and record.metadata.get("excluded_from_portfolio_equity") is False
            ):
                terminal_positions.add(record.position_id)

    spool_invalid = 0
    spool = root / "ledger" / "spool"
    for item in sorted(spool.glob("*.json")) if spool.is_dir() else []:
        try:
            raw = json.loads(item.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("record_kind") == "ECONOMIC_JOURNAL":
                continue
            record = LedgerEvent.from_dict(raw)
        except Exception:
            spool_invalid += 1
            continue
        if record.model_sha != model_sha:
            continue
        existing_record_ids.add(record.record_id)
        if (record.strategy.upper() != STRATEGY
                or (record.metadata or {}).get("component") != COMPONENT
                or (record.metadata or {}).get("paper_forward_test") is True):
            continue
        if record.event_type == "FILL" and record.fill_id:
            fills.setdefault(record.fill_id, record)
        elif (
            record.event_type == "FINAL"
            and record.position_id
            and record.metadata.get("paper_exploration") is True
            and record.metadata.get("economic_authority") == "PAPER_EXPLORATION"
            and record.metadata.get("counterfactual") is False
            and record.metadata.get("excluded_from_portfolio_equity") is False
        ):
            terminal_positions.add(record.position_id)

    spooled = 0
    missing_fill: list[str] = []
    invalid_final: list[str] = []
    for fill_id, virtual_final in sorted(virtual_finals.items()):
        position_id = str(virtual_final.get("position_id") or "")
        if not position_id:
            invalid_final.append(fill_id)
            continue
        final_id = canonical_exploration_final_record_id(
            model_sha, position_id, fill_id
        )
        if final_id in existing_record_ids or position_id in terminal_positions:
            continue
        fill = fills.get(fill_id)
        if fill is None:
            missing_fill.append(fill_id)
            continue
        fill_metadata = dict(fill.metadata)
        receipt = fill_metadata.get("coordinator_receipt")
        if (
            fill_metadata.get("paper_exploration") is not True
            or fill_metadata.get("economic_authority") != "PAPER_EXPLORATION"
            or not isinstance(receipt, dict)
        ):
            missing_fill.append(fill_id)
            continue
        pnl = finite(virtual_final.get("counterfactual_pnl"), math.nan)
        payout = finite(virtual_final.get("virtual_cashflow"), math.nan)
        shares = finite(fill.filled_size, math.nan)
        entry_price = finite(fill.fill_price, math.nan)
        entry_fee = finite(fill.fee, math.nan)
        settlement = virtual_final.get("metadata")
        settlement = settlement if isinstance(settlement, dict) else {}
        if (
            not math.isfinite(pnl)
            or not math.isfinite(payout) or payout < 0.0
            or not math.isfinite(shares) or shares <= 0.0
            or not math.isfinite(entry_price) or not 0.0 <= entry_price <= 1.0
            or not math.isfinite(entry_fee) or entry_fee < 0.0
        ):
            invalid_final.append(fill_id)
            continue
        entry_debit = shares * entry_price + entry_fee
        won = settlement.get("won")
        expected_payout = shares if won is True else 0.0 if won is False else None
        if (
            payout > shares + 1e-7
            or abs(pnl - (payout - entry_debit)) > 1e-7
            or (expected_payout is not None and abs(payout - expected_payout) > 1e-7)
        ):
            invalid_final.append(fill_id)
            continue
        recorded_ms = max(
            int(virtual_final.get("timestamp_ms") or 0),
            int(fill.recorded_ts_ms) + 1,
        )
        duration = virtual_final.get("capital_duration_ms")
        try:
            duration_ms = max(0, int(duration)) if duration is not None else None
        except (TypeError, ValueError, OverflowError):
            duration_ms = None
        metadata = {
            **fill_metadata,
            "paper_exploration": True,
            "economic_authority": "PAPER_EXPLORATION",
            "counterfactual": False,
            "excluded_from_portfolio_equity": False,
            "research_evidence_only": False,
            "canonical_terminal_reconciled_from": "VIRTUAL_FINAL",
            "virtual_final_record_id": str(virtual_final.get("record_id") or ""),
            "realized": True,
            "unwind_accounted": True,
            "cost_vector_complete": True,
            "terminal_id": f"paper-exploration:{position_id}:final",
            "settlement_outcome": settlement.get("settlement_outcome"),
            "winning_token_id": settlement.get("winning_token_id"),
            "won": settlement.get("won"),
            "hold_to_settlement": settlement.get("hold_to_settlement") is True,
            "entry_debit": entry_debit,
            "settlement_payout": payout,
            "cash_identity_verified": True,
            "pnl_decomposition": {
                "trading_pnl": pnl,
                "spread_capture": 0.0,
                "adverse_markout": 0.0,
                "inventory_pnl": 0.0,
                "maker_rebates": 0.0,
                "liquidity_rewards": 0.0,
                "own_reward_share_verified": False,
            },
        }
        spool_event(root, LedgerEvent(
            event_type="FINAL", strategy=fill.strategy, model_sha=model_sha,
            model_version=fill.model_version, record_id=final_id,
            recorded_ts_ms=recorded_ms, opportunity_id=fill.opportunity_id,
            candidate_id=fill.candidate_id,
            order_id=fill.order_id, fill_id=fill_id, position_id=position_id,
            market_id=fill.market_id, event_id=fill.event_id,
            token_id=fill.token_id, side=fill.side, intended_action="TAKE",
            final_pnl=pnl, realized_cashflow=payout, fee=0.0, slippage=0.0,
            unwind_loss=0.0, capital_cost=0.0, latency_cost=0.0,
            capital_duration_ms=duration_ms, metadata=metadata,
        ))
        existing_record_ids.add(final_id)
        terminal_positions.add(position_id)
        spooled += 1

    expected_positions = {
        str(row.get("position_id") or "") for row in virtual_finals.values()
        if row.get("position_id")
    }
    completed = len(expected_positions & terminal_positions)
    report = {
        "schema": "polymarket_v7_paper_exploration_final_reconciliation_v1",
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "expected_terminal_positions": len(expected_positions),
        "canonical_or_spooled_terminal_positions": completed,
        "spooled_this_pass": spooled,
        "missing_canonical_fills": sorted(missing_fill),
        "invalid_virtual_finals": sorted(invalid_final),
        "invalid_spool_records_observed": spool_invalid,
    }
    report["complete"] = (
        completed == len(expected_positions)
        and not missing_fill
        and not invalid_final
    )
    return report



def _canonical_paper_exploration_event(event: LedgerEvent) -> bool:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    return (
        event.strategy.upper() == STRATEGY
        and metadata.get("component") == COMPONENT
        and metadata.get("paper_exploration") is True
        and metadata.get("paper_forward_test") is not True
        and metadata.get("economic_authority") == "PAPER_EXPLORATION"
        and metadata.get("counterfactual") is False
        and metadata.get("excluded_from_portfolio_equity") is False
        and metadata.get("research_evidence_only") is False
    )


def _canonical_and_spooled_events(
    run_root: Path, model_sha: str,
) -> tuple[list[LedgerEvent], list[str]]:
    """Load exact-SHA ledger plus not-yet-drained spool without double counting."""
    root = Path(run_root)
    by_record_id: dict[str, LedgerEvent] = {}
    canonical: dict[str, str] = {}
    ledger = canonical_ledger_path(root)
    if ledger.is_file():
        for record in iter_records(ledger):
            if not isinstance(record, LedgerEvent) or record.model_sha != model_sha:
                continue
            rendered = json.dumps(
                record.to_dict(), separators=(",", ":"), sort_keys=True,
            )
            prior = canonical.get(record.record_id)
            if prior is not None and prior != rendered:
                raise RuntimeError(
                    f"paper_exploration_record_id_conflict:{record.record_id}"
                )
            canonical[record.record_id] = rendered
            by_record_id.setdefault(record.record_id, record)

    invalid_spool: list[str] = []
    spool = root / "ledger" / "spool"
    for item in sorted(spool.glob("*.json")) if spool.is_dir() else []:
        try:
            raw = json.loads(item.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("record_kind") == "ECONOMIC_JOURNAL":
                continue
            record = LedgerEvent.from_dict(raw)
        except Exception:
            invalid_spool.append(item.name)
            continue
        if record.model_sha != model_sha:
            continue
        rendered = json.dumps(
            record.to_dict(), separators=(",", ":"), sort_keys=True,
        )
        prior = canonical.get(record.record_id)
        if prior is not None and prior != rendered:
            raise RuntimeError(
                f"paper_exploration_record_id_conflict:{record.record_id}"
            )
        canonical[record.record_id] = rendered
        by_record_id.setdefault(record.record_id, record)
    events = sorted(
        by_record_id.values(), key=lambda event: (
            int(event.recorded_ts_ms), event.record_id,
        )
    )
    return events, invalid_spool


def canonical_exploration_nonfill_record_id(
    model_sha: str, order_id: str,
) -> str:
    return stable_id(
        "PAPER_EXPLORATION_CANONICAL_NONFILL_V1", model_sha, order_id,
    )


def reconcile_paper_exploration_orphan_orders(
    run_root: Path, model_sha: str, *, current_ms: int | None = None,
    orphan_age_ms: int = 1_000,
) -> dict[str, Any]:
    """Close crash-stranded virtual FAK orders without inventing a fill."""
    root = Path(run_root)
    now = now_ms() if current_ms is None else int(current_ms)
    events, invalid_spool = _canonical_and_spooled_events(root, model_sha)
    orders: dict[str, LedgerEvent] = {}
    filled_orders: set[str] = set()
    terminal_orders: set[str] = set()
    existing_ids = {event.record_id for event in events}
    conflicts: list[str] = []
    for event in events:
        if not _canonical_paper_exploration_event(event):
            continue
        order_id = str(event.order_id or "")
        if event.event_type == "ORDER_SUBMITTED" and order_id:
            prior = orders.get(order_id)
            if prior is not None and prior.record_id != event.record_id:
                conflicts.append(f"duplicate_order:{order_id}")
            else:
                orders[order_id] = event
        elif event.event_type == "FILL" and order_id:
            filled_orders.add(order_id)
        elif (
            event.event_type == "ORDER_STATE"
            and order_id
            and event.complete is True
            and event.order_state in {"CANCELLED", "EXPIRED", "REJECTED", "NONFILL"}
        ):
            terminal_orders.add(order_id)

    spooled = 0
    pending: list[str] = []
    for order_id, order in sorted(orders.items()):
        if order_id in filled_orders or order_id in terminal_orders:
            continue
        age = now - int(order.recorded_ts_ms)
        if age < orphan_age_ms:
            pending.append(order_id)
            continue
        record_id = canonical_exploration_nonfill_record_id(model_sha, order_id)
        if record_id in existing_ids:
            terminal_orders.add(order_id)
            continue
        metadata = dict(order.metadata)
        metadata.update({
            "paper_exploration": True,
            "economic_authority": "PAPER_EXPLORATION",
            "counterfactual": False,
            "excluded_from_portfolio_equity": False,
            "research_evidence_only": False,
            "recovered_after_process_interruption": True,
            "no_fill_fabricated": True,
            "terminal_id": f"paper-exploration:{order_id}:nonfill",
        })
        spool_event(root, LedgerEvent(
            event_type="ORDER_STATE", strategy=order.strategy,
            model_sha=model_sha, model_version=order.model_version,
            record_id=record_id, recorded_ts_ms=max(now, order.recorded_ts_ms + 1),
            opportunity_id=order.opportunity_id, candidate_id=order.candidate_id, order_id=order_id,
            position_id=order.position_id, market_id=order.market_id,
            event_id=order.event_id, token_id=order.token_id, side=order.side,
            intended_action=order.intended_action, intended_size=order.intended_size,
            order_state="NONFILL", complete=True,
            cancel_reason="PAPER_EXPLORATION_PROCESS_INTERRUPTED_BEFORE_FILL",
            metadata=metadata,
        ))
        existing_ids.add(record_id)
        terminal_orders.add(order_id)
        spooled += 1

    unresolved = sorted(set(orders) - filled_orders - terminal_orders)
    report = {
        "schema": "polymarket_v7_paper_exploration_order_reconciliation_v1",
        "model_sha": model_sha, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "orders": len(orders), "filled_orders": len(filled_orders & set(orders)),
        "terminal_nonfills": len(terminal_orders & set(orders)),
        "spooled_this_pass": spooled,
        "pending_within_grace": sorted(pending),
        "unresolved_orders": unresolved,
        "invalid_spool_records": sorted(invalid_spool),
        "conflicts": sorted(set(conflicts)),
    }
    report["complete"] = (
        not unresolved and not invalid_spool and not conflicts
    )
    return report


def reconstruct_paper_exploration_account(
    run_root: Path,
    model_sha: str,
    starting_capital: float,
    *,
    cached_positions: dict[str, Any] | None = None,
    prior_peak_equity: float | None = None,
) -> dict[str, Any]:
    """Rebuild the exact-SHA simulated account from canonical lifecycle facts.

    State JSON is only a cache for executable marks. ORDER/FILL/FINAL records in
    the canonical ledger plus its undrained single-writer spool own cash, PnL,
    inventory identity and counters. Any ambiguity fails closed.
    """
    if not math.isfinite(starting_capital) or starting_capital <= 0.0:
        raise RuntimeError("paper_exploration_starting_capital_invalid")
    events, invalid_spool = _canonical_and_spooled_events(run_root, model_sha)
    orders: dict[str, LedgerEvent] = {}
    fills: dict[str, LedgerEvent] = {}
    finals_by_position: dict[str, LedgerEvent] = {}
    finals_by_fill: dict[str, LedgerEvent] = {}
    order_terminals: dict[str, LedgerEvent] = {}
    issues: list[str] = []

    def bind(
        bucket: dict[str, LedgerEvent], key: str | None,
        event: LedgerEvent, label: str,
    ) -> None:
        identity = str(key or "")
        if not identity:
            issues.append(f"{label}_identity_missing:{event.record_id}")
            return
        prior = bucket.get(identity)
        if prior is not None and prior.record_id != event.record_id:
            issues.append(f"duplicate_{label}:{identity}")
            return
        bucket[identity] = event

    account_events = [
        event for event in events if _canonical_paper_exploration_event(event)
    ]
    for event in account_events:
        if event.event_type == "ORDER_SUBMITTED":
            bind(orders, event.order_id, event, "order")
        elif event.event_type == "FILL":
            bind(fills, event.fill_id, event, "fill")
        elif event.event_type == "ORDER_STATE" and event.complete is True:
            bind(order_terminals, event.order_id, event, "order_terminal")
        elif event.event_type == "FINAL":
            bind(finals_by_position, event.position_id, event, "position_final")
            bind(finals_by_fill, event.fill_id, event, "fill_final")

    filled_order_ids = {str(fill.order_id or "") for fill in fills.values()}
    for order_id in orders:
        has_fill = order_id in filled_order_ids
        has_terminal = order_id in order_terminals
        if not has_fill and not has_terminal:
            issues.append(f"order_without_fill_or_terminal:{order_id}")
        if has_fill and has_terminal:
            issues.append(f"filled_order_has_nonfill_terminal:{order_id}")
    for order_id in order_terminals:
        if order_id not in orders:
            issues.append(f"terminal_without_order:{order_id}")

    fills_by_position: dict[str, LedgerEvent] = {}
    total_entry_debit = 0.0
    total_settlement_payout = 0.0
    realized_pnl = 0.0
    terminal_positions = 0
    for fill_id, fill in fills.items():
        if not fill.order_id or fill.order_id not in orders:
            issues.append(f"fill_without_order:{fill_id}")
        else:
            order = orders[fill.order_id]
            if (
                order.position_id != fill.position_id
                or order.market_id != fill.market_id
                or order.token_id != fill.token_id
                or order.side != fill.side
            ):
                issues.append(f"order_fill_identity_mismatch:{fill_id}")
        position_id = str(fill.position_id or "")
        if not position_id:
            issues.append(f"fill_position_missing:{fill_id}")
            continue
        prior_fill = fills_by_position.get(position_id)
        if prior_fill is not None and prior_fill.fill_id != fill_id:
            issues.append(f"multiple_fills_per_position:{position_id}")
        else:
            fills_by_position[position_id] = fill
        entry_price = finite(fill.fill_price, math.nan)
        shares = finite(fill.filled_size, math.nan)
        fee = finite(fill.fee, math.nan)
        if (
            not math.isfinite(entry_price) or not 0.0 <= entry_price <= 1.0
            or not math.isfinite(shares) or shares <= 0.0
            or not math.isfinite(fee) or fee < 0.0
        ):
            issues.append(f"fill_economics_invalid:{fill_id}")
            continue
        total_entry_debit += entry_price * shares + fee

    for position_id, final in finals_by_position.items():
        fill = fills_by_position.get(position_id)
        if fill is None:
            issues.append(f"final_without_fill:{position_id}")
            continue
        if final.fill_id != fill.fill_id:
            issues.append(f"final_fill_identity_mismatch:{position_id}")
            continue
        payout = finite(final.realized_cashflow, math.nan)
        pnl = finite(final.final_pnl, math.nan)
        shares = finite(fill.filled_size, math.nan)
        entry_debit = (
            finite(fill.fill_price, math.nan) * shares
            + finite(fill.fee, math.nan)
        )
        if (
            not math.isfinite(payout) or payout < 0.0
            or not math.isfinite(pnl)
            or not math.isfinite(entry_debit)
            or not math.isfinite(shares)
        ):
            issues.append(f"final_economics_invalid:{position_id}")
            continue
        if payout > shares + 1e-7:
            issues.append(f"final_payout_above_binary_par:{position_id}")
        expected_pnl = payout - entry_debit
        if abs(pnl - expected_pnl) > 1e-7:
            issues.append(f"final_pnl_cash_identity_mismatch:{position_id}")
        won = final.metadata.get("won")
        if isinstance(won, bool):
            expected_payout = shares if won else 0.0
            if abs(payout - expected_payout) > 1e-7:
                issues.append(f"final_binary_payout_mismatch:{position_id}")
        total_settlement_payout += payout
        realized_pnl += pnl
        terminal_positions += 1

    for fill_id, final in finals_by_fill.items():
        if fill_id not in fills:
            issues.append(f"final_references_unknown_fill:{fill_id}")
        elif final.position_id not in finals_by_position:
            issues.append(f"final_position_index_missing:{fill_id}")

    cached = cached_positions if isinstance(cached_positions, dict) else {}
    evidence = _read_complete_counterfactual_records(
        _paper_exploration_recovery_paths(Path(run_root)),
        event_types=("VIRTUAL_FILL", "VIRTUAL_MARKOUT"), model_sha=model_sha,
    )
    virtual_fills: dict[str, dict[str, Any]] = {}
    markout_horizons: dict[str, set[int]] = {}
    for row in evidence.values():
        if row.get("model_sha") != model_sha:
            continue
        fill_id = str(row.get("fill_id") or "")
        if row.get("event_type") == "VIRTUAL_FILL" and fill_id:
            virtual_fills.setdefault(fill_id, row)
        elif row.get("event_type") == "VIRTUAL_MARKOUT" and fill_id:
            for key in (row.get("markouts") or {}):
                try:
                    markout_horizons.setdefault(fill_id, set()).add(
                        int(str(key).removesuffix("s"))
                    )
                except ValueError:
                    continue

    open_positions: dict[str, dict[str, Any]] = {}
    open_entry_debit = 0.0
    marked_open_value = 0.0
    for position_id, fill in fills_by_position.items():
        if position_id in finals_by_position:
            continue
        fill_id = str(fill.fill_id or "")
        metadata = fill.metadata if isinstance(fill.metadata, dict) else {}
        shares = float(fill.filled_size)
        price = float(fill.fill_price)
        fee = float(fill.fee)
        debit = shares * price + fee
        open_entry_debit += debit
        prior = cached.get(position_id)
        prior = prior if isinstance(prior, dict) else {}
        if str(prior.get("fill_id") or "") != fill_id:
            prior = {}
        virtual = virtual_fills.get(fill_id, {})
        executable = finite(prior.get("executable_value"), 0.0)
        if not math.isfinite(executable) or executable < 0.0:
            executable = 0.0
        executable = min(executable, shares)
        marked_open_value += executable
        fee_schedule = virtual.get("fee_schedule")
        if not isinstance(fee_schedule, dict):
            fee_schedule = prior.get("fee_schedule")
        if not isinstance(fee_schedule, dict):
            fee_schedule = {
                "rate": float(fill.fee_rate or 0.0),
                "exponent": 1,
                "takerOnly": True,
            }
        markouts = set(markout_horizons.get(fill_id, set()))
        for value in prior.get("markouts", []) if isinstance(
            prior.get("markouts"), list
        ) else []:
            try:
                markouts.add(int(value))
            except (TypeError, ValueError):
                continue
        open_positions[position_id] = {
            "position_id": position_id,
            "counterfactual_id": str(fill.candidate_id or ""),
            "fill_id": fill_id,
            "order_id": str(fill.order_id or ""),
            "market_id": str(fill.market_id or ""),
            "event_id": str(fill.event_id or ""),
            "token_id": str(fill.token_id or ""),
            "outcome": str(metadata.get("outcome") or ""),
            "shares": shares,
            "entry_price": price,
            "entry_fee": fee,
            "entry_cost": shares * price,
            "entry_debit": debit,
            "executable_value": executable,
            "opened_ms": int(fill.receive_ts_ms or fill.recorded_ts_ms),
            "resolution_due_ms": int(
                metadata.get("resolution_due_ms")
                or int(fill.receive_ts_ms or fill.recorded_ts_ms) + 300_000
            ),
            "fee_schedule": fee_schedule,
            "markouts": sorted(markouts),
            "settled": False,
            "coordinator_receipt": metadata.get("coordinator_receipt"),
            "paper_exploration": True,
            "paper_bootstrap_probe": metadata.get("paper_bootstrap_probe") is True,
            "model_yes": finite(metadata.get("fair_yes")),
            "market_yes": finite(metadata.get("arrival_pm_mid")),
            "market_mid_source": "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
        }

    maker = project_maker(events, cached)
    total_entry_debit += maker["entry_debit"]
    total_settlement_payout += maker["settlement_payout"]
    realized_pnl += maker["realized_pnl"]
    marked_open_value += maker["marked_open_value"]
    open_entry_debit += maker["open_entry_debit"]
    terminal_positions += maker["terminal_positions"]
    if set(open_positions) & set(maker["positions"]):
        issues.append("maker_taker_position_id_collision")
    open_positions.update(maker["positions"])
    issues.extend(maker["issues"])

    cash = starting_capital - total_entry_debit + total_settlement_payout
    equity = cash + marked_open_value
    if cash < -1e-7:
        issues.append("paper_account_cash_negative")
    if not math.isfinite(cash) or not math.isfinite(equity):
        issues.append("paper_account_nonfinite")
    prior_peak = finite(prior_peak_equity, starting_capital)
    if not math.isfinite(prior_peak) or prior_peak < starting_capital:
        prior_peak = starting_capital
    peak = max(starting_capital, prior_peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 1.0
    account = {
        "schema": "polymarket_v7_paper_exploration_account_v1",
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "accounting_owner": "V7_CANONICAL_LEDGER_AND_SINGLE_WRITER_SPOOL",
        "execution_authority": "SIMULATED_PAPER_EXPLORATION_ONLY",
        "starting_capital": starting_capital,
        "orders_submitted": len(orders) + maker["orders_submitted"],
        "fills": len(fills) + maker["fills"],
        "terminal_nonfills": len(order_terminals) + maker["terminal_nonfills"],
        "pending_maker_orders": maker["pending_orders"],
        "terminal_positions": terminal_positions,
        "open_positions": len(open_positions),
        "probe_fills": sum(
            event.metadata.get("paper_bootstrap_probe") is True
            for event in fills.values()
        ) + maker["probe_fills"],
        "traded_markets": sorted({
            str(event.market_id) for event in fills.values() if event.market_id
        } | set(maker["traded_markets"])),
        "entry_debit": total_entry_debit,
        "settlement_payout": total_settlement_payout,
        "open_entry_debit": open_entry_debit,
        "marked_open_value": marked_open_value,
        "cash": cash,
        "realized_pnl": realized_pnl,
        "equity": equity,
        "peak_equity": peak,
        "drawdown": drawdown,
        "invalid_spool_records": sorted(invalid_spool),
        "issues": sorted(set(issues)),
        "maker_accounting": {k:v for k,v in maker.items() if k != "positions"},
        "positions": open_positions,
    }
    account["complete"] = not account["issues"] and not invalid_spool
    return account


def expected_calibration_error(
    predictions: list[float], actuals: list[float], *, bins: int = 10,
) -> float | None:
    """Fixed-bin ECE over independent settlement-cluster observations."""
    pairs = [
        (float(prediction), float(actual))
        for prediction, actual in zip(predictions, actuals)
        if math.isfinite(prediction) and math.isfinite(actual)
        and 0.0 <= prediction <= 1.0 and 0.0 <= actual <= 1.0
    ]
    if not pairs:
        return None
    groups: list[list[tuple[float, float]]] = [
        [] for _ in range(max(1, int(bins)))
    ]
    for prediction, actual in pairs:
        index = min(len(groups) - 1, int(prediction * len(groups)))
        groups[index].append((prediction, actual))
    return sum(
        len(group) / len(pairs)
        * abs(statistics.fmean(p for p, _ in group)
              - statistics.fmean(y for _, y in group))
        for group in groups if group
    )


def logistic_calibration_line(
    predictions: list[float], actuals: list[float], *, ridge: float = 1e-6,
    max_iter: int = 50,
) -> tuple[float | None, float | None]:
    """Fit outcome ~ intercept + slope*logit(prediction) by logistic MLE."""
    pairs = [
        (min(1.0 - 1e-9, max(1e-9, float(prediction))), int(actual))
        for prediction, actual in zip(predictions, actuals)
        if math.isfinite(prediction) and math.isfinite(actual)
        and 0.0 <= prediction <= 1.0 and actual in (0.0, 1.0)
    ]
    if len(pairs) < 2 or len({actual for _, actual in pairs}) < 2:
        return None, None
    intercept, slope = 0.0, 1.0
    for _ in range(max(1, int(max_iter))):
        g0, g1 = ridge * intercept, ridge * (slope - 1.0)
        h00, h01, h11 = ridge, 0.0, ridge
        for probability, actual in pairs:
            x = math.log(probability / (1.0 - probability))
            z = max(-35.0, min(35.0, intercept + slope * x))
            fitted = 1.0 / (1.0 + math.exp(-z))
            error = fitted - actual
            variance = max(1e-9, fitted * (1.0 - fitted))
            g0 += error
            g1 += error * x
            h00 += variance
            h01 += variance * x
            h11 += variance * x * x
        determinant = h00 * h11 - h01 * h01
        if abs(determinant) < 1e-12:
            return None, None
        delta_intercept = (h11 * g0 - h01 * g1) / determinant
        delta_slope = (-h01 * g0 + h00 * g1) / determinant
        intercept -= delta_intercept
        slope = max(0.01, min(10.0, slope - delta_slope))
        if abs(delta_intercept) + abs(delta_slope) < 1e-9:
            break
    return intercept, slope


def _wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return math.nan, math.nan
    proportion = successes / trials
    denominator = 1.0 + z * z / trials
    centre = (proportion + z * z / (2.0 * trials)) / denominator
    radius = z / denominator * math.sqrt(
        proportion * (1.0 - proportion) / trials
        + z * z / (4.0 * trials * trials))
    return max(0.0, centre - radius), min(1.0, centre + radius)


def probability_interval_bin_diagnostics(
    predictions: list[float], actuals: list[float],
    lowers: list[float], uppers: list[float], *, bins: int = 10,
    minimum_bin_size: int = 3,
) -> dict[str, Any]:
    """Validate epistemic probability bands against independent bin rates.

    A Bernoulli realization is always 0 or 1, so asking whether it lies inside
    a fair-probability band is mathematically invalid. We instead compare each
    bin's mean model band with the Wilson interval for its empirical event rate.
    """
    groups: list[list[tuple[float, float, float, float]]] = [
        [] for _ in range(max(1, int(bins)))
    ]
    for prediction, actual, lower, upper in zip(
        predictions, actuals, lowers, uppers
    ):
        values = (prediction, actual, lower, upper)
        if (
            not all(math.isfinite(value) for value in values)
            or not 0.0 <= lower <= prediction <= upper <= 1.0
            or actual not in (0.0, 1.0)
        ):
            continue
        index = min(len(groups) - 1, int(prediction * len(groups)))
        groups[index].append(values)
    rows: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        if len(group) < max(1, int(minimum_bin_size)):
            continue
        mean_prediction = statistics.fmean(row[0] for row in group)
        mean_lower = statistics.fmean(row[2] for row in group)
        mean_upper = statistics.fmean(row[3] for row in group)
        successes = sum(int(row[1]) for row in group)
        observed_rate = successes / len(group)
        observed_lower, observed_upper = _wilson_interval(successes, len(group))
        consistent = mean_lower <= observed_upper and mean_upper >= observed_lower
        rows.append({
            "bin": index,
            "markets": len(group),
            "mean_prediction": mean_prediction,
            "mean_lower": mean_lower,
            "mean_upper": mean_upper,
            "observed_rate": observed_rate,
            "observed_rate_wilson95_lower": observed_lower,
            "observed_rate_wilson95_upper": observed_upper,
            "probability_band_consistent": consistent,
        })
    return {
        "eligible_bin_count": len(rows),
        "consistency_rate": (
            sum(int(row["probability_band_consistent"]) for row in rows) / len(rows)
            if rows else None),
        "mean_probability_band_width": (
            statistics.fmean(row["mean_upper"] - row["mean_lower"] for row in rows)
            if rows else None),
        "bins": rows,
    }


def fee_per_share(price: float, schedule: dict[str, Any], *, taker: bool = True) -> float:
    if not 0.0 < price < 1.0:
        return math.inf
    try:
        rate = float(schedule["rate"])
        exponent = float(schedule["exponent"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return math.inf
    if not math.isfinite(rate) or not math.isfinite(exponent) or rate < 0.0 or exponent < 0.0:
        return math.inf
    if not taker and bool(schedule.get("takerOnly", True)):
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


def entry_tte_allowed(fair: dict[str, Any], policy: dict[str, Any]) -> bool:
    """Fail closed unless the forecast is inside the configured entry window."""
    tte = finite(fair.get("tte_seconds"))
    minimum = finite(policy.get("minimum_entry_tte_seconds"))
    maximum = finite(policy.get("maximum_entry_tte_seconds"))
    buckets = policy.get("tte_bucket_policy")
    if not isinstance(buckets, list) or not buckets:
        return False
    return any(
        isinstance(bucket, dict)
        and finite(bucket.get("minimum_seconds"), math.nan) <= tte
        <= finite(bucket.get("maximum_seconds"), math.nan)
        for bucket in buckets
    )


def tte_policy(fair: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    tte = finite(fair.get("tte_seconds"), math.nan)
    for bucket in policy.get("tte_bucket_policy") if isinstance(policy.get("tte_bucket_policy"), list) else []:
        if not isinstance(bucket, dict):
            continue
        minimum = finite(bucket.get("minimum_seconds"), math.nan)
        maximum = finite(bucket.get("maximum_seconds"), math.nan)
        if math.isfinite(tte) and minimum <= tte <= maximum:
            return bucket
    return {}


def model_market_disagreement_allowed(
    fair: dict[str, Any],
    policy: dict[str, Any],
    market_yes: float | None = None,
) -> bool:
    """Reject uncalibrated model forecasts that radically contradict the market.

    The market is the benchmark the external model must beat, not an input that
    can be ignored.  Until forward calibration proves otherwise, a large gap is
    evidence of model/semantic risk rather than executable alpha.
    """
    model_yes = finite(fair.get("yes"))
    market_yes = finite(fair.get("pm_mid")) if market_yes is None else market_yes
    maximum = finite(policy.get("maximum_model_market_disagreement"))
    return (
        math.isfinite(model_yes)
        and math.isfinite(market_yes)
        and math.isfinite(maximum)
        and 0.0 <= model_yes <= 1.0
        and 0.0 <= market_yes <= 1.0
        and 0.0 <= maximum <= 1.0
        and abs(model_yes - market_yes) <= maximum
    )


def live_market_yes(books: dict[str, "Book"], market: dict[str, Any]) -> float | None:
    """Return a complement-consistent YES midpoint from one live book batch.

    Gamma's market midpoint is an opening/discovery snapshot and can remain
    unchanged while a five-minute CLOB moves from 50c to 99c. Execution and
    forecast benchmarks must therefore use the same arrival books that a PAPER
    FAK would face. The intersection of direct YES and complement-implied NO
    bounds also rejects crossed or semantically mismatched token books.
    """
    yes = books.get(str(market.get("yes_token") or ""))
    no = books.get(str(market.get("no_token") or ""))
    if yes is None or no is None or not (yes.bids and yes.asks and no.bids and no.asks):
        return None
    lower = max(yes.bids[0][0], 1.0 - no.asks[0][0])
    upper = min(yes.asks[0][0], 1.0 - no.bids[0][0])
    tolerance = max(yes.tick_size, no.tick_size, 1e-6)
    if lower > upper + tolerance:
        return None
    lower = max(0.0, min(1.0, lower))
    upper = max(lower, min(1.0, upper))
    return 0.5 * (lower + upper)


def hybrid_probability(external_yes: float, market_yes: float, weight: float) -> float:
    external = min(1.0 - 1e-9, max(1e-9, external_yes))
    market = min(1.0 - 1e-9, max(1e-9, market_yes))
    bounded_weight = min(1.0, max(0.0, weight))
    external_logit = math.log(external / (1.0 - external))
    market_logit = math.log(market / (1.0 - market))
    value = external_logit + bounded_weight * (market_logit - external_logit)
    return 1.0 / (1.0 + math.exp(-value)) if value >= 0.0 else math.exp(value) / (1.0 + math.exp(value))


@dataclass(frozen=True)
class Book:
    token_id: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    tick_size: float
    min_order_size: float
    exchange_ts_ms: int
    receive_ts_ms: int
    snapshot_id: str


def parse_book(raw: Any, receive_ts_ms: int) -> Book | None:
    if not isinstance(raw, dict):
        return None
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for key, output in (("bids", bids), ("asks", asks)):
        for row in raw.get(key) if isinstance(raw.get(key), list) else []:
            if not isinstance(row, dict):
                continue
            price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
            if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
                output.append((price, size))
    bids.sort(reverse=True)
    asks.sort()
    token = str(raw.get("asset_id") or "")
    exchange = int(finite(raw.get("timestamp"), 0.0))
    if exchange and exchange < 10_000_000_000:
        exchange *= 1000
    if (not token or (not bids and not asks) or exchange <= 0
            or exchange > receive_ts_ms + MAX_CLOB_CLOCK_SKEW_MS):
        return None
    # The public CLOB clock can lead the local host by a few milliseconds.  A
    # bounded skew is safe to accept, but the canonical ledger clock must stay
    # causal (exchange <= receive).  Larger future timestamps still fail closed.
    exchange = min(exchange, receive_ts_ms)
    snapshot = str(raw.get("hash") or "") or stable_id(token, exchange, bids, asks)
    return Book(
        token, tuple(bids), tuple(asks), max(1e-6, finite(raw.get("tick_size"), 0.01)),
        max(1.0, finite(raw.get("min_order_size"), 1.0)), exchange, receive_ts_ms, snapshot,
    )


def candidate_input_rejection_reason(status: dict[str, Any], *, current_ns: int | None = None) -> str:
    """Explain an empty candidate set without changing admission or EV gates."""
    if (status.get("paper_only") is not True
            or status.get("authenticated_execution") is not False
            or status.get("real_order_submission") is not False):
        return "PAPER_SAFETY_CONTRACT_INVALID"
    def section(name: str) -> dict[str, Any]:
        value = status.get(name)
        return value if isinstance(value, dict) else {}
    contract = section("contract")
    if contract.get("verified") is not True or contract.get("rules_hash_recognized") is not True:
        return "CONTRACT_RULES_NOT_VERIFIED"
    if section("settlement_reference").get("valid") is not True:
        return "SETTLEMENT_REFERENCE_NOT_CAPTURED"
    oracle = section("oracle")
    if oracle.get("healthy") is not True or oracle.get("continuity") == "CONTINUITY_UNKNOWN":
        return "ORACLE_NOT_READY"
    if section("external").get("healthy") is not True:
        return "EXTERNAL_FEEDS_NOT_READY"
    fair = section("fair")
    if fair.get("valid") is not True:
        return "FAIR_VALUE_INVALID"
    try:
        calculated = int(fair.get("calculated_monotonic_ns") or 0)
        valid_until = int(fair.get("valid_until_monotonic_ns") or 0)
    except (TypeError, ValueError, OverflowError):
        return "FAIR_SNAPSHOT_CLOCK_INVALID"
    now = time.monotonic_ns() if current_ns is None else current_ns
    if calculated <= 0 or calculated > now or valid_until < calculated:
        return "FAIR_SNAPSHOT_CLOCK_INVALID"
    if valid_until < now:
        return "FAIR_SNAPSHOT_EXPIRED"
    return ""


def robust_candidates(status: dict[str, Any], books: dict[str, Book], policy: dict[str, Any]) -> list[dict[str, Any]]:
    if status.get("paper_only") is not True or status.get("authenticated_execution") is not False:
        return []
    if status.get("real_order_submission") is not False:
        return []
    contract, reference = status.get("contract") or {}, status.get("settlement_reference") or {}
    oracle, external, fair, market = (
        status.get("oracle") or {}, status.get("external") or {}, status.get("fair") or {}, status.get("market") or {},
    )
    # The structural bootstrap is explicitly research-only. It may generate
    # bounded PAPER probes, but must never be promoted implicitly into the
    # ordinary robust Taker path merely because its interval clears a gate.
    if fair.get("probability_model_id") == BOOTSTRAP_PROBABILITY_MODEL_ID:
        return []
    if not (contract.get("verified") and contract.get("rules_hash_recognized") and reference.get("valid")
            and oracle.get("healthy") and oracle.get("continuity") != "CONTINUITY_UNKNOWN"
            and external.get("healthy") and fair.get("valid")):
        return []
    if not entry_tte_allowed(fair, policy):
        return []
    market_yes = live_market_yes(books, market)
    if market_yes is None or not model_market_disagreement_allowed(
        fair, policy, market_yes
    ):
        return []
    calculated = int(fair.get("calculated_monotonic_ns") or 0)
    fair_yes = finite(fair.get("yes"), math.nan)
    if not math.isfinite(fair_yes) or not 0 <= fair_yes <= 1:
        return []
    valid_until = int(fair.get("valid_until_monotonic_ns") or 0)
    current = time.monotonic_ns()
    if calculated <= 0 or calculated > current or valid_until < current:
        return []
    schedule = market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {}
    bucket = tte_policy(fair, policy)
    if bucket and bucket.get("action") != "TAKER_SHADOW":
        return []
    minimum_ev = float(bucket.get("minimum_robust_ev_per_share", policy.get("minimum_robust_ev_per_share", 0.001)))
    execution_risk = float(bucket.get("execution_risk_per_share", policy.get("base_execution_risk_per_share", 0.0005)))
    rows: list[dict[str, Any]] = []
    for outcome, token, robust_value in (
        ("YES", str(market.get("yes_token") or ""), float(fair.get("lower") or 0.0)),
        ("NO", str(market.get("no_token") or ""), 1.0 - float(fair.get("upper") or 1.0)),
    ):
        book = books.get(token)
        if book is None or not book.asks:
            continue
        ask = book.asks[0][0]
        fee = fee_per_share(ask, schedule)
        robust_ev = robust_value - ask - fee - execution_risk
        if math.isfinite(robust_ev) and robust_ev >= minimum_ev:
            rows.append({
                "outcome": outcome, "token_id": token, "book": book, "ask": ask,
                "fee_per_share": fee, "execution_risk": execution_risk,
                "robust_probability": robust_value, "robust_ev": robust_ev,
                "point_probability": fair_yes if outcome == "YES" else 1.0 - fair_yes,
                "point_ev": (fair_yes if outcome == "YES" else 1.0 - fair_yes) - ask - fee - execution_risk,
                "probability_model_id": fair.get("probability_model_id"),
                "probability_model_hash": fair.get("probability_model_hash"),
                "market_yes": market_yes,
                "tte_seconds": float(fair["tte_seconds"]),
                "tte_bucket_id": str(bucket.get("id") or "UNBUCKETED_INVALID"),
            })
    return sorted(rows, key=lambda row: (-row["robust_ev"], row["outcome"]))


def validate_probe_policy(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None or raw.get("enabled") is not True:
        return None
    expected = {
        "schema": "polymarket_v7_paper_exploration_probe_v1",
        "authority": "PAPER_EXPLORATION",
        "asset": "BTC", "horizon": "M5",
        "required_probability_model_id": BOOTSTRAP_PROBABILITY_MODEL_ID,
        "one_probe_per_market": True,
        "require_no_robust_candidate": True,
        "require_arrival_revalidation": True,
        "research_only": True,
        "real_money_authority": False,
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise RuntimeError("paper_exploration_probe_authority_invalid")
    policy = dict(raw)
    if not isinstance(policy.get("allow_frozen_rich_ml"), bool):
        raise RuntimeError("paper_exploration_probe_parameter_invalid:allow_frozen_rich_ml")
    ranges = {
        "minimum_point_ev_per_share": (0.01, 0.25),
        "maximum_rich_pm_prior_drift": (0.005, 0.10),
        "maximum_rich_pm_prior_arrival_age_ms": (100.0, 2000.0),
        "minimum_model_market_disagreement": (0.01, 0.25),
        "maximum_model_market_disagreement": (0.05, 0.30),
        "minimum_tte_seconds": (1.0, 30.0),
        "maximum_tte_seconds": (30.0, 180.0),
        "max_capital_fraction": (0.00001, 0.0005),
        "max_notional_usd": (0.25, 2.0),
        "max_loss_usd": (0.25, 2.0),
    }
    for key, (minimum, maximum) in ranges.items():
        value = finite(policy.get(key), math.nan)
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise RuntimeError(f"paper_exploration_probe_parameter_invalid:{key}")
        policy[key] = value
    if policy["maximum_model_market_disagreement"] < policy["minimum_model_market_disagreement"]:
        raise RuntimeError("paper_exploration_probe_disagreement_interval_invalid")
    if policy["maximum_tte_seconds"] <= policy["minimum_tte_seconds"]:
        raise RuntimeError("paper_exploration_probe_tte_interval_invalid")
    return policy


def paper_probe_candidates(
    status: dict[str, Any], books: dict[str, Book], policy: dict[str, Any],
    probe_policy: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if probe_policy is None:
        return []
    fair = status.get("fair") if isinstance(status.get("fair"), dict) else {}
    learned = (probe_policy.get("allow_frozen_rich_ml") is True
               and is_paper_learning_fair(fair, str(status.get("code_sha") or "")))
    bootstrap = (fair.get("valid") is True
        and fair.get("paper_exploration_bootstrap") is True
        and fair.get("research_only") is True and fair.get("real_money_authority") is False
        and fair.get("probability_model_id") == probe_policy["required_probability_model_id"]
        and identity_hash(fair.get("probability_model_hash")))
    if not (bootstrap or learned):
        return []
    contract = status.get("contract") if isinstance(status.get("contract"), dict) else {}
    reference = status.get("settlement_reference") if isinstance(status.get("settlement_reference"), dict) else {}
    oracle = status.get("oracle") if isinstance(status.get("oracle"), dict) else {}
    external = status.get("external") if isinstance(status.get("external"), dict) else {}
    market = status.get("market") if isinstance(status.get("market"), dict) else {}
    if not (
        status.get("paper_only") is True
        and status.get("authenticated_execution") is False
        and status.get("real_order_submission") is False
        and contract.get("verified") is True
        and contract.get("rules_hash_recognized") is True
        and reference.get("valid") is True
        and oracle.get("healthy") is True
        and oracle.get("continuity") != "CONTINUITY_UNKNOWN"
        and external.get("healthy") is True
    ):
        return []
    tte = finite(fair.get("tte_seconds"), math.nan)
    if not probe_policy["minimum_tte_seconds"] <= tte <= probe_policy["maximum_tte_seconds"]:
        return []
    bucket = tte_policy(fair, policy)
    if bucket and bucket.get("action") != "TAKER_SHADOW":
        return []
    calculated = int(fair.get("calculated_monotonic_ns") or 0)
    valid_until = int(fair.get("valid_until_monotonic_ns") or 0)
    current = time.monotonic_ns()
    if calculated <= 0 or calculated > current or valid_until < current:
        return []
    market_yes = live_market_yes(books, market)
    fair_yes = finite(fair.get("yes"), math.nan)
    if market_yes is None or not math.isfinite(fair_yes):
        return []
    prior_drift = 0.0
    if learned and fair.get("uses_polymarket_price_as_feature") is True:
        prior = finite(fair.get("pm_mid"), math.nan)
        prior_receive_ms = finite(fair.get("pm_mid_receive_ts_ms"), math.nan)
        newest_book_ms = max((book.receive_ts_ms for book in books.values()), default=0)
        prior_age_at_candidate_ms = newest_book_ms - prior_receive_ms
        if (not math.isfinite(prior) or not math.isfinite(prior_receive_ms)
                or prior_age_at_candidate_ms < -250
                or prior_age_at_candidate_ms > probe_policy["maximum_rich_pm_prior_arrival_age_ms"]):
            return []
        prior_drift = abs(prior - market_yes)
        if prior_drift > probe_policy["maximum_rich_pm_prior_drift"]:
            return []
    disagreement = abs(fair_yes - market_yes)
    if not probe_policy["minimum_model_market_disagreement"] <= disagreement <= probe_policy["maximum_model_market_disagreement"]:
        return []
    schedule = market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {}
    execution_risk = float(bucket.get(
        "execution_risk_per_share", policy.get("base_execution_risk_per_share", 0.0005)
    ))
    rows: list[dict[str, Any]] = []
    for outcome, token, point_probability, robust_probability in (
        ("YES", str(market.get("yes_token") or ""), fair_yes, finite(fair.get("lower"), math.nan)),
        ("NO", str(market.get("no_token") or ""), 1.0 - fair_yes, 1.0 - finite(fair.get("upper"), math.nan)),
    ):
        book = books.get(token)
        if book is None or not book.asks or not math.isfinite(robust_probability):
            continue
        ask = book.asks[0][0]
        fee = fee_per_share(ask, schedule)
        point_ev = point_probability - ask - fee - execution_risk
        robust_ev = robust_probability - ask - fee - execution_risk
        if math.isfinite(point_ev) and point_ev >= probe_policy["minimum_point_ev_per_share"]:
            rows.append({
                "outcome": outcome, "token_id": token, "book": book, "ask": ask,
                "fee_per_share": fee, "execution_risk": execution_risk,
                "point_probability": point_probability, "point_ev": point_ev,
                "robust_probability": robust_probability, "robust_ev": robust_ev,
                "market_yes": market_yes, "model_market_disagreement": disagreement,
                "pm_prior_arrival_drift": prior_drift,
                "tte_seconds": tte,
                "tte_bucket_id": str(bucket.get("id") or "UNBUCKETED_INVALID"),
                "paper_bootstrap_probe": True,
                "probability_model_id": fair.get("probability_model_id"),
                "probability_model_hash": fair.get("probability_model_hash"),
            })
    return sorted(rows, key=lambda row: (-row["point_ev"], row["outcome"]))


def executable_sell_value(book: Book, shares: float, schedule: dict[str, Any]) -> float:
    """Return a full-depth liquidation value net of authoritative exit fees."""
    remaining = max(0.0, shares)
    value = 0.0
    for price, available in book.bids:
        quantity = min(remaining, available)
        value += quantity * max(0.0, price - fee_per_share(price, schedule))
        remaining -= quantity
        if remaining <= 1e-9:
            return value
    return 0.0


def serialize_book(book: Book) -> dict[str, Any]:
    """Persist the complete visible book needed for arrival-time replay."""
    return {
        "token_id": book.token_id,
        "bids": [[price, size] for price, size in book.bids],
        "asks": [[price, size] for price, size in book.asks],
        "tick_size": book.tick_size,
        "min_order_size": book.min_order_size,
        "exchange_ts_ms": book.exchange_ts_ms,
        "receive_ts_ms": book.receive_ts_ms,
        "snapshot_id": book.snapshot_id,
    }


def opportunity_set(
    status: dict[str, Any], books: dict[str, Book], policy: dict[str, Any],
) -> dict[str, Any] | None:
    """Describe TAKE YES/NO and ABSTAIN from one causal book batch.

    Unlike ``robust_candidates`` this records both sides even when the policy
    abstains.  It is evidence only: the execution path continues to use the
    existing fail-closed candidate function.
    """
    market = status.get("market") if isinstance(status.get("market"), dict) else {}
    fair = status.get("fair") if isinstance(status.get("fair"), dict) else {}
    contract = status.get("contract") if isinstance(status.get("contract"), dict) else {}
    reference = status.get("settlement_reference") if isinstance(
        status.get("settlement_reference"), dict) else {}
    oracle = status.get("oracle") if isinstance(status.get("oracle"), dict) else {}
    external = status.get("external") if isinstance(status.get("external"), dict) else {}
    yes_token = str(market.get("yes_token") or "")
    no_token = str(market.get("no_token") or "")
    if not yes_token or not no_token or yes_token not in books or no_token not in books:
        return None
    market_yes = live_market_yes(books, market)
    if market_yes is None:
        return None
    schedule = market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {}
    bucket = tte_policy(fair, policy)
    threshold = finite(
        bucket.get("minimum_robust_ev_per_share"),
        finite(policy.get("minimum_robust_ev_per_share"), math.nan),
    )
    execution_risk = finite(
        bucket.get("execution_risk_per_share"),
        finite(policy.get("base_execution_risk_per_share"), math.nan),
    )
    fair_yes = finite(fair.get("yes"), math.nan)
    lower = finite(fair.get("lower"), math.nan)
    upper = finite(fair.get("upper"), math.nan)
    globally_eligible = bool(
        status.get("paper_only") is True
        and status.get("authenticated_execution") is False
        and status.get("real_order_submission") is False
        and contract.get("verified") is True
        and contract.get("rules_hash_recognized") is True
        and reference.get("valid") is True
        and (status.get("oracle") or {}).get("healthy") is True
        and (status.get("external") or {}).get("healthy") is True
        and fair.get("valid") is True
        and entry_tte_allowed(fair, policy)
        and model_market_disagreement_allowed(fair, policy, market_yes)
        and (not bucket or bucket.get("action") == "TAKER_SHADOW")
        and math.isfinite(threshold)
        and math.isfinite(execution_risk)
    )
    selected = {
        row["token_id"]: row for row in robust_candidates(status, books, policy)
    }
    actions: list[dict[str, Any]] = []
    for outcome, token, point_probability, robust_probability in (
        ("YES", yes_token, fair_yes, lower),
        ("NO", no_token, 1.0 - fair_yes, 1.0 - upper),
    ):
        book = books[token]
        ask = book.asks[0][0] if book.asks else math.nan
        fee = fee_per_share(ask, schedule) if math.isfinite(ask) else math.inf
        robust_ev = robust_probability - ask - fee - execution_risk
        row = selected.get(token)
        actions.append({
            "action": f"TAKE_{outcome}",
            "outcome": outcome,
            "token_id": token,
            "point_probability": point_probability if math.isfinite(point_probability) else None,
            "robust_probability": robust_probability if math.isfinite(robust_probability) else None,
            "best_ask": ask if math.isfinite(ask) else None,
            "best_ask_visible_size": book.asks[0][1] if book.asks else None,
            "fee_per_share": fee if math.isfinite(fee) else None,
            "execution_risk_per_share": execution_risk if math.isfinite(execution_risk) else None,
            "robust_ev_per_share": robust_ev if math.isfinite(robust_ev) else None,
            "minimum_robust_ev_per_share": threshold if math.isfinite(threshold) else None,
            "eligible": bool(globally_eligible and row is not None),
        })
    eligible = [row for row in actions if row["eligible"]]
    best = max(eligible, key=lambda row: float(row["robust_ev_per_share"]), default=None)
    snapshot_id = stable_id(
        market.get("market_id"), books[yes_token].snapshot_id, books[no_token].snapshot_id,
    )
    return {
        "snapshot_id": snapshot_id,
        "market_id": str(market.get("market_id") or ""),
        "event_id": str(market.get("event_id") or ""),
        "contract_rules_hash": str(contract.get("rules_hash") or ""),
        "reference_version": int(reference.get("version") or 0),
        "decision_ts_ms": max(book.receive_ts_ms for book in books.values()),
        "tte_seconds": finite(fair.get("tte_seconds")),
        "tte_bucket_id": str(bucket.get("id") or "UNBUCKETED_INVALID"),
        "fair_yes": fair_yes if math.isfinite(fair_yes) else None,
        "fair_yes_lower": lower if math.isfinite(lower) else None,
        "fair_yes_upper": upper if math.isfinite(upper) else None,
        "market_yes": market_yes,
        "fee_schedule": schedule,
        "oracle": {
            "value": finite(oracle.get("value")),
            "age_ns": int(finite(oracle.get("age_ns"), 0.0)),
        },
        "settlement_reference_value": finite(reference.get("value")),
        "external_features": {
            key: external.get(key) for key in (
                "composite_price", "composite_microprice", "dispersion_bps",
                "fresh_venue_count", "age_ns", "feature_semantics_version",
                "return_history_available", "return_100ms", "return_250ms", "return_1s",
                "return_5s", "return_30s", "realized_vol_fast",
                "realized_vol_medium", "realized_vol_slow", "realized_vol_30s",
                "aggregate_ofi", "aggregate_trade_imbalance",
            )
        },
        "books": {
            "YES": serialize_book(books[yes_token]),
            "NO": serialize_book(books[no_token]),
        },
        "actions": actions,
        "decision": best["action"] if best is not None else "ABSTAIN",
        "selected_robust_ev_per_share": (
            best["robust_ev_per_share"] if best is not None else None
        ),
        "global_policy_gates_passed": globally_eligible,
    }
