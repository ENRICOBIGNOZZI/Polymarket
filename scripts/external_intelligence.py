#!/usr/bin/env python3
"""Public/free external-information collector and research backtester for Polymarket.

The worker is deliberately research-only. It discovers active Polymarket markets,
collects timestamped public data, persists compact point-in-time histories, and
runs purged chronological backtests. It never writes production signals, changes
the live champion, handles credentials, or submits orders.

Initial source adapters:
  * Kalshi public market data -> conservative cross-venue probability matches.
  * Binance public market-data-only API -> crypto returns/volatility features.
  * GDELT DOC API -> news count/tone features for a rotating market sample.

External data is not assumed to be true. It becomes useful only after incremental
OOS evidence survives quote/cost stress and temporal-stability gates.
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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Sequence

REPORT_SCHEMA = "polymarket_external_intelligence_report_v1"
STATE_SCHEMA = "polymarket_external_intelligence_state_v1"
OBS_SCHEMA = "polymarket_external_observation_v1"
PRICE_SCHEMA = "polymarket_external_price_v1"

PM_GAMMA = "https://gamma-api.polymarket.com"
PM_CLOB = "https://clob.polymarket.com"
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
BINANCE = "https://data-api.binance.vision"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "before", "by",
    "did", "do", "does", "for", "from", "has", "have", "if", "in", "is", "it",
    "its", "of", "on", "or", "the", "their", "there", "this", "to", "was", "were",
    "what", "when", "where", "which", "who", "will", "with", "would", "market",
    "event", "contract", "occur", "happen",
}
MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}
UP_WORDS = {"above", "over", "higher", "greater", "exceed", "exceeds", "more", "rise", "increase"}
DOWN_WORDS = {"below", "under", "lower", "less", "decrease", "fall", "drop"}
NEGATIONS = {"no", "not", "never", "without", "fail", "fails", "failed"}
BARRIER_WORDS = {"reach", "reaches", "hit", "hits", "touch", "touches", "break", "breaks", "dip", "dips"}

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
class Match:
    score: float
    margin: float
    confidence: float
    rejection: str
    numeric_match: bool
    orientation_match: bool
    expiry_hours: float | None
    market: KMarket


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def clip(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def probability_price(value: Any, default: float = math.nan) -> float:
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
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
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
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
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


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-zA-Z0-9%.$+-]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def tokens(value: str) -> list[str]:
    return [token for token in normalize_text(value).split() if token not in STOPWORDS and len(token) > 1]


def threshold_numbers(value: str) -> set[str]:
    normalized = normalize_text(value)
    has_month = bool(set(normalized.split()).intersection(MONTHS))
    output: set[str] = set()
    for token in re.findall(r"(?<![a-z])[-+]?\d+(?:[.,]\d+)?%?", normalized):
        token = token.replace(",", "")
        raw = token[:-1] if token.endswith("%") else token
        number = finite(raw, math.nan)
        if not math.isfinite(number):
            continue
        absolute = abs(number)
        integer_like = number.is_integer()
        if integer_like and 1900 <= absolute <= 2100:
            continue
        if has_month and integer_like and 1 <= absolute <= 31:
            continue
        if token.endswith("%") or "." in raw or number < 0 or absolute > 31 or not has_month:
            output.add(token)
    return output


def orientation(value: str) -> tuple[int, int]:
    token_set = set(tokens(value))
    direction = 1 if token_set.intersection(UP_WORDS) else (-1 if token_set.intersection(DOWN_WORDS) else 0)
    negated = 1 if token_set.intersection(NEGATIONS) else 0
    return direction, negated


def detect_asset(value: str) -> str | None:
    normalized = normalize_text(value)
    token_set = set(normalized.split())
    for asset, aliases in ASSET_ALIASES.items():
        for alias in aliases:
            if (" " in alias and alias in normalized) or (" " not in alias and alias in token_set):
                return asset
    return None


def classify_market(value: str, raw: dict[str, Any] | None = None) -> str:
    normalized = normalize_text(value)
    if detect_asset(normalized):
        return "crypto"
    if any(word in normalized for word in ("temperature", "rain", "snow", "weather", "hurricane")):
        return "weather"
    if any(word in normalized for word in ("election", "president", "prime minister", "vote", "congress")):
        return "politics"
    if any(word in normalized for word in ("inflation", "gdp", "unemployment", "interest rate", "cpi", "federal reserve")):
        return "macro"
    tags = [] if raw is None else parse_array(raw.get("tags"))
    tag_text = normalize_text(" ".join(
        str(tag.get("label") or tag.get("slug") or tag) if isinstance(tag, dict) else str(tag)
        for tag in tags
    ))
    if any(word in tag_text for word in ("sports", "nba", "nfl", "mlb", "soccer", "tennis")):
        return "sports"
    return "general"


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def price_threshold(value: str, spot: float) -> float | None:
    if not math.isfinite(spot) or spot <= 0.0:
        return None
    has_month = bool(set(normalize_text(value).split()).intersection(MONTHS))
    candidates: list[tuple[float, bool]] = []
    pattern = re.compile(
        r"(?i)(?P<currency>\$|usd\s*)?"
        r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
        r"\s*(?P<suffix>[kmb])?\s*(?P<unit>usd|usdt|dollars?)?"
    )
    for match in pattern.finditer(value or ""):
        raw = match.group("number").replace(",", "")
        number = finite(raw, math.nan)
        if not math.isfinite(number) or number <= 0.0:
            continue
        suffix = (match.group("suffix") or "").lower()
        multiplier = {"": 1.0, "k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}[suffix]
        number *= multiplier
        integer_like = float(number).is_integer()
        if integer_like and 1900 <= number <= 2100:
            continue
        if has_month and integer_like and 1 <= number <= 31:
            continue
        ratio = number / spot
        if ratio < 0.02 or ratio > 50.0:
            continue
        explicit = bool(match.group("currency") or suffix or match.group("unit"))
        candidates.append((number, explicit))

    explicit_values = sorted({round(number, 8) for number, explicit in candidates if explicit})
    values = explicit_values or sorted({round(number, 8) for number, _ in candidates})
    # Multiple distinct price levels generally encode a range or a multi-strike
    # question. A single-threshold model must abstain rather than choose one.
    return float(values[0]) if len(values) == 1 else None


def kalshi_candidate_compatible(pm: PmMarket, km: KMarket) -> bool:
    pm_text = pm.question + " " + pm.description
    kalshi_text = km.title + " " + km.subtitle + " " + km.rules
    pm_asset = detect_asset(pm_text)
    kalshi_asset = detect_asset(kalshi_text)
    if pm_asset or kalshi_asset:
        return pm_asset is not None and pm_asset == kalshi_asset
    pm_category = pm.category or classify_market(pm_text)
    kalshi_category = classify_market(kalshi_text)
    if pm_category != "general" and kalshi_category != "general" and pm_category != kalshi_category:
        return False
    return True


def crypto_threshold_probability(
    market: PmMarket,
    asset: str,
    features: dict[str, float],
    now: int,
    config: dict[str, Any],
) -> tuple[float, float, dict[str, Any]] | None:
    detected = detect_asset(market.question + " " + market.description)
    if market.category != "crypto" or detected != asset:
        return None
    spot = finite(features.get("spot"), math.nan)
    daily_vol = abs(finite(features.get("realized_vol_24h"), math.nan))
    if not math.isfinite(spot) or spot <= 0.0 or not math.isfinite(daily_vol) or daily_vol <= 0.0:
        return None
    threshold = price_threshold(market.question + " " + market.description, spot)
    if threshold is None or market.end_ts <= now:
        return None

    source = (config.get("sources") or {}).get("binance") or {}
    settings = source.get("probability_model") or {}
    min_hours = max(0.25, finite(settings.get("min_horizon_hours"), 1.0))
    max_days = max(1.0, finite(settings.get("max_horizon_days"), 365.0))
    horizon_hours = clip((market.end_ts - now) / 3600.0, min_hours, 24.0 * max_days)
    horizon_days = horizon_hours / 24.0
    daily_vol = clip(
        daily_vol,
        finite(settings.get("min_daily_vol"), 0.005),
        finite(settings.get("max_daily_vol"), 0.25),
    )
    horizon_sigma = max(1e-6, daily_vol * math.sqrt(horizon_days))
    drift_shrink = clip(finite(settings.get("drift_shrink"), 0.10), 0.0, 0.25)
    daily_drift = clip(
        drift_shrink * finite(features.get("return_24h"), 0.0),
        -finite(settings.get("max_abs_daily_drift"), 0.01),
        finite(settings.get("max_abs_daily_drift"), 0.01),
    )

    normalized = normalize_text(market.question + " " + market.description)
    word_set = set(normalized.split())
    direction, _ = orientation(normalized)
    is_barrier = bool(word_set.intersection(BARRIER_WORDS))
    if "dip" in word_set or "dips" in word_set:
        direction = -1
        is_barrier = True
    elif is_barrier and direction == 0:
        direction = 1 if threshold >= spot else -1
    if direction == 0:
        return None

    if is_barrier:
        crossed = (direction > 0 and spot >= threshold) or (direction < 0 and spot <= threshold)
        if crossed:
            probability = 0.997
        else:
            distance = abs(math.log(threshold / spot))
            probability = 2.0 * (1.0 - normal_cdf(distance / horizon_sigma))
        event_type = "upper_barrier" if direction > 0 else "lower_barrier"
    else:
        d_above = (
            math.log(spot / threshold)
            + (daily_drift - 0.5 * daily_vol * daily_vol) * horizon_days
        ) / horizon_sigma
        probability_above = normal_cdf(d_above)
        probability = probability_above if direction > 0 else 1.0 - probability_above
        event_type = "terminal_above" if direction > 0 else "terminal_below"

    probability = clip(probability, 0.003, 0.997)
    standardized_distance = abs(math.log(threshold / spot)) / horizon_sigma
    confidence = (
        0.48
        + 0.08 * min(1.0, standardized_distance)
        + 0.05 * (1.0 - min(1.0, horizon_days / max_days))
        - (0.05 if is_barrier else 0.0)
    )
    confidence = clip(
        confidence,
        finite(settings.get("min_confidence"), 0.40),
        finite(settings.get("max_confidence"), 0.70),
    )
    metadata = {
        "asset": asset,
        "model": "lognormal_terminal_or_reflection_barrier_v1",
        "event_type": event_type,
        "spot": spot,
        "threshold": threshold,
        "horizon_hours": horizon_hours,
        "daily_vol": daily_vol,
        "horizon_sigma": horizon_sigma,
        "daily_drift": daily_drift,
        "pm_mid": market.mid,
    }
    return probability, confidence, metadata


def request_json(url: str, *, timeout: float = 20.0, retries: int = 3) -> Any:
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "polymarket-external-intelligence/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < max(1, retries):
                time.sleep(min(4.0, 0.5 * (2**attempt)))
    raise RuntimeError(f"request failed: {url}: {last}")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def atomic_text(path: Path, payload: str) -> None:
    atomic_bytes(path, payload.encode("utf-8"))


def atomic_json(path: Path, payload: Any) -> None:
    atomic_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


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
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    output.append(value)
    except (OSError, EOFError):
        return []
    return output


def write_jsonl_gz(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    atomic_bytes(path, gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0))


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema") != "polymarket_external_intelligence_config_v1":
        errors.append("unexpected config schema")
    for key, expected in (
        ("paper_only", True),
        ("allow_authenticated_execution", False),
        ("allow_direct_champion_mutation", False),
        ("allow_production_signal_write", False),
    ):
        if config.get(key) is not expected:
            errors.append(f"{key} must be {str(expected).lower()}")
    sources = config.get("sources") or {}
    if not any(isinstance(value, dict) and value.get("enabled") for value in sources.values()):
        errors.append("at least one source must be enabled")
    horizons = [integer(value) for value in (config.get("backtest") or {}).get("horizons_seconds", [])]
    if not horizons or any(value <= 0 for value in horizons):
        errors.append("positive backtest horizons are required")
    stresses = {finite(value) for value in (config.get("backtest") or {}).get("cost_stress_multipliers", [])}
    if not {1.0, 1.5, 2.0}.issubset(stresses):
        errors.append("cost stress must include 1.0x, 1.5x and 2.0x")
    if errors:
        raise ValueError("; ".join(errors))


def parse_pm_market(raw: dict[str, Any]) -> PmMarket | None:
    market_id = str(raw.get("id") or raw.get("marketId") or "").strip()
    question = str(raw.get("question") or raw.get("title") or "").strip()
    if not market_id or not question:
        return None
    outcomes = [str(value).strip().lower() for value in parse_array(raw.get("outcomes"))]
    prices = [probability_price(value) for value in parse_array(raw.get("outcomePrices"))]
    yes_index = next((index for index, value in enumerate(outcomes) if value == "yes"), 0)
    mid = prices[yes_index] if yes_index < len(prices) else math.nan
    if not math.isfinite(mid):
        mid = probability_price(raw.get("lastTradePrice"), probability_price(raw.get("price")))
    bid = probability_price(raw.get("bestBid"))
    ask = probability_price(raw.get("bestAsk"))
    if not math.isfinite(mid) and math.isfinite(bid) and math.isfinite(ask):
        mid = 0.5 * (bid + ask)
    if not math.isfinite(mid):
        return None
    if not math.isfinite(bid):
        bid = mid - 0.01
    if not math.isfinite(ask):
        ask = mid + 0.01
    bid = clip(bid, 0.001, 0.999)
    ask = clip(ask, 0.001, 0.999)
    if ask < bid:
        bid, ask = ask, bid
    tokens_ = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
    resolved: int | None = None
    if parse_bool(raw.get("closed")) or parse_bool(raw.get("resolved")):
        if mid >= 0.999:
            resolved = 1
        elif mid <= 0.001:
            resolved = 0
    description = str(raw.get("description") or "")
    return PmMarket(
        market_id=market_id,
        condition_id=str(raw.get("conditionId") or ""),
        event_id=str(raw.get("eventId") or raw.get("event_id") or ""),
        question=question,
        description=description,
        category=classify_market(question + " " + description, raw),
        end_ts=parse_timestamp(raw.get("endDate") or raw.get("end_date") or raw.get("endDateIso")),
        liquidity=max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity")))),
        volume24h=max(0.0, finite(raw.get("volume24hr"), finite(raw.get("volume_24hr"), finite(raw.get("volume24h"))))),
        bid=bid,
        ask=ask,
        mid=clip(mid, 0.001, 0.999),
        yes_token=tokens_[0] if tokens_ else "",
        no_token=tokens_[1] if len(tokens_) > 1 else "",
        resolved_outcome=resolved,
    )


def parse_k_market(raw: dict[str, Any]) -> KMarket | None:
    ticker = str(raw.get("ticker") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not ticker or not title or str(raw.get("market_type") or "binary") != "binary":
        return None
    bid = probability_price(raw.get("yes_bid_dollars"), probability_price(raw.get("yes_bid")))
    ask = probability_price(raw.get("yes_ask_dollars"), probability_price(raw.get("yes_ask")))
    last = probability_price(raw.get("last_price_dollars"), probability_price(raw.get("last_price")))
    if not math.isfinite(bid):
        bid = last
    if not math.isfinite(ask):
        ask = last
    if not math.isfinite(bid) or not math.isfinite(ask):
        return None
    bid, ask = clip(bid, 0.001, 0.999), clip(ask, 0.001, 0.999)
    if ask < bid:
        bid, ask = ask, bid
    subtitle = " ".join(str(raw.get(key) or "") for key in ("subtitle", "yes_sub_title", "no_sub_title")).strip()
    rules = " ".join(str(raw.get(key) or "") for key in ("rules_primary", "rules_secondary")).strip()
    return KMarket(
        ticker=ticker,
        event_ticker=str(raw.get("event_ticker") or ""),
        title=title,
        subtitle=subtitle,
        rules=rules,
        close_ts=parse_timestamp(raw.get("close_time") or raw.get("expected_expiration_time") or raw.get("expiration_time")),
        updated_ts=parse_timestamp(raw.get("updated_time")),
        bid=bid,
        ask=ask,
        mid=0.5 * (bid + ask),
        spread=max(0.0, ask - bid),
        volume=max(0.0, finite(raw.get("volume_fp"), finite(raw.get("volume")))),
        liquidity=max(0.0, finite(raw.get("liquidity_dollars"), finite(raw.get("open_interest_fp")))),
    )


def fetch_pm_markets(config: dict[str, Any]) -> tuple[list[PmMarket], list[str]]:
    universe = config.get("universe") or {}
    page_size = max(1, min(100, integer(universe.get("page_size"), 100)))
    maximum = max(1, integer(universe.get("max_markets"), 400))
    min_liquidity = finite(universe.get("min_liquidity"), 100.0)
    min_volume = finite(universe.get("min_volume_24h"), 0.0)
    rows: list[PmMarket] = []
    errors: list[str] = []
    offset = 0
    while len(rows) < maximum and offset < maximum * 3:
        params = urllib.parse.urlencode({
            "active": "true", "closed": "false", "limit": page_size, "offset": offset,
            "order": str(universe.get("order_field") or "volume24hr"), "ascending": "false",
        })
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
        if len(batch) < page_size:
            break
        offset += page_size
    rows.sort(key=lambda item: (item.volume24h, item.liquidity), reverse=True)
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
            market = parse_k_market(raw) if isinstance(raw, dict) else None
            if market and market.spread <= max_spread:
                rows.append(market)
                if len(rows) >= maximum:
                    break
        cursor = str(payload.get("cursor") or "") if isinstance(payload, dict) else ""
        if not cursor:
            break
    rows.sort(key=lambda item: (item.volume, item.liquidity), reverse=True)
    return rows[:maximum], errors


def score_pair(pm: PmMarket, km: KMarket, max_expiry_days: float) -> tuple[float, bool, bool, float | None, str]:
    pm_text = normalize_text(pm.question + " " + pm.description)
    k_text = normalize_text(km.title + " " + km.subtitle + " " + km.rules)
    p_tokens, k_tokens = set(tokens(pm_text)), set(tokens(k_text))
    overlap = p_tokens.intersection(k_tokens)
    union = p_tokens.union(k_tokens)
    jaccard = len(overlap) / len(union) if union else 0.0
    containment = len(overlap) / max(1, min(len(p_tokens), len(k_tokens)))
    sequence = SequenceMatcher(None, normalize_text(pm.question), normalize_text(km.title)).ratio()
    p_numbers = threshold_numbers(pm.question)
    k_numbers = threshold_numbers(km.title + " " + km.subtitle)
    numeric_match = not p_numbers or not k_numbers or p_numbers == k_numbers
    p_orientation = orientation(pm.question)
    k_orientation = orientation(km.title + " " + km.subtitle)
    orientation_match = p_orientation == k_orientation or 0 in {p_orientation[0], k_orientation[0]}
    expiry_hours: float | None = None
    expiry_score = 0.5
    if pm.end_ts and km.close_ts:
        expiry_hours = abs(pm.end_ts - km.close_ts) / 3600.0
        expiry_score = math.exp(-expiry_hours / max(24.0, max_expiry_days * 12.0))
    score = 0.35 * jaccard + 0.25 * containment + 0.20 * sequence + 0.20 * expiry_score
    rejection = ""
    if not numeric_match:
        rejection = "critical_number_mismatch"
    elif not orientation_match:
        rejection = "orientation_mismatch"
    elif expiry_hours is not None and expiry_hours > max_expiry_days * 24.0:
        rejection = "expiry_mismatch"
    return score, numeric_match, orientation_match, expiry_hours, rejection


def match_kalshi(pm: PmMarket, candidates: Sequence[KMarket], config: dict[str, Any], now: int) -> Match | None:
    source = (config.get("sources") or {}).get("kalshi") or {}
    max_expiry = finite(source.get("max_expiry_difference_days"), 14.0)
    scored: list[tuple[float, bool, bool, float | None, str, KMarket]] = []
    for km in candidates:
        if not kalshi_candidate_compatible(pm, km):
            continue
        score, numeric, orient, expiry, rejection = score_pair(pm, km, max_expiry)
        scored.append((score, numeric, orient, expiry, rejection, km))
    if not scored:
        return None
    scored.sort(key=lambda row: row[0], reverse=True)
    score, numeric, orient, expiry, rejection, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    margin = score - second
    min_score = finite(source.get("min_match_score"), 0.68)
    min_margin = finite(source.get("min_match_margin"), 0.04)
    freshness_half_life = max(3600.0, finite(source.get("freshness_half_life_seconds"), 21600.0))
    quote_quality = math.exp(-best.spread / max(0.01, finite(source.get("spread_scale"), 0.05)))
    freshness = math.exp(-max(0, now - best.updated_ts) / freshness_half_life) if best.updated_ts else 0.75
    confidence = score * quote_quality * freshness * clip(margin / max(min_margin, 1e-6), 0.0, 1.0)
    if not rejection and score < min_score:
        rejection = "weak_match"
    if not rejection and margin < min_margin:
        rejection = "ambiguous_match"
    if not rejection and confidence < finite(source.get("min_confidence"), 0.35):
        rejection = "low_confidence"
    return Match(score, margin, confidence, rejection, numeric, orient, expiry, best)


def price_row(market: PmMarket, observed_ts: int, provenance: str = "gamma_live") -> dict[str, Any]:
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
    market: PmMarket,
    *,
    observed_ts: int,
    source: str,
    source_id: str,
    source_event_ts: int,
    feature_name: str,
    feature_value: float,
    confidence: float,
    mapping_score: float,
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
    row["observation_id"] = stable_hash({
        key: row[key] for key in ("observed_ts", "market_id", "source", "source_id", "feature_name")
    })
    return row


def collect_kalshi(
    pm_markets: Sequence[PmMarket], k_markets: Sequence[KMarket], config: dict[str, Any], now: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    index: dict[str, list[KMarket]] = {}
    for market in k_markets:
        for token in set(tokens(market.title + " " + market.subtitle)):
            if len(token) >= 3:
                index.setdefault(token, []).append(market)
    observations: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for pm in pm_markets:
        candidates: set[KMarket] = set()
        for token in set(tokens(pm.question)):
            candidates.update(index.get(token, []))
        if not candidates:
            continue
        match = match_kalshi(pm, list(candidates), config, now)
        if match is None:
            continue
        diagnostics.append({
            "market_id": pm.market_id,
            "question": pm.question,
            "kalshi_ticker": match.market.ticker,
            "kalshi_title": match.market.title,
            "score": match.score,
            "margin": match.margin,
            "confidence": match.confidence,
            "rejection": match.rejection,
            "numeric_match": match.numeric_match,
            "orientation_match": match.orientation_match,
            "expiry_hours": match.expiry_hours,
        })
        if match.rejection:
            continue
        observations.append(observation_row(
            pm,
            observed_ts=now,
            source="kalshi",
            source_id=match.market.ticker,
            source_event_ts=match.market.updated_ts or now,
            feature_name="external_probability",
            feature_value=match.market.mid - pm.mid,
            q_external=match.market.mid,
            confidence=match.confidence,
            mapping_score=match.score,
            metadata={
                "external_bid": match.market.bid,
                "external_ask": match.market.ask,
                "external_spread": match.market.spread,
                "event_ticker": match.market.event_ticker,
                "match_margin": match.margin,
            },
        ))
    diagnostics.sort(key=lambda row: finite(row.get("score")), reverse=True)
    return observations, diagnostics[:100]


def fetch_binance_klines(
    symbol: str, interval: str, limit: int, start_ms: int | None = None, end_ms: int | None = None
) -> list[list[Any]]:
    params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": max(1, min(1000, limit))}
    if start_ms is not None:
        params["startTime"] = start_ms
    if end_ms is not None:
        params["endTime"] = end_ms
    payload = request_json(f"{BINANCE}/api/v3/klines?{urllib.parse.urlencode(params)}")
    return payload if isinstance(payload, list) else []


def crypto_features(klines: Sequence[Sequence[Any]]) -> tuple[dict[str, float], int]:
    points = [(integer(row[6]) // 1000, finite(row[4], math.nan)) for row in klines if len(row) > 6]
    points = [(timestamp, close) for timestamp, close in points if timestamp and math.isfinite(close) and close > 0]
    if len(points) < 3:
        return {}, 0
    closes = [close for _, close in points]
    returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
    features = {
        "spot": closes[-1],
        "return_5m": returns[-1],
        "return_1h": sum(returns[-min(12, len(returns)):]),
        "return_24h": sum(returns[-min(288, len(returns)):]),
        "realized_vol_24h": statistics.pstdev(returns[-min(288, len(returns)):]) * math.sqrt(288) if len(returns) >= 2 else 0.0,
    }
    return features, points[-1][0]


def collect_binance(
    pm_markets: Sequence[PmMarket], config: dict[str, Any], now: int
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    source = (config.get("sources") or {}).get("binance") or {}
    if not source.get("enabled"):
        return [], {}, []
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
            features, source_ts = crypto_features(fetch_binance_klines(
                symbol, "5m", max(20, integer(source.get("kline_limit"), 289))
            ))
        except RuntimeError as exc:
            errors.append(str(exc))
            health[asset] = {"status": "error"}
            continue
        health[asset] = {"status": "ok", "source_event_ts": source_ts, "features": features}
        maximum = max(1, integer(source.get("max_markets_per_asset"), 20))
        ranked: list[tuple[bool, float, float, PmMarket, tuple[float, float, dict[str, Any]] | None]] = []
        for market in markets:
            estimate = crypto_threshold_probability(market, asset, features, now, config)
            ranked.append((
                estimate is not None,
                market.volume24h,
                market.liquidity,
                market,
                estimate,
            ))
        ranked.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        selected = ranked[:maximum]
        health[asset]["eligible_markets"] = len(markets)
        health[asset]["selected_markets"] = len(selected)
        health[asset]["direct_probability_markets"] = sum(row[0] for row in ranked)
        for _, _, _, market, estimate in selected:
            for name, value in sorted(features.items()):
                observations.append(observation_row(
                    market,
                    observed_ts=now,
                    source="binance",
                    source_id=symbol,
                    source_event_ts=source_ts or now,
                    feature_name=name,
                    feature_value=value,
                    confidence=finite(source.get("base_confidence"), 0.60),
                    mapping_score=1.0,
                    metadata={"asset": asset, "symbol": symbol},
                ))
            if estimate is not None:
                q_external, confidence, metadata = estimate
                observations.append(observation_row(
                    market,
                    observed_ts=now,
                    source="binance",
                    source_id=symbol,
                    source_event_ts=source_ts or now,
                    feature_name="external_probability",
                    feature_value=q_external - market.mid,
                    q_external=q_external,
                    confidence=confidence,
                    mapping_score=1.0,
                    metadata={**metadata, "symbol": symbol},
                ))
    return observations, health, errors


def gdelt_query(question: str, maximum_terms: int = 7) -> str:
    useful = [token for token in tokens(question) if not re.fullmatch(r"\d+", token)]
    return " ".join(sorted(dict.fromkeys(useful), key=lambda token: (-len(token), token))[:maximum_terms])


def collect_gdelt(
    pm_markets: Sequence[PmMarket], config: dict[str, Any], now: int
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    source = (config.get("sources") or {}).get("gdelt") or {}
    if not source.get("enabled"):
        return [], {}, []
    count = max(0, integer(source.get("markets_per_run"), 8))
    if not pm_markets or count == 0:
        return [], {}, []
    offset = ((now // 1800) * count) % len(pm_markets)
    selected = list(pm_markets[offset:offset + count])
    if len(selected) < count:
        selected.extend(pm_markets[:count - len(selected)])
    observations: list[dict[str, Any]] = []
    health: dict[str, Any] = {}
    errors: list[str] = []
    for market in selected:
        query = gdelt_query(market.question)
        if not query:
            continue
        params = urllib.parse.urlencode({
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": max(1, min(75, integer(source.get("max_records"), 50))),
            "timespan": str(source.get("timespan") or "1d"),
            "sort": "datedesc",
        })
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
        latest = max((parse_timestamp(article.get("seendate")) for article in articles if isinstance(article, dict)), default=now)
        health[market.market_id] = {"status": "ok", "query": query, "articles": len(articles), "latest_ts": latest}
        for name, value in (
            ("news_count_24h", math.log1p(len(articles))),
            ("news_tone", statistics.fmean(tones) if tones else 0.0),
        ):
            observations.append(observation_row(
                market,
                observed_ts=now,
                source="gdelt",
                source_id=query,
                source_event_ts=latest,
                feature_name=name,
                feature_value=value,
                confidence=finite(source.get("base_confidence"), 0.35),
                mapping_score=0.55,
                metadata={"query": query, "article_count": len(articles)},
            ))
    return observations, health, errors


def merge_rows(
    existing: Sequence[dict[str, Any]], incoming: Sequence[dict[str, Any]], *,
    key_fields: Sequence[str], min_timestamp: int, max_rows: int,
) -> list[dict[str, Any]]:
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in list(existing) + list(incoming):
        if integer(row.get("observed_ts")) < min_timestamp:
            continue
        key = tuple(row.get(field) for field in key_fields)
        previous = by_key.get(key)
        if previous is None or integer(previous.get("retrieved_ts")) <= integer(row.get("retrieved_ts")):
            by_key[key] = dict(row)
    rows = sorted(by_key.values(), key=lambda row: (
        integer(row.get("observed_ts")), str(row.get("market_id")), str(row.get("source", "")), str(row.get("feature_name", ""))
    ))
    return rows[-max_rows:] if max_rows > 0 and len(rows) > max_rows else rows


def synthetic_quote(mid: float, half_spread: float) -> tuple[float, float]:
    return clip(mid - half_spread, 0.001, 0.999), clip(mid + half_spread, 0.001, 0.999)


def history_interval_for_range(start_ts: int, end_ts: int) -> str:
    seconds = max(0, end_ts - start_ts)
    if seconds <= 3600:
        return "1h"
    if seconds <= 6 * 3600:
        return "6h"
    if seconds <= 86400:
        return "1d"
    if seconds <= 7 * 86400:
        return "1w"
    if seconds <= 31 * 86400:
        return "1m"
    return "max"


def fetch_pm_history(token_id: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
    if not token_id or end_ts <= start_ts:
        return []
    params = urllib.parse.urlencode({
        "market": token_id,
        "interval": history_interval_for_range(start_ts, end_ts),
        "fidelity": 60,
    })
    payload = request_json(f"{PM_CLOB}/prices-history?{params}")
    history = payload.get("history") if isinstance(payload, dict) else []
    if not isinstance(history, list):
        return []
    return [
        row for row in history
        if isinstance(row, dict) and start_ts <= parse_timestamp(row.get("t")) <= end_ts
    ]


def backfill_crypto_market(
    market: PmMarket, config: dict[str, Any], now: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    settings = config.get("backfill") or {}
    days = max(1, integer(settings.get("lookback_days"), 30))
    start_ts = now - days * 86400
    asset = detect_asset(market.question + " " + market.description)
    if not asset or not market.yes_token:
        return [], [], []
    try:
        pm_history = fetch_pm_history(market.yes_token, start_ts, now)
        klines = fetch_binance_klines(f"{asset}USDT", "1h", min(1000, days * 24 + 2), start_ts * 1000, now * 1000)
    except RuntimeError as exc:
        return [], [], [str(exc)]
    crypto_points = sorted((integer(row[6]) // 1000, finite(row[4], math.nan)) for row in klines if len(row) > 6)
    crypto_points = [(ts, close) for ts, close in crypto_points if ts and math.isfinite(close) and close > 0]
    if len(crypto_points) < 25:
        return [], [], []
    crypto_times = [row[0] for row in crypto_points]
    crypto_closes = [row[1] for row in crypto_points]
    half_spread = finite(settings.get("historical_half_spread"), 0.015)
    prices: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for row in pm_history:
        timestamp = parse_timestamp(row.get("t"))
        mid = finite(row.get("p"), math.nan)
        if not timestamp or not math.isfinite(mid) or not 0.0 < mid < 1.0:
            continue
        bid, ask = synthetic_quote(mid, half_spread)
        synthetic_market = PmMarket(**{**asdict(market), "bid": bid, "ask": ask, "mid": mid})
        prices.append(price_row(synthetic_market, timestamp, "clob_history_synthetic_spread"))
        index = bisect.bisect_right(crypto_times, timestamp) - 1
        if index < 24:
            continue
        returns = [math.log(crypto_closes[j] / crypto_closes[j - 1]) for j in range(index - 23, index + 1)]
        features = {
            "return_1h": math.log(crypto_closes[index] / crypto_closes[index - 1]),
            "return_24h": math.log(crypto_closes[index] / crypto_closes[index - 24]),
            "realized_vol_24h": statistics.pstdev(returns) * math.sqrt(24),
        }
        for name, value in features.items():
            observations.append(observation_row(
                synthetic_market,
                observed_ts=timestamp,
                source="binance",
                source_id=f"{asset}USDT",
                source_event_ts=crypto_times[index],
                feature_name=name,
                feature_value=value,
                confidence=0.55,
                mapping_score=1.0,
                metadata={"asset": asset, "backfill": True},
            ))
    return prices, observations, []


def bounded_backfill(
    markets: Sequence[PmMarket], config: dict[str, Any], state: dict[str, Any], now: int, mode: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[str]]:
    settings = config.get("backfill") or {}
    if not settings.get("enabled"):
        return [], [], state, []
    completed = set(str(value) for value in state.get("backfilled_market_ids") or [])
    maximum = integer(settings.get("manual_max_markets_per_run"), 20) if mode == "backfill" else integer(settings.get("max_markets_per_run"), 2)
    selected = [market for market in markets if market.category == "crypto" and market.market_id not in completed][:max(0, maximum)]
    prices: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    errors: list[str] = []
    for market in selected:
        p_rows, o_rows, failures = backfill_crypto_market(market, config, now)
        prices.extend(p_rows)
        observations.extend(o_rows)
        errors.extend(failures)
        if p_rows:
            completed.add(market.market_id)
    updated = dict(state)
    updated.update({
        "schema": STATE_SCHEMA,
        "updated_ts": now,
        "backfilled_market_ids": sorted(completed)[-5000:],
        "last_backfill_market_ids": [market.market_id for market in selected],
    })
    return prices, observations, updated, errors


def price_index(prices: Sequence[dict[str, Any]]) -> dict[str, tuple[list[int], list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in prices:
        market_id = str(row.get("market_id") or "")
        if market_id and integer(row.get("observed_ts")):
            grouped.setdefault(market_id, []).append(row)
    output: dict[str, tuple[list[int], list[dict[str, Any]]]] = {}
    for market_id, rows in grouped.items():
        rows.sort(key=lambda row: integer(row.get("observed_ts")))
        output[market_id] = ([integer(row.get("observed_ts")) for row in rows], rows)
    return output


def label_observations(
    observations: Sequence[dict[str, Any]], prices: Sequence[dict[str, Any]], horizon: int, tolerance: int
) -> list[dict[str, Any]]:
    index = price_index(prices)
    labeled: list[dict[str, Any]] = []
    for observation in observations:
        series = index.get(str(observation.get("market_id") or ""))
        if not series:
            continue
        t0 = integer(observation.get("observed_ts"))
        target = t0 + horizon
        times, rows = series
        position = bisect.bisect_left(times, target)
        if position >= len(rows) or times[position] > target + tolerance:
            continue
        future = rows[position]
        current_mid = finite(observation.get("pm_mid"), math.nan)
        future_mid = finite(future.get("mid"), math.nan)
        if not math.isfinite(current_mid) or not math.isfinite(future_mid):
            continue
        row = dict(observation)
        row.update({
            "horizon_seconds": horizon,
            "future_ts": times[position],
            "future_mid": future_mid,
            "future_bid": finite(future.get("bid"), future_mid),
            "future_ask": finite(future.get("ask"), future_mid),
            "target_delta": future_mid - current_mid,
        })
        labeled.append(row)
    labeled.sort(key=lambda row: (integer(row.get("observed_ts")), str(row.get("market_id"))))
    return labeled


def purged_training_rows(rows: Sequence[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    decision_ts = integer(rows[index].get("observed_ts"))
    return [row for row in rows[:index] if integer(row.get("future_ts")) < decision_ts]


def fit_signal(rows: Sequence[dict[str, Any]], direct: bool, ridge: float) -> tuple[float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for row in rows:
        x = (
            finite(row.get("q_external"), finite(row.get("pm_mid"))) - finite(row.get("pm_mid"))
            if direct else finite(row.get("feature_value"), math.nan)
        )
        y = finite(row.get("target_delta"), math.nan)
        if math.isfinite(x) and math.isfinite(y):
            xs.append(x)
            ys.append(y)
    if len(xs) < 2:
        return 0.0, 0.0, 0.0
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    variance = statistics.fmean((value - mean_x) ** 2 for value in xs)
    covariance = statistics.fmean((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = covariance / (variance + ridge)
    if direct:
        return 0.0, clip(slope, 0.0, 1.5), 0.0
    return mean_y, clip(slope, -10.0, 10.0), mean_x


def trade_pnl(row: dict[str, Any], predicted_delta: float, extra_cost: float) -> tuple[float, int]:
    bid = finite(row.get("pm_bid"), finite(row.get("pm_mid")))
    ask = finite(row.get("pm_ask"), finite(row.get("pm_mid")))
    future_bid = finite(row.get("future_bid"), finite(row.get("future_mid")))
    future_ask = finite(row.get("future_ask"), finite(row.get("future_mid")))
    threshold = 0.5 * max(0.0, ask - bid) + extra_cost
    if predicted_delta > threshold:
        return future_bid - ask - extra_cost, 1
    if predicted_delta < -threshold:
        return bid - future_ask - extra_cost, -1
    return 0.0, 0


def max_drawdown(values: Sequence[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def bootstrap_pvalue(values: Sequence[float], block: int, reps: int, seed: int) -> float:
    values = [finite(value) for value in values if math.isfinite(finite(value, math.nan))]
    if len(values) < 3 or statistics.fmean(values) <= 0.0:
        return 1.0
    observed = statistics.fmean(values)
    centered = [value - observed for value in values]
    width = max(1, min(block, len(values)))
    rng = random.Random(seed)
    exceed = 0
    for _ in range(max(100, reps)):
        sample: list[float] = []
        while len(sample) < len(values):
            start = rng.randrange(len(values))
            sample.extend(centered[(start + offset) % len(values)] for offset in range(width))
        if statistics.fmean(sample[:len(values)]) >= observed:
            exceed += 1
    return (exceed + 1) / (max(100, reps) + 1)


def evaluate_candidate(
    rows: Sequence[dict[str, Any]], config: dict[str, Any], source: str, feature: str, horizon: int
) -> dict[str, Any]:
    settings = config.get("backtest") or {}
    min_train = max(2, integer(settings.get("min_train_observations"), 30))
    ridge = max(1e-12, finite(settings.get("ridge"), 1e-5))
    direct = feature == "external_probability"
    multipliers = [finite(value) for value in settings.get("cost_stress_multipliers", [1.0, 1.5, 2.0])]
    base_cost = max(0.0, finite(settings.get("extra_cost_bps"), 20.0) / 10000.0)
    pnls = {str(value): [] for value in multipliers}
    predictions: list[float] = []
    targets: list[float] = []
    normal_sides: list[int] = []
    for index, row in enumerate(rows):
        train = purged_training_rows(rows, index)
        if len(train) < min_train:
            continue
        intercept, slope, center = fit_signal(train, direct, ridge)
        x = (
            finite(row.get("q_external"), finite(row.get("pm_mid"))) - finite(row.get("pm_mid"))
            if direct else finite(row.get("feature_value"))
        )
        predicted = intercept + slope * (x - center)
        predictions.append(predicted)
        targets.append(finite(row.get("target_delta")))
        side_normal = 0
        for multiplier in multipliers:
            pnl, side = trade_pnl(row, predicted, base_cost * multiplier)
            pnls[str(multiplier)].append(pnl)
            if multiplier == 1.0:
                side_normal = side
        normal_sides.append(side_normal)
    normal = pnls.get("1.0", [])
    traded = [pnl for pnl, side in zip(normal, normal_sides) if side != 0]
    gains = [pnl for pnl in traded if pnl > 0]
    losses = [pnl for pnl in traded if pnl < 0]
    profit_factor = sum(gains) / abs(sum(losses)) if losses else (math.inf if gains else 0.0)
    mse = statistics.fmean((p - y) ** 2 for p, y in zip(predictions, targets)) if predictions else 0.0
    baseline_mse = statistics.fmean(y ** 2 for y in targets) if targets else 0.0
    folds = max(2, integer(settings.get("folds"), 4))
    fold_sums: list[float] = []
    for fold in range(folds):
        lo = math.floor(len(normal) * fold / folds)
        hi = math.floor(len(normal) * (fold + 1) / folds)
        if hi > lo:
            fold_sums.append(sum(normal[lo:hi]))
    positive_fold_fraction = sum(value > 0 for value in fold_sums) / len(fold_sums) if fold_sums else 0.0
    pvalue = bootstrap_pvalue(
        traded,
        max(1, integer(settings.get("bootstrap_block"), 5)),
        max(100, integer(settings.get("bootstrap_reps"), 1000)),
        20260824 + sum(ord(char) for char in source + feature) + horizon,
    )
    metrics = {
        "labeled_observations": len(rows),
        "oos_predictions": len(predictions),
        "trades": len(traded),
        "net_pnl_per_share": sum(normal),
        "mean_pnl_per_trade": statistics.fmean(traded) if traded else 0.0,
        "hit_rate": sum(value > 0 for value in traded) / len(traded) if traded else 0.0,
        "profit_factor": profit_factor,
        "max_drawdown_per_share": max_drawdown(normal),
        "prediction_mse": mse,
        "baseline_zero_delta_mse": baseline_mse,
        "mse_improvement": baseline_mse - mse,
        "active_folds": len(fold_sums),
        "positive_fold_fraction": positive_fold_fraction,
        "cost_stress_net_pnl": {key: sum(values) for key, values in pnls.items()},
    }
    gates = config.get("gates") or {}
    reasons: list[str] = []
    if len(predictions) < integer(gates.get("min_oos_predictions"), 40):
        reasons.append("insufficient_oos_predictions")
    if len(traded) < integer(gates.get("min_trades"), 20):
        reasons.append("insufficient_trades")
    if sum(normal) <= 0:
        reasons.append("nonpositive_net_pnl")
    for multiplier in (1.5, 2.0):
        if sum(pnls.get(str(multiplier), [])) <= 0:
            reasons.append(f"nonpositive_{multiplier:g}x_cost_stress")
    if pvalue > finite(gates.get("max_bootstrap_pvalue"), 0.10):
        reasons.append("bootstrap_gate")
    if positive_fold_fraction < finite(gates.get("min_positive_fold_fraction"), 0.50):
        reasons.append("fold_stability_gate")
    if metrics["mse_improvement"] <= 0:
        reasons.append("no_predictive_mse_improvement")
    return {
        "candidate_id": f"external:{source}:{feature}:{horizon}s",
        "source": source,
        "feature_name": feature,
        "horizon_seconds": horizon,
        "observations": len(predictions),
        "raw_pvalue": pvalue,
        "metrics": metrics,
        "gate_pass": not reasons,
        "reasons": reasons,
        "evidence_type": "purged_chronological_external_information_backtest",
        "executable_proxy": True,
        "requires_exact_clob_replay_before_integration": True,
    }


def run_backtests(
    observations: Sequence[dict[str, Any]], prices: Sequence[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in observations:
        source = str(row.get("source") or "")
        feature = str(row.get("feature_name") or "")
        if source and feature:
            groups.setdefault((source, feature), []).append(row)
    settings = config.get("backtest") or {}
    candidates: list[dict[str, Any]] = []
    for horizon in [integer(value) for value in settings.get("horizons_seconds", [3600, 21600, 86400])]:
        tolerance = max(60, integer(settings.get("future_price_tolerance_seconds"), horizon // 2))
        for (source, feature), rows in sorted(groups.items()):
            labeled = label_observations(rows, prices, horizon, tolerance)
            if labeled:
                candidates.append(evaluate_candidate(labeled, config, source, feature, horizon))
    candidates.sort(key=lambda row: (
        0 if row.get("gate_pass") else 1,
        finite(row.get("raw_pvalue"), 1.0),
        -finite((row.get("metrics") or {}).get("cost_stress_net_pnl", {}).get("2.0")),
        str(row.get("candidate_id")),
    ))
    return candidates


def reliability(candidates: Sequence[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    prior = config.get("reliability") or {}
    prior_score = finite(prior.get("prior_score"), 0.50)
    prior_weight = max(1.0, finite(prior.get("prior_weight"), 20.0))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(str(candidate.get("source") or "unknown"), []).append(candidate)
    output: dict[str, Any] = {}
    for source, rows in sorted(grouped.items()):
        observations = sum(integer(row.get("observations")) for row in rows)
        passes = sum(bool(row.get("gate_pass")) for row in rows)
        pnl2 = sum(finite((row.get("metrics") or {}).get("cost_stress_net_pnl", {}).get("2.0")) for row in rows)
        evidence_score = clip(0.5 + 0.25 * math.tanh(pnl2) + 0.25 * passes / max(1, len(rows)), 0.0, 1.0)
        weight = min(500.0, float(observations))
        output[source] = {
            "score": (prior_weight * prior_score + weight * evidence_score) / (prior_weight + weight),
            "observations": observations,
            "candidate_count": len(rows),
            "passing_candidates": passes,
            "two_x_cost_stressed_pnl_per_share": pnl2,
        }
    return output


def alpha_evidence(candidates: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    best = candidates[0]
    metrics = best.get("metrics") or {}
    reasons = list(best.get("reasons") or []) + ["exact_clob_replay_required_before_integration"]
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
        "# Polymarket External Intelligence", "",
        f"- generated: `{report['generated_utc']}`",
        f"- status: **{report['status']}**",
        "- boundary: **public/free data, read-only, paper research only**",
        f"- active Polymarket markets: {report['collection']['polymarket_markets']}",
        f"- new observations: {report['collection']['new_observations']}",
        f"- stored observations: {report['storage']['observation_rows']}",
        f"- stored price snapshots: {report['storage']['price_rows']}", "",
        "## Backtest candidates", "",
    ]
    candidates = report.get("backtest", {}).get("candidates") or []
    if not candidates:
        lines.append("- No candidate has enough chronologically labeled data yet.")
    for candidate in candidates[:10]:
        metrics = candidate.get("metrics") or {}
        lines.extend([
            f"### `{candidate['candidate_id']}`",
            f"- gate pass: `{str(bool(candidate.get('gate_pass'))).lower()}`",
            f"- OOS predictions: {metrics.get('oos_predictions', 0)}",
            f"- trades: {metrics.get('trades', 0)}",
            f"- normal PnL/share: {finite(metrics.get('net_pnl_per_share')):.6g}",
            f"- 2x-cost PnL/share: {finite((metrics.get('cost_stress_net_pnl') or {}).get('2.0')):.6g}",
            f"- bootstrap p-value: {finite(candidate.get('raw_pvalue'), 1.0):.6g}",
            "- reasons: " + (", ".join(candidate.get("reasons") or []) or "none"), "",
        ])
    lines.extend([
        "## Interpretation", "",
        "- Historical CLOB price history lacks full historical books; backfilled tests use a conservative synthetic spread and remain a proxy.",
        "- Ambiguous market mappings abstain.",
        "- A passing result is research evidence only; exact executable replay and incumbent ablation are still required.",
        "- The worker cannot mutate `config/live_champion.json`, deploy, or submit authenticated orders.",
    ])
    return "\n".join(lines) + "\n"


def demo_markets(now: int) -> tuple[list[PmMarket], list[KMarket]]:
    pm: list[PmMarket] = []
    kalshi: list[KMarket] = []
    for index in range(6):
        strike = 60000 + 1000 * index
        price = 0.35 + 0.04 * index
        pm.append(PmMarket(
            market_id=f"pm-{index}", condition_id=f"condition-{index}", event_id=f"event-{index}",
            question=f"Will Bitcoin be above ${strike:,} by December 31 2026?",
            description="Bitcoin price market", category="crypto", end_ts=now + 30 * 86400,
            liquidity=10000.0, volume24h=5000.0, bid=price - 0.01, ask=price + 0.01, mid=price,
            yes_token=f"yes-{index}", no_token=f"no-{index}", resolved_outcome=None,
        ))
        kalshi.append(KMarket(
            ticker=f"KXBTC-{index}", event_ticker="KXBTC",
            title=f"Bitcoin above ${strike:,} on December 31 2026", subtitle="", rules="",
            close_ts=now + 30 * 86400, updated_ts=now,
            bid=price + 0.01, ask=price + 0.03, mid=price + 0.02, spread=0.02,
            volume=1000.0, liquidity=1000.0,
        ))
    return pm, kalshi


def main() -> int:
    parser = argparse.ArgumentParser()
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
    parser.add_argument("--now", type=int)
    args = parser.parse_args()

    config = read_json(args.config, {})
    validate_config(config)
    now = args.now if args.now is not None else int(time.time())
    state = read_json(args.state_in, {})
    state = state if isinstance(state, dict) else {}
    old_observations = read_jsonl_gz(args.observations_in)
    old_prices = read_jsonl_gz(args.prices_in)
    source_errors: list[str] = []

    if args.mode == "demo":
        pm_markets, k_markets = demo_markets(now)
        pm_errors: list[str] = []
        k_errors: list[str] = []
    else:
        pm_markets, pm_errors = fetch_pm_markets(config)
        k_markets, k_errors = fetch_kalshi_markets(config)
        source_errors.extend(pm_errors + k_errors)

    live_prices = [price_row(market, now) for market in pm_markets]
    kalshi_observations, mapping_diagnostics = collect_kalshi(pm_markets, k_markets, config, now)

    if args.mode == "demo":
        binance_observations, binance_health, binance_errors = [], {"demo": "skipped"}, []
        gdelt_observations, gdelt_health, gdelt_errors = [], {"demo": "skipped"}, []
    else:
        binance_observations, binance_health, binance_errors = collect_binance(pm_markets, config, now)
        gdelt_observations, gdelt_health, gdelt_errors = collect_gdelt(pm_markets, config, now)
        source_errors.extend(binance_errors + gdelt_errors)

    backfill_prices: list[dict[str, Any]] = []
    backfill_observations: list[dict[str, Any]] = []
    if args.mode != "demo":
        backfill_prices, backfill_observations, state, backfill_errors = bounded_backfill(
            pm_markets, config, state, now, args.mode
        )
        source_errors.extend(backfill_errors)

    new_observations = kalshi_observations + binance_observations + gdelt_observations + backfill_observations
    new_prices = live_prices + backfill_prices
    storage = config.get("storage") or {}
    min_timestamp = now - max(1, integer(storage.get("retention_days"), 180)) * 86400
    observations = merge_rows(
        old_observations, new_observations, key_fields=("observation_id",), min_timestamp=min_timestamp,
        max_rows=max(1, integer(storage.get("max_observation_rows"), 250000)),
    )
    prices = merge_rows(
        old_prices, new_prices, key_fields=("market_id", "observed_ts"), min_timestamp=min_timestamp,
        max_rows=max(1, integer(storage.get("max_price_rows"), 250000)),
    )
    candidates = run_backtests(observations, prices, config)
    source_reliability = reliability(candidates, config)
    evidence = alpha_evidence(candidates)
    min_confidence = finite((config.get("gates") or {}).get("min_signal_confidence"), 0.35)
    accepted = [row for row in new_observations if finite(row.get("confidence")) >= min_confidence]
    state.update({
        "schema": STATE_SCHEMA,
        "updated_ts": now,
        "last_run_mode": args.mode,
        "source_reliability": source_reliability,
        "last_observation_count": len(new_observations),
        "last_price_count": len(new_prices),
        "paper_only": True,
        "authenticated_execution": False,
        "direct_champion_mutation": False,
    })
    passing = [candidate for candidate in candidates if candidate.get("gate_pass")]
    if source_errors and not new_observations:
        status = "DEGRADED_SOURCE_FAILURE"
    elif passing:
        status = "VALIDATED_CHALLENGER_EVIDENCE"
    elif candidates:
        status = "BACKTESTING"
    else:
        status = "COLLECTING_HISTORY"
    source_health = {
        "polymarket": {"status": "ok" if pm_markets else "degraded", "markets": len(pm_markets), "errors": pm_errors},
        "kalshi": {"status": "ok" if k_markets else "degraded", "markets": len(k_markets), "accepted_matches": len(kalshi_observations), "errors": k_errors},
        "binance": {"status": "ok" if not binance_errors else "degraded", "details": binance_health, "errors": binance_errors},
        "gdelt": {"status": "ok" if not gdelt_errors else "degraded", "details": gdelt_health, "errors": gdelt_errors},
    }
    report = {
        "schema": REPORT_SCHEMA,
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
            "kalshi_matches": len(kalshi_observations),
            "binance_rows": len(binance_observations) + len(backfill_observations),
            "direct_probability_rows": sum(1 for row in new_observations if row.get("q_external") is not None),
            "gdelt_rows": len(gdelt_observations),
            "source_errors": source_errors,
        },
        "storage": {
            "observation_rows": len(observations),
            "price_rows": len(prices),
            "retention_days": integer(storage.get("retention_days"), 180),
        },
        "source_health": source_health,
        "mapping_diagnostics": mapping_diagnostics,
        "source_reliability": source_reliability,
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
        f" status={status} markets={len(pm_markets)} observations={len(new_observations)}"
        f" stored={len(observations)} candidates={len(candidates)} passing={len(passing)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
