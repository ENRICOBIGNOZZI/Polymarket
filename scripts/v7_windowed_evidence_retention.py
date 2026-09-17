#!/usr/bin/env python3
"""Bounded rolling retention for high-frequency PAPER research detail.

Canonical economic records and compact final research artifacts are excluded.
Expired raw HFT detail is represented by immutable receipts and source/revision
hash metadata before physical bytes are removed. No execution authority.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from v7_evidence_capacity import allocated_data_bytes
from v7_evidence_store import AUTH, canonical, digest, immutable, fsync_dir
from v7_evidence_catalog import classify

POLICY = "USER_AUTHORIZED_HFT_ROLLING_RAW_WINDOW_20260916"
WINDOWED = {
    "pm_causal_book", "pm_public_trades", "pm_l2_binary", "external_raw",
    "external_normalized", "oracle_rtds", "derivative_rest", "diagnostics",
    "structural_candidates",
}
RAW_RECEIPT_SCHEMA = "polymarket_v7_raw_expiry_receipt_v1"
SUMMARY_SCHEMA = "polymarket_v7_windowed_raw_summary_v1"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _allocated(path: Path) -> int:
    try:
        stat = path.lstat()
        return int(getattr(stat, "st_blocks", 0)) * 512 or stat.st_size
    except OSError:
        return 0


def _closed_path(path: Path, runs: Path) -> bool:
    try:
        relative = path.resolve().relative_to(runs.resolve())
    except (OSError, ValueError):
        return False
    if path.is_symlink() or not path.is_file():
        return False
    name = path.name
    if name in {"current.jsonl", "fillability_ws.jsonl", "trade_tape.csv", "execution.jsonl"}:
        return False
    if relative.parts and relative.parts[0] == "paper_v7_archives":
        return True
    if relative.parts and relative.parts[0] == "paper_v7_live":
        return ".segment-" in name or name.endswith(".gz")
    return False


def _scan(store: Path):
    revisions = {}
    by_source = defaultdict(list)
    for path in sorted((store / "revisions").glob("*/*.json")):
        value = _load(path)
        if value.get("schema") != "polymarket_v7_permanent_source_revision_v1":
            continue
        sha = path.stem
        revisions[sha] = value
        by_source[str(value.get("source_id"))].append((sha, value))
    return revisions, by_source


def _source_expired(rows, cutoff_ns: int) -> bool:
    families = {(value.get("contract") or {}).get("source_family") for _, value in rows}
    if len(families) != 1 or next(iter(families)) not in WINDOWED:
        return False
    latest = 0
    for _, value in rows:
        latest = max(latest, int(value.get("captured_ns") or 0))
        stat = value.get("stat") or []
        if len(stat) >= 4:
            latest = max(latest, int(stat[3] or 0))
    return latest > 0 and latest < cutoff_ns


def _summary(source_id: str, rows, expired_ns: int):
    family = (rows[-1][1].get("contract") or {}).get("source_family")
    paths = sorted({str(value.get("original_path_at_capture") or "") for _, value in rows})
    chunks = []
    for sha, value in rows:
        for ref in value.get("chunks") or []:
            chunks.append({
                "revision": sha,
                "object_sha256": ref.get("sha256"),
                "offset": ref.get("offset"),
                "bytes": ref.get("bytes"),
            })
    return {
        "schema": SUMMARY_SCHEMA,
        **AUTH,
        "policy": POLICY,
        "raw_detail_available": False,
        "source_id": source_id,
        "source_family": family,
        "expired_ns": expired_ns,
        "revision_sha256": [sha for sha, _ in rows],
        "original_paths": paths,
        "logical_source_bytes": max((int(v.get("source_bytes") or 0) for _, v in rows), default=0),
        "chunk_hashes": chunks,
        "limitations": [
            "Raw HFT detail older than the configured rolling window was intentionally retired.",
            "This summary preserves identity and byte hashes, not replayable observations.",
        ],
    }


def _alias_family(alias: str, runs: Path) -> str:
    path = Path(alias)
    text = str(path)
    for root_name in ("paper_v7_live", "paper_v7_durable"):
        marker = f"/{root_name}/"
        if marker in text:
            return str(classify(text.split(marker, 1)[1]).get("source_family") or "unclassified")
    marker = "/paper_v7_archives/"
    if marker in text:
        tail = text.split(marker, 1)[1]
        parts = tail.split("/", 1)
        relative = parts[1] if len(parts) == 2 else ""
        return str(classify(relative).get("source_family") or "unclassified")
    return str(classify(path.name).get("source_family") or "unclassified")


def _manifest_windowed_old(value: dict[str, Any], cutoff_ns: int, runs: Path) -> tuple[bool, set[str]]:
    aliases = value.get("source_aliases") if isinstance(value.get("source_aliases"), list) else []
    families = {_alias_family(str(alias), runs) for alias in aliases if isinstance(alias, str) and alias}
    stat = value.get("source_original_stat") if isinstance(value.get("source_original_stat"), list) else []
    latest_ns = int(value.get("created_ns") or 0)
    if len(stat) >= 4:
        latest_ns = max(latest_ns, int(stat[3] or 0))
    eligible = bool(aliases and families and families <= WINDOWED and latest_ns > 0 and latest_ns < cutoff_ns)
    return eligible, families




def _existing_tombstone_allows_cleanup(path: Path, current: dict[str, Any], store: Path) -> bool:
    if not path.exists(): return False
    previous=_load(path)
    required={"schema":"polymarket_v7_windowed_pack_tombstone_v1",**AUTH,"policy":POLICY,"raw_detail_available":False,"pack_sha256":current["pack_sha256"]}
    if any(previous.get(k)!=v for k,v in required.items()): raise ValueError(f"unsafe existing windowed tombstone:{current['pack_sha256']}")
    additions={}
    for key in ("source_aliases","source_families","object_sha256s","manifest_sha256s"):
        old=previous.get(key); now=current.get(key)
        if not isinstance(old,list) or not isinstance(now,list): raise ValueError(f"windowed tombstone identity malformed:{current['pack_sha256']}:{key}")
        additions[key]=sorted(set(now)-set(old))
    if any(additions.values()):
        receipt={"schema":"polymarket_v7_windowed_pack_resume_receipt_v1",**AUTH,"policy":POLICY,"raw_detail_available":False,"pack_sha256":current["pack_sha256"],"parent_tombstone_sha256":digest(canonical(previous)),"source_aliases":current["source_aliases"],"source_families":current["source_families"],"added_source_aliases":additions["source_aliases"],"added_source_families":additions["source_families"],"added_object_sha256s":additions["object_sha256s"],"added_manifest_sha256s":additions["manifest_sha256s"]}
        payload=canonical(receipt); sha=digest(payload); immutable(store/"windowed_pack_resume_receipts"/sha[:2]/(sha+".json"),payload)
    return True

def _sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block=handle.read(4*1024*1024)
            if not block: break
            h.update(block)
    return h.hexdigest()


def _verified_windowed_alias(alias: str, *, runs: Path, cutoff_ns: int, pack_sha: str, pack_path: Path | None) -> tuple[Path | None, dict[str, Any] | None, str]:
    path=Path(alias)
    if not path.exists(): return None,None,"MISSING"
    if path.is_symlink() or not path.is_file(): return None,None,"UNSAFE_PATH"
    try: relative=path.resolve().relative_to(runs.resolve())
    except (OSError,ValueError): return None,None,"OUTSIDE_RUNS"
    if relative.parts and relative.parts[0]=="paper_v7_live" and not _closed_path(path,runs):
        return None,None,"ACTIVE_LIVE_ALIAS"
    family=_alias_family(str(path),runs)
    if family not in WINDOWED: return None,None,"NON_WINDOWED_FAMILY"
    info=path.stat()
    if info.st_mtime_ns>=cutoff_ns: return None,None,"RECENT_ALIAS"
    same_inode=False
    if pack_path is not None and pack_path.exists():
        pinfo=pack_path.stat(); same_inode=(info.st_dev,info.st_ino,info.st_size)==(pinfo.st_dev,pinfo.st_ino,pinfo.st_size)
    verified_sha=pack_sha if same_inode else _sha256_file(path)
    if verified_sha!=pack_sha: return None,None,"SHA256_MISMATCH"
    evidence={"path":str(path),"source_family":family,"sha256":verified_sha,"stat":[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns],"verified_by":"SAME_INODE_AS_PACK" if same_inode else "FULL_SHA256"}
    return path,evidence,"VERIFIED"


def _record_alias_retirement(store: Path, tombstone: dict[str, Any], evidence: dict[str, Any]) -> None:
    receipt={"schema":"polymarket_v7_windowed_alias_retirement_receipt_v1",**AUTH,"policy":POLICY,"raw_detail_available":False,"pack_sha256":tombstone["pack_sha256"],"parent_tombstone_sha256":digest(canonical(tombstone)),"alias":evidence}
    payload=canonical(receipt); sha=digest(payload)
    immutable(store/"windowed_alias_retirement_receipts"/sha[:2]/(sha+".json"),payload)


def _safe_unlink(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_symlink():
        raise ValueError(f"refuse symlink unlink:{path}")
    info = path.lstat()
    before = _allocated(path) if int(getattr(info, "st_nlink", 1)) <= 1 else 0
    path.unlink()
    fsync_dir(path.parent)
    return before


def run(runs_root: Path, *, raw_detail_seconds: int = 21600,
        maximum_seconds: float = 180, dry_run: bool = False) -> dict[str, Any]:
    if raw_detail_seconds < 7200 or raw_detail_seconds > 7 * 86400:
        raise ValueError("unsafe raw detail window")
    if maximum_seconds <= 0 or maximum_seconds > 600:
        raise ValueError("unsafe maximum pass duration")
    runs = Path(runs_root).resolve()
    if runs.name != "runs":
        raise ValueError("explicit runs root required")
    store = runs / "paper_v7_durable/permanent_evidence/store"
    started = time.monotonic()
    now_ns = time.time_ns()
    cutoff_ns = now_ns - int(raw_detail_seconds) * 1_000_000_000
    result = {
        "schema": "polymarket_v7_windowed_retention_status_v1",
        **AUTH,
        "policy": POLICY,
        "raw_detail_seconds": raw_detail_seconds,
        "started_ns": now_ns,
        "retired_sources": 0,
        "removed_source_files": 0,
        "removed_source_aliases": 0,
        "alias_integrity_skips": 0,
        "removed_objects": 0,
        "removed_packs": 0,
        "removed_manifests": 0,
        "reclaimed_allocated_bytes": 0,
        "deferred": 0,
        "dry_run": dry_run,
    }
    if not store.is_dir():
        result.update(state="NO_PERMANENT_STORE", after_bytes=allocated_data_bytes([runs]))
        return result

    _, by_source = _scan(store)
    already_retired = {sid for sid in by_source if (store / "retired_sources" / (sid + ".json")).exists()}
    expired = {sid for sid, rows in by_source.items()
               if sid not in already_retired and _source_expired(rows, cutoff_ns)}
    retired_now = set()
    source_phase_seconds = max(1.0, min(maximum_seconds * 0.55, maximum_seconds - 10.0))

    for sid in sorted(expired):
        if time.monotonic() - started >= source_phase_seconds:
            result["deferred"] += len(expired - retired_now)
            break
        rows = sorted(by_source[sid], key=lambda item: int(item[1].get("captured_ns") or 0))
        summary = _summary(sid, rows, now_ns)
        payload = canonical(summary)
        summary_sha = digest(payload)
        summary_path = store / "windowed_summaries" / summary_sha[:2] / (summary_sha + ".json")
        receipt = {
            "schema": RAW_RECEIPT_SCHEMA,
            **AUTH,
            "policy": POLICY,
            "raw_detail_available": False,
            "aggregate": str(summary_path.relative_to(store)),
            "aggregate_sha256": summary_sha,
            "source_ids": [sid],
            "revisions": [sha for sha, _ in rows],
            "retired_at_ns": now_ns,
            "source_paths": summary["original_paths"],
            "source_family": summary["source_family"],
        }
        if not dry_run:
            immutable(summary_path, payload)
            immutable(store / "retired_sources" / (sid + ".json"), canonical(receipt))
        retired_now.add(sid)
        result["retired_sources"] += 1
        for name in summary["original_paths"]:
            path = Path(name)
            if not path.exists() or not _closed_path(path, runs):
                continue
            try:
                if path.stat().st_mtime_ns >= cutoff_ns:
                    continue
                if not dry_run:
                    result["reclaimed_allocated_bytes"] += _safe_unlink(path)
                result["removed_source_files"] += 1
            except OSError:
                pass

    # Only sources with a durable retired_sources receipt may lose replayable bytes.
    # Unprocessed expired sources remain protected until a later pass.
    retirement_ready = already_retired | retired_now
    protected_objects = set()
    expired_objects = set()
    for sid, rows in by_source.items():
        target = expired_objects if sid in retirement_ready else protected_objects
        for _, value in rows:
            for ref in value.get("chunks") or []:
                if ref.get("sha256"):
                    target.add(ref["sha256"])
    expired_objects -= protected_objects

    candidate_packs = set()
    locators = {}
    for path in (store / "objects").glob("*/*.locator.json"):
        value = _load(path)
        obj = value.get("object_sha256")
        pack = value.get("pack_sha256")
        if obj and pack:
            locators[obj] = (path, pack)
            if obj in expired_objects:
                candidate_packs.add(pack)

    for obj in sorted(expired_objects):
        if time.monotonic() - started >= maximum_seconds:
            result["deferred"] += 1
            break
        path = store / "objects" / obj[:2] / (obj + ".gz")
        if path.exists():
            if not dry_run:
                result["reclaimed_allocated_bytes"] += _safe_unlink(path)
            result["removed_objects"] += 1

    protected_packs = {pack for obj, (_, pack) in locators.items() if obj in protected_objects}
    all_manifest_by_pack = defaultdict(list)
    manifest_safe_packs = set()
    manifest_families = defaultdict(set)
    for path in (store / "pack_manifests").glob("*.json"):
        value = _load(path)
        pack = str(value.get("pack_sha256") or "")
        if not pack:
            continue
        all_manifest_by_pack[pack].append((path, value))
    for pack, rows in all_manifest_by_pack.items():
        safe = True
        for _, value in rows:
            eligible, families = _manifest_windowed_old(value, cutoff_ns, runs)
            manifest_families[pack].update(families)
            if not eligible:
                safe = False
        if safe:
            manifest_safe_packs.add(pack)

    raw_candidates = candidate_packs | manifest_safe_packs
    removable_packs = {pack for pack in raw_candidates
                       if pack not in protected_packs
                       and (pack not in all_manifest_by_pack or pack in manifest_safe_packs)}
    result["manifest_windowed_pack_candidates"] = len(manifest_safe_packs)
    result["protected_pack_references"] = len(protected_packs)

    for pack in sorted(removable_packs, key=lambda sha: _allocated(store / "packs" / sha[:2] / (sha + ".pack")), reverse=True):
        if time.monotonic() - started >= maximum_seconds:
            result["deferred"] += 1
            break
        manifest_rows = all_manifest_by_pack.get(pack, [])
        object_ids = sorted(obj for obj, (_, psha) in locators.items() if psha == pack and obj not in protected_objects)
        aliases = sorted({str(alias) for _, value in manifest_rows
                          for alias in (value.get("source_aliases") or []) if isinstance(alias, str)})
        tombstone = {
            "schema": "polymarket_v7_windowed_pack_tombstone_v1",
            **AUTH,
            "policy": POLICY,
            "raw_detail_available": False,
            "pack_sha256": pack,
            "source_aliases": aliases,
            "source_families": sorted(manifest_families.get(pack, set())),
            "object_sha256s": object_ids,
            "manifest_sha256s": sorted(path.stem for path, _ in manifest_rows),
            "retired_at_ns": now_ns,
            "cutoff_ns": cutoff_ns,
        }
        path = store / "packs" / pack[:2] / (pack + ".pack")
        verified_aliases=[]; alias_failure=None
        for alias in aliases:
            alias_path,evidence,reason=_verified_windowed_alias(alias,runs=runs,cutoff_ns=cutoff_ns,pack_sha=pack,pack_path=path)
            if reason=="MISSING": continue
            if reason!="VERIFIED": alias_failure=(alias,reason); break
            verified_aliases.append((alias_path,evidence))
        if alias_failure is not None:
            result["alias_integrity_skips"] += 1
            continue
        tombstone_path = store / "windowed_pack_tombstones" / (pack + ".json")
        if not dry_run:
            if not _existing_tombstone_allows_cleanup(tombstone_path, tombstone, store):
                immutable(tombstone_path, canonical(tombstone))
            persisted_tombstone=_load(tombstone_path)
            for alias_path,evidence in verified_aliases:
                _record_alias_retirement(store,persisted_tombstone,evidence)
                result["reclaimed_allocated_bytes"] += _safe_unlink(alias_path)
                result["removed_source_aliases"] += 1
        else:
            result["removed_source_aliases"] += len(verified_aliases)
        if path.exists():
            if not dry_run:
                result["reclaimed_allocated_bytes"] += _safe_unlink(path)
            result["removed_packs"] += 1
        for obj, (locator, psha) in list(locators.items()):
            if psha == pack and obj not in protected_objects and locator.exists():
                if not dry_run:
                    _safe_unlink(locator)
        for manifest, _ in manifest_rows:
            if manifest.exists():
                if not dry_run:
                    _safe_unlink(manifest)
                result["removed_manifests"] += 1

    # Resume aliases left behind by an interrupted older pass even when the pack/manifest link is already gone.
    alias_candidates=[]
    for tombstone_path in (store / "windowed_pack_tombstones").glob("*.json"):
        tombstone=_load(tombstone_path)
        if (tombstone.get("schema")!="polymarket_v7_windowed_pack_tombstone_v1" or tombstone.get("policy")!=POLICY
                or tombstone.get("paper_only") is not True or tombstone.get("authenticated_execution") is not False
                or tombstone.get("real_order_submission") is not False or tombstone.get("raw_detail_available") is not False):
            continue
        pack=str(tombstone.get("pack_sha256") or "")
        if len(pack)!=64: continue
        pack_path=store/"packs"/pack[:2]/(pack+".pack")
        for alias in tombstone.get("source_aliases") or []:
            if isinstance(alias,str) and Path(alias).exists():
                alias_candidates.append((_allocated(Path(alias)),alias,pack,pack_path,tombstone))
    for _,alias,pack,pack_path,tombstone in sorted(alias_candidates,reverse=True):
        if time.monotonic()-started>=maximum_seconds:
            result["deferred"] += 1; break
        alias_path,evidence,reason=_verified_windowed_alias(alias,runs=runs,cutoff_ns=cutoff_ns,pack_sha=pack,pack_path=pack_path)
        if reason=="MISSING": continue
        if reason!="VERIFIED":
            result["alias_integrity_skips"] += 1; continue
        if not dry_run:
            _record_alias_retirement(store,tombstone,evidence)
            result["reclaimed_allocated_bytes"] += _safe_unlink(alias_path)
        result["removed_source_aliases"] += 1

    result["after_bytes"] = allocated_data_bytes([runs])
    result["completed_ns"] = time.time_ns()
    result["state"] = "PASS_TIME_LIMIT_REACHED" if result["deferred"] else "WINDOW_ENFORCED"
    status = runs / "paper_v7_durable/permanent_evidence/windowed_retention_status.json"
    if not dry_run:
        status.parent.mkdir(parents=True, exist_ok=True)
        temporary = status.with_name(status.name + f".tmp.{os.getpid()}")
        temporary.write_text(json.dumps(result, sort_keys=True) + "\n")
        os.replace(temporary, status)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--raw-detail-seconds", type=int, default=21600)
    parser.add_argument("--maximum-seconds", type=float, default=180)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.runs_root, raw_detail_seconds=args.raw_detail_seconds,
                         maximum_seconds=args.maximum_seconds, dry_run=args.dry_run), sort_keys=True))


if __name__ == "__main__":
    main()
