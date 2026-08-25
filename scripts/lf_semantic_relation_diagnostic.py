#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Sequence


def words(text: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    for char in text:
        if char.isalnum():
            cur.append(char.lower())
        else:
            if len(cur) >= 3:
                out.append("".join(cur))
            cur = []
    if len(cur) >= 3:
        out.append("".join(cur))
    return sorted(set(out))


def jaccard(a: str, b: str) -> float:
    left, right = set(words(a)), set(words(b))
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def semantic_adjustment(
    question: str,
    mid: float,
    peers: Sequence[tuple[str, float, float]],
    *,
    minimum_similarity: float,
    shrink: float,
) -> tuple[float, list[dict[str, float]]]:
    weighted_probability = 0.0
    total_weight = 0.0
    accepted: list[dict[str, float]] = []
    for peer_question, peer_mid, liquidity in peers:
        similarity = jaccard(question, peer_question)
        if similarity < minimum_similarity:
            continue
        weight = similarity * similarity * math.sqrt(max(1.0, liquidity))
        total_weight += weight
        weighted_probability += weight * peer_mid
        accepted.append({"similarity": similarity, "weight": weight, "peer_mid": peer_mid})
    if total_weight <= 0.0:
        return mid, accepted
    peer_probability = weighted_probability / total_weight
    return (1.0 - shrink) * mid + shrink * peer_probability, accepted


def semantic_source_contract(engine_source: str) -> dict[str, Any]:
    start = engine_source.find("double sw = 0.0, spv = 0.0;")
    end = engine_source.find("auto add_external", start)
    block = engine_source[start:end] if start >= 0 and end > start else ""
    return {
        "semantic_block_found": bool(block),
        "lexical_jaccard_used": "jaccard(m.question, peer.question)" in block,
        "direct_peer_probability_average": "spv += w * p" in block,
        "shrink_to_peer_probability": "cfg_.semantic_shrink * peer" in block,
        "explicit_market_relation_guard": "market_relation" in block,
        "explicit_polarity_guard": any(token in block for token in ("polarity", "orientation", "negation")),
        "explicit_threshold_guard": any(token in block for token in ("threshold", "strike", "monotonic", "isotonic")),
        "explicit_expiry_guard": any(token in block for token in ("end_ts", "expiry", "resolution")),
    }


def run_diagnostic(engine_source: str, config: dict[str, Any]) -> dict[str, Any]:
    minimum_similarity = float(config.get("semantic_min_similarity", 0.18))
    shrink = float(config.get("semantic_shrink", 0.50))

    positive = "Will Alice win the 2026 mayor election?"
    negative = "Will Alice not win the 2026 mayor election?"
    positive_q, positive_peers = semantic_adjustment(
        positive, 0.80, [(negative, 0.20, 10_000.0)],
        minimum_similarity=minimum_similarity, shrink=shrink,
    )
    negative_q, negative_peers = semantic_adjustment(
        negative, 0.20, [(positive, 0.80, 10_000.0)],
        minimum_similarity=minimum_similarity, shrink=shrink,
    )

    low_strike = "Will Bitcoin be above 100000 by December 31 2026?"
    high_strike = "Will Bitcoin be above 150000 by December 31 2026?"
    low_q, low_peers = semantic_adjustment(
        low_strike, 0.75, [(high_strike, 0.25, 10_000.0)],
        minimum_similarity=minimum_similarity, shrink=shrink,
    )
    high_q, high_peers = semantic_adjustment(
        high_strike, 0.25, [(low_strike, 0.75, 10_000.0)],
        minimum_similarity=minimum_similarity, shrink=shrink,
    )

    polarity_similarity = jaccard(positive, negative)
    threshold_similarity = jaccard(low_strike, high_strike)
    source = semantic_source_contract(engine_source)
    material = (
        source["lexical_jaccard_used"]
        and source["direct_peer_probability_average"]
        and not source["explicit_polarity_guard"]
        and not source["explicit_threshold_guard"]
        and polarity_similarity >= minimum_similarity
        and threshold_similarity >= minimum_similarity
        and abs(positive_q - 0.80) >= 0.10
        and abs(low_q - 0.75) >= 0.10
    )

    return {
        "schema": "polymarket_lf_semantic_relation_diagnostic_v1",
        "evidence_state": "MORE_EVIDENCE_REQUIRED",
        "material_structural_defect": material,
        "current_contract": source,
        "config": {
            "semantic_min_similarity": minimum_similarity,
            "semantic_shrink": shrink,
        },
        "counterexamples": {
            "opposite_polarity": {
                "questions": [positive, negative],
                "market_probabilities": [0.80, 0.20],
                "lexical_similarity": polarity_similarity,
                "passes_similarity_gate": polarity_similarity >= minimum_similarity,
                "semantic_probabilities": [positive_q, negative_q],
                "absolute_probability_shifts": [abs(positive_q - 0.80), abs(negative_q - 0.20)],
                "accepted_peer_counts": [len(positive_peers), len(negative_peers)],
                "interpretation": "An exact logical negation pair is treated as exchangeable probability peers instead of a complement relation.",
            },
            "ordered_thresholds": {
                "questions": [low_strike, high_strike],
                "market_probabilities": [0.75, 0.25],
                "lexical_similarity": threshold_similarity,
                "passes_similarity_gate": threshold_similarity >= minimum_similarity,
                "semantic_probabilities": [low_q, high_q],
                "absolute_probability_shifts": [abs(low_q - 0.75), abs(high_q - 0.25)],
                "accepted_peer_counts": [len(low_peers), len(high_peers)],
                "market_monotonic_gap": 0.50,
                "semantic_monotonic_gap": low_q - high_q,
                "interpretation": "A logically ordered threshold curve is collapsed by direct probability averaging even when the input probabilities are already coherent.",
            },
        },
        "research_only_candidate": {
            "name": "relation_aware_semantic_relative_value",
            "rules": [
                "classify proposition relation before using any peer probability",
                "abstain on opposite polarity unless an exact complement relation is verified",
                "treat same-underlying same-expiry threshold ladders with monotone/isotonic constraints rather than exchangeable averaging",
                "require compatible expiry/time-to-resolution before peer pooling",
                "use direct probability averaging only for proposition-equivalent peers",
            ],
            "evaluation": [
                "common chronological rows versus incumbent semantic expert",
                "purged Brier and log-loss by time-to-resolution bucket",
                "logical-violation rate for complement and threshold families",
                "executable OOS PnL at 1x, 1.5x and 2x costs",
                "turnover, drawdown and cross-strategy covariance",
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit lexical semantic relative value against proposition relations")
    parser.add_argument("--engine", type=Path, default=Path("src/engine.cpp"))
    parser.add_argument("--config", type=Path, default=Path("config/paper_v5.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = run_diagnostic(
        args.engine.read_text(encoding="utf-8"),
        json.loads(args.config.read_text(encoding="utf-8")),
    )
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["material_structural_defect"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
