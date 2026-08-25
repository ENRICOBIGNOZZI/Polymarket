#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[a-z0-9]+")
GENERIC_RELATION_TOKENS = {
    "a", "an", "and", "are", "at", "be", "before", "by", "champion",
    "championship", "election", "final", "finals", "for", "from", "happen",
    "in", "is", "it", "league", "market", "no", "of", "on", "or",
    "president", "presidential", "the", "this", "title", "to", "was", "were",
    "who", "will", "win", "winning", "wins", "yes",
}


@dataclass(frozen=True)
class Certificate:
    scope: str
    similarity: float
    shared_tokens: int


def parse_certificate(raw: str) -> Certificate | None:
    parts = raw.rsplit(":", 2)
    if len(parts) != 3:
        return None
    scope, similarity_raw, shared_raw = parts
    try:
        similarity = float(similarity_raw)
        shared_tokens = int(shared_raw)
    except ValueError:
        return None
    return Certificate(scope=scope, similarity=similarity, shared_tokens=shared_tokens)


def certificate_is_strong(
    cert: Certificate,
    min_jaccard: float,
    min_shared_tokens: int,
    allow_same_category: bool,
) -> bool:
    if cert.scope == "same_event":
        return True
    if cert.scope == "semantic":
        return cert.similarity >= min_jaccard and cert.shared_tokens >= min_shared_tokens
    if cert.scope == "same_category":
        return allow_same_category
    return False


def meaningful_context_tokens(meta: dict[str, Any]) -> set[str]:
    text = f"{meta.get('slug', '')} {meta.get('question', '')}".lower()
    return {
        token
        for token in TOKEN_RE.findall(text)
        if len(token) >= 2 and token not in GENERIC_RELATION_TOKENS
    }


def load_metadata(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = raw.get("markets", raw) if isinstance(raw, dict) else {}
    if not isinstance(records, dict):
        return {}
    return {str(key): value for key, value in records.items() if isinstance(value, dict)}


def parse_hedge_ids(row: dict[str, str]) -> list[str]:
    target = (row.get("market") or "").strip()
    out: list[str] = []
    for raw in (row.get("legs") or "").split("|"):
        market_id = raw.split(":", 1)[0].strip()
        if market_id and market_id != target and market_id not in out:
            out.append(market_id)
    return out


def semantic_context_is_strong(
    target: dict[str, Any] | None,
    hedge: dict[str, Any] | None,
    min_context_shared_tokens: int,
) -> tuple[bool, list[str]]:
    if not target or not hedge:
        return False, []
    shared = meaningful_context_tokens(target) & meaningful_context_tokens(hedge)
    return len(shared) >= min_context_shared_tokens, sorted(shared)


def classify_row(
    row: dict[str, str],
    metadata: dict[str, dict[str, Any]],
    min_jaccard: float,
    min_shared_tokens: int,
    min_context_shared_tokens: int,
    allow_same_category: bool,
) -> tuple[bool, list[str], list[str]]:
    raw_items = [item for item in (row.get("coherence_scope") or "").split("|") if item]
    hedge_ids = parse_hedge_ids(row)
    if not raw_items:
        return False, ["missing_relation_certificate"], []
    if len(raw_items) != len(hedge_ids):
        return False, ["certificate_hedge_count_mismatch"], []

    target_id = (row.get("market") or "").strip()
    target = metadata.get(target_id)
    weak: list[str] = []
    context_notes: list[str] = []
    for raw, hedge_id in zip(raw_items, hedge_ids):
        cert = parse_certificate(raw)
        if cert is None or not certificate_is_strong(
            cert, min_jaccard, min_shared_tokens, allow_same_category
        ):
            weak.append(raw)
            continue
        if cert.scope != "semantic":
            context_notes.append(f"{hedge_id}:{cert.scope}")
            continue
        context_ok, shared_context = semantic_context_is_strong(
            target, metadata.get(hedge_id), min_context_shared_tokens
        )
        context_notes.append(
            f"{hedge_id}:context={','.join(shared_context) if shared_context else '<none>'}"
        )
        if not context_ok:
            weak.append(raw)
    return not weak, weak, context_notes


def gate_rows(
    rows: list[dict[str, str]],
    metadata: dict[str, dict[str, Any]],
    min_jaccard: float,
    min_shared_tokens: int,
    min_context_shared_tokens: int,
    allow_same_category: bool,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    kept: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    for row in rows:
        ok, weak, notes = classify_row(
            row,
            metadata,
            min_jaccard,
            min_shared_tokens,
            min_context_shared_tokens,
            allow_same_category,
        )
        item = dict(row)
        item["strong_relation_context"] = "|".join(notes)
        if ok:
            kept.append(item)
            continue
        item["strong_relation_reason"] = "weak_generic_or_nonsemantic_relation"
        item["weak_relation_certificates"] = "|".join(weak)
        rejected.append(item)
    return kept, rejected


def write_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rejections", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--min-jaccard", type=float, default=0.20)
    parser.add_argument("--min-shared-tokens", type=int, default=2)
    parser.add_argument("--min-context-shared-tokens", type=int, default=2)
    parser.add_argument("--allow-same-category", action="store_true")
    args = parser.parse_args()

    if args.input.exists() and args.input.stat().st_size > 0:
        with args.input.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
    else:
        fieldnames = []
        rows = []

    if "coherence_scope" not in fieldnames and rows:
        raise SystemExit("input CSV must contain coherence_scope")

    cache_path = args.cache or args.input.parent / "market_metadata_cache.json"
    metadata = load_metadata(cache_path)
    kept, rejected = gate_rows(
        rows,
        metadata=metadata,
        min_jaccard=args.min_jaccard,
        min_shared_tokens=args.min_shared_tokens,
        min_context_shared_tokens=args.min_context_shared_tokens,
        allow_same_category=args.allow_same_category,
    )
    base_fields = fieldnames or ["coherence_scope"]
    output_fields = base_fields + [
        field for field in ("strong_relation_context",) if field not in base_fields
    ]
    rejection_fields = output_fields + [
        field
        for field in ("strong_relation_reason", "weak_relation_certificates")
        if field not in output_fields
    ]
    write_rows(args.output, output_fields, kept)
    write_rows(args.rejections, rejection_fields, rejected)

    report = {
        "input_rows": len(rows),
        "kept_rows": len(kept),
        "rejected_rows": len(rejected),
        "metadata_rows": len(metadata),
        "min_jaccard": args.min_jaccard,
        "min_shared_tokens": args.min_shared_tokens,
        "min_context_shared_tokens": args.min_context_shared_tokens,
        "allow_same_category": args.allow_same_category,
    }
    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "b2_strong_relation_gate "
        f"input={len(rows)} kept={len(kept)} rejected={len(rejected)} metadata={len(metadata)} "
        f"min_jaccard={args.min_jaccard:.2f} min_shared={args.min_shared_tokens} "
        f"min_context_shared={args.min_context_shared_tokens} "
        f"allow_same_category={args.allow_same_category}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
