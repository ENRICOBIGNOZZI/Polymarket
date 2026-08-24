#!/usr/bin/env python3
"""Read-only Kalshi -> Polymarket external-information shadow feed.

The matcher is deliberately conservative. It produces an engine-compatible
shadow signal only when contract text, critical numeric thresholds, logical
orientation, expiry and quote quality all agree. It never submits orders and
never overwrites the production external-signals file.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

PM_GAMMA = "https://gamma-api.polymarket.com"
PM_CLOB = "https://clob.polymarket.com"
KALSHI = "https://external-api.kalshi.com/trade-api/v2"

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "did", "do", "does",
    "for", "from", "has", "have", "if", "in", "is", "it", "its", "of", "on", "or", "the",
    "their", "there", "this", "to", "was", "were", "what", "when", "where", "which", "who",
    "will", "with", "would", "market", "event", "contract", "occur", "happen",
}
NEGATIONS = {"no", "not", "never", "without", "fail", "fails", "failed", "neither"}
UP_TERMS = {"above", "over", "higher", "greater", "exceed", "exceeds", "exceeding", "more", "increase", "rise"}
DOWN_TERMS = {"below", "under", "lower", "less", "decrease", "fall", "drop"}
BEFORE_TERMS = {"before", "by", "until"}
AFTER_TERMS = {"after", "following"}


@dataclass(frozen=True)
class PmMarket:
    market_id: str
    condition_id: str
    slug: str
    question: str
    description: str
    end_ts: int
    liquidity: float
    volume24h: float
    yes_token: str
    no_token: str


@dataclass(frozen=True)
class KMarket:
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    rules: str
    settlement_ts: int
    yes_bid: float
    yes_ask: float
    volume: float
    open_interest: float

    @property
    def midpoint(self) -> float:
        return 0.5 * (self.yes_bid + self.yes_ask)

    @property
    def spread(self) -> float:
        return self.yes_ask - self.yes_bid


@dataclass(frozen=True)
class PmQuote:
    midpoint: float
    spread: float
    best_bid: float
    best_ask: float


@dataclass(frozen=True)
class Similarity:
    score: float
    title_score: float
    jaccard: float
    containment: float
    sequence: float
    context: float
    date_score: float
    date_diff_hours: float | None
    numbers_match: bool
    orientation_match: bool
    rejection: str


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no"}:
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
    except ValueError:
        return 0


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: Any | None = None,
    timeout: float = 20.0,
    retries: int = 3,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "User-Agent": "polymarket-kalshi-cross-venue-shadow/1.0",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < max(1, retries):
                time.sleep(min(4.0, 0.5 * 2**attempt))
    raise RuntimeError(f"request failed after {retries} attempts: {url}: {last}")


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def atomic_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def normalized_text(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.lower().replace("'", " ")
    ascii_text = re.sub(r"[^a-z0-9.%$]+", " ", ascii_text)
    return " ".join(ascii_text.split())


def token_set(text: str) -> set[str]:
    tokens = []
    for token in normalized_text(text).split():
        if token in STOPWORDS or len(token) < 2:
            continue
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        tokens.append(token)
    return set(tokens)


def numeric_signature(text: str) -> tuple[str, ...]:
    values = []
    for raw in re.findall(r"(?<![a-z])\$?\d+(?:\.\d+)?%?", normalized_text(text)):
        value = raw.replace("$", "")
        suffix = "%" if value.endswith("%") else ""
        value = value.rstrip("%")
        try:
            number = float(value)
        except ValueError:
            continue
        if abs(number - round(number)) < 1e-10:
            canonical = str(int(round(number)))
        else:
            canonical = f"{number:.8g}"
        values.append(canonical + suffix)
    return tuple(sorted(set(values)))


def orientation_signature(text: str) -> tuple[bool, int, int]:
    tokens = token_set(text)
    negated = bool(tokens.intersection(NEGATIONS))
    vertical = 1 if tokens.intersection(UP_TERMS) else -1 if tokens.intersection(DOWN_TERMS) else 0
    temporal = -1 if tokens.intersection(BEFORE_TERMS) else 1 if tokens.intersection(AFTER_TERMS) else 0
    return negated, vertical, temporal


def orientation_compatible(left: str, right: str) -> bool:
    lneg, lvertical, ltemporal = orientation_signature(left)
    rneg, rvertical, rtemporal = orientation_signature(right)
    if lneg != rneg:
        return False
    if lvertical and rvertical and lvertical != rvertical:
        return False
    if ltemporal and rtemporal and ltemporal != rtemporal:
        return False
    return True


def jaccard(left: set[str], right: set[str]) -> float:
    union = left.union(right)
    return len(left.intersection(right)) / len(union) if union else 0.0


def containment(left: set[str], right: set[str]) -> float:
    denominator = min(len(left), len(right))
    return len(left.intersection(right)) / denominator if denominator else 0.0


def contract_similarity(pm: PmMarket, kalshi: KMarket) -> Similarity:
    pm_title = pm.question
    k_title = " ".join(x for x in (kalshi.title, kalshi.subtitle) if x)
    pm_tokens = token_set(pm_title)
    k_tokens = token_set(k_title)
    jac = jaccard(pm_tokens, k_tokens)
    contain = containment(pm_tokens, k_tokens)
    sequence = SequenceMatcher(None, normalized_text(pm_title), normalized_text(k_title)).ratio()
    title_score = 0.45 * jac + 0.30 * contain + 0.25 * sequence

    context = 0.0
    if pm.description and kalshi.rules:
        context = containment(token_set(pm.description), token_set(kalshi.rules))

    date_diff_hours: float | None = None
    date_score = 0.5
    if pm.end_ts > 0 and kalshi.settlement_ts > 0:
        date_diff_hours = abs(pm.end_ts - kalshi.settlement_ts) / 3600.0
        date_score = math.exp(-date_diff_hours / (24.0 * 3.0))

    numbers_match = numeric_signature(pm_title) == numeric_signature(k_title)
    logical_match = orientation_compatible(pm_title, k_title)
    score = 0.82 * title_score + 0.08 * context + 0.10 * date_score

    rejection = ""
    if len(pm_tokens.intersection(k_tokens)) < 2:
        rejection = "too_few_shared_tokens"
    elif not numbers_match:
        rejection = "critical_numbers_mismatch"
    elif not logical_match:
        rejection = "logical_orientation_mismatch"
    elif date_diff_hours is not None and date_diff_hours > 24.0 * 10.0:
        rejection = "settlement_date_mismatch"
    elif jac < 0.40 or contain < 0.62 or title_score < 0.68:
        rejection = "weak_text_match"

    return Similarity(
        score=score,
        title_score=title_score,
        jaccard=jac,
        containment=contain,
        sequence=sequence,
        context=context,
        date_score=date_score,
        date_diff_hours=date_diff_hours,
        numbers_match=numbers_match,
        orientation_match=logical_match,
        rejection=rejection,
    )


def parse_pm_market(item: dict[str, Any]) -> PmMarket | None:
    if not as_bool(item.get("active"), True) or as_bool(item.get("closed"), False):
        return None
    if not as_bool(item.get("enableOrderBook"), True) or not as_bool(item.get("acceptingOrders"), True):
        return None
    market_id = str(item.get("id") or "")
    condition = str(item.get("conditionId") or "")
    question = str(item.get("question") or "")
    tokens = [str(value) for value in parse_array(item.get("clobTokenIds"))]
    outcomes = [str(value).strip().lower() for value in parse_array(item.get("outcomes"))]
    if len(tokens) < 2 or len(outcomes) < 2:
        return None
    yes_index = next((i for i, value in enumerate(outcomes) if value == "yes"), 0)
    no_index = next((i for i, value in enumerate(outcomes) if value == "no"), 1)
    if yes_index >= len(tokens) or no_index >= len(tokens):
        return None
    if not market_id or not condition or not question or tokens[yes_index] == tokens[no_index]:
        return None
    end_ts = 0
    for key in ("endDate", "endDateIso", "closedTime", "gameStartTime"):
        end_ts = parse_timestamp(item.get(key))
        if end_ts:
            break
    return PmMarket(
        market_id=market_id,
        condition_id=condition,
        slug=str(item.get("slug") or ""),
        question=question,
        description=str(item.get("description") or item.get("rules") or ""),
        end_ts=end_ts,
        liquidity=finite(item.get("liquidityNum"), finite(item.get("liquidity"))),
        volume24h=finite(item.get("volume24hr"), finite(item.get("volume24h"))),
        yes_token=tokens[yes_index],
        no_token=tokens[no_index],
    )


def fetch_pm_markets(gamma_url: str, limit: int, timeout: float) -> list[PmMarket]:
    output: list[PmMarket] = []
    seen: set[str] = set()
    page_size = 100
    for offset in range(0, max(0, limit), page_size):
        query = urllib.parse.urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": min(page_size, limit - len(output)),
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            }
        )
        root = request_json(gamma_url.rstrip("/") + "/markets?" + query, timeout=timeout)
        values = root if isinstance(root, list) else root.get("markets", []) if isinstance(root, dict) else []
        if not isinstance(values, list):
            raise RuntimeError("unexpected Gamma markets response")
        for item in values:
            if not isinstance(item, dict):
                continue
            market = parse_pm_market(item)
            if market is not None and market.market_id not in seen:
                output.append(market)
                seen.add(market.market_id)
                if len(output) >= limit:
                    return output
        if len(values) < page_size:
            break
    return output


def parse_levels(values: Any) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    if not isinstance(values, list):
        return levels
    for item in values:
        if not isinstance(item, dict):
            continue
        price, size = finite(item.get("price"), -1.0), finite(item.get("size"), 0.0)
        if 0.0 < price < 1.0 and size > 0.0:
            levels.append((price, size))
    return levels


def fetch_pm_quotes(clob_url: str, markets: list[PmMarket], timeout: float) -> dict[str, PmQuote]:
    tokens = [market.yes_token for market in markets]
    books: dict[str, dict[str, Any]] = {}
    for position in range(0, len(tokens), 100):
        chunk = tokens[position : position + 100]
        root = request_json(
            clob_url.rstrip("/") + "/books",
            method="POST",
            body=[{"token_id": token} for token in chunk],
            timeout=timeout,
        )
        if not isinstance(root, list):
            raise RuntimeError("unexpected CLOB /books response")
        for item in root:
            if isinstance(item, dict):
                token = str(item.get("asset_id") or item.get("token_id") or "")
                if token:
                    books[token] = item
    output: dict[str, PmQuote] = {}
    for market in markets:
        raw = books.get(market.yes_token)
        if raw is None:
            continue
        bids, asks = parse_levels(raw.get("bids")), parse_levels(raw.get("asks"))
        if not bids or not asks:
            continue
        best_bid, best_ask = max(x[0] for x in bids), min(x[0] for x in asks)
        if best_ask <= best_bid:
            continue
        output[market.market_id] = PmQuote(
            midpoint=0.5 * (best_bid + best_ask),
            spread=best_ask - best_bid,
            best_bid=best_bid,
            best_ask=best_ask,
        )
    return output


def dollar_price(item: dict[str, Any], dollar_key: str, cent_key: str) -> float:
    value = finite(item.get(dollar_key), math.nan)
    if math.isfinite(value):
        return value
    cents = finite(item.get(cent_key), math.nan)
    return cents / 100.0 if math.isfinite(cents) else math.nan


def parse_kalshi_market(item: dict[str, Any]) -> KMarket | None:
    ticker = str(item.get("ticker") or "")
    title = str(item.get("title") or item.get("yes_sub_title") or "")
    if not ticker or not title:
        return None
    yes_bid = dollar_price(item, "yes_bid_dollars", "yes_bid")
    yes_ask = dollar_price(item, "yes_ask_dollars", "yes_ask")
    no_bid = dollar_price(item, "no_bid_dollars", "no_bid")
    if not math.isfinite(yes_ask) and math.isfinite(no_bid):
        yes_ask = 1.0 - no_bid
    if not math.isfinite(yes_bid) or not math.isfinite(yes_ask):
        return None
    if not (0.0 <= yes_bid < yes_ask <= 1.0):
        return None
    settlement_ts = 0
    for key in (
        "settlement_ts",
        "expected_expiration_time",
        "expiration_time",
        "close_time",
        "latest_expiration_time",
    ):
        settlement_ts = parse_timestamp(item.get(key))
        if settlement_ts:
            break
    status = str(item.get("status") or "").lower()
    if status in {"closed", "determined", "disputed", "amended", "finalized"}:
        return None
    return KMarket(
        ticker=ticker,
        event_ticker=str(item.get("event_ticker") or ""),
        title=title,
        subtitle=str(item.get("subtitle") or item.get("sub_title") or item.get("yes_sub_title") or ""),
        rules=str(item.get("rules_primary") or "") + " " + str(item.get("rules_secondary") or ""),
        settlement_ts=settlement_ts,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        volume=finite(item.get("volume_fp"), finite(item.get("volume"))),
        open_interest=finite(item.get("open_interest_fp"), finite(item.get("open_interest"))),
    )


def fetch_kalshi_markets(base_url: str, limit: int, timeout: float) -> list[KMarket]:
    output: list[KMarket] = []
    seen: set[str] = set()
    cursor = ""
    for _ in range(100):
        parameters: dict[str, Any] = {"limit": min(1000, max(1, limit - len(output))), "status": "open"}
        if cursor:
            parameters["cursor"] = cursor
        root = request_json(base_url.rstrip("/") + "/markets?" + urllib.parse.urlencode(parameters), timeout=timeout)
        if not isinstance(root, dict) or not isinstance(root.get("markets"), list):
            raise RuntimeError("unexpected Kalshi markets response")
        for item in root["markets"]:
            if not isinstance(item, dict):
                continue
            market = parse_kalshi_market(item)
            if market is not None and market.ticker not in seen:
                output.append(market)
                seen.add(market.ticker)
                if len(output) >= limit:
                    return output
        next_cursor = str(root.get("cursor") or "")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return output


def inverted_index(markets: list[KMarket]) -> dict[str, list[int]]:
    frequencies: Counter[str] = Counter()
    tokenized: list[set[str]] = []
    for market in markets:
        tokens = token_set(market.title + " " + market.subtitle)
        tokenized.append(tokens)
        frequencies.update(tokens)
    index: dict[str, list[int]] = defaultdict(list)
    maximum_common = max(20, len(markets) // 8)
    for position, tokens in enumerate(tokenized):
        for token in tokens:
            if frequencies[token] <= maximum_common:
                index[token].append(position)
    return index


def candidate_indices(pm: PmMarket, index: dict[str, list[int]], market_count: int) -> list[int]:
    counts: Counter[int] = Counter()
    for token in token_set(pm.question):
        counts.update(index.get(token, []))
    if not counts:
        return list(range(min(200, market_count)))
    return [position for position, _ in counts.most_common(120)]


def match_markets(
    pm_markets: list[PmMarket],
    pm_quotes: dict[str, PmQuote],
    kalshi_markets: list[KMarket],
    *,
    min_score: float,
    min_margin: float,
    max_kalshi_spread: float,
    max_pm_spread: float,
    min_confidence: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    index = inverted_index(kalshi_markets)
    diagnostics: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    now = int(time.time())

    for pm in pm_markets:
        quote = pm_quotes.get(pm.market_id)
        if quote is None:
            continue
        ranked: list[tuple[float, KMarket, Similarity]] = []
        for position in candidate_indices(pm, index, len(kalshi_markets)):
            market = kalshi_markets[position]
            similarity = contract_similarity(pm, market)
            if similarity.score >= 0.35:
                ranked.append((similarity.score, market, similarity))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            continue
        best_score, best, similarity = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        margin = best_score - second_score

        rejection = similarity.rejection
        if not rejection and best_score < min_score:
            rejection = "score_below_gate"
        if not rejection and margin < min_margin:
            rejection = "ambiguous_match"
        if not rejection and best.spread > max_kalshi_spread:
            rejection = "kalshi_spread_too_wide"
        if not rejection and quote.spread > max_pm_spread:
            rejection = "polymarket_spread_too_wide"

        spread_confidence = math.exp(-5.0 * best.spread)
        activity = 0.45 + 0.55 * min(1.0, math.log1p(max(0.0, best.volume)) / math.log1p(10_000.0))
        confidence = max(0.0, min(0.95, best_score * spread_confidence * activity))
        if not rejection and confidence < min_confidence:
            rejection = "confidence_below_gate"
        accepted = not rejection

        row = {
            "pm_market_id": pm.market_id,
            "pm_condition_id": pm.condition_id,
            "pm_slug": pm.slug,
            "pm_question": pm.question,
            "pm_end_ts": pm.end_ts,
            "pm_mid": quote.midpoint,
            "pm_bid": quote.best_bid,
            "pm_ask": quote.best_ask,
            "pm_spread": quote.spread,
            "pm_volume24h": pm.volume24h,
            "pm_liquidity": pm.liquidity,
            "kalshi_ticker": best.ticker,
            "kalshi_event_ticker": best.event_ticker,
            "kalshi_title": best.title,
            "kalshi_subtitle": best.subtitle,
            "kalshi_settlement_ts": best.settlement_ts,
            "kalshi_mid": best.midpoint,
            "kalshi_bid": best.yes_bid,
            "kalshi_ask": best.yes_ask,
            "kalshi_spread": best.spread,
            "kalshi_volume": best.volume,
            "kalshi_open_interest": best.open_interest,
            "match_score": similarity.score,
            "title_score": similarity.title_score,
            "jaccard": similarity.jaccard,
            "containment": similarity.containment,
            "sequence": similarity.sequence,
            "context_score": similarity.context,
            "date_score": similarity.date_score,
            "date_diff_hours": "" if similarity.date_diff_hours is None else similarity.date_diff_hours,
            "numbers_match": int(similarity.numbers_match),
            "orientation_match": int(similarity.orientation_match),
            "ambiguity_margin": margin,
            "confidence": confidence,
            "external_edge_vs_pm_mid": best.midpoint - quote.midpoint,
            "accepted": int(accepted),
            "rejection_reason": rejection,
        }
        diagnostics.append(row)
        if accepted:
            signals.append(
                {
                    "market_key": pm.market_id,
                    "q_yes": best.midpoint,
                    "confidence": confidence,
                    "source": f"kalshi:{best.ticker}",
                    "timestamp": now,
                }
            )

    diagnostics.sort(key=lambda row: (int(row["accepted"]), float(row["match_score"])), reverse=True)
    signals.sort(key=lambda row: float(row["confidence"]), reverse=True)
    return diagnostics, signals


DIAGNOSTIC_FIELDS = [
    "pm_market_id", "pm_condition_id", "pm_slug", "pm_question", "pm_end_ts",
    "pm_mid", "pm_bid", "pm_ask", "pm_spread", "pm_volume24h", "pm_liquidity",
    "kalshi_ticker", "kalshi_event_ticker", "kalshi_title", "kalshi_subtitle",
    "kalshi_settlement_ts", "kalshi_mid", "kalshi_bid", "kalshi_ask", "kalshi_spread",
    "kalshi_volume", "kalshi_open_interest", "match_score", "title_score", "jaccard",
    "containment", "sequence", "context_score", "date_score", "date_diff_hours",
    "numbers_match", "orientation_match", "ambiguity_margin", "confidence",
    "external_edge_vs_pm_mid", "accepted", "rejection_reason",
]
SIGNAL_FIELDS = ["market_key", "q_yes", "confidence", "source", "timestamp"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--gamma-url", default=PM_GAMMA)
    parser.add_argument("--clob-url", default=PM_CLOB)
    parser.add_argument("--kalshi-url", default=KALSHI)
    parser.add_argument("--pm-markets", type=int, default=800)
    parser.add_argument("--kalshi-markets", type=int, default=3000)
    parser.add_argument("--min-score", type=float, default=0.82)
    parser.add_argument("--min-margin", type=float, default=0.05)
    parser.add_argument("--max-kalshi-spread", type=float, default=0.08)
    parser.add_argument("--max-pm-spread", type=float, default=0.15)
    parser.add_argument("--min-confidence", type=float, default=0.45)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    args = parser.parse_args()

    if args.pm_markets <= 0 or args.kalshi_markets <= 0:
        raise SystemExit("market limits must be positive")
    pm_markets = fetch_pm_markets(args.gamma_url, args.pm_markets, args.timeout_seconds)
    pm_quotes = fetch_pm_quotes(args.clob_url, pm_markets, args.timeout_seconds)
    kalshi_markets = fetch_kalshi_markets(args.kalshi_url, args.kalshi_markets, args.timeout_seconds)
    diagnostics, signals = match_markets(
        pm_markets,
        pm_quotes,
        kalshi_markets,
        min_score=args.min_score,
        min_margin=args.min_margin,
        max_kalshi_spread=args.max_kalshi_spread,
        max_pm_spread=args.max_pm_spread,
        min_confidence=args.min_confidence,
    )
    atomic_csv(args.output, DIAGNOSTIC_FIELDS, diagnostics)
    atomic_csv(args.signals, SIGNAL_FIELDS, signals)

    accepted = [row for row in diagnostics if int(row["accepted"]) == 1]
    summary = {
        "schema": "polymarket_kalshi_cross_venue_shadow_v1",
        "generated_ts": int(time.time()),
        "read_only": True,
        "production_external_signals_modified": False,
        "polymarket_markets": len(pm_markets),
        "polymarket_books": len(pm_quotes),
        "kalshi_markets": len(kalshi_markets),
        "diagnostic_matches": len(diagnostics),
        "accepted_matches": len(accepted),
        "signal_rows": len(signals),
        "mean_accepted_confidence": statistics.fmean(float(row["confidence"]) for row in accepted) if accepted else 0.0,
        "max_absolute_external_edge": max((abs(float(row["external_edge_vs_pm_mid"])) for row in accepted), default=0.0),
        "rejections": dict(Counter(str(row["rejection_reason"] or "accepted") for row in diagnostics)),
        "gates": {
            "min_score": args.min_score,
            "min_margin": args.min_margin,
            "max_kalshi_spread": args.max_kalshi_spread,
            "max_pm_spread": args.max_pm_spread,
            "min_confidence": args.min_confidence,
            "same_critical_numbers": True,
            "same_logical_orientation": True,
            "max_settlement_difference_days": 10,
        },
        "top_matches": diagnostics[:25],
    }
    atomic_json(args.summary, summary)
    print(
        "kalshi_cross_venue_shadow"
        f" pm_markets={len(pm_markets)}"
        f" pm_books={len(pm_quotes)}"
        f" kalshi_markets={len(kalshi_markets)}"
        f" diagnostics={len(diagnostics)}"
        f" accepted={len(accepted)}"
        f" max_abs_edge={summary['max_absolute_external_edge']:.12g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
