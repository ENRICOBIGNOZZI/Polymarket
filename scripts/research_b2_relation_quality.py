#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RelationEvidence:
    scope: str
    score: float
    shared_tokens: int


@dataclass(frozen=True)
class CandidateAudit:
    market: str
    slug: str
    maker_entry_net_edge: float
    taker_net_edge: float
    raw_expected_edge: float
    relation_quality_pass: bool
    weak_relations: tuple[str, ...]
    completion_hurdle: float | None


def parse_relation(value: str) -> RelationEvidence:
    parts = value.strip().split(":")
    if len(parts) != 3:
        return RelationEvidence(value.strip() or "missing", 0.0, 0)
    scope, score_raw, shared_raw = parts
    try:
        score = float(score_raw)
        shared = int(shared_raw)
    except ValueError:
        return RelationEvidence(scope or "malformed", 0.0, 0)
    return RelationEvidence(scope, score, shared)


def relation_is_strong(
    relation: RelationEvidence,
    min_semantic_score: float = 0.20,
    min_shared_tokens: int = 2,
) -> bool:
    if relation.scope == "same_event":
        return True
    if relation.scope == "semantic":
        return (
            relation.score >= min_semantic_score
            and relation.shared_tokens >= min_shared_tokens
        )
    # Category membership is allowed as a research prior but cannot independently
    # authorize an executable hedge. PR #179 showed that broad categories can
    # connect lexically unrelated markets.
    return False


def completion_hurdle(maker_edge: float, taker_edge: float) -> float | None:
    """Break-even full maker completion under a taker-fallback scenario.

    This is a scenario hurdle, not an estimator of the actual broker process.
    It is defined only when maker economics are positive and fallback economics
    are negative.
    """
    if not (maker_edge > 0.0 and taker_edge < 0.0 and maker_edge > taker_edge):
        return None
    value = (-taker_edge) / (maker_edge - taker_edge)
    return min(1.0, max(0.0, value))


def audit_candidate(
    candidate: dict[str, Any],
    min_semantic_score: float = 0.20,
    min_shared_tokens: int = 2,
) -> CandidateAudit:
    scope_text = str(candidate.get("coherence_scope") or "")
    relations = tuple(
        parse_relation(piece) for piece in scope_text.split("|") if piece.strip()
    )
    weak = tuple(
        f"{relation.scope}:{relation.score:.4f}:{relation.shared_tokens}"
        for relation in relations
        if not relation_is_strong(
            relation,
            min_semantic_score=min_semantic_score,
            min_shared_tokens=min_shared_tokens,
        )
    )
    relation_pass = bool(relations) and not weak
    maker = float(candidate.get("maker_entry_net_edge") or 0.0)
    taker = float(candidate.get("taker_net_edge") or 0.0)
    raw = float(candidate.get("raw_expected_edge") or 0.0)
    return CandidateAudit(
        market=str(candidate.get("market") or ""),
        slug=str(candidate.get("slug") or ""),
        maker_entry_net_edge=maker,
        taker_net_edge=taker,
        raw_expected_edge=raw,
        relation_quality_pass=relation_pass,
        weak_relations=weak,
        completion_hurdle=completion_hurdle(maker, taker),
    )


def evaluate_snapshot(
    payload: dict[str, Any],
    min_semantic_score: float = 0.20,
    min_shared_tokens: int = 2,
) -> dict[str, Any]:
    candidates = payload.get("candidates", {}).get("b2", [])
    if not isinstance(candidates, list):
        candidates = []
    audits = [
        audit_candidate(
            candidate,
            min_semantic_score=min_semantic_score,
            min_shared_tokens=min_shared_tokens,
        )
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    maker_positive = [item for item in audits if item.maker_entry_net_edge > 0.0]
    strong_maker = [
        item
        for item in maker_positive
        if item.relation_quality_pass
    ]
    return {
        "schema": "b2_relation_quality_research_v1",
        "policy": {
            "same_event": "always strong",
            "semantic_min_score": min_semantic_score,
            "semantic_min_shared_tokens": min_shared_tokens,
            "same_category": "prior_only_not_execution_authority",
        },
        "candidate_count": len(audits),
        "maker_positive_count": len(maker_positive),
        "strong_relation_maker_positive_count": len(strong_maker),
        "maker_positive": [asdict(item) for item in maker_positive],
        "strong_relation_maker_positive": [asdict(item) for item in strong_maker],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-semantic-score", type=float, default=0.20)
    parser.add_argument("--min-shared-tokens", type=int, default=2)
    args = parser.parse_args()

    if not (0.0 <= args.min_semantic_score <= 1.0):
        raise SystemExit("min semantic score must be between 0 and 1")
    if args.min_shared_tokens < 1:
        raise SystemExit("min shared tokens must be positive")

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    report = evaluate_snapshot(
        payload,
        min_semantic_score=args.min_semantic_score,
        min_shared_tokens=args.min_shared_tokens,
    )
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
