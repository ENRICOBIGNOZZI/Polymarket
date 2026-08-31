#!/usr/bin/env python3
"""Archive public HTTP evidence bytes without credentials or execution authority."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

RUN = re.compile(r"^[A-Za-z0-9._-]+$")


class PublicCollectorError(ValueError): pass


def collect(root: Path, *, run_id: str, source: str, url: str,
            fetch: Callable[[str], bytes] | None = None,
            now: datetime | None = None) -> dict:
    if not RUN.fullmatch(run_id) or not RUN.fullmatch(source) or not url.startswith("https://"):
        raise PublicCollectorError("collector_identity")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None: raise PublicCollectorError("collector_time")
    payload = fetch(url) if fetch else urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-public-collector/1"}), timeout=15).read()
    if not isinstance(payload, bytes) or not payload: raise PublicCollectorError("collector_payload")
    sha = hashlib.sha256(payload).hexdigest()
    directory = Path(root) / "artifacts" / "raw" / run_id / source / sha
    directory.mkdir(parents=True, exist_ok=True)
    raw_path = directory / "payload.bin"
    if raw_path.exists() and raw_path.read_bytes() != payload: raise PublicCollectorError("immutable_collision")
    if not raw_path.exists(): raw_path.write_bytes(payload)
    observed_at = now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest = {"schema_version": 1, "source": source, "source_instance": url, "connection_epoch": 0,
                "sequence_or_message_id": sha, "exchange_timestamp": None, "source_observation_timestamp": None,
                "local_kernel_receive_timestamp": None, "local_wall_receive_timestamp": observed_at,
                "parse_complete_timestamp": observed_at, "publish_timestamp": observed_at,
                "raw_payload_hash": sha, "raw_payload_location": str(raw_path.relative_to(root)),
                "gap_state": "UNKNOWN", "reconnect_state": "NOT_APPLICABLE", "clock_quality": "LOCAL_ONLY", "exact_run_id": run_id}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    event_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    manifest_path = directory / "events" / f"{event_hash}.json"
    manifest_path.parent.mkdir(exist_ok=True)
    if manifest_path.exists() and manifest_path.read_text(encoding="utf-8") != encoded: raise PublicCollectorError("immutable_manifest_collision")
    if not manifest_path.exists(): manifest_path.write_text(encoded, encoding="utf-8")
    return {**manifest, "manifest_hash": event_hash, "manifest_location": str(manifest_path.relative_to(root))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".")); parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", required=True); parser.add_argument("--url", required=True)
    args = parser.parse_args(); print(json.dumps(collect(args.root.resolve(), run_id=args.run_id, source=args.source, url=args.url), sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
