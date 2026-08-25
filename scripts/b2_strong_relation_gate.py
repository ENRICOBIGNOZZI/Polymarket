#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path


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


def classify_scope(
    scope: str,
    min_jaccard: float,
    min_shared_tokens: int,
    allow_same_category: bool,
) -> tuple[bool, list[str]]:
    raw_items = [item for item in scope.split("|") if item]
    if not raw_items:
        return False, ["missing_relation_certificate"]
    weak: list[str] = []
    for raw in raw_items:
        cert = parse_certificate(raw)
        if cert is None or not certificate_is_strong(
            cert, min_jaccard, min_shared_tokens, allow_same_category
        ):
            weak.append(raw)
    return not weak, weak


def gate_rows(
    rows: list[dict[str, str]],
    min_jaccard: float,
    min_shared_tokens: int,
    allow_same_category: bool,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    kept: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    for row in rows:
        ok, weak = classify_scope(
            row.get("coherence_scope", ""),
            min_jaccard,
            min_shared_tokens,
            allow_same_category,
        )
        if ok:
            kept.append(dict(row))
            continue
        item = dict(row)
        item["strong_relation_reason"] = "weak_or_nonsemantic_relation"
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
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--min-jaccard", type=float, default=0.20)
    parser.add_argument("--min-shared-tokens", type=int, default=2)
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

    kept, rejected = gate_rows(
        rows,
        min_jaccard=args.min_jaccard,
        min_shared_tokens=args.min_shared_tokens,
        allow_same_category=args.allow_same_category,
    )
    base_fields = fieldnames or ["coherence_scope"]
    rejection_fields = base_fields + [
        field
        for field in ("strong_relation_reason", "weak_relation_certificates")
        if field not in base_fields
    ]
    write_rows(args.output, base_fields, kept)
    write_rows(args.rejections, rejection_fields, rejected)

    report = {
        "input_rows": len(rows),
        "kept_rows": len(kept),
        "rejected_rows": len(rejected),
        "min_jaccard": args.min_jaccard,
        "min_shared_tokens": args.min_shared_tokens,
        "allow_same_category": args.allow_same_category,
    }
    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "b2_strong_relation_gate "
        f"input={len(rows)} kept={len(kept)} rejected={len(rejected)} "
        f"min_jaccard={args.min_jaccard:.2f} min_shared={args.min_shared_tokens} "
        f"allow_same_category={args.allow_same_category}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
