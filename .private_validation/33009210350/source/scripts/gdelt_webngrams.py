#!/usr/bin/env python3
from __future__ import annotations

import gzip
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

GDELT_WEBNGRAMS_ROOT = (
    "https://storage.googleapis.com/data.gdeltproject.org/"
    "gdeltv5/weblegacy/ngrams"
)
DEFAULT_MIN_AGE_MINUTES = 5
DEFAULT_LOOKBACK_MINUTES = 40
DEFAULT_MIN_TOKEN_MATCHES = 2
WORD_RE = re.compile(r"[a-z0-9$%]+")


def _candidate_minutes(now: int, *, min_age_minutes: int, lookback_minutes: int) -> list[int]:
    base = (int(now) // 60) * 60
    minimum = max(2, int(min_age_minutes))
    maximum = max(minimum, int(lookback_minutes))
    return [base - offset * 60 for offset in range(minimum, maximum + 1)]


def _dataset_url(timestamp: int) -> str:
    stamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%d%H%M00")
    return f"{GDELT_WEBNGRAMS_ROOT}/{stamp}.ngrams.txt.gz"


def _words(value: str) -> set[str]:
    return {word for word in WORD_RE.findall((value or "").lower()) if len(word) >= 3}


def _scan_ngram_lines(
    lines: Iterable[bytes], market_terms: Sequence[set[str]], *, min_token_matches: int
) -> tuple[list[set[str]], list[int]]:
    documents = [set() for _ in market_terms]
    mentions = [0 for _ in market_terms]
    inverted: dict[str, set[int]] = {}
    for index, terms in enumerate(market_terms):
        for term in terms:
            inverted.setdefault(term, set()).add(index)

    for raw in lines:
        try:
            text = raw.decode("utf-8", errors="replace").rstrip("\n")
            doc_id, quadgram, raw_count = text.split("\t", 2)
            count = max(0, int(float(raw_count)))
        except (ValueError, TypeError):
            continue
        quad_terms = _words(quadgram)
        candidates: set[int] = set()
        for term in quad_terms:
            candidates.update(inverted.get(term, ()))
        for index in candidates:
            required = min(max(1, int(min_token_matches)), max(1, len(market_terms[index])))
            if len(quad_terms.intersection(market_terms[index])) < required:
                continue
            documents[index].add(doc_id)
            mentions[index] += count
    return documents, mentions


def collect_gdelt_webngrams(
    pm_markets: Sequence[Any], config: dict[str, Any], now: int, module: Any,
    *, opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    source = (config.get("sources") or {}).get("gdelt") or {}
    if not source.get("enabled"):
        return [], {}, []
    count = max(0, module.integer(source.get("markets_per_run"), 8))
    if not pm_markets or count == 0:
        return [], {}, []

    offset = ((now // 1800) * count) % len(pm_markets)
    selected = list(pm_markets[offset:offset + count])
    if len(selected) < count:
        selected.extend(pm_markets[:count - len(selected)])

    market_terms: list[set[str]] = []
    filtered_markets: list[Any] = []
    months = {str(value).lower() for value in getattr(module, "MONTHS", set())}
    for market in selected:
        query = module.gdelt_query(market.question)
        terms = {term for term in _words(query) if term not in months}
        if not terms:
            continue
        filtered_markets.append(market)
        market_terms.append(terms)
    if not filtered_markets:
        return [], {}, []

    min_age = max(2, module.integer(source.get("webngrams_min_age_minutes"), DEFAULT_MIN_AGE_MINUTES))
    lookback = max(min_age, module.integer(source.get("webngrams_lookback_minutes"), DEFAULT_LOOKBACK_MINUTES))
    min_matches = max(1, module.integer(source.get("webngrams_min_token_matches"), DEFAULT_MIN_TOKEN_MATCHES))

    last_error: Exception | None = None
    dataset_ts = 0
    dataset_url = ""
    documents: list[set[str]] = []
    mentions: list[int] = []
    for candidate_ts in _candidate_minutes(now, min_age_minutes=min_age, lookback_minutes=lookback):
        url = _dataset_url(candidate_ts)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/gzip", "User-Agent": "polymarket-external-intelligence/1.0"},
        )
        try:
            with opener(request, timeout=45.0) as response:
                with gzip.GzipFile(fileobj=response, mode="rb") as handle:
                    documents, mentions = _scan_ngram_lines(
                        handle, market_terms, min_token_matches=min_matches
                    )
            dataset_ts = candidate_ts
            dataset_url = url
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, EOFError) as exc:
            last_error = exc
            continue

    if not dataset_ts:
        detail = str(last_error) if last_error is not None else "no recent Web NGrams file found"
        raise RuntimeError(f"GDELT Web NGrams unavailable over {lookback}m lookback: {detail}")

    observations: list[dict[str, Any]] = []
    health: dict[str, Any] = {
        "_transport": {
            "status": "ok",
            "transport": "webngrams",
            "dataset_ts": dataset_ts,
            "dataset_url": dataset_url,
        }
    }
    confidence = module.finite(source.get("base_confidence"), 0.35)
    for index, market in enumerate(filtered_markets):
        doc_count = len(documents[index])
        mention_count = mentions[index]
        query = module.gdelt_query(market.question)
        health[market.market_id] = {
            "status": "ok",
            "transport": "webngrams",
            "query": query,
            "documents": doc_count,
            "mentions": mention_count,
            "latest_ts": dataset_ts,
        }
        observations.append(module.observation_row(
            market,
            observed_ts=now,
            source="gdelt",
            source_id=dataset_url,
            source_event_ts=dataset_ts,
            feature_name="news_count_recent",
            feature_value=math.log1p(doc_count),
            confidence=confidence,
            mapping_score=0.55,
            metadata={
                "transport": "webngrams",
                "query": query,
                "document_count": doc_count,
                "mention_count": mention_count,
                "dataset_ts": dataset_ts,
            },
        ))
    return observations, health, []


def wrap_collect_gdelt(module: Any, delegate: Callable[..., Any]) -> Callable[..., Any]:
    def collect(
        pm_markets: Sequence[Any], config: dict[str, Any], now: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
        source = (config.get("sources") or {}).get("gdelt") or {}
        transport = str(source.get("transport") or "doc_api").strip().lower()
        if transport != "webngrams":
            return delegate(pm_markets, config, now)
        try:
            return collect_gdelt_webngrams(pm_markets, config, now, module)
        except RuntimeError as primary:
            observations, health, errors = delegate(pm_markets, config, now)
            if observations:
                health["_transport"] = {
                    "status": "fallback",
                    "transport": "doc_api",
                    "primary_error": str(primary),
                }
                return observations, health, errors
            return observations, health, [str(primary), *errors]

    return collect
