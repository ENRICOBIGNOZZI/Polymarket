#!/usr/bin/env python3
"""Read-only external-information research, storage and walk-forward backtesting.

The worker discovers active Polymarket contracts, collects only public/free data,
normalizes point-in-time observations, performs bounded historical backfills, and
measures incremental predictive/trading value without touching the live champion.

Implemented source families:
  * Kalshi public market data: direct probability estimates for conservatively
    matched contracts.
  * Binance public market-data-only API: crypto spot returns and volatility
    features for crypto-linked Polymarket contracts.
  * GDELT DOC 2.0: news-volume/tone features for a rotating liquid-market sample.

Every observation records event time, retrieval time, source provenance, mapping
confidence and availability semantics. The backtester is chronological and purged:
a row can only use parameters estimated from labels whose future horizon had already
elapsed before that row's decision timestamp.
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import json
import math
import os
import random
import re
import statistics
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = "polymarket_external_intelligence_report_v1"
STATE_SCHEMA = "polymarket_external_intelligence_state_v1"
OBS_SCHEMA = "polymarket_external_observation_v1"
PRICE_SCHEMA = "polymarket_price_snapshot_v1"

PM_GAMMA = "https://gamma-api.polymarket.com"
PM_CLOB = "https://clob.polymarket.com"
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
BINANCE = "https://data-api.binance.vision"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "before",
    "by", "did", "do", "does", "for", "from", "has", "have", "if", "in",
    "is", "it", "its", "of", "on", "or", "the", "their", "there", "this",
    "to", "was", "were", "what", "when", "where", "which", "who", "will",
    "with", "would", "market", "event", "contract", "occur", "happen",
}
UP_TERMS = {"above", "over", "higher", "greater", "exceed", "exceeds", "more", "rise", "increase"}
DOWN_TERMS = {"below", "under", "lower", "less", "decrease", "fall", "drop"}
NEGATIONS = {"no", "not", "never", "without", "fail", "fails", "failed"}

MONTH_NAMES = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"}

ASSET_ALIASES = {
    "BTC": {"btc", "bitcoin"},
    "ETH": {"eth", "ethereum", "ether"},
    "SOL": {"sol", "solana"},
    "XRP": {"xrp", "ripple"},
    "BNB": {"bnb", "binance coin"},
    "DOGE": {"doge", "dogecoin"},
    "ADA": {"ada", "cardano"},
    "AVAX": {"avax", "avalanche"},
    "LINK": {"link", "chainlink"},
}


@dataclass(frozen=True)
class PmMarket:
    market_id: str
    condition_id: str
    event_id: str
    slug: str
    question: str
    description: str
    category: str
    end_ts: int
    liquidity: float
    volume24h: float
    bid: float
    ask: float
    mid: float
    yes_token: str
    no_token: str
    resolved_outcome: int | None


@dataclass(frozen=True)
class KMarket:
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    rules: str
    close_ts: int
    updated_ts: int
    bid: float
    ask: float
    mid: float
    spread: float
    volume: float
    liquidity: float


@dataclass(frozen=True)
class MatchScore:
    score: float
    token_jaccard: float
    containment: float
    sequence: float
    numeric_match: bool
    orientation_match: bool
    expiry_hours: float | None
    rejection: str


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def clip(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def probability_price(value: Any, default: float = math.nan) -> float:
    """Normalize dollar probabilities and legacy integer-cent fields."""
    parsed = finite(value, default)
    if math.isfinite(parsed) and 1.0 < parsed <= 100.0:
        parsed /= 100.0
    return parsed


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return default


def parse_timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw // 1000 if raw > 10_000_000_000 else raw
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        raw = int(float(text))
        return raw // 1000 if raw > 10_000_000_000 else raw
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError, OverflowError):
        pass
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return int(datetime.strptime(text, pattern).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return 0


def iso_utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as handle:
        handle.write(data)
        tmp = Path(handle.name)
    os.replace(tmp, path)


def atomic_text(path: Path, text: str) -> None:
    atomic_write(path, text.encode("utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl_gz(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    output: list[dict[str, Any]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    output.append(row)
    except (OSError, EOFError):
        return []
    return output


def write_jsonl_gz(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    payload = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    compressed = gzip.compress(payload.encode("utf-8"), compresslevel=9, mtime=0)
    atomic_write(path, compressed)


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def request_json(
    url: str,
    *,
    timeout: float = 20.0,
    retries: int = 3,
    user_agent: str = "polymarket-external-intelligence/1.0",
) -> Any:
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": user_agent},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
            return json.loads(body.decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < max(1, retries):
                time.sleep(min(4.0, 0.5 * (2**attempt)))
    raise RuntimeError(f"request failed: {url}: {last}")


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%.$+-]+", " ", text.lower())).strip()


def tokens(value: str) -> list[str]:
    return [token for token in normalize_text(value).split() if token not in STOPWORDS and len(token) > 1]


def critical_numbers(value: str) -> set[str]:
    output: set[str] = set()
    for match in re.findall(r"(?<![a-z])[-+]?\d+(?:[.,]\d+)?%?", normalize_text(value)):
        normalized = match.replace(",", "")
        if normalized.endswith("%"):
            normalized = normalized[:-1] + "%"
        output.add(normalized)
    return output


def contract_threshold_numbers(value: str) -> set[str]:
    """Drop obvious calendar numbers before comparing contract thresholds.

    Expiry consistency is checked separately. Retaining currency, percent, decimals,
    negatives and large non-year integers catches the economically important strikes.
    """
    normalized = normalize_text(value)
    has_month = bool(set(normalized.split()).intersection(MONTH_NAMES))
    output: set[str] = set()
    for token in critical_numbers(value):
        raw = token.rstrip("%")
        numeric = finite(raw, math.nan)
        if not math.isfinite(numeric):
            continue
        integer_like = float(numeric).is_integer()
        absolute = abs(numeric)
        if integer_like and 1900 <= absolute <= 2100:
            continue
        if has_month and integer_like and 1 <= absolute <= 31:
            continue
        if token.endswith("%") or "." in raw or numeric < 0 or absolute > 31:
            output.add(token)
        elif not has_month:
            output.add(token)
    return output


def orientation(value: str) -> tuple[int, int]:
    ts = set(tokens(value))
    direction = 1 if ts.intersection(UP_TERMS) else (-1 if ts.intersection(DOWN_TERMS) else 0)
    negation = 1 if ts.intersection(NEGATIONS) else 0
    return direction, negation


def detect_asset(text: str) -> str | None:
    normalized = normalize_text(text)
    token_set = set(normalized.split())
    for asset, aliases in ASSET_ALIASES.items():
        for alias in aliases:
            if " " in alias:
                if alias in normalized:
                    return asset
            elif alias in token_set:
                return asset
    return None


def classify_market(text: str, raw: dict[str, Any] | None = None) -> str:
    normalized = normalize_text(text)
    if detect_asset(normalized):
        return "crypto"
    if any(term in normalized for term in ("temperature", "rain", "snow", "hurricane", "weather")):
        return "weather"
    if any(term in normalized for term in ("election", "president", "prime minister", "vote", "congress")):
        return "politics"
    if any(term in normalized for term in ("fed", "inflation", "gdp", "unemployment", "interest rate", "cpi")):
        return "macro"
    tags = [] if raw is None else parse_array(raw.get("tags"))
    tag_text = normalize_text(" ".join(str(tag.get("label") or tag.get("slug") or tag) if isinstance(tag, dict) else str(tag) for tag in tags))
    if any(term in tag_text for term in ("sports", "nba", "nfl", "mlb", "soccer", "tennis")):
        return "sports"
    return "general"


def outcome_price(raw: dict[str, Any]) -> tuple[float, int | None]:
    outcomes = [str(value).strip().lower() for value in parse_array(raw.get("outcomes"))]
    prices = [probability_price(value, math.nan) for value in parse_array(raw.get("outcomePrices"))]
    yes_index = next((index for index, value in enumerate(outcomes) if value == "yes"), 0)
    price = prices[yes_index] if yes_index < len(prices) else math.nan
    if not math.isfinite(price):
        price = probability_price(raw.get("lastTradePrice"), probability_price(raw.get("price"), math.nan))
    result: int | None = None
    if parse_bool(raw.get("closed")) or parse_bool(raw.get("resolved")):
        if price >= 0.999:
            result = 1
        elif price <= 0.001:
            result = 0
    return clip(price, 0.001, 0.999) if math.isfinite(price) else math.nan, result


def parse_pm_market(raw: dict[str, Any]) -> PmMarket | None:
    market_id = str(raw.get("id") or raw.get("marketId") or "").strip()
    question = str(raw.get("question") or raw.get("title") or "").strip()
    if not market_id or not question:
        return None
    mid, resolved = outcome_price(raw)
    bid = probability_price(raw.get("bestBid"), math.nan)
    ask = probability_price(raw.get("bestAsk"), math.nan)
    if not math.isfinite(mid):
        if math.isfinite(bid) and math.isfinite(ask) and ask >= bid:
            mid = 0.5 * (bid + ask)
        else:
            return None
    if not math.isfinite(bid):
        bid = max(0.001, mid - 0.01)
    if not math.isfinite(ask):
        ask = min(0.999, mid + 0.01)
    if ask < bid:
        bid, ask = min(bid, ask), max(bid, ask)
    token_ids = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
    return PmMarket(
        market_id=market_id,
        condition_id=str(raw.get("conditionId") or ""),
        event_id=str(raw.get("eventId") or raw.get("event_id") or ""),
        slug=str(raw.get("slug") or ""),
        question=question,
        description=str(raw.get("description") or ""),
        category=classify_market(question + " " + str(raw.get("description") or ""), raw),
        end_ts=parse_timestamp(raw.get("endDate") or raw.get("end_date") or raw.get("endDateIso")),
        liquidity=max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity")))),
        volume24h=max(0.0, finite(raw.get("volume24hr"), finite(raw.get("volume_24hr"), finite(raw.get("volume24h"))))),
        bid=clip(bid, 0.001, 0.999),
        ask=clip(ask, 0.001, 0.999),
        mid=clip(mid, 0.001, 0.999),
        yes_token=token_ids[0] if token_ids else "",
        no_token=token_ids[1] if len(token_ids) > 1 else "",
        resolved_outcome=resolved,
    )


def parse_k_market(raw: dict[str, Any]) -> KMarket | None:
    ticker = str(raw.get("ticker") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not ticker or not title or str(raw.get("market_type") or "binary") != "binary":
        return None
    bid = probability_price(raw.get("yes_bid_dollars"), probability_price(raw.get("yes_bid"), math.nan))
    ask = probability_price(raw.get("yes_ask_dollars"), probability_price(raw.get("yes_ask"), math.nan))
    last = probability_price(raw.get("last_price_dollars"), probability_price(raw.get("last_price"), math.nan))
    if not math.isfinite(bid) and math.isfinite(last):
        bid = last
    if not math.isfinite(ask) and math.isfinite(last):
        ask = last
    if not math.isfinite(bid) or not math.isfinite(ask):
        return None
    if ask < bid:
        bid, ask = ask, bid
    mid = 0.5 * (bid + ask)
    return KMarket(
        ticker=ticker,
        event_ticker=str(raw.get("event_ticker") or ""),
        title=title,
        subtitle=" ".join(
            str(raw.get(key) or "")
            for key in ("subtitle", "yes_sub_title", "no_sub_title")
        ).strip(),
        rules=" ".join(str(raw.get(key) or "") for key in ("rules_primary", "rules_secondary")).strip(),
        close_ts=parse_timestamp(raw.get("close_time") or raw.get("expected_expiration_time") or raw.get("expiration_time")),
        updated_ts=parse_timestamp(raw.get("updated_time")),
        bid=clip(bid, 0.001, 0.999),
        ask=clip(ask, 0.001, 0.999),
        mid=clip(mid, 0.001, 0.999),
        spread=max(0.0, ask - bid),
        volume=max(0.0, finite(raw.get("volume_fp"), finite(raw.get("volume")))),
        liquidity=max(0.0, finite(raw.get("liquidity_dollars"), finite(raw.get("open_interest_fp")))),
    )


def score_match(pm: PmMarket, km: KMarket, max_expiry_days: float) -> MatchScore:
    pm_text = normalize_text(pm.question + " " + pm.description)
    k_text = normalize_text(km.title + " " + km.subtitle + " " + km.rules)
    p_tokens = set(tokens(pm_text))
    k_tokens = set(tokens(k_text))
    overlap = p_tokens.intersection(k_tokens)
    union = p_tokens.union(k_tokens)
    jaccard = len(overlap) / len(union) if union else 0.0
    containment = len(overlap) / max(1, min(len(p_tokens), len(k_tokens)))
    sequence = SequenceMatcher(None, normalize_text(pm.question), normalize_text(km.title)).ratio()

    p_numbers = contract_threshold_numbers(pm.question)
    k_numbers = contract_threshold_numbers(km.title + " " + km.subtitle)
    numeric_match = not p_numbers or not k_numbers or p_numbers == k_numbers
    p_orientation = orientation(pm.question)
    k_orientation = orientation(km.title + " " + km.subtitle)
    orientation_match = p_orientation == k_orientation or 0 in {p_orientation[0], k_orientation[0]}
    expiry_hours: float | None = None
    expiry_score = 0.5
    if pm.end_ts and km.close_ts:
        expiry_hours = abs(pm.end_ts - km.close_ts) / 3600.0
        expiry_score = math.exp(-expiry_hours / max(24.0, max_expiry_days * 24.0 / 2.0))

    score = 0.35 * jaccard + 0.25 * containment + 0.20 * sequence + 0.20 * expiry_score
    rejection = ""
    if not numeric_match:
        rejection = "critical_number_mismatch"
    elif not orientation_match:
        rejection = "orientation_mismatch"
    elif expiry_hours is not None and expiry_hours > max_expiry_days * 24.0:
        rejection = "expiry_mismatch"
    return MatchScore(score, jaccard, containment, sequence, numeric_match, orientation_match, expiry_hours, rejection)


def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema") != "polymarket_external_intelligence_config_v1":
        errors.append("unexpected external-intelligence config schema")
    for key, expected in (
        ("paper_only", True),
        ("allow_authenticated_execution", False),
        ("allow_direct_champion_mutation", False),
        ("allow_production_signal_write", False),
    ):
        if config.get(key) is not expected:
            errors.append(f"{key} must be {str(expected).lower()}")
    sources = config.get("sources") or {}
    if not any(bool((value or {}).get("enabled")) for value in sources.values() if isinstance(value, dict)):
        errors.append("at least one source must be enabled")
    horizons = [integer(value) for value in (config.get("backtest") or {}).get("horizons_seconds", [])]
    if not horizons or any(value <= 0 for value in horizons):
        errors.append("backtest.horizons_seconds must contain positive horizons")
    stresses = {finite(value) for value in (config.get("backtest") or {}).get("cost_stress_multipliers", [])}
    if not {1.0, 1.5, 2.0}.issubset(stresses):
        errors.append("cost stress multipliers must include 1.0, 1.5 and 2.0")
    if errors:
        raise ValueError("; ".join(errors))


def fetch_pm_markets(config: dict[str, Any]) -> tuple[list[PmMarket], list[str]]:
    universe = config.get("universe") or {}
    limit = max(1, min(500, integer(universe.get("page_size"), 500)))
    maximum = max(1, integer(universe.get("max_markets"), 400))
    min_liquidity = finite(universe.get("min_liquidity"), 100.0)
    min_volume = finite(universe.get("min_volume_24h"), 0.0)
    rows: list[PmMarket] = []
    errors: list[str] = []
    offset = 0
    while len(rows) < maximum and offset < maximum * 3:
        params = urllib.parse.urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
                "order": str(universe.get("order_field") or "volume24hr"),
                "ascending": "false",
            }
        )
        try:
            payload = request_json(f"{PM_GAMMA}/markets?{params}")
        except RuntimeError as exc:
            errors.append(str(exc))
            break
        batch = payload if isinstance(payload, list) else (payload.get("markets") or payload.get("data") or [])
        if not isinstance(batch, list) or not batch:
            break
        for raw in batch:
            if not isinstance(raw, dict):
                continue
            market = parse_pm_market(raw)
            if market and market.liquidity >= min_liquidity and market.volume24h >= min_volume:
                rows.append(market)
                if len(rows) >= maximum:
                    break
        if len(batch) < limit:
            break
        offset += limit
    rows.sort(key=lambda market: (market.volume24h, market.liquidity), reverse=True)
    return rows[:maximum], errors


def fetch_kalshi_markets(config: dict[str, Any]) -> tuple[list[KMarket], list[str]]:
    source = (config.get("sources") or {}).get("kalshi") or {}
    if not source.get("enabled"):
        return [], []
    maximum = max(1, integer(source.get("max_markets"), 2500))
    page_size = max(1, min(1000, integer(source.get("page_size"), 1000)))
    max_spread = finite(source.get("max_spread"), 0.12)
    rows: list[KMarket] = []
    errors: list[str] = []
    cursor = ""
    while len(rows) < maximum:
        params: dict[str, Any] = {"status": "open", "limit": page_size}
        if cursor:
            params["cursor"] = cursor
        try:
            payload = request_json(f"{KALSHI}/markets?{urllib.parse.urlencode(params)}")
        except RuntimeError as exc:
            errors.append(str(exc))
            break
        batch = payload.get("markets") if isinstance(payload, dict) else []
        if not isinstance(batch, list) or not batch:
            break
        for raw in batch:
            if not isinstance(raw, dict):
                continue
            market = parse_k_market(raw)
            if market and market.spread <= max_spread:
                rows.append(market)
                if len(rows) >= maximum:
                    break
        cursor = str(payload.get("cursor") or "") if isinstance(payload, dict) else ""
        if not cursor:
            break
    rows.sort(key=lambda market: (market.volume, market.liquidity), reverse=True)
    return rows[:maximum], errors


def market_price_row(market: PmMarket, observed_ts: int, provenance: str = "gamma_live") -> dict[str, Any]:
    return {
        "schema": PRICE_SCHEMA,
        "observed_ts": observed_ts,
        "observed_utc": iso_utc(observed_ts),
        "market_id": market.market_id,
        "event_id": market.event_id,
        "question": market.question,
        "category": market.category,
        "end_ts": market.end_ts,
        "bid": market.bid,
        "ask": market.ask,
        "mid": market.mid,
        "resolved_outcome": market.resolved_outcome,
        "quote_provenance": provenance,
    }


def observation_row(
    *,
    observed_ts: int,
    market: PmMarket,
    source: str,
    source_id: str,
    feature_name: str,
    feature_value: float,
    confidence: float,
    mapping_score: float,
    source_event_ts: int,
    q_external: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "schema": OBS_SCHEMA,
        "observed_ts": observed_ts,
        "observed_utc": iso_utc(observed_ts),
        "retrieved_ts": observed_ts,
        "market_id": market.market_id,
        "event_id": market.event_id,
        "question": market.question,
        "category": market.category,
        "end_ts": market.end_ts,
        "pm_bid": market.bid,
        "pm_ask": market.ask,
        "pm_mid": market.mid,
        "source": source,
        "source_id": source_id,
        "source_event_ts": source_event_ts,
        "source_age_seconds": max(0, observed_ts - source_event_ts) if source_event_ts else 0,
        "feature_name": feature_name,
        "feature_value": feature_value,
        "q_external": q_external,
        "confidence": clip(confidence, 0.0, 1.0),
        "mapping_score": clip(mapping_score, 0.0, 1.0),
        "metadata": metadata or {},
    }
    identity = {key: row[key] for key in ("observed_ts", "market_id", "source", "source_id", "feature_name")}
    row["observation_id"] = stable_hash(identity)
    return row


def collect_kalshi_observations(
    pm_markets: Sequence[PmMarket],
    k_markets: Sequence[KMarket],
    config: dict[str, Any],
    observed_ts: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = (config.get("sources") or {}).get("kalshi") or {}
    if not source.get("enabled"):
        return [], []
    min_score = finite(source.get("min_match_score"), 0.70)
    min_margin = finite(source.get("min_match_margin"), 0.05)
    min_confidence = finite(source.get("min_confidence"), 0.40)
    max_expiry_days = finite(source.get("max_expiry_difference_days"), 14.0)
    diagnostics: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []

    by_token: dict[str, list[KMarket]] = {}
    for km in k_markets:
        for token in set(tokens(km.title + " " + km.subtitle)):
            if len(token) >= 3:
                by_token.setdefault(token, []).append(km)

    for pm in pm_markets:
        candidates: set[KMarket] = set()
        for token in set(tokens(pm.question)):
            candidates.update(by_token.get(token, []))
        if not candidates:
            continue
        scored = sorted(
            ((score_match(pm, km, max_expiry_days), km) for km in candidates),
            key=lambda item: item[0].score,
            reverse=True,
        )
        best_score, best = scored[0]
        second = scored[1][0].score if len(scored) > 1 else 0.0
        margin = best_score.score - second
        quote_quality = math.exp(-best.spread / max(0.01, finite(source.get("spread_scale"), 0.05)))
        freshness = math.exp(-max(0, observed_ts - best.updated_ts) / max(3600.0, finite(source.get("freshness_half_life_seconds"), 21600.0))) if best.updated_ts else 0.75
        confidence = best_score.score * quote_quality * freshness * clip(margin / max(min_margin, 1e-6), 0.0, 1.0)
        rejection = best_score.rejection
        if not rejection and best_score.score < min_score:
            rejection = "weak_match"
        if not rejection and margin < min_margin:
            rejection = "ambiguous_match"
        if not rejection and confidence < min_confidence:
            rejection = "low_confidence"
        diagnostic = {
            "market_id": pm.market_id,
            "question": pm.question,
            "kalshi_ticker": best.ticker,
            "kalshi_title": best.title,
            "score": best_score.score,
            "margin": margin,
            "confidence": confidence,
            "rejection": rejection,
            "critical_numbers_match": best_score.numeric_match,
            "orientation_match": best_score.orientation_match,
            "expiry_hours": best_score.expiry_hours,
        }
        diagnostics.append(diagnostic)
        if rejection:
            continue
        observations.append(
            observation_row(
                observed_ts=observed_ts,
                market=pm,
                source="kalshi",
                source_id=best.ticker,
                feature_name="external_probability",
                feature_value=best.mid - pm.mid,
                q_external=best.mid,
                confidence=confidence,
                mapping_score=best_score.score,
                source_event_ts=best.updated_ts or observed_ts,
                metadata={
                    "external_bid": best.bid,
                    "external_ask": best.ask,
                    "external_spread": best.spread,
                    "match_margin": margin,
                    "event_ticker": best.event_ticker,
                },
            )
        )
    diagnostics.sort(key=lambda row: finite(row.get("score")), reverse=True)
    return observations, diagnostics[:100]


def fetch_binance_klines(symbol: str, interval: str, limit: int, start_ms: int | None = None, end_ms: int | None = None) -> list[list[Any]]:
    params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": max(1, min(1000, limit))}
    if start_ms is not None:
        params["startTime"] = start_ms
    if end_ms is not None:
        params["endTime"] = end_ms
    payload = request_json(f"{BINANCE}/api/v3/klines?{urllib.parse.urlencode(params)}")
    return payload if isinstance(payload, list) else []


def kline_features(klines: Sequence[Sequence[Any]]) -> dict[str, float]:
    closes = [finite(row[4], math.nan) for row in klines if len(row) > 5]
    closes = [value for value in closes if math.isfinite(value) and value > 0]
    if len(closes) < 3:
        return {}
    log_returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
    one_hour_steps = min(12, len(log_returns))
    return {
        "return_5m": log_returns[-1],
        "return_1h": sum(log_returns[-one_hour_steps:]),
        "return_24h": sum(log_returns[-min(288, len(log_returns)):]),
        "realized_vol_24h": statistics.pstdev(log_returns[-min(288, len(log_returns)):]) * math.sqrt(288) if len(log_returns) >= 2 else 0.0,
    }


def collect_binance_observations(
    pm_markets: Sequence[PmMarket], config: dict[str, Any], observed_ts: int
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    source = (config.get("sources") or {}).get("binance") or {}
    if not source.get("enabled"):
        return [], {}, []
    maximum_per_asset = max(1, integer(source.get("max_markets_per_asset"), 20))
    by_asset: dict[str, list[PmMarket]] = {}
    for market in pm_markets:
        asset = detect_asset(market.question + " " + market.description)
        if asset:
            by_asset.setdefault(asset, []).append(market)
    observations: list[dict[str, Any]] = []
    health: dict[str, Any] = {}
    errors: list[str] = []
    for asset, markets in sorted(by_asset.items()):
        symbol = f"{asset}USDT"
        try:
            klines = fetch_binance_klines(symbol, "5m", max(20, integer(source.get("kline_limit"), 289)))
            features = kline_features(klines)
        except RuntimeError as exc:
            errors.append(str(exc))
            health[asset] = {"status": "error"}
            continue
        source_event_ts = integer(klines[-1][6]) // 1000 if klines and len(klines[-1]) > 6 else observed_ts
        health[asset] = {"status": "ok", "features": features, "source_event_ts": source_event_ts}
        for market in sorted(markets, key=lambda item: (item.volume24h, item.liquidity), reverse=True)[:maximum_per_asset]:
            for name, value in sorted(features.items()):
                observations.append(
                    observation_row(
                        observed_ts=observed_ts,
                        market=market,
                        source="binance",
                        source_id=symbol,
                        feature_name=name,
                        feature_value=value,
                        confidence=finite(source.get("base_confidence"), 0.60),
                        mapping_score=1.0,
                        source_event_ts=source_event_ts,
                        metadata={"asset": asset, "symbol": symbol},
                    )
                )
    return observations, health, errors


def gdelt_query(question: str, maximum_terms: int = 7) -> str:
    useful = [token for token in tokens(question) if not re.fullmatch(r"\d+", token)]
    useful = sorted(dict.fromkeys(useful), key=lambda token: (-len(token), token))[:maximum_terms]
    return " ".join(useful)


def collect_gdelt_observations(
    pm_markets: Sequence[PmMarket], config: dict[str, Any], observed_ts: int
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    source = (config.get("sources") or {}).get("gdelt") or {}
    if not source.get("enabled"):
        return [], {}, []
    count = max(0, integer(source.get("markets_per_run"), 8))
    if count == 0 or not pm_markets:
        return [], {}, []
    offset = (observed_ts // 1800 * count) % max(1, len(pm_markets))
    selected = list(pm_markets[offset:offset + count])
    if len(selected) < count:
        selected.extend(pm_markets[: count - len(selected)])
    observations: list[dict[str, Any]] = []
    health: dict[str, Any] = {}
    errors: list[str] = []
    for market in selected:
        query = gdelt_query(market.question)
        if not query:
            continue
        params = urllib.parse.urlencode(
            {
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": max(1, min(75, integer(source.get("max_records"), 50))),
                "timespan": str(source.get("timespan") or "1d"),
                "sort": "datedesc",
            }
        )
        try:
            payload = request_json(f"{GDELT}?{params}", timeout=30.0, retries=2)
        except RuntimeError as exc:
            errors.append(str(exc))
            health[market.market_id] = {"status": "error", "query": query}
            continue
        articles = payload.get("articles") if isinstance(payload, dict) else []
        articles = articles if isinstance(articles, list) else []
        tones = [finite(article.get("tone"), math.nan) for article in articles if isinstance(article, dict)]
        tones = [value for value in tones if math.isfinite(value)]
        count_value = float(len(articles))
        tone_value = statistics.fmean(tones) if tones else 0.0
        latest = max((parse_timestamp(article.get("seendate")) for article in articles if isinstance(article, dict)), default=observed_ts)
        health[market.market_id] = {"status": "ok", "query": query, "articles": len(articles), "latest_ts": latest}
        for name, value in (("news_count_24h", math.log1p(count_value)), ("news_tone", tone_value)):
            observations.append(
                observation_row(
                    observed_ts=observed_ts,
                    market=market,
                    source="gdelt",
                    source_id=query,
                    feature_name=name,
                    feature_value=value,
                    confidence=finite(source.get("base_confidence"), 0.35),
                    mapping_score=0.55,
                    source_event_ts=latest,
                    metadata={"query": query, "article_count": len(articles)},
                )
            )
    return observations, health, errors


def merge_rows(
    existing: Sequence[dict[str, Any]],
    incoming: Sequence[dict[str, Any]],
    *,
    identity_fields: Sequence[str],
    max_rows: int,
    min_timestamp: int,
) -> list[dict[str, Any]]:
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in list(existing) + list(incoming):
        timestamp = integer(row.get("observed_ts"))
        if timestamp < min_timestamp:
            continue
        key = tuple(row.get(field) for field in identity_fields)
        previous = by_key.get(key)
        if previous is None or integer(previous.get("retrieved_ts")) <= integer(row.get("retrieved_ts")):
            by_key[key] = dict(row)
    rows = sorted(by_key.values(), key=lambda row: (integer(row.get("observed_ts")), str(row.get("market_id")), str(row.get("source", "")), str(row.get("feature_name", ""))))
    return rows[-max_rows:] if max_rows > 0 and len(rows) > max_rows else rows


def synthetic_quote(mid: float, half_spread: float) -> tuple[float, float]:
    return clip(mid - half_spread, 0.001, 0.999), clip(mid + half_spread, 0.001, 0.999)


def fetch_pm_history(token_id: str, start_ts: int, end_ts: int, interval: str = "1h") -> list[dict[str, Any]]:
    if not token_id:
        return []
    params = urllib.parse.urlencode({"market": token_id, "startTs": start_ts, "endTs": end_ts, "interval": interval, "fidelity": 60})
    payload = request_json(f"{PM_CLOB}/prices-history?{params}")
    history = payload.get("history") if isinstance(payload, dict) else []
    return history if isinstance(history, list) else []


def backfill_crypto_market(
    market: PmMarket,
    config: dict[str, Any],
    observed_ts: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    backfill = config.get("backfill") or {}
    days = max(1, integer(backfill.get("lookback_days"), 30))
    start_ts = observed_ts - days * 86400
    asset = detect_asset(market.question + " " + market.description)
    if not asset or not market.yes_token:
        return [], [], []
    errors: list[str] = []
    try:
        pm_history = fetch_pm_history(market.yes_token, start_ts, observed_ts, "1h")
    except RuntimeError as exc:
        return [], [], [str(exc)]
    try:
        klines = fetch_binance_klines(f"{asset}USDT", "1h", min(1000, days * 24 + 2), start_ts * 1000, observed_ts * 1000)
    except RuntimeError as exc:
        return [], [], [str(exc)]
    crypto_by_ts: list[tuple[int, float]] = []
    closes: list[float] = []
    for row in klines:
        if len(row) < 7:
            continue
        timestamp = integer(row[6]) // 1000
        close = finite(row[4], math.nan)
        if timestamp and math.isfinite(close) and close > 0:
            crypto_by_ts.append((timestamp, close))
            closes.append(close)
    crypto_by_ts.sort()
    price_rows: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    half_spread = finite(backfill.get("historical_half_spread"), 0.015)
    pm_points: list[tuple[int, float]] = []
    for row in pm_history:
        timestamp = parse_timestamp(row.get("t"))
        price = finite(row.get("p"), math.nan)
        if timestamp and math.isfinite(price) and 0.0 < price < 1.0:
            pm_points.append((timestamp, price))
            bid, ask = synthetic_quote(price, half_spread)
            price_rows.append(
                {
                    "schema": PRICE_SCHEMA,
                    "observed_ts": timestamp,
                    "observed_utc": iso_utc(timestamp),
                    "market_id": market.market_id,
                    "event_id": market.event_id,
                    "question": market.question,
                    "category": market.category,
                    "end_ts": market.end_ts,
                    "bid": bid,
                    "ask": ask,
                    "mid": price,
                    "resolved_outcome": market.resolved_outcome,
                    "quote_provenance": "clob_price_history_synthetic_spread",
                }
            )
    if len(crypto_by_ts) < 25 or len(pm_points) < 10:
        return price_rows, observations, errors
    crypto_times = [row[0] for row in crypto_by_ts]
    crypto_closes = [row[1] for row in crypto_by_ts]
    for timestamp, pm_mid in pm_points:
        index = bisect.bisect_right(crypto_times, timestamp) - 1
        if index < 24:
            continue
        one = math.log(crypto_closes[index] / crypto_closes[index - 1])
        ret24 = math.log(crypto_closes[index] / crypto_closes[index - 24])
        returns = [math.log(crypto_closes[j] / crypto_closes[j - 1]) for j in range(index - 23, index + 1)]
        vol = statistics.pstdev(returns) * math.sqrt(24) if len(returns) >= 2 else 0.0
        bid, ask = synthetic_quote(pm_mid, half_spread)
        synthetic_market = PmMarket(
            **{**market.__dict__, "bid": bid, "ask": ask, "mid": pm_mid}
        )
        for name, value in (("return_1h", one), ("return_24h", ret24), ("realized_vol_24h", vol)):
            observations.append(
                observation_row(
                    observed_ts=timestamp,
                    market=synthetic_market,
                    source="binance",
                    source_id=f"{asset}USDT",
                    feature_name=name,
                    feature_value=value,
                    confidence=0.55,
                    mapping_score=1.0,
                    source_event_ts=crypto_times[index],
                    metadata={"asset": asset, "backfill": True},
                )
            )
    return price_rows, observations, errors


def bounded_backfill(
    pm_markets: Sequence[PmMarket],
    config: dict[str, Any],
    state: dict[str, Any],
    observed_ts: int,
    mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[str]]:
    backfill = config.get("backfill") or {}
    if not backfill.get("enabled"):
        return [], [], state, []
    completed = set(str(value) for value in state.get("backfilled_market_ids") or [])
    maximum = integer(backfill.get("max_markets_per_run"), 2)
    if mode == "backfill":
        maximum = integer(backfill.get("manual_max_markets_per_run"), 20)
    selected = [market for market in pm_markets if market.category == "crypto" and market.market_id not in completed][:max(0, maximum)]
    prices: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    errors: list[str] = []
    for market in selected:
        p_rows, o_rows, errs = backfill_crypto_market(market, config, observed_ts)
        prices.extend(p_rows)
        observations.extend(o_rows)
        errors.extend(errs)
        if p_rows:
            completed.add(market.market_id)
    updated = dict(state)
    updated["schema"] = STATE_SCHEMA
    updated["updated_ts"] = observed_ts
    updated["backfilled_market_ids"] = sorted(completed)[-5000:]
    updated["last_backfill_market_ids"] = [market.market_id for market in selected]
    return prices, observations, updated, errors


def future_price_index(price_rows: Sequence[dict[str, Any]]) -> dict[str, tuple[list[int], list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in price_rows:
        market_id = str(row.get("market_id") or "")
        timestamp = integer(row.get("observed_ts"))
        if market_id and timestamp:
            grouped.setdefault(market_id, []).append(row)
    output: dict[str, tuple[list[int], list[dict[str, Any]]]] = {}
    for market_id, rows in grouped.items():
        rows.sort(key=lambda row: integer(row.get("observed_ts")))
        output[market_id] = ([integer(row.get("observed_ts")) for row in rows], rows)
    return output


def label_observations(
    observations: Sequence[dict[str, Any]],
    price_rows: Sequence[dict[str, Any]],
    horizon_seconds: int,
    tolerance_seconds: int,
) -> list[dict[str, Any]]:
    index = future_price_index(price_rows)
    labeled: list[dict[str, Any]] = []
    for observation in observations:
        market_id = str(observation.get("market_id") or "")
        t0 = integer(observation.get("observed_ts"))
        target = t0 + horizon_seconds
        series = index.get(market_id)
        if not series:
            continue
        times, rows = series
        position = bisect.bisect_left(times, target)
        if position >= len(rows) or times[position] > target + tolerance_seconds:
            continue
        future = rows[position]
        current_mid = finite(observation.get("pm_mid"), math.nan)
        future_mid = finite(future.get("mid"), math.nan)
        if not math.isfinite(current_mid) or not math.isfinite(future_mid):
            continue
        row = dict(observation)
        row.update(
            {
                "horizon_seconds": horizon_seconds,
                "future_ts": times[position],
                "future_mid": future_mid,
                "future_bid": finite(future.get("bid"), future_mid),
                "future_ask": finite(future.get("ask"), future_mid),
                "target_delta": future_mid - current_mid,
            }
        )
        labeled.append(row)
    labeled.sort(key=lambda row: (integer(row.get("observed_ts")), str(row.get("market_id"))))
    return labeled


def fit_signal(train: Sequence[dict[str, Any]], direct_probability: bool, ridge: float) -> tuple[float, float, float]:
    if not train:
        return 0.0, 0.0, 1.0
    xs = []
    ys = []
    for row in train:
        if direct_probability:
            x = finite(row.get("q_external"), finite(row.get("pm_mid"))) - finite(row.get("pm_mid"))
        else:
            x = finite(row.get("feature_value"), math.nan)
        y = finite(row.get("target_delta"), math.nan)
        if math.isfinite(x) and math.isfinite(y):
            xs.append(x)
            ys.append(y)
    if len(xs) < 2:
        return 0.0, 0.0, 1.0
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    variance = statistics.fmean((x - mean_x) ** 2 for x in xs)
    covariance = statistics.fmean((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = covariance / (variance + ridge)
    if direct_probability:
        slope = clip(slope, 0.0, 1.5)
        mean_y = 0.0
        mean_x = 0.0
    else:
        slope = clip(slope, -10.0, 10.0)
    return mean_y, slope, mean_x


def trade_pnl(row: dict[str, Any], predicted_delta: float, extra_cost: float) -> tuple[float, int]:
    bid = finite(row.get("pm_bid"), finite(row.get("pm_mid")))
    ask = finite(row.get("pm_ask"), finite(row.get("pm_mid")))
    future_bid = finite(row.get("future_bid"), finite(row.get("future_mid")))
    future_ask = finite(row.get("future_ask"), finite(row.get("future_mid")))
    threshold = max(extra_cost, 0.5 * max(0.0, ask - bid) + extra_cost)
    if predicted_delta > threshold:
        return future_bid - ask - extra_cost, 1
    if predicted_delta < -threshold:
        return bid - future_ask - extra_cost, -1
    return 0.0, 0


def max_drawdown(pnls: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def block_bootstrap_pvalue(values: Sequence[float], block: int = 5, reps: int = 1000, seed: int = 20260824) -> float:
    xs = [finite(value) for value in values if math.isfinite(finite(value, math.nan))]
    n = len(xs)
    if n < 3 or statistics.fmean(xs) <= 0.0:
        return 1.0
    observed = statistics.fmean(xs)
    centered = [value - observed for value in xs]
    width = max(1, min(block, n))
    rng = random.Random(seed)
    exceed = 0
    for _ in range(reps):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(centered[(start + offset) % n] for offset in range(width))
        if statistics.fmean(sample[:n]) >= observed:
            exceed += 1
    return (exceed + 1) / (reps + 1)


def purged_training_rows(rows: Sequence[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    """Return only labels that were observable before the current decision."""
    decision_ts = integer(rows[index].get("observed_ts"))
    return [candidate for candidate in rows[:index] if integer(candidate.get("future_ts")) < decision_ts]


def evaluate_candidate(
    rows: Sequence[dict[str, Any]],
    config: dict[str, Any],
    source: str,
    feature_name: str,
    horizon_seconds: int,
) -> dict[str, Any]:
    backtest = config.get("backtest") or {}
    min_train = max(2, integer(backtest.get("min_train_observations"), 30))
    ridge = max(1e-12, finite(backtest.get("ridge"), 1e-5))
    base_extra_cost = max(0.0, finite(backtest.get("extra_cost_bps"), 20.0) / 10000.0)
    direct_probability = feature_name == "external_probability"
    trades_by_stress: dict[str, list[float]] = {str(multiplier): [] for multiplier in backtest.get("cost_stress_multipliers", [1.0, 1.5, 2.0])}
    predictions: list[float] = []
    targets: list[float] = []
    directions: list[int] = []
    fold_pnls: list[float] = []
    test_rows = 0

    for index, row in enumerate(rows):
        train = purged_training_rows(rows, index)
        if len(train) < min_train:
            continue
        intercept, slope, center = fit_signal(train, direct_probability, ridge)
        x = (finite(row.get("q_external"), finite(row.get("pm_mid"))) - finite(row.get("pm_mid"))) if direct_probability else finite(row.get("feature_value"))
        predicted = intercept + slope * (x - center)
        predictions.append(predicted)
        targets.append(finite(row.get("target_delta")))
        test_rows += 1
        unit_pnl = 0.0
        direction = 0
        for multiplier in backtest.get("cost_stress_multipliers", [1.0, 1.5, 2.0]):
            pnl, side = trade_pnl(row, predicted, base_extra_cost * finite(multiplier, 1.0))
            trades_by_stress[str(multiplier)].append(pnl)
            if finite(multiplier, 1.0) == 1.0:
                unit_pnl = pnl
                direction = side
        directions.append(direction)
        fold_pnls.append(unit_pnl)

    normal = trades_by_stress.get("1.0", [])
    traded = [pnl for pnl, side in zip(normal, directions) if side != 0]
    wins = sum(pnl > 0.0 for pnl in traded)
    losses = [pnl for pnl in traded if pnl < 0.0]
    gains = [pnl for pnl in traded if pnl > 0.0]
    profit_factor = sum(gains) / abs(sum(losses)) if losses else (math.inf if gains else 0.0)
    mse = statistics.fmean((p - y) ** 2 for p, y in zip(predictions, targets)) if predictions else 0.0
    baseline_mse = statistics.fmean(y**2 for y in targets) if targets else 0.0

    fold_count = max(2, integer(backtest.get("folds"), 4))
    fold_sums: list[float] = []
    if fold_pnls:
        for fold in range(fold_count):
            lo = math.floor(len(fold_pnls) * fold / fold_count)
            hi = math.floor(len(fold_pnls) * (fold + 1) / fold_count)
            if hi > lo:
                fold_sums.append(sum(fold_pnls[lo:hi]))
    positive_fold_fraction = sum(value > 0 for value in fold_sums) / len(fold_sums) if fold_sums else 0.0
    pvalue = block_bootstrap_pvalue(traded, block=max(1, integer(backtest.get("bootstrap_block"), 5)), reps=max(100, integer(backtest.get("bootstrap_reps"), 1000)), seed=20260824 + sum(ord(c) for c in source + feature_name) + horizon_seconds)

    metrics = {
        "labeled_observations": len(rows),
        "oos_predictions": test_rows,
        "trades": len(traded),
        "net_pnl_per_share": sum(normal),
        "mean_pnl_per_trade": statistics.fmean(traded) if traded else 0.0,
        "hit_rate": wins / len(traded) if traded else 0.0,
        "profit_factor": profit_factor,
        "max_drawdown_per_share": max_drawdown(normal),
        "prediction_mse": mse,
        "baseline_zero_delta_mse": baseline_mse,
        "mse_improvement": baseline_mse - mse,
        "positive_fold_fraction": positive_fold_fraction,
        "active_folds": len(fold_sums),
        "cost_stress_net_pnl": {key: sum(values) for key, values in trades_by_stress.items()},
    }
    gate = config.get("gates") or {}
    reasons: list[str] = []
    if test_rows < integer(gate.get("min_oos_predictions"), 40):
        reasons.append("insufficient_oos_predictions")
    if len(traded) < integer(gate.get("min_trades"), 20):
        reasons.append("insufficient_trades")
    if sum(normal) <= 0.0:
        reasons.append("nonpositive_net_pnl")
    for multiplier in (1.5, 2.0):
        if sum(trades_by_stress.get(str(multiplier), [])) <= 0.0:
            reasons.append(f"nonpositive_{multiplier:g}x_cost_stress")
    if pvalue > finite(gate.get("max_bootstrap_pvalue"), 0.10):
        reasons.append("bootstrap_gate")
    if positive_fold_fraction < finite(gate.get("min_positive_fold_fraction"), 0.50):
        reasons.append("fold_stability_gate")
    if metrics["mse_improvement"] <= 0.0:
        reasons.append("no_predictive_mse_improvement")
    return {
        "candidate_id": f"external:{source}:{feature_name}:{horizon_seconds}s",
        "source": source,
        "feature_name": feature_name,
        "horizon_seconds": horizon_seconds,
        "observations": test_rows,
        "raw_pvalue": pvalue,
        "metrics": metrics,
        "gate_pass": not reasons,
        "reasons": reasons,
        "evidence_type": "purged_chronological_external_information_backtest",
        "executable_proxy": True,
        "requires_exact_clob_replay_before_integration": True,
    }


def run_backtests(
    observations: Sequence[dict[str, Any]],
    prices: Sequence[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    backtest = config.get("backtest") or {}
    candidates: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in observations:
        source = str(row.get("source") or "")
        feature = str(row.get("feature_name") or "")
        if source and feature:
            groups.setdefault((source, feature), []).append(row)
    for horizon in [integer(value) for value in backtest.get("horizons_seconds", [3600, 21600, 86400])]:
        tolerance = max(integer(backtest.get("future_price_tolerance_seconds"), horizon // 2), 60)
        for (source, feature), rows in sorted(groups.items()):
            labeled = label_observations(rows, prices, horizon, tolerance)
            if labeled:
                candidates.append(evaluate_candidate(labeled, config, source, feature, horizon))
    candidates.sort(
        key=lambda candidate: (
            0 if candidate.get("gate_pass") else 1,
            finite(candidate.get("raw_pvalue"), 1.0),
            -finite((candidate.get("metrics") or {}).get("cost_stress_net_pnl", {}).get("2.0"), 0.0),
            str(candidate.get("candidate_id")),
        )
    )
    return candidates


def source_reliability(candidates: Sequence[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    prior_score = finite((config.get("reliability") or {}).get("prior_score"), 0.50)
    prior_weight = max(1.0, finite((config.get("reliability") or {}).get("prior_weight"), 20.0))
    by_source: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_source.setdefault(str(candidate.get("source") or "unknown"), []).append(candidate)
    output: dict[str, Any] = {}
    for source, rows in sorted(by_source.items()):
        observations = sum(integer(row.get("observations")) for row in rows)
        passes = sum(bool(row.get("gate_pass")) for row in rows)
        pnl = sum(finite((row.get("metrics") or {}).get("cost_stress_net_pnl", {}).get("2.0")) for row in rows)
        evidence_score = clip(0.5 + 0.25 * math.tanh(pnl) + 0.25 * (passes / max(1, len(rows))), 0.0, 1.0)
        weight = min(500.0, float(observations))
        score = (prior_weight * prior_score + weight * evidence_score) / (prior_weight + weight)
        output[source] = {
            "score": score,
            "observations": observations,
            "candidate_count": len(rows),
            "passing_candidates": passes,
            "two_x_cost_stressed_pnl_per_share": pnl,
        }
    return output


def alpha_factory_evidence(candidates: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    best = candidates[0]
    metrics = best.get("metrics") or {}
    reasons = list(best.get("reasons") or [])
    if best.get("requires_exact_clob_replay_before_integration"):
        reasons.append("exact_clob_replay_required_before_integration")
    return {
        "candidate_id": str(best.get("candidate_id")),
        "family": "external_information",
        "specification": f"{best.get('source')}:{best.get('feature_name')}:{best.get('horizon_seconds')}s",
        "evidence_type": str(best.get("evidence_type")),
        "observations": integer(best.get("observations")),
        "raw_pvalue": finite(best.get("raw_pvalue"), 1.0),
        "gate_pass_before_fdr": bool(best.get("gate_pass")),
        "integration_evidence_pass": False,
        "integration_reasons": ["exact_executable_clob_replay_and_incumbent_ablation_required"],
        "reasons": sorted(set(reasons)),
        "critical_failures": [],
        "metrics": {
            "oos_predictions": integer(metrics.get("oos_predictions")),
            "trades": integer(metrics.get("trades")),
            "net_pnl_per_share": finite(metrics.get("net_pnl_per_share")),
            "two_x_cost_stressed_pnl_per_share": finite((metrics.get("cost_stress_net_pnl") or {}).get("2.0")),
            "max_drawdown_per_share": finite(metrics.get("max_drawdown_per_share")),
            "profit_factor": finite(metrics.get("profit_factor")),
            "mse_improvement": finite(metrics.get("mse_improvement")),
            "positive_fold_fraction": finite(metrics.get("positive_fold_fraction")),
            "incremental_utility": finite((metrics.get("cost_stress_net_pnl") or {}).get("2.0")),
            "single_model_compatible": True,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Polymarket External Intelligence",
        "",
        f"- generated: `{report['generated_utc']}`",
        f"- status: **{report['status']}**",
        "- boundary: **public/free data, read-only, paper research only**",
        f"- active Polymarket markets: {report['collection']['polymarket_markets']}",
        f"- new normalized observations: {report['collection']['new_observations']}",
        f"- stored observations: {report['storage']['observation_rows']}",
        f"- stored price snapshots: {report['storage']['price_rows']}",
        "",
        "## Source health",
        "",
        "```json",
        json.dumps(report.get("source_health") or {}, indent=2, sort_keys=True),
        "```",
        "",
        "## Walk-forward candidates",
        "",
    ]
    candidates = report.get("backtest", {}).get("candidates") or []
    if not candidates:
        lines.append("- No candidate has enough chronologically labeled data yet.")
    for candidate in candidates[:10]:
        metrics = candidate.get("metrics") or {}
        lines.extend(
            [
                f"### `{candidate['candidate_id']}`",
                f"- gate pass: `{str(bool(candidate.get('gate_pass'))).lower()}`",
                f"- OOS predictions: {metrics.get('oos_predictions', 0)}",
                f"- trades: {metrics.get('trades', 0)}",
                f"- normal net PnL/share: {finite(metrics.get('net_pnl_per_share')):.6g}",
                f"- 2x-cost net PnL/share: {finite((metrics.get('cost_stress_net_pnl') or {}).get('2.0')):.6g}",
                f"- bootstrap p-value: {finite(candidate.get('raw_pvalue'), 1.0):.6g}",
                "- reasons: " + (", ".join(candidate.get("reasons") or []) or "none"),
                "",
            ]
        )
    lines.extend(
        [
            "## Safety and interpretation",
            "",
            "- Historical CLOB price histories do not contain full historical order books; backfilled tests use a conservative synthetic spread and remain an executable proxy.",
            "- No external source is treated as truth. A source earns reliability only through chronological incremental evidence.",
            "- A passing result is research evidence, not authority to mutate `config/live_champion.json`, deploy, or submit an authenticated order.",
        ]
    )
    return "\n".join(lines) + "\n"


def demo_payload(now: int) -> tuple[list[PmMarket], list[KMarket]]:
    pm = []
    km = []
    for index in range(6):
        price = 0.35 + 0.04 * index
        pm.append(
            PmMarket(
                market_id=f"pm-{index}", condition_id=f"c-{index}", event_id=f"e-{index}",
                slug=f"btc-above-{60000 + index * 1000}",
                question=f"Will Bitcoin be above {60000 + index * 1000} by December 31 2026?",
                description="Bitcoin price market", category="crypto", end_ts=now + 30 * 86400,
                liquidity=10000.0, volume24h=5000.0, bid=price - 0.01, ask=price + 0.01,
                mid=price, yes_token=f"yes-{index}", no_token=f"no-{index}", resolved_outcome=None,
            )
        )
        km.append(
            KMarket(
                ticker=f"KXBTC-{index}", event_ticker="KXBTC", title=f"Bitcoin above {60000 + index * 1000} on December 31 2026",
                subtitle="", rules="", close_ts=now + 30 * 86400, updated_ts=now,
                bid=price + 0.01, ask=price + 0.03, mid=price + 0.02, spread=0.02,
                volume=1000.0, liquidity=1000.0,
            )
        )
    return pm, km


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect and backtest external information for Polymarket")
    parser.add_argument("--config", type=Path, default=Path("config/external_intelligence.json"))
    parser.add_argument("--observations-in", type=Path, required=True)
    parser.add_argument("--prices-in", type=Path, required=True)
    parser.add_argument("--state-in", type=Path, required=True)
    parser.add_argument("--observations-out", type=Path, required=True)
    parser.add_argument("--prices-out", type=Path, required=True)
    parser.add_argument("--state-out", type=Path, required=True)
    parser.add_argument("--signals-out", type=Path, required=True)
    parser.add_argument("--report-json", type=Path, required=True)
    parser.add_argument("--report-markdown", type=Path, required=True)
    parser.add_argument("--mode", choices=("incremental", "backfill", "demo"), default="incremental")
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()

    config = read_json(args.config, {})
    validate_config(config)
    now = int(time.time()) if args.now is None else args.now
    state = read_json(args.state_in, {})
    state = state if isinstance(state, dict) else {}
    old_observations = read_jsonl_gz(args.observations_in)
    old_prices = read_jsonl_gz(args.prices_in)

    source_errors: list[str] = []
    if args.mode == "demo":
        pm_markets, k_markets = demo_payload(now)
        pm_errors: list[str] = []
        k_errors: list[str] = []
    else:
        pm_markets, pm_errors = fetch_pm_markets(config)
        k_markets, k_errors = fetch_kalshi_markets(config)
        source_errors.extend(pm_errors)
        source_errors.extend(k_errors)

    live_prices = [market_price_row(market, now) for market in pm_markets]
    kalshi_obs, match_diagnostics = collect_kalshi_observations(pm_markets, k_markets, config, now)

    if args.mode == "demo":
        binance_obs: list[dict[str, Any]] = []
        binance_health: dict[str, Any] = {"demo": "skipped"}
        binance_errors: list[str] = []
        gdelt_obs: list[dict[str, Any]] = []
        gdelt_health: dict[str, Any] = {"demo": "skipped"}
        gdelt_errors: list[str] = []
    else:
        binance_obs, binance_health, binance_errors = collect_binance_observations(pm_markets, config, now)
        gdelt_obs, gdelt_health, gdelt_errors = collect_gdelt_observations(pm_markets, config, now)
        source_errors.extend(binance_errors)
        source_errors.extend(gdelt_errors)

    backfill_prices: list[dict[str, Any]] = []
    backfill_obs: list[dict[str, Any]] = []
    if args.mode != "demo":
        backfill_prices, backfill_obs, state, backfill_errors = bounded_backfill(pm_markets, config, state, now, args.mode)
        source_errors.extend(backfill_errors)

    new_observations = kalshi_obs + binance_obs + gdelt_obs + backfill_obs
    new_prices = live_prices + backfill_prices
    storage = config.get("storage") or {}
    min_timestamp = now - max(1, integer(storage.get("retention_days"), 180)) * 86400
    observations = merge_rows(
        old_observations,
        new_observations,
        identity_fields=("observation_id",),
        max_rows=max(1, integer(storage.get("max_observation_rows"), 250000)),
        min_timestamp=min_timestamp,
    )
    prices = merge_rows(
        old_prices,
        new_prices,
        identity_fields=("market_id", "observed_ts"),
        max_rows=max(1, integer(storage.get("max_price_rows"), 250000)),
        min_timestamp=min_timestamp,
    )

    candidates = run_backtests(observations, prices, config)
    reliability = source_reliability(candidates, config)
    evidence = alpha_factory_evidence(candidates)
    accepted = [row for row in new_observations if finite(row.get("confidence")) >= finite((config.get("gates") or {}).get("min_signal_confidence"), 0.35)]
    state.update(
        {
            "schema": STATE_SCHEMA,
            "updated_ts": now,
            "last_run_mode": args.mode,
            "source_reliability": reliability,
            "last_observation_count": len(new_observations),
            "last_price_count": len(new_prices),
            "paper_only": True,
            "authenticated_execution": False,
            "direct_champion_mutation": False,
        }
    )
    source_health = {
        "polymarket": {"status": "ok" if pm_markets else "degraded", "markets": len(pm_markets), "errors": pm_errors},
        "kalshi": {"status": "ok" if k_markets else "degraded", "markets": len(k_markets), "accepted_matches": len(kalshi_obs), "errors": k_errors},
        "binance": {"status": "ok" if not binance_errors else "degraded", "details": binance_health, "errors": binance_errors},
        "gdelt": {"status": "ok" if not gdelt_errors else "degraded", "details": gdelt_health, "errors": gdelt_errors},
    }
    passing = [candidate for candidate in candidates if candidate.get("gate_pass")]
    if source_errors and not new_observations:
        status = "DEGRADED_SOURCE_FAILURE"
    elif passing:
        status = "VALIDATED_CHALLENGER_EVIDENCE"
    elif candidates:
        status = "BACKTESTING"
    else:
        status = "COLLECTING_HISTORY"
    report = {
        "schema": SCHEMA,
        "generated_ts": now,
        "generated_utc": iso_utc(now),
        "status": status,
        "paper_only": True,
        "submitted_orders": 0,
        "authenticated_execution": False,
        "direct_champion_mutation": False,
        "production_signal_write": False,
        "collection": {
            "polymarket_markets": len(pm_markets),
            "kalshi_markets": len(k_markets),
            "new_observations": len(new_observations),
            "accepted_signal_rows": len(accepted),
            "kalshi_matches": len(kalshi_obs),
            "binance_rows": len(binance_obs) + len(backfill_obs),
            "gdelt_rows": len(gdelt_obs),
            "source_errors": source_errors,
        },
        "storage": {
            "observation_rows": len(observations),
            "price_rows": len(prices),
            "retention_days": integer(storage.get("retention_days"), 180),
        },
        "source_health": source_health,
        "mapping_diagnostics": match_diagnostics,
        "source_reliability": reliability,
        "backtest": {"candidate_count": len(candidates), "passing_candidates": len(passing), "candidates": candidates[:50]},
        "alpha_factory_evidence": evidence,
        "safety": {
            "data_sources_public_or_free": True,
            "point_in_time_timestamps_required": True,
            "lookahead_rejected": True,
            "ambiguous_mapping_abstains": True,
            "external_source_is_not_truth": True,
            "real_money_execution": False,
        },
    }

    write_jsonl_gz(args.observations_out, observations)
    write_jsonl_gz(args.prices_out, prices)
    atomic_json(args.state_out, state)
    atomic_text(args.signals_out, "".join(json.dumps(row, sort_keys=True) + "\n" for row in accepted))
    atomic_json(args.report_json, report)
    atomic_text(args.report_markdown, render_markdown(report))
    print(
        "external_intelligence"
        f" status={status}"
        f" markets={len(pm_markets)}"
        f" observations={len(new_observations)}"
        f" stored={len(observations)}"
        f" candidates={len(candidates)}"
        f" passing={len(passing)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
