"""Read-only exhaustive inventory; compatible sources frozen in existing store.

Unknown schemas/binary formats remain inventoried, never guessed into features.
Inventory hashes are of actual stored bytes; evidence revisions hash decoded
bytes. Both identities are retained. Sources are never deleted or modified.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
import os
from pathlib import Path
import tarfile
import tempfile
import hashlib
import shutil

from .common import SAFETY, canonical, digest, immutable, publish, sha_file
from v7_evidence_catalog import classify
from v7_evidence_store import EvidenceStore, stat_identity

NATIVE = "polymarket_v7_native_observation_v1"
FAIR = "polymarket_v7_external_fair_counterfactual_v1"
LABEL = "v7_public_settlement_evidence_v1"
CLOSED = "polymarket_v7_native_capture_closed_v1"
SUPPORTED = {NATIVE, FAIR, LABEL, CLOSED}
IDENTITY_FIELDS = ("model_sha", "code_sha", "run_id", "config_hash", "policy_hash",
                   "policy_sha256", "asset", "horizon", "market_id")
TIMES = {"decision_wall_ns": 1, "observed_wall_ns": 1, "timestamp_ns": 1,
         "observed_ms": 1_000_000, "timestamp_ms": 1_000_000,
         "recorded_ts_ms": 1_000_000}


def metadata(stream, *, jsonl):
    schemas = Counter(); identities = defaultdict(set); events = Counter()
    first = last = None; rows = invalid = labels = 0
    for raw in stream if jsonl else [stream.read()]:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError):
            invalid += 1; continue
        if not isinstance(row, dict):
            invalid += 1; continue
        rows += 1
        schemas[str(row.get("schema") or "UNKNOWN")] += 1
        events[str(row.get("event_type") or row.get("kind") or "UNKNOWN")] += 1
        labels += row.get("event_type") in {"FINAL", "FORECAST_FINAL"} or row.get("schema") == LABEL
        for key in IDENTITY_FIELDS:
            value = row.get(key)
            if isinstance(value, (str, int)) and value != "":
                identities[key].add(str(value))
        # Wall clocks only; never mislabel monotonic or file mtime as event time.
        for key, scale in TIMES.items():
            v = row.get(key)
            if type(v) is int and v > 0:
                t = v * scale
                first = t if first is None else min(first, t)
                last = t if last is None else max(last, t)
                break
    return {"rows": rows, "invalid_records": invalid, "schemas": dict(schemas),
            "events": dict(events), "label_records": labels, "first_timestamp_ns": first,
            "last_timestamp_ns": last, **{k: sorted(identities[k]) or None for k in IDENTITY_FIELDS}}


def inventory(roots, output, *, freeze=True):
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=True)
    entries = []; errors = []; seen_paths = set(); revisions = set()
    store = EvidenceStore(output / "store") if freeze else None
    try:
        for root in sorted({str(Path(p).resolve()) for p in roots}):
            if not Path(root).is_dir():
                errors.append({"path": root, "reason": "ROOT_UNAVAILABLE"}); continue
            for folder, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__", ".rsync-partial"}
                                  and not (Path(folder) / d).is_symlink()
                                  and not (Path(folder) / d).resolve().is_relative_to(output))
                for name in sorted(names):
                    path = Path(folder) / name
                    if path in seen_paths:
                        continue
                    seen_paths.add(path)
                    if path.is_symlink() or not path.is_file():
                        errors.append({"path": str(path), "reason": "NONREGULAR_SOURCE"}); continue
                    if any(s in name.lower() for s in ("credential", "secret", ".pem", ".key", ".env")):
                        errors.append({"path": str(path), "reason": "CREDENTIAL_FILE_NOT_EVIDENCE"}); continue
                    before = path.stat()
                    entry = {"path": str(path), "root": root, "source_generation": str(path.parent),
                             "source_family": classify(str(path.relative_to(root)))["source_family"],
                             "bytes": before.st_size, "allocated_bytes": before.st_blocks * 512,
                             "device_inode": [before.st_dev, before.st_ino], "sha256": None,
                             "compression": "GZIP" if name.endswith(".gz") else "NONE",
                             "raw_available": True, "labels_available": None, "timing_causal": None,
                             "training_eligible": False, "reason": "UNSUPPORTED_SCHEMA_OR_BINARY_REQUIRES_EXPLICIT_ADAPTER",
                             "rows": None, "schemas": None, "first_timestamp_ns": None, "last_timestamp_ns": None,
                             **{k: None for k in IDENTITY_FIELDS}}
                    try:
                        entry["sha256"] = sha_file(path)
                        is_lines = ".jsonl" in name
                        if is_lines or (name.endswith((".json", ".json.gz")) and before.st_size < 16 * 1024**2):
                            opener = gzip.open if name.endswith(".gz") else open
                            with opener(path, "rb") as stream:
                                entry.update(metadata(stream, jsonl=is_lines))
                            entry["labels_available"] = entry["label_records"] > 0
                        if stat_identity(path.stat()) != stat_identity(before):
                            raise ValueError("SOURCE_CHANGED_DURING_INVENTORY")
                        if name.endswith((".tar.gz", ".tgz", ".tar")):
                            archive_entries, archive_revisions = archive_members(path, entry["sha256"], store)
                            entries.extend(archive_entries); revisions.update(archive_revisions)
                            if stat_identity(path.stat()) != stat_identity(before):
                                raise ValueError("ARCHIVE_CHANGED_DURING_INVENTORY")
                        compatible = set(entry.get("schemas") or {}) & SUPPORTED
                        if compatible:
                            entry["reason"] = "ROW_CAUSALITY_AND_LABEL_AUDIT_REQUIRED"
                            if store and entry.get("invalid_records") == 0:
                                contract = {"source_family": "learning_evidence", "schemas": entry["schemas"],
                                            "original_sha256": entry["sha256"]}
                                captured = store.capture(path, partition="learning:" + digest(root.encode()),
                                                         relative=str(path.relative_to(root)), contract=contract,
                                                         append=False)
                                revision = captured["revision"]
                                # Capture must still correspond to the inventoried bytes.
                                if stat_identity(path.stat()) != stat_identity(before) or sha_file(path) != entry["sha256"]:
                                    raise ValueError("SOURCE_CHANGED_DURING_FREEZE")
                                entry["source_revision"] = revision; revisions.add(revision)
                        entries.append(entry)
                    except (OSError, ValueError, EOFError) as exc:
                        entry.update(reason=str(exc), training_eligible=False)
                        entries.append(entry); errors.append({"path": str(path), "reason": str(exc)})
    finally:
        if store:
            store.close()
    payload = b"".join(canonical(r) + b"\n" for r in sorted(entries, key=lambda e: e["path"]))
    content_sha = digest(payload)
    immutable(output / "catalogs" / (content_sha + ".jsonl.gz"), gzip.compress(payload, mtime=0))
    distinct = {}
    for row in entries:
        if row["sha256"]:
            distinct[row["sha256"]] = row["bytes"]
    summary = {"schema": "v7_learning_catalog_v1", **SAFETY, "roots": sorted(map(str, roots)),
               "catalog_sha256": content_sha, "files": len(entries),
               "logical_bytes": sum(e["bytes"] for e in entries if not e.get("archive_member")),
               "decoded_archive_member_bytes": sum(e["bytes"] for e in entries if e.get("archive_member")),
               "unique_content_bytes": sum(distinct.values()),
               "sources_with_labels": sum(e["labels_available"] is True for e in entries),
               "source_revisions": sorted(revisions), "errors": errors,
               "coverage_scope": "EXPLICIT_ROOTS_ONLY; unavailable roots and unsupported archive members are not claimed recovered"}
    summary["manifest_sha256"] = publish(output, "catalog_manifests", summary)
    return summary


def archive_members(path, archive_sha, store):
    """Stream all members without extracting paths or changing the source archive."""
    entries = []; revisions = set()
    with tarfile.open(path, "r|*") as archive, tempfile.TemporaryDirectory(prefix="v7-archive-read-") as tmp:
        for member in archive:
            if not member.isfile():
                continue
            name = member.name
            if any(s in Path(name).name.lower() for s in ("credential", "secret", ".pem", ".key", ".env")):
                continue
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("UNSAFE_ARCHIVE_MEMBER_PATH")
            entry = {"path": str(path)+"!"+name, "archive_member": True, "archive_sha256": archive_sha,
                     "root": str(path), "source_generation": name, "bytes": member.size,
                     "compression": "TAR_MEMBER", "raw_available": True, "labels_available": None,
                     "timing_causal": None, "training_eligible": False, "rows": None, "schemas": None,
                     "reason": "UNSUPPORTED_SCHEMA_OR_BINARY_REQUIRES_EXPLICIT_ADAPTER",
                     "first_timestamp_ns": None, "last_timestamp_ns": None,
                     **{k: None for k in IDENTITY_FIELDS}}
            checksum = hashlib.sha256()
            is_json = name.endswith((".jsonl", ".json"))
            staged = Path(tmp)/"member.jsonl"
            with archive.extractfile(member) as source:
                if is_json:
                    with staged.open("wb") as target:
                        for block in iter(lambda: source.read(1 << 20), b""):
                            checksum.update(block); target.write(block)
                    with staged.open("rb") as source:
                        entry.update(metadata(source, jsonl=name.endswith(".jsonl")))
                    entry["labels_available"] = entry["label_records"] > 0
                    if store and set(entry["schemas"]) & SUPPORTED and not entry["invalid_records"]:
                        partition = "archive:"+archive_sha
                        source_id = digest(canonical({"partition": partition, "path": name}))
                        contract = {"source_family": "learning_evidence", "schemas": entry["schemas"],
                                    "archive_sha256": archive_sha, "member_sha256": checksum.hexdigest()}
                        previous_sha, previous = store.latest(source_id)
                        if previous and previous.get("contract") == contract:
                            # Reuse immutable archive content independent of temp inode.
                            store.revision(previous_sha)
                            for _ in store.bytes(previous_sha):
                                pass
                            result = {"revision": previous_sha}
                        else:
                            result = store.capture(staged, partition=partition, relative=name, contract=contract, append=False)
                        entry["source_revision"] = result["revision"]; revisions.add(result["revision"])
                        entry["reason"] = "ROW_CAUSALITY_AND_LABEL_AUDIT_REQUIRED"
                    staged.unlink()
                else:
                    for block in iter(lambda: source.read(1 << 20), b""):
                        checksum.update(block)
            entry["sha256"] = checksum.hexdigest(); entries.append(entry)
    return entries, revisions


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, action="append", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--no-freeze", action="store_true")
    args = ap.parse_args()
    result = inventory(args.root, args.output, freeze=not args.no_freeze)
    print(json.dumps({k: v for k, v in result.items() if k != "source_revisions"}, sort_keys=True))


if __name__ == "__main__":
    main()
