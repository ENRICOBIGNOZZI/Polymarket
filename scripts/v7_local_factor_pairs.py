#!/usr/bin/env python3
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Protocol

TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
NUMBER_RE = re.compile(
    r"(?<![a-z0-9])(?P<currency>[$€£]?)\s*(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>bps?|%|k|m|b)?(?![a-z0-9])",
    re.I,
)
DIRECTION_TERMS = (
    "above", "below", "over", "under", "reach", "reaches", "reached",
    "exceed", "exceeds", "exceeded", "dip", "dips", "at least", "at most",
    "more than", "less than", "higher than", "lower than",
)
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "before", "by", "did",
    "do", "does", "end", "for", "from", "has", "have", "in", "is", "it",
    "market", "no", "of", "on", "or", "the", "this", "to", "was", "were",
    "will", "with", "yes",
}


class MarketLike(Protocol):
    market_id: str
    question: str


@dataclass(frozen=True)
class StructuralPairGraph:
    method: str
    pairs: tuple[tuple[str, str], ...]
    threshold_markets: int
    text_markets: int

    @property
    def pair_count(self) -> int:
        return len(self.pairs)


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _numeric_value(raw: str, unit: str) -> float:
    value = float(raw.replace(",", ""))
    scale = {
        "": 1.0,
        "k": 1e3,
        "m": 1e6,
        "b": 1e9,
        "%": 1e-2,
        "bp": 1e-4,
        "bps": 1e-4,
    }.get(unit.lower(), 1.0)
    return value * scale


def threshold_value(question: str) -> float | None:
    """Return the most structurally plausible threshold encoded in the contract text.

    This parser deliberately uses contract metadata only. Plain 4-digit calendar years
    are ignored unless a unit/currency makes them unambiguously an economic threshold.
    Candidates receive deterministic context scores; no market price, return, residual,
    p-value, volume or liquidity enters the choice.
    """
    text = question.lower()
    candidates: list[tuple[int, int, float]] = []
    for match in NUMBER_RE.finditer(text):
        raw = match.group("value")
        unit = (match.group("unit") or "").lower()
        currency = match.group("currency") or ""
        plain = raw.replace(",", "")
        try:
            numeric = float(plain)
        except ValueError:
            continue
        if not unit and not currency and numeric.is_integer() and 1900 <= numeric <= 2100:
            continue
        left = max(0, match.start() - 36)
        right = min(len(text), match.end() + 36)
        context = text[left:right]
        score = 0
        if currency:
            score += 4
        if unit:
            score += 3
        if any(term in context for term in DIRECTION_TERMS):
            score += 2
        # A bare number without threshold-like context is usually a date, ordinal or
        # candidate label; do not use it to define adjacency.
        if score == 0:
            continue
        value = _numeric_value(raw, unit)
        if math.isfinite(value):
            candidates.append((score, -match.start(), value))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def structural_tokens(question: str) -> frozenset[str]:
    """Price-independent lexical fingerprint with values/dates/directions removed."""
    text = question.lower()
    text = NUMBER_RE.sub(" value ", text)
    for term in sorted(DIRECTION_TERMS, key=len, reverse=True):
        text = text.replace(term, " direction ")
    tokens = {
        token
        for token in TOKEN_RE.findall(text)
        if token not in STOPWORDS and token not in {"value", "direction"} and len(token) > 1
    }
    return frozenset(tokens)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _threshold_matching(markets: list[MarketLike]) -> tuple[list[tuple[str, str]], set[str]]:
    parsed = [
        (threshold_value(market.question), str(market.market_id))
        for market in markets
    ]
    parsed = [(value, market_id) for value, market_id in parsed if value is not None]
    if len(parsed) < 2:
        return [], set()
    parsed.sort(key=lambda item: (float(item[0]), item[1]))
    pairs: list[tuple[str, str]] = []
    used: set[str] = set()
    # Greedy non-overlapping nearest-neighbour matching in threshold space. Each
    # contract contributes to at most one hypothesis, sharply reducing multiplicity.
    remaining = list(parsed)
    while len(remaining) >= 2:
        best_index = min(
            range(len(remaining) - 1),
            key=lambda i: (abs(float(remaining[i + 1][0]) - float(remaining[i][0])), remaining[i][1], remaining[i + 1][1]),
        )
        left = remaining.pop(best_index)
        right = remaining.pop(best_index)
        pair = _canonical_pair(left[1], right[1])
        pairs.append(pair)
        used.update(pair)
    return pairs, used


def _text_matching(
    markets: list[MarketLike],
    excluded: set[str],
    minimum_similarity: float,
) -> list[tuple[str, str]]:
    remaining = [market for market in markets if str(market.market_id) not in excluded]
    fingerprints = {str(market.market_id): structural_tokens(market.question) for market in remaining}
    edges: list[tuple[float, str, str]] = []
    for i, left in enumerate(remaining):
        a = str(left.market_id)
        for right in remaining[i + 1 :]:
            b = str(right.market_id)
            similarity = jaccard(fingerprints[a], fingerprints[b])
            if similarity + 1e-15 >= minimum_similarity:
                edges.append((similarity, *_canonical_pair(a, b)))
    edges.sort(key=lambda item: (-item[0], item[1], item[2]))
    used: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for _similarity, a, b in edges:
        if a in used or b in used:
            continue
        used.add(a)
        used.add(b)
        pairs.append((a, b))
    return pairs


def build_structural_pair_graph(
    cluster_key: str,
    markets: Iterable[MarketLike],
    *,
    min_controls: int,
    minimum_text_similarity: float = 0.20,
) -> StructuralPairGraph:
    """Freeze a sparse pair universe before any price history is inspected.

    Threshold/payoff families use non-overlapping nearest-threshold matching. Any
    remaining contracts, and generic event clusters, use a deterministic greedy
    lexical matching. Pair construction depends only on IDs/question text/cluster
    membership, so subsequent bootstrap/BH inference is not selected on returns.
    """
    rows = sorted(list(markets), key=lambda market: str(market.market_id))
    if len(rows) < int(min_controls) + 2:
        return StructuralPairGraph("insufficient_controls", (), 0, 0)

    threshold_pairs: list[tuple[str, str]] = []
    threshold_used: set[str] = set()
    if cluster_key.startswith("payoff:"):
        threshold_pairs, threshold_used = _threshold_matching(rows)

    text_pairs = _text_matching(rows, threshold_used, float(minimum_text_similarity))
    pairs = sorted(set(threshold_pairs + text_pairs))
    method = "threshold_matching_plus_text_matching" if threshold_pairs else "text_matching"
    return StructuralPairGraph(
        method=method,
        pairs=tuple(pairs),
        threshold_markets=len(threshold_used),
        text_markets=2 * len(text_pairs),
    )
