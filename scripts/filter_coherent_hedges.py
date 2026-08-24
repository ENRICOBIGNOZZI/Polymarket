#!/usr/bin/env python3
"""Coherence gate for executable B2 PCA hedge baskets.

Explicit event, category and semantic hedges remain preferred. In opt-in paper
mode, a regularized PCA-factor basket may also pass when aggregate hedge error,
stability, residual extremeness and maker economics satisfy explicit thresholds.
Metadata failures remain fail-closed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

STOPWORDS = {
    "a", "an", "and", "are", "at", "be", "before", "by", "for", "from",
    "happen", "in", "is", "it", "market", "no", "of", "on", "or", "the",
    "this", "to", "was", "were", "who", "will", "win", "yes",
}
TOKEN_RE = re.compile(r"[a-z0-9]+")
PCA_FIELDS = [
    "market", "slug", "side", "obs", "hedges", "explained", "residual_z",
    "phi", "half_life_h", "t_reversion", "stability", "hedge_error",
    "expected_mark_move", "raw_expected_edge", "taker_net_edge",
    "maker_entry_net_edge", "executable_notional", "legs",
]
COHERENCE_FIELDS = ["coherence_scope", "coherence_min_similarity"]
REJECTION_FIELDS = ["coherence_reason", "unrelated_market_ids"]


@dataclass(frozen=True)
class MarketMeta:
    market_id: str
    slug: str
    question: str
    event_id: str
    category: str
    fetched_ts: int


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def initialise_fail_closed(output: Path, rejections: Path) -> None:
    """Remove any previously executable B2 rows before touching external metadata."""
    write_csv(output, PCA_FIELDS + COHERENCE_FIELDS, [])
    write_csv(rejections, PCA_FIELDS + COHERENCE_FIELDS + REJECTION_FIELDS, [])


def tokens(meta: MarketMeta) -> set[str]:
    out: set[str] = set()
    for token in TOKEN_RE.findall(f"{meta.slug} {meta.question}".lower()):
        if len(token) < 3 or token.isdigit() or token in STOPWORDS:
            continue
        out.add(token)
    return out


def normalized_category(value: str) -> str:
    return " ".join(TOKEN_RE.findall(value.lower()))


def relation(
    target: MarketMeta,
    hedge: MarketMeta,
    min_jaccard: float,
    min_shared: int,
) -> tuple[str, float, int]:
    if target.event_id and target.event_id == hedge.event_id:
        return "same_event", 1.0, 0

    target_tokens, hedge_tokens = tokens(target), tokens(hedge)
    shared = len(target_tokens & hedge_tokens)
    union = len(target_tokens | hedge_tokens)
    score = shared / union if union else 0.0

    target_category = normalized_category(target.category)
    hedge_category = normalized_category(hedge.category)
    if target_category and target_category == hedge_category:
        return "same_category", max(0.50, score), shared
    if shared >= min_shared and score >= min_jaccard:
        return "semantic", score, shared
    return "unrelated", score, shared


def event_id_from_payload(payload: dict[str, Any]) -> str:
    direct = payload.get("eventId") or payload.get("event_id")
    if direct not in (None, ""):
        return str(direct)
    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict) and event.get("id") not in (None, ""):
                return str(event["id"])
    return ""


def category_from_payload(payload: dict[str, Any]) -> str:
    direct = payload.get("category") or payload.get("groupItemTitle")
    if direct not in (None, ""):
        return str(direct)
    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            value = event.get("category") or event.get("groupItemTitle")
            if value not in (None, ""):
                return str(value)
    return ""


def load_cache(path: Path) -> dict[str, MarketMeta]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = raw.get("markets", raw) if isinstance(raw, dict) else {}
    if not isinstance(records, dict):
        return {}
    out: dict[str, MarketMeta] = {}
    for market_id, item in records.items():
        if not isinstance(item, dict):
            continue
        try:
            out[str(market_id)] = MarketMeta(
                market_id=str(item.get("market_id") or market_id),
                slug=str(item.get("slug") or ""),
                question=str(item.get("question") or ""),
                event_id=str(item.get("event_id") or ""),
                category=str(item.get("category") or ""),
                fetched_ts=int(item.get("fetched_ts") or 0),
            )
        except (TypeError, ValueError):
            continue
    return out


def save_cache(path: Path, cache: dict[str, MarketMeta]) -> None:
    payload = {
        "schema": "polymarket_market_metadata_cache_v1",
        "markets": {key: asdict(value) for key, value in sorted(cache.items())},
    }
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def fetch_metadata(
    gamma_url: str,
    market_id: str,
    now: int,
    timeout: float,
) -> MarketMeta:
    url = gamma_url.rstrip("/") + "/markets/" + urllib.parse.quote(market_id, safe="")
    request = urllib.request.Request(
        url, headers={"User-Agent": "polymarket-coherent-hedges/2"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected metadata payload for market {market_id}")
    return MarketMeta(
        market_id=str(payload.get("id") or market_id),
        slug=str(payload.get("slug") or ""),
        question=str(payload.get("question") or ""),
        event_id=event_id_from_payload(payload),
        category=category_from_payload(payload),
        fetched_ts=now,
    )


def fnum(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def parse_leg_ids(spec: str) -> list[str]:
    out: list[str] = []
    for raw in spec.split("|"):
        market_id = raw.split(":", 1)[0].strip()
        if market_id and market_id not in out:
            out.append(market_id)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rejections", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--cache-ttl-seconds", type=int, default=21600)
    parser.add_argument("--min-jaccard", type=float, default=0.08)
    parser.add_argument("--min-shared-tokens", type=int, default=1)
    parser.add_argument("--allow-factor-hedges", action="store_true")
    parser.add_argument("--max-factor-hedge-error", type=float, default=0.80)
    parser.add_argument("--min-factor-stability", type=float, default=0.20)
    parser.add_argument("--min-factor-z", type=float, default=0.65)
    parser.add_argument("--min-factor-maker-edge", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()

    initialise_fail_closed(args.output, args.rejections)

    now = int(time.time()) if args.now is None else args.now
    if not args.input.exists() or args.input.stat().st_size == 0:
        print("coherent_hedges input=0 kept=0 rejected=0 metadata_errors=0")
        return 0

    with args.input.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        base_fields = list(reader.fieldnames or [])
        rows = list(reader)
    if "market" not in base_fields or "legs" not in base_fields:
        raise SystemExit("input CSV must contain market and legs columns")

    cache = load_cache(args.cache)
    metadata_errors = 0

    def get_meta(market_id: str) -> MarketMeta | None:
        nonlocal metadata_errors
        cached = cache.get(market_id)
        if cached and now - cached.fetched_ts <= max(0, args.cache_ttl_seconds):
            return cached
        try:
            fetched = fetch_metadata(
                args.gamma_url, market_id, now, args.timeout_seconds
            )
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ):
            metadata_errors += 1
            return cached if cached else None
        cache[market_id] = fetched
        return fetched

    kept: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    relation_counts: Counter[str] = Counter()
    for row in rows:
        target_id = (row.get("market") or "").strip()
        leg_ids = parse_leg_ids(row.get("legs") or "")
        hedge_ids = [market_id for market_id in leg_ids if market_id != target_id]
        reason = ""
        unrelated: list[str] = []
        scopes: list[str] = []
        similarities: list[float] = []
        factor_evidence = (
            args.allow_factor_hedges
            and fnum(row.get("hedge_error"), 1.0) <= args.max_factor_hedge_error
            and fnum(row.get("stability"), 0.0) >= args.min_factor_stability
            and abs(fnum(row.get("residual_z"), 0.0)) >= args.min_factor_z
            and fnum(row.get("maker_entry_net_edge"), -1.0) > args.min_factor_maker_edge
        )

        target = get_meta(target_id) if target_id else None
        if not target:
            reason = "target_metadata_unavailable"
        elif not hedge_ids:
            reason = "no_hedge_legs"
        else:
            for hedge_id in hedge_ids:
                hedge = get_meta(hedge_id)
                if not hedge:
                    unrelated.append(hedge_id)
                    scopes.append("metadata_unavailable")
                    continue
                scope, similarity, shared = relation(
                    target, hedge, args.min_jaccard, args.min_shared_tokens
                )
                similarities.append(similarity)
                if scope == "unrelated" and factor_evidence:
                    relation_counts["pca_factor"] += 1
                    scopes.append(
                        f"pca_factor:{similarity:.4f}:{shared}:"
                        f"error={fnum(row.get('hedge_error'), 1.0):.4f}"
                    )
                else:
                    relation_counts[scope] += 1
                    scopes.append(f"{scope}:{similarity:.4f}:{shared}")
                    if scope == "unrelated":
                        unrelated.append(hedge_id)
            if unrelated:
                reason = "unrelated_or_unknown_hedge_legs"

        enriched = dict(row)
        enriched["coherence_scope"] = "|".join(scopes)
        enriched["coherence_min_similarity"] = (
            f"{min(similarities):.6f}" if similarities else ""
        )
        if reason:
            enriched["coherence_reason"] = reason
            enriched["unrelated_market_ids"] = "|".join(unrelated)
            rejected.append(enriched)
        else:
            kept.append(enriched)

    save_cache(args.cache, cache)
    output_fields = base_fields + [
        field for field in COHERENCE_FIELDS if field not in base_fields
    ]
    rejection_fields = output_fields + [
        field for field in REJECTION_FIELDS if field not in output_fields
    ]
    write_csv(args.output, output_fields, kept)
    write_csv(args.rejections, rejection_fields, rejected)
    print(
        f"coherent_hedges input={len(rows)} kept={len(kept)} "
        f"rejected={len(rejected)} metadata_errors={metadata_errors} "
        f"relations={dict(sorted(relation_counts.items()))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
