#!/usr/bin/env python3
"""Read exact receive-time compact PM label tapes produced by the V7 book observer."""
from __future__ import annotations

import bisect
import json
import math
import struct
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

MANIFEST_SCHEMA_V1 = "polymarket_v7_compact_pm_label_tape_manifest_v1"
MANIFEST_SCHEMA_V2 = "polymarket_v7_compact_pm_label_tape_manifest_v2"
RECORD_SCHEMA_V1 = "polymarket_v7_compact_pm_label_record_v1"
RECORD_SCHEMA_V2 = "polymarket_v7_compact_pm_label_record_v2"
RECORD = struct.Struct("<QQQQqqiiiBBBB")  # backward-compatible v1 alias
RECORD_V2 = struct.Struct("<QQQQqqiiiqqBBBB")


def load_manifest(path: Path, expected_sha: str | None = None) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("compact_pm:manifest_not_object")
    identity=(value.get("schema"),int(value.get("version") or 0),value.get("record_schema"),int(value.get("record_size") or 0))
    allowed={(MANIFEST_SCHEMA_V1,1,RECORD_SCHEMA_V1,RECORD.size),(MANIFEST_SCHEMA_V2,2,RECORD_SCHEMA_V2,RECORD_V2.size)}
    if (identity not in allowed
            or value.get("byte_order") != "little_endian"
            or value.get("paper_only") is not True
            or value.get("authenticated_execution") is not False
            or value.get("real_order_submission") is not False
            or value.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"
            or value.get("selection_only") is not True):
        raise ValueError("compact_pm:manifest_identity_or_authority")
    if expected_sha is not None and value.get("model_sha") != expected_sha:
        raise ValueError("compact_pm:model_sha_mismatch")
    tokens = value.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        raise ValueError("compact_pm:tokens_missing")
    return value


def record_struct(manifest: dict[str, Any]) -> struct.Struct:
    return RECORD_V2 if manifest.get("record_schema")==RECORD_SCHEMA_V2 else RECORD


def token_map(manifest: dict[str, Any]) -> dict[int, dict[str, Any]]:
    output: dict[int, dict[str, Any]] = {}
    for row in manifest["tokens"]:
        if not isinstance(row, dict):
            raise ValueError("compact_pm:token_row")
        handle = int(row.get("instrument_handle") or 0)
        market = str(row.get("market_id") or "")
        token = str(row.get("token_id") or "")
        outcome = str(row.get("outcome") or "")
        if handle <= 0 or not market or not token or outcome not in {"YES", "NO"} or handle in output:
            raise ValueError("compact_pm:token_identity")
        output[handle] = row
    return output


def read_records(paths: Iterable[Path], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = token_map(manifest); record=record_struct(manifest)
    rows: list[dict[str, Any]] = []
    previous_sequence = 0
    for path in paths:
        payload = path.read_bytes()
        if len(payload) % record.size:
            raise ValueError(f"compact_pm:partial_record:{path}")
        for offset in range(0, len(payload), record.size):
            values = record.unpack_from(payload, offset)
            if record is RECORD_V2:
                seq,handle,state_version,epoch,wall_ms,mono_ns,bid_e4,ask_e4,tick_e4,bid_depth,ask_depth,valid,lineage,kind,_=values
            else:
                seq,handle,state_version,epoch,wall_ms,mono_ns,bid_e4,ask_e4,tick_e4,valid,lineage,kind,_=values; bid_depth=ask_depth=None
            meta = mapping.get(handle)
            if meta is None:
                raise ValueError("compact_pm:unknown_instrument_handle")
            if seq <= previous_sequence:
                raise ValueError("compact_pm:nonmonotone_sequence")
            previous_sequence = seq
            rows.append({
                "observer_sequence": seq, "instrument_handle": handle,
                "state_version": state_version, "connection_epoch": epoch,
                "receive_wall_ms": wall_ms, "receive_monotonic_ns": mono_ns,
                "best_bid": bid_e4 / 10_000.0, "best_ask": ask_e4 / 10_000.0,
                "tick_size": tick_e4 / 10_000.0,
                "bid_depth_l1": None if bid_depth is None else bid_depth / 1_000_000.0,
                "ask_depth_l1": None if ask_depth is None else ask_depth / 1_000_000.0,
                "valid": bool(valid), "lineage_continuous": bool(lineage), "event_kind": int(kind),
                "market_id": str(meta["market_id"]), "event_id": str(meta.get("event_id") or ""),
                "token_id": str(meta["token_id"]), "outcome": str(meta["outcome"]),
            })
    return rows


def build_timelines(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    output: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        output[(str(row["market_id"]), str(row["outcome"]))].append(row)
    for seq in output.values():
        seq.sort(key=lambda r: (int(r["receive_wall_ms"]), int(r["observer_sequence"])))
    return dict(output)


def build_indexed_timelines(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    plain = build_timelines(rows)
    return {key: {"rows": seq, "stamps": [int(row["receive_wall_ms"]) for row in seq]}
            for key, seq in plain.items()}


def _asof_indexed(index: dict[str, Any] | None, target_ms: float) -> dict[str, Any] | None:
    if not index:
        return None
    seq=index["rows"]; stamps=index["stamps"]; position=bisect.bisect_right(stamps,target_ms)-1
    if position < 0:
        return None
    row=seq[position]
    if row.get("valid") is not True or row.get("lineage_continuous") is not True:
        return None
    bid,ask,tick=float(row["best_bid"]),float(row["best_ask"]),float(row["tick_size"])
    if not (math.isfinite(bid) and math.isfinite(ask) and math.isfinite(tick) and 0 < bid < ask < 1 and 0 < tick < 1):
        return None
    return row


def pair_asof_indexed(indexed: dict[tuple[str, str], dict[str, Any]], market_id: str, target_ms: float) -> dict[str, Any] | None:
    yes=_asof_indexed(indexed.get((market_id,"YES")),target_ms); no=_asof_indexed(indexed.get((market_id,"NO")),target_ms)
    if yes is None or no is None or yes["connection_epoch"] != no["connection_epoch"]:
        return None
    yes_mid=(float(yes["best_bid"])+float(yes["best_ask"]))/2.0; no_mid=(float(no["best_bid"])+float(no["best_ask"]))/2.0
    tolerance=2.0*max(float(yes["tick_size"]),float(no["tick_size"]))+1e-12
    if abs(yes_mid+no_mid-1.0)>tolerance:
        return None
    return {"pm_yes":(yes_mid+1.0-no_mid)/2.0,"yes_mid":yes_mid,"no_mid":no_mid,"connection_epoch":int(yes["connection_epoch"]),
            "yes_sequence":int(yes["observer_sequence"]),"no_sequence":int(no["observer_sequence"]),
            "state_available_wall_ms":max(int(yes["receive_wall_ms"]),int(no["receive_wall_ms"])),
            "yes_tick_size":float(yes["tick_size"]),"no_tick_size":float(no["tick_size"]),
            "yes_bid_depth_l1":yes.get("bid_depth_l1"),"yes_ask_depth_l1":yes.get("ask_depth_l1"),
            "no_bid_depth_l1":no.get("bid_depth_l1"),"no_ask_depth_l1":no.get("ask_depth_l1")}


def _asof(seq: list[dict[str, Any]], target_ms: float) -> dict[str, Any] | None:
    if not seq:
        return None
    stamps = [int(row["receive_wall_ms"]) for row in seq]
    index = bisect.bisect_right(stamps, target_ms) - 1
    if index < 0:
        return None
    row = seq[index]
    if row.get("valid") is not True or row.get("lineage_continuous") is not True:
        return None
    bid, ask, tick = float(row["best_bid"]), float(row["best_ask"]), float(row["tick_size"])
    if not (math.isfinite(bid) and math.isfinite(ask) and math.isfinite(tick)
            and 0 < bid < ask < 1 and 0 < tick < 1):
        return None
    return row


def pair_asof(timelines: dict[tuple[str, str], list[dict[str, Any]]], market_id: str,
              target_ms: float) -> dict[str, Any] | None:
    yes = _asof(timelines.get((market_id, "YES"), []), target_ms)
    no = _asof(timelines.get((market_id, "NO"), []), target_ms)
    if yes is None or no is None or yes["connection_epoch"] != no["connection_epoch"]:
        return None
    yes_mid = (float(yes["best_bid"]) + float(yes["best_ask"])) / 2.0
    no_mid = (float(no["best_bid"]) + float(no["best_ask"])) / 2.0
    tolerance = 2.0 * max(float(yes["tick_size"]), float(no["tick_size"])) + 1e-12
    if abs(yes_mid + no_mid - 1.0) > tolerance:
        return None
    pm_yes = (yes_mid + 1.0 - no_mid) / 2.0
    return {
        "pm_yes": pm_yes, "yes_mid": yes_mid, "no_mid": no_mid,
        "connection_epoch": int(yes["connection_epoch"]),
        "yes_sequence": int(yes["observer_sequence"]),
        "no_sequence": int(no["observer_sequence"]),
        "state_available_wall_ms": max(int(yes["receive_wall_ms"]), int(no["receive_wall_ms"])),
        "yes_tick_size": float(yes["tick_size"]), "no_tick_size": float(no["tick_size"]),
        "yes_bid_depth_l1": yes.get("bid_depth_l1"), "yes_ask_depth_l1": yes.get("ask_depth_l1"),
        "no_bid_depth_l1": no.get("bid_depth_l1"), "no_ask_depth_l1": no.get("ask_depth_l1"),
    }


def validate_status(status: dict[str, Any], manifest: dict[str, Any], *, require_no_reconnect: bool = True) -> None:
    if (status.get("paper_only") is not True
            or status.get("authenticated_execution") is not False
            or status.get("real_order_submission") is not False
            or status.get("model_sha") != manifest.get("model_sha")
            or status.get("observer_session_id") != manifest.get("observer_session_id")
            or status.get("evidence_complete") is not True
            or status.get("compact_label_tape_enabled") is not True
            or int(status.get("compact_label_record_size") or 0) != int(manifest.get("record_size") or 0)):
        raise ValueError("compact_pm:status_identity_or_evidence")
    if int(status.get("dropped_events") or 0) or int(status.get("decoder_failures") or 0):
        raise ValueError("compact_pm:status_dropped_or_decoder_failure")
    if require_no_reconnect and (int(status.get("reconnects") or 0) or int(status.get("feed_reconnects") or 0)):
        raise ValueError("compact_pm:reconnect_present")



def discover_sessions(directory: Path, *, expected_sha: str | None = None,
                      require_no_reconnect: bool = True) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for manifest_path in sorted(directory.glob("*.manifest.json")):
        manifest = load_manifest(manifest_path, expected_sha)
        session_id = str(manifest.get("observer_session_id") or "")
        if not session_id or manifest_path.name != f"{session_id}.manifest.json":
            raise ValueError("compact_pm:manifest_filename_identity")
        status_path = directory / f"{session_id}.status.json"
        if not status_path.is_file():
            raise ValueError(f"compact_pm:session_status_missing:{session_id}")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        validate_status(status, manifest, require_no_reconnect=require_no_reconnect)
        tape_paths = sorted(directory.glob(f"{session_id}.segment-*.bin"))
        current = directory / f"{session_id}.current.bin"
        if current.is_file():
            tape_paths.append(current)
        if not tape_paths:
            raise ValueError(f"compact_pm:session_tape_missing:{session_id}")
        rows = read_records(tape_paths, manifest)
        expected_records = int(status.get("compact_label_records") or 0)
        if expected_records != len(rows):
            raise ValueError(f"compact_pm:record_count_mismatch:{session_id}")
        sessions.append({
            "session_id": session_id, "manifest": manifest, "status": status,
            "tape_paths": tape_paths, "rows": rows, "timelines": build_timelines(rows),
            "indexed_timelines": build_indexed_timelines(rows),
            "records": len(rows),
            "first_receive_wall_ms": min((int(r["receive_wall_ms"]) for r in rows), default=None),
            "last_receive_wall_ms": max((int(r["receive_wall_ms"]) for r in rows), default=None),
        })
    if not sessions:
        raise ValueError("compact_pm:no_sessions")
    return sessions


def session_for_origin(sessions: list[dict[str, Any]], market_id: str, origin_ms: float) -> dict[str, Any] | None:
    # Never bridge a restart/reconnect boundary. The origin and every future
    # target must be resolved inside the same returned session.
    candidates=[]
    for session in sessions:
        first=session.get("first_receive_wall_ms"); last=session.get("last_receive_wall_ms")
        if first is None or last is None or not (first <= origin_ms <= last):
            continue
        indexed=session.get("indexed_timelines")
        state=pair_asof_indexed(indexed,market_id,origin_ms) if indexed is not None else pair_asof(session["timelines"],market_id,origin_ms)
        if state is not None:
            candidates.append(session)
    if len(candidates) > 1:
        raise ValueError("compact_pm:overlapping_sessions")
    return candidates[0] if candidates else None

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tape", type=Path, action="append", required=True)
    parser.add_argument("--status", type=Path)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    rows = read_records(args.tape, manifest)
    if args.status:
        validate_status(json.loads(args.status.read_text()), manifest)
    timelines = build_timelines(rows)
    print(json.dumps({
        "schema": "polymarket_v7_compact_pm_label_tape_report_v1",
        "model_sha": manifest["model_sha"], "records": len(rows),
        "markets": len({market for market, _ in timelines}),
        "bytes": sum(path.stat().st_size for path in args.tape),
        "record_size": int(manifest["record_size"]), "paper_only": True, "execution_authority": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
