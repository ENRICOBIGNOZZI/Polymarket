#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Direct crypto probability model and conservative cross-venue compatibility.
# ---------------------------------------------------------------------------
replace_once(
    "scripts/external_intelligence.py",
    "ASSET_ALIASES = {\n",
    "BARRIER_WORDS = {\"reach\", \"reaches\", \"hit\", \"hits\", \"touch\", \"touches\", \"break\", \"breaks\", \"dip\", \"dips\"}\n\nASSET_ALIASES = {\n",
)

replace_once(
    "scripts/external_intelligence.py",
    "def request_json(url: str, *, timeout: float = 20.0, retries: int = 3) -> Any:\n",
    r'''def normal_cdf(value: float) -> float:
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
''',
)

replace_once(
    "scripts/external_intelligence.py",
    "    for km in candidates:\n        score, numeric, orient, expiry, rejection = score_pair(pm, km, max_expiry)\n        scored.append((score, numeric, orient, expiry, rejection, km))",
    "    for km in candidates:\n"
    "        if not kalshi_candidate_compatible(pm, km):\n"
    "            continue\n"
    "        score, numeric, orient, expiry, rejection = score_pair(pm, km, max_expiry)\n"
    "        scored.append((score, numeric, orient, expiry, rejection, km))",
)

replace_once(
    "scripts/external_intelligence.py",
    "    features = {\n        \"return_5m\": returns[-1],",
    "    features = {\n        \"spot\": closes[-1],\n        \"return_5m\": returns[-1],",
)

replace_once(
    "scripts/external_intelligence.py",
    "                    metadata={\"asset\": asset, \"symbol\": symbol},\n                ))\n    return observations, health, errors",
    "                    metadata={\"asset\": asset, \"symbol\": symbol},\n"
    "                ))\n"
    "            estimate = crypto_threshold_probability(market, asset, features, now, config)\n"
    "            if estimate is not None:\n"
    "                q_external, confidence, metadata = estimate\n"
    "                observations.append(observation_row(\n"
    "                    market,\n"
    "                    observed_ts=now,\n"
    "                    source=\"binance\",\n"
    "                    source_id=symbol,\n"
    "                    source_event_ts=source_ts or now,\n"
    "                    feature_name=\"external_probability\",\n"
    "                    feature_value=q_external - market.mid,\n"
    "                    q_external=q_external,\n"
    "                    confidence=confidence,\n"
    "                    mapping_score=1.0,\n"
    "                    metadata={**metadata, \"symbol\": symbol},\n"
    "                ))\n"
    "    return observations, health, errors",
)

replace_once(
    "scripts/external_intelligence.py",
    '''def fetch_pm_history(token_id: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
    if not token_id:
        return []
    params = urllib.parse.urlencode({
        "market": token_id, "startTs": start_ts, "endTs": end_ts, "interval": "1h", "fidelity": 60,
    })
    payload = request_json(f"{PM_CLOB}/prices-history?{params}")
    history = payload.get("history") if isinstance(payload, dict) else []
    return history if isinstance(history, list) else []
''',
    '''def history_interval_for_range(start_ts: int, end_ts: int) -> str:
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
''',
)

replace_once(
    "scripts/external_intelligence.py",
    '            "binance_rows": len(binance_observations) + len(backfill_observations),',
    '            "binance_rows": len(binance_observations) + len(backfill_observations),\n'
    '            "direct_probability_rows": sum(1 for row in new_observations if row.get("q_external") is not None),',
)

# ---------------------------------------------------------------------------
# Align the external universe with the aggressive 500-market paper runtime.
# ---------------------------------------------------------------------------
config_path = ROOT / "config" / "external_intelligence.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
config["universe"].update({
    "max_markets": 500,
    "min_liquidity": 25.0,
    "order_field": "liquidityNum",
})
config["sources"]["binance"].update({
    "max_markets_per_asset": 60,
    "probability_model": {
        "enabled": True,
        "min_horizon_hours": 1.0,
        "max_horizon_days": 365.0,
        "min_daily_vol": 0.005,
        "max_daily_vol": 0.25,
        "drift_shrink": 0.10,
        "max_abs_daily_drift": 0.01,
        "min_confidence": 0.40,
        "max_confidence": 0.70,
    },
})
config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

# ---------------------------------------------------------------------------
# Regression coverage.
# ---------------------------------------------------------------------------
replace_once(
    "tests/test_external_intelligence.py",
    "    def test_gdelt_compact_timestamp(self) -> None:\n",
    r'''    def test_kalshi_prefilter_rejects_cross_asset(self) -> None:
        ethereum = self.kalshi("Ethereum above $100,000 on December 31 2026")
        self.assertFalse(external.kalshi_candidate_compatible(self.market(), ethereum))
        self.assertIsNone(external.match_kalshi(self.market(), [ethereum], self.config, self.now))

    def test_crypto_threshold_probability_is_direct_and_bounded(self) -> None:
        features = {
            "spot": 90_000.0,
            "return_5m": 0.001,
            "return_1h": 0.003,
            "return_24h": 0.01,
            "realized_vol_24h": 0.04,
        }
        reach = external.crypto_threshold_probability(
            self.market("Will Bitcoin reach $100,000 in August?"), "BTC", features, self.now, self.config
        )
        self.assertIsNotNone(reach)
        assert reach is not None
        q_reach, confidence, metadata = reach
        self.assertGreater(q_reach, 0.05)
        self.assertLess(q_reach, 0.997)
        self.assertGreaterEqual(confidence, 0.35)
        self.assertEqual(metadata["event_type"], "upper_barrier")

        dip = external.crypto_threshold_probability(
            self.market("Will Bitcoin dip to $75,000 in August?"), "BTC", features, self.now, self.config
        )
        self.assertIsNotNone(dip)
        assert dip is not None
        self.assertGreater(dip[0], 0.01)
        self.assertEqual(dip[2]["event_type"], "lower_barrier")

        crossed = dict(features, spot=105_000.0)
        crossed_estimate = external.crypto_threshold_probability(
            self.market("Will Bitcoin reach $100,000 in August?"), "BTC", crossed, self.now, self.config
        )
        assert crossed_estimate is not None
        self.assertGreater(crossed_estimate[0], 0.99)

    def test_crypto_threshold_probability_abstains_on_ranges(self) -> None:
        features = {"spot": 100_000.0, "return_24h": 0.0, "realized_vol_24h": 0.04}
        market = self.market("Will Bitcoin trade between $90,000 and $110,000 in August?")
        self.assertIsNone(external.crypto_threshold_probability(market, "BTC", features, self.now, self.config))

    def test_crypto_features_and_collector_emit_direct_probability(self) -> None:
        rows = []
        for index in range(289):
            close = 90_000.0 * math.exp(0.00005 * index)
            close_ts_ms = (self.now - (288 - index) * 300) * 1000
            rows.append([0, 0, 0, 0, str(close), 0, close_ts_ms])
        features, source_ts = external.crypto_features(rows)
        self.assertGreater(features["spot"], 90_000.0)
        original = external.fetch_binance_klines
        external.fetch_binance_klines = lambda *args, **kwargs: rows
        try:
            observations, health, errors = external.collect_binance(
                [self.market("Will Bitcoin reach $100,000 in August?")], self.config, self.now
            )
        finally:
            external.fetch_binance_klines = original
        self.assertFalse(errors)
        self.assertEqual(health["BTC"]["status"], "ok")
        direct = [row for row in observations if row["feature_name"] == "external_probability"]
        self.assertEqual(len(direct), 1)
        self.assertIsNotNone(direct[0]["q_external"])
        self.assertEqual(direct[0]["source_event_ts"], source_ts)

    def test_pm_history_uses_supported_interval_without_oversized_range(self) -> None:
        calls = []
        original = external.request_json
        external.request_json = lambda url, **kwargs: calls.append(url) or {
            "history": [{"t": self.now - 3600, "p": 0.5}]
        }
        try:
            rows = external.fetch_pm_history("token", self.now - 14 * 86400, self.now)
        finally:
            external.request_json = original
        self.assertEqual(len(rows), 1)
        self.assertIn("interval=1m", calls[0])
        self.assertNotIn("startTs", calls[0])
        self.assertNotIn("endTs", calls[0])

    def test_gdelt_compact_timestamp(self) -> None:
''',
)
replace_once(
    "tests/test_external_intelligence.py",
    "import json\nimport subprocess",
    "import json\nimport math\nimport subprocess",
)

# Keep the one-shot patch file out of the resulting research commit.
Path(__file__).unlink()
print("direct external probability model applied")
