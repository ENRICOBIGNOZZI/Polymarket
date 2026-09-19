"""Causal research datasets and bounded publication, without runtime authority.

Works on explicit executable-evidence bundles. It does not crawl live files,
change collectors, infer missing depth or authenticate to any exchange.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from v7_research_economic_contract import (
    AUTHORITY, BookFrame, BookSeries, CoverageProof, LatencyScenario,
    MarketEconomics, ResearchContractError, ResearchDecision, canonical_hash,
    decimal, digest, identity, integer, round_trip_label,
)

BUNDLE_SCHEMA = "polymarket_v7_executable_evidence_bundle_v1"
MANIFEST_SCHEMA = "polymarket_v7_executable_dataset_manifest_v1"
MAXIMUM_MANAGED_BYTES = 60_000_000_000


def strict_json(payload: str | bytes) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ResearchContractError("duplicate_json_key")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ResearchContractError("nonfinite_json:" + value)
    return json.loads(payload, object_pairs_hook=unique, parse_constant=invalid_constant)


def evaluate_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "market", "decision", "books", "coverage", "entry_latency",
                "exit_latency", "holding_ns", "features", "calendar_block", "market_open_ns"}
    if not isinstance(bundle, dict) or set(bundle) != required or bundle.get("schema") != BUNDLE_SCHEMA:
        raise ResearchContractError("bundle_schema")
    market = MarketEconomics(**bundle["market"])
    decision = ResearchDecision(**bundle["decision"])
    features = bundle["features"]
    if not isinstance(features, dict) or not features:
        raise ResearchContractError("missing_features")
    for name, value in features.items():
        identity(name, "feature_name")
        if value is not None:
            decimal(value)
    if canonical_hash(features) != decision.feature_hash:
        raise ResearchContractError("feature_cut_hash_mismatch")
    identity(bundle["calendar_block"], "calendar_block")
    integer(bundle["market_open_ns"], "market_open_ns", 1)
    if bundle["market_open_ns"] > decision.decision_ns:
        raise ResearchContractError("market_not_open_at_decision")
    frames = tuple(BookFrame(**row) for row in bundle["books"])
    series = BookSeries(frames, CoverageProof(**bundle["coverage"]))
    label = round_trip_label(decision, market, series, LatencyScenario(**bundle["entry_latency"]),
                            LatencyScenario(**bundle["exit_latency"]), bundle["holding_ns"])
    return {"schema": "polymarket_v7_executable_example_v1", "paper_only": True,
            "execution_authority": AUTHORITY, "features": features,
            "calendar_block": bundle["calendar_block"], "market_open_ns": bundle["market_open_ns"],
            "bundle_hash": canonical_hash(bundle), "label": label}


@dataclass(frozen=True)
class SplitTimes:
    clock_domain: str
    train_start_ns: int
    validation_start_ns: int
    audit_start_ns: int
    freeze_ns: int
    embargo_ns: int = 0

    def __post_init__(self) -> None:
        identity(self.clock_domain, "split_clock_domain")
        for name in ("train_start_ns", "validation_start_ns", "audit_start_ns", "freeze_ns"):
            integer(getattr(self, name), name, 1)
        integer(self.embargo_ns, "embargo_ns")
        if not self.train_start_ns < self.validation_start_ns < self.audit_start_ns < self.freeze_ns:
            raise ResearchContractError("split_time_order")


def split_examples(rows: Iterable[dict[str, Any]], times: SplitTimes) -> dict[str, Any]:
    """Purged chronological split by market AND shared calendar block.

    Union components prevent overlapping markets or correlated calendar blocks
    from being split across partitions. A component crossing a boundary is
    excluded, not partially retained. No randomized split or row-level shuffle.
    """
    materialized = list(rows)
    parent: dict[str, str] = {}
    seen: set[str] = set()
    excluded: Counter[str] = Counter()
    eligible: list[dict[str, Any]] = []
    def find(key: str) -> str:
        parent.setdefault(key, key)
        root = key
        while parent[root] != root:
            root = parent[root]
        while parent[key] != key:
            key, parent[key] = parent[key], root
        return root
    def union(a: str, b: str) -> None:
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)
    for row in materialized:
        if not isinstance(row, dict) or row.get("schema") != "polymarket_v7_executable_example_v1" or row.get("paper_only") is not True or row.get("execution_authority") != AUTHORITY:
            raise ResearchContractError("example_schema_or_authority")
        label = row["label"]
        if label.get("clock_domain") != times.clock_domain:
            raise ResearchContractError("mixed_clock_domains")
        if label.get("schema") != "polymarket_v7_executable_research_label_v1" or label.get("execution_authority") != AUTHORITY or label.get("paper_only") is not True or label.get("real_order_submission") is not False:
            raise ResearchContractError("label_schema_or_authority")
        supplied_hash = digest(label.get("label_hash"), "label_hash")
        if canonical_hash({k: v for k, v in label.items() if k != "label_hash"}) != supplied_hash:
            raise ResearchContractError("label_hash_mismatch")
        decision_id = identity(label["decision_id"], "decision_id")
        if decision_id in seen:
            raise ResearchContractError("duplicate_decision")
        seen.add(decision_id)
        if canonical_hash(row["features"]) != label["feature_hash"]:
            raise ResearchContractError("feature_hash_mismatch")
        market = "market:" + identity(label["market_id"], "market_id")
        block = "block:" + identity(row["calendar_block"], "calendar_block")
        union(market, block)
        # Even censored/pending rows participate in grouping. They cannot make
        # their market's earlier observations leak into a different partition.
        eligible.append(row)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        groups[find("market:" + row["label"]["market_id"])].append(row)
    partitions: dict[str, list[dict[str, Any]]] = {name: [] for name in ("train", "validation", "audit")}
    boundaries = (
        ("train", times.train_start_ns, times.validation_start_ns),
        ("validation", times.validation_start_ns, times.audit_start_ns),
        ("audit", times.audit_start_ns, times.freeze_ns),
    )
    for group in groups.values():
        first = min(integer(row["label"]["origin_ns"], "origin_ns", 1) for row in group)
        last = max(integer(row["label"]["origin_ns"], "origin_ns", 1) for row in group)
        part = next(((name, stop) for name, start, stop in boundaries if start <= first <= last < stop), None)
        if part is None:
            excluded["GROUP_CROSSES_TIME_BOUNDARY_OR_OUTSIDE_WINDOW"] += len(group)
            continue
        name, stop = part
        for row in group:
            label = row["label"]
            origin = label["origin_ns"]
            feature = integer(label["feature_available_ns"], "feature_time", 1)
            end = integer(label["information_end_ns"], "information_end", origin)
            available = integer(label["label_available_ns"], "label_available", end)
            if feature > origin:
                raise ResearchContractError("future_feature_in_dataset")
            if label["status"] not in {"COMPLETE", "NO_FILL"} or label["net_pnl"] is None:
                excluded["CENSORED_NOT_ZERO_IMPUTED"] += 1
                continue
            if available + times.embargo_ns >= stop:
                excluded["LABEL_UNAVAILABLE_BEFORE_NEXT_PARTITION"] += 1
                continue
            decimal(label["net_pnl"])
            partitions[name].append(row)
    for rows_in_part in partitions.values():
        rows_in_part.sort(key=lambda r: (r["label"]["origin_ns"], r["label"]["decision_id"]))
    return {"partitions": partitions, "excluded": dict(sorted(excluded.items())),
            "split_times": asdict(times), "split_hash": canonical_hash(asdict(times)),
            "unit": "COUNTERFACTUAL_DECISION_NOT_TRADABLE_PORTFOLIO",
            "input_rows": len(materialized), "group_count": len(groups)}


def retention_admission(*, managed_bytes: int, new_bytes: int, budget_bytes: int,
                        source_recoverable: bool, labels_mature: bool) -> dict[str, Any]:
    """Read-only preflight. Does not delete, migrate, reserve or resize storage."""
    for name, value in (("managed_bytes", managed_bytes), ("new_bytes", new_bytes), ("budget_bytes", budget_bytes)):
        integer(value, name)
    if not 0 < budget_bytes <= MAXIMUM_MANAGED_BYTES:
        raise ResearchContractError("unauthorized_storage_budget")
    if type(source_recoverable) is not bool or type(labels_mature) is not bool:
        raise ResearchContractError("retention_evidence_unknown")
    return {"admit_under_snapshot_budget": managed_bytes + new_bytes <= budget_bytes,
            "projected_bytes": managed_bytes + new_bytes, "budget_bytes": budget_bytes,
            "source_deletion_authorized": False,
            "prune_preconditions_met": source_recoverable and labels_mature,
            "requires_single_owner_recheck": True, "external_storage_allowed": False}


def process_jsonl(source: Path, output: Path, *, maximum_input_bytes: int = 64 * 1024 * 1024,
                  maximum_output_bytes: int = 64 * 1024 * 1024,
                  maximum_line_bytes: int = 4 * 1024 * 1024) -> dict[str, Any]:
    """Bounded streaming builder, atomic no-overwrite publication.

    Input must be a closed immutable file. File metadata and exact content are
    checked; manifests include a content hash, not an unverified path identity.
    Output carries per-row exclusions. Unknown economics remain excluded.
    """
    import hashlib
    import stat
    for name, value in (("maximum_input_bytes", maximum_input_bytes),
                        ("maximum_output_bytes", maximum_output_bytes),
                        ("maximum_line_bytes", maximum_line_bytes)):
        integer(value, name, 1)
    source, output = Path(source), Path(output)
    if source.is_symlink() or not source.is_file() or source.resolve() == output.resolve():
        raise ResearchContractError("unsafe_input_or_output")
    manifest_path = output.with_name(output.name + ".manifest.json")
    if output.exists() or manifest_path.exists():
        raise ResearchContractError("immutable_output_exists")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ResearchContractError("output_parent_must_exist")
    before = source.stat()
    if before.st_size > maximum_input_bytes:
        raise ResearchContractError("input_budget_exceeded")
    source_hash, output_hash = hashlib.sha256(), hashlib.sha256()
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    total = 0
    fd, temporary = tempfile.mkstemp(prefix=".v7-research-", dir=output.parent)
    try:
        with source.open("rb") as inp, os.fdopen(fd, "wb") as out:
            opened = os.fstat(inp.fileno())
            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if not stat.S_ISREG(opened.st_mode) or signature(opened) != signature(before):
                raise ResearchContractError("input_changed_before_read")
            consumed = 0
            while True:
                raw = inp.readline(maximum_line_bytes + 1)
                if not raw:
                    break
                consumed += len(raw)
                if consumed > maximum_input_bytes or len(raw) > maximum_line_bytes or not raw.endswith(b"\n"):
                    raise ResearchContractError("input_size_or_incomplete_record")
                source_hash.update(raw)
                bundle = strict_json(raw)
                bundle_id = canonical_hash(bundle)
                if bundle_id in seen:
                    raise ResearchContractError("duplicate_bundle")
                seen.add(bundle_id)
                try:
                    example = evaluate_bundle(bundle)
                    counts[example["label"]["status"]] += 1
                except (ResearchContractError, TypeError, KeyError) as exc:
                    counts["EXCLUDED"] += 1
                    # Exclusions are diagnostic records, never training zeros.
                    example = {"schema": "polymarket_v7_executable_exclusion_v1",
                               "bundle_hash": bundle_id, "reason": str(exc),
                               "paper_only": True, "execution_authority": AUTHORITY}
                rendered = (json.dumps(example, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
                total += len(rendered)
                if total > maximum_output_bytes:
                    raise ResearchContractError("output_budget_exceeded")
                output_hash.update(rendered)
                out.write(rendered)
            if signature(source.stat()) != signature(before) or signature(os.fstat(inp.fileno())) != signature(before):
                raise ResearchContractError("input_changed_during_read")
            out.flush()
            os.fsync(out.fileno())
        manifest = {"schema": MANIFEST_SCHEMA, "paper_only": True, "research_only": True,
                    "execution_authority": AUTHORITY, "source_sha256": source_hash.hexdigest(),
                    "output_sha256": output_hash.hexdigest(), "output_bytes": total,
                    "counts": dict(sorted(counts.items())), "automatic_promotion": False,
                    "source_bytes": before.st_size, "economic_performance_claim": False}
        manifest["manifest_hash"] = canonical_hash(manifest)
        # Hard-link publication is atomic and refuses an existing destination.
        # Publish the manifest last: consumers must reject an orphan data file.
        os.link(temporary, output)
        manifest_fd, manifest_tmp = tempfile.mkstemp(prefix=".v7-manifest-", dir=output.parent)
        try:
            with os.fdopen(manifest_fd, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.link(manifest_tmp, manifest_path)
        finally:
            os.unlink(manifest_tmp)
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return manifest
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
        # Never remove a published evidence file on failure. An orphan needs
        # explicit recovery, not silent replacement with a different dataset.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-input-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--maximum-output-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    try:
        manifest = process_jsonl(args.input, args.output, maximum_input_bytes=args.maximum_input_bytes,
                                 maximum_output_bytes=args.maximum_output_bytes)
    except (OSError, ValueError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
