"""Private, deterministic research publication and safety boundaries."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from v7_evidence_store import AUTH, canonical, digest, immutable

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
HORIZONS = ("M5", "M15", "H1", "H4", "D1")
CONTEXTS = tuple(f"{a}:{h}" for a in ASSETS for h in HORIZONS)
SAFETY = {**AUTH, "automatic_promotion": False, "runtime_training": False}


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError("NONFINITE_JSON")))


def publish(root, kind, value):
    payload = canonical(value)
    sha = digest(payload)
    path = Path(root) / kind / (sha + ".json")
    immutable(path, payload)
    os.chmod(path, 0o600)
    return sha


def source_rows(store, revision):
    """Read verified JSON snapshots or complete JSONL records, never source paths."""
    metadata = store.revision(revision)
    name = metadata["relative_path"]
    if name.endswith((".json", ".json.gz")):
        if not metadata.get("capture_complete") or metadata["source_bytes"] > 16*1024**2:
            raise ValueError("INCOMPLETE_OR_OVERSIZED_JSON_SNAPSHOT")
        value = json.loads(b"".join(store.bytes(revision)))
        if not isinstance(value, dict):
            raise ValueError("NON_OBJECT_JSON_SNAPSHOT")
        yield value
    else:
        yield from store.json_rows(revision)


def research_host(root):
    """Explicit enrollment, checked on every invocation; no London fallback."""
    root = Path(root).resolve()
    marker = read_json(root / "research_host.json")
    if (marker.get("schema") != "v7_research_host_v1"
            or marker.get("hostname") != platform.node()
            or marker.get("role") != "RESEARCH_ONLY"
            or os.environ.get("PM_V7_RUNTIME_PROFILE") == "london"
            or Path("/etc/systemd/system/polymarket-v7-paper.service").exists()):
        raise ValueError("LONDON_CANNOT_TRAIN_OR_RESEARCH_HOST_NOT_ENROLLED")
    if root == ROOT or ROOT in root.parents:
        # Only explicitly ignored private run directories are accepted in repo.
        if ROOT / "runs" not in root.parents:
            raise ValueError("PRIVATE_OUTPUT_MUST_BE_OUTSIDE_PUBLIC_TREE")
    return root
